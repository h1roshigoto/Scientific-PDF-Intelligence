"""Exercise a disposable TLS Compose deployment and a separate cold restore."""

import argparse
import json
import os
import re
import socket
import ssl
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, suppress
from hashlib import sha256
from pathlib import Path
from secrets import token_hex, token_urlsafe
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic, sleep
from urllib.parse import quote
from zipfile import BadZipFile

import httpx
import pymupdf

from spi.upload_export_verify import verify
from spi.ops.encrypted_backup import Repository

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "frontend_redezigned/tests/e2e"))
from serve_multiuser_store import rag_fixture  # noqa: E402
from serve_test_store import processing_fixture  # noqa: E402


class Compose:
    def __init__(self, project, port, backups):
        self.project = project
        self.origin = f"https://localhost:{port}"
        segment = int(backups.split("-")[-2][:2], 16)
        self.env = {**{key: value for key, value in os.environ.items() if not key.startswith("SPI_")},
                    "SPI_LOCAL_PORT": str(port), "SPI_LOCAL_BACKUP_VOLUME": backups,
                    "SPI_BACKEND_SUBNET": f"10.92.{segment}.0/28",
                    "SPI_BACKEND_DYNAMIC_RANGE": f"10.92.{segment}.8/29",
                    "SPI_PROXY_IP": f"10.92.{segment}.2", "SPI_EDGE_SUBNET": f"10.92.{segment}.16/28"}
        self.command = ["docker", "compose", "--env-file", os.devnull, "-p", project,
                        "-f", "compose.yaml", "-f", "compose.local.yaml"]

    def run(self, *args, stdin=None, expected=0, timeout=180):
        result = subprocess.run(
            [*self.command, *args], cwd=ROOT, env=self.env, input=stdin, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        if result.returncode != expected:
            # Never emit CLI output from a password/invitation operation.
            diagnostic = f" [{result.stderr[-2500:]}]" if stdin is None else ""
            raise AssertionError(f"Compose {args[0]} returned {result.returncode}, expected {expected}{diagnostic}")
        return result.stdout.strip()

    def start(self, *extra):
        self.run("up", "-d", "--wait", "--wait-timeout", "90", *extra, timeout=300)

    def container_health(self, service):
        container = self.run("ps", "-q", service)
        return subprocess.check_output(["docker", "inspect", container, "--format", "{{.State.Health.Status}}"],
                                       encoding="utf-8").strip()

    def client(self, directory):
        ca = directory / f"{self.project}-ca.crt"
        ca.unlink(missing_ok=True)
        wait_for(lambda: self._copy_ca(ca))
        context = ssl.create_default_context(cafile=str(ca))
        client = httpx.Client(base_url=self.origin, verify=context, trust_env=False, timeout=15)
        wait_for(lambda: client.get("/api/health").status_code == 200)
        return client, ca

    def _copy_ca(self, path):
        try:
            self.run("cp", "proxy:/data/caddy/pki/authorities/local/root.crt", str(path))
        except AssertionError:
            return False
        return path.is_file()


def wait_for(check, timeout=45, delay=0.25):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except httpx.TransportError:
            pass
        sleep(delay)
    raise AssertionError("Timed out waiting for deployment assertion")


def status(response, expected):
    assert response.status_code == expected, (
        response.request.method, response.request.url.path, response.status_code, expected,
        response.json().get("detail") if response.headers.get("content-type") == "application/json"
        and response.content.startswith(b"{") else None,
    )
    return response


def csrf(client, origin):
    return {"Origin": origin, "X-SPI-CSRF": status(client.get("/api/auth/csrf"), 200).json()["csrf_token"]}


def account(client, origin, name, password, invitation=None):
    values = dict(email=f"{name.lower()}@example.test", password=password)
    if invitation:
        status(client.post("/api/auth/register", headers=csrf(client, origin),
                           json={**values, "display_name": name, "invitation_code": invitation}), 201)
    response = status(client.post("/api/auth/login", headers=csrf(client, origin), json=values), 200)
    cookie = next(header for header in response.headers.get_list("set-cookie") if header.startswith("__Host-spi-session="))
    attributes = {item.strip().lower() for item in cookie.split(";")[1:]}
    assert {"secure", "httponly", "path=/", "samesite=lax"} <= attributes
    assert all(not item.startswith("domain=") for item in attributes)
    return response.json()


def upload(client, origin, path, domain, filename=None):
    response = status(client.post("/api/uploads", headers=csrf(client, origin), data={"domain": domain},
                                 files={"file": (filename or path.name, path.read_bytes(), "application/pdf")}), 201)
    row = response.json()
    assert row["sha256"] == sha256(path.read_bytes()).hexdigest()
    return f"/api/uploads/{row['upload_id']}"


def finished(client, path):
    result = status(client.get(path + "/processing"), 200).json()
    assert result["status"] != "failed", result.get("diagnostic")
    return result if result["status"] in {"completed", "needs_review"} else None


def export(client, path, target):
    response = status(client.get(path + "/export/manifest"), 200)
    snapshot = response.headers["x-spi-snapshot-sha256"]
    archive = status(client.get(path + "/export/project.zip", headers={"X-SPI-Snapshot-SHA256": snapshot}), 200)
    assert archive.headers["cache-control"] == "no-store"
    target.write_bytes(archive.content)
    manifest = verify(target)
    assert manifest.metadata.snapshot_sha256 == snapshot
    return snapshot


def network_contract(client, origin):
    private_query = token_urlsafe(24)
    for path in ("/documents", "/account", "/uploads/example", "/not-a-route"):
        response = status(client.get(path), 200)
        assert "text/html" in response.headers["content-type"]
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["strict-transport-security"] == "max-age=31536000"
        csp = response.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in csp and "'unsafe-eval'" not in csp
        assert response.headers["x-content-type-options"] == "nosniff"
    html = client.get("/documents").text
    asset = re.search(r'src="(/assets/[^"]+\.js)"', html)[1]
    assert "immutable" in status(client.get(asset), 200).headers["cache-control"]
    assert status(client.get("/api/health", params={"log_probe": private_query}, headers={
        "X-Forwarded-For": "192.0.2.1,192.0.2.2", "X-Forwarded-Proto": "http",
        "X-Forwarded-Host": "foreign.example", "Forwarded": "host=foreign.example;proto=http",
    }), 200).json() == {"status": "ok"}
    assert client.get("/api/health", headers={"Host": "foreign.example"}).status_code in (400, 404, 421)
    for path in ("documents", "results", "issues", "export/results.json", "__ops/ready", "openapi.json"):
        status(client.get("/api/" + path), 404)
    status(client.post("/api/auth/logout", headers={"Origin": origin}), 403)
    status(client.post("/api/auth/logout", headers={**csrf(client, origin), "X-SPI-CSRF": "invalid"}), 403)
    status(client.get("/api/auth/me", headers={"Cookie": "__Host-spi-session=invalid-base64!!!"}), 401)
    status(client.post("/api/auth/logout", headers={**csrf(client, origin), "Origin": "https://foreign.example"}), 403)
    cors = client.options("/api/auth/login", headers={"Origin": "https://foreign.example",
                          "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in cors.headers
    print("PASS TLS trust chain, hostname, SPA assets, proxy overwrite, Host, CORS, CSRF and route isolation", flush=True)
    return private_query


def untrusted_backend(compose):
    probe = """
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import sys
request = Request('http://api:8000/health', headers={
    'Host': sys.argv[1], 'X-Forwarded-For': '192.0.2.1',
    'X-Forwarded-Proto': 'https', 'Forwarded': 'proto=https'})
try:
    urlopen(request, timeout=5)
except HTTPError as exc:
    assert exc.code == 403 and b'https_required' in exc.read()
else:
    raise AssertionError('Untrusted backend peer was accepted')
"""
    subprocess.run(["docker", "run", "--rm", "--network", f"{compose.project}_backend",
                    "spi-runtime:local", "python", "-c", probe, compose.origin.removeprefix("https://")], check=True)
    print("PASS untrusted backend peer cannot assert forwarded client or HTTPS", flush=True)


def crash_parser(compose, client, source):
    program = r"""
import os, signal, time
from pathlib import Path

print('ready', flush=True)
until = time.monotonic() + 8
while time.monotonic() < until:
    for entry in Path('/proc').glob('[0-9]*/cmdline'):
        if int(entry.parent.name) == os.getpid():
            continue
        try:
            if b'spi.ops.pdf_sandbox\x00child\x00pages\x00' in entry.read_bytes():
                os.kill(int(entry.parent.name), signal.SIGKILL)
                print('interrupted', flush=True)
                raise SystemExit
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    time.sleep(.005)
raise SystemExit('No disposable parser child observed')
"""
    command = [*compose.command, "exec", "-T", "sandbox", "python", "-c", program]
    before = status(client.get("/api/uploads"), 200).json()
    headers = csrf(client, compose.origin)
    with subprocess.Popen(command, cwd=ROOT, env=compose.env, stdout=subprocess.PIPE, text=True) as watcher:
        assert watcher.stdout.readline().strip() == "ready"
        reply = client.post("/api/uploads", headers=headers, data={"domain": "solar"},
                            files={"file": ("synthetic-crash.pdf", source.read_bytes(), "application/pdf")})
        assert watcher.communicate(timeout=12)[0].strip() == "interrupted" and watcher.returncode == 0
    assert status(reply, 503).json() == {"detail": "pdf_parser_unavailable"}
    assert status(client.get("/api/uploads"), 200).json() == before
    print("PASS parser child interruption returned a recoverable error without publishing an upload", flush=True)


def local_alert(compose, directory):
    def delivered():
        for line in compose.run("logs", "--no-log-prefix", "monitoring").splitlines():
            if not line.startswith("{"):
                continue
            record = json.loads(line)
            if record.get("alert") == "backup_failed" and record.get("state") == "firing":
                return record
        return None

    target = directory / "local-alert-receiver.json"
    target.write_text(json.dumps(wait_for(delivered, timeout=25)), encoding="utf-8")
    event = json.loads(target.read_text(encoding="utf-8"))
    assert set(event) == {"timestamp", "service", "severity", "code", "alert", "state"}
    assert event["service"] == "monitoring" and event["code"] == "alert"
    print("PASS backup failure alert delivered to a local receiver through monitoring logs", flush=True)


def encrypted_rehearsal(compose, directory):
    plain, secrets, exports = (directory / name for name in ("plaintext", "restic-keys", "restic-restored"))
    for path in (plain, secrets, exports):
        path.mkdir(mode=0o700)
    for name in ("correct", "incorrect"):
        (secrets / name).write_text(token_urlsafe(40) + "\n", encoding="ascii")
        (secrets / name).chmod(0o600)
    holder = compose.run("run", "-d", "--rm", "--no-deps", "--entrypoint", "sleep", "backup", "120").splitlines()[-1]
    archive = plain / "rehearsal.tar"
    try:
        subprocess.run(["docker", "cp", f"{holder}:/backups/rehearsal.tar", str(archive)], check=True)
        repo = Repository(directory / "restic-repository", secrets / "correct")
        repo.initialize()
        point = repo.store(archive, directory)
        repo.verify()
        target = exports / "snapshot.tar"
        wrong = Repository(repo.path, secrets / "incorrect")
        try:
            wrong.restore(point, target, directory)
        except subprocess.CalledProcessError:
            assert not target.exists()
        else:
            raise AssertionError("Incorrect restic password was accepted")
        assert repo.restore(point, target, directory) == "sha256:" + sha256(archive.read_bytes()).hexdigest()
        another = repo.store(archive, directory)
        assert another != point
        assert point in repo.retention(7, 4, 12, point)["keep"]
        with target.open("rb") as stream:
            subprocess.run(["docker", "exec", "-i", holder, "sh", "-ec",
                            "umask 077; cat > /backups/encrypted-roundtrip.tar"], stdin=stream, check=True)
    finally:
        subprocess.run(["docker", "stop", holder], check=True, stdout=subprocess.DEVNULL)
    print("PASS restic encryption, data verification, wrong-key refusal, byte-identical restore and retention dry-run",
          flush=True)
    return "encrypted-roundtrip"


def worker_recovery(compose, client, origin, path, owner, upload_id, other_client, other_path):
    compose.run("pause", "worker")
    headers = csrf(client, origin)
    with ThreadPoolExecutor(max_workers=2) as executor:
        def enqueue(_):
            return status(client.post(path + "/process", headers=headers), 202).json()["job_id"]
        jobs = set(executor.map(enqueue, range(2)))
    assert len(jobs) == 1
    compose.run("pause", "sandbox")
    try:
        compose.run("unpause", "worker")
        probe = ("import json,sys; from pathlib import Path; p=Path(sys.argv[1]); "
                 "print(json.loads(p.read_text())['status'] if p.is_file() else 'missing')")
        checkpoint = f"/state-tenants/users/{owner}/processing/{upload_id}/batch/rag/{upload_id}.json"
        wait_for(lambda: compose.run("exec", "-T", "api", "python", "-c", probe, checkpoint) == "running", timeout=15)
        compose.run("kill", "-s", "SIGKILL", "worker")
        assert status(client.get(path + "/processing"), 200).json()["status"] == "processing"
        status(other_client.post(other_path + "/process", headers=csrf(other_client, origin)), 202)
        assert status(other_client.get(other_path + "/processing"), 200).json()["status"] == "queued"
    finally:
        compose.run("unpause", "sandbox")
    wait_for(lambda: compose.container_health("sandbox") == "healthy")
    compose.start("--scale", "worker=2")
    recovered = wait_for(lambda: finished(client, path), timeout=70)
    assert recovered["job_id"] == jobs.pop() and recovered["recoveries"] == 1
    wait_for(lambda: finished(other_client, other_path), timeout=70)
    compose.start("--scale", "worker=1")
    print("PASS concurrent enqueue and two workers, SIGKILL after persisted checkpoint, same job recovered", flush=True)


def disk_and_proxy_failure(compose, client, origin, source):
    before = client.get("/api/uploads").json()
    fill = ("import os; path='/tmp/spi-e2e-full'; s=os.statvfs('/tmp'); "
            "f=open(path,'xb'); os.posix_fallocate(f.fileno(),0,s.f_bavail*s.f_frsize)")
    compose.run("exec", "-T", "api", "python", "-c", fill)
    try:
        request = client.post("/api/uploads", headers=csrf(client, origin), data={"domain": "solar"},
                              files={"file": ("failure.pdf", source.read_bytes() + b" " * 2_000_000, "application/pdf")})
        assert status(request, 507).json() == {"detail": "storage_full"}
        assert client.get("/api/uploads").json() == before
    finally:
        compose.run("exec", "-T", "api", "python", "-c", "from pathlib import Path; Path('/tmp/spi-e2e-full').unlink()")
    compose.run("stop", "proxy")
    try:
        client.get("/api/health")
    except httpx.TransportError:
        pass
    else:
        raise AssertionError("Proxy outage was not observable")
    compose.run("start", "proxy")
    wait_for(lambda: client.get("/api/health").status_code == 200)
    status(client.get("/api/auth/me"), 200)
    print("PASS actual tmpfs ENOSPC preserves upload catalog, proxy outage and session recovery", flush=True)


def interrupted_writes(compose, client, origin, source):
    before = status(client.get("/api/uploads"), 200).json()
    headers = {**csrf(client, origin), "Content-Type": "multipart/form-data; boundary=spi_e2e"}
    sent = Event()
    resumed = Event()

    def body():
        yield (b'--spi_e2e\r\nContent-Disposition: form-data; name="domain"\r\n\r\nsolar\r\n'
               b'--spi_e2e\r\nContent-Disposition: form-data; name="file"; filename="stop.pdf"\r\n'
               b'Content-Type: application/pdf\r\n\r\n' + source.read_bytes() + b" " * 1_500_000)
        sent.set()
        resumed.wait(20)
        yield b"\r\n--spi_e2e--\r\n"

    def upload_partial():
        try:
            return client.post("/api/uploads", headers=headers, content=body()).status_code
        except httpx.TransportError:
            return 502

    with ThreadPoolExecutor(max_workers=1) as executor:
        upload_attempt = executor.submit(upload_partial)
        try:
            assert sent.wait(10)
            transaction = ("import sqlite3, time; from pathlib import Path; "
                           "db=sqlite3.connect('/state-auth/auth.sqlite3'); db.execute('BEGIN IMMEDIATE'); "
                           "db.execute(\"UPDATE users SET display_name='unexpected uncommitted name'\"); "
                           "Path('/tmp/spi-e2e-db-transaction').touch(); time.sleep(30)")
            compose.run("exec", "-T", "-d", "api", "python", "-c", transaction)
            check = ("from pathlib import Path; "
                     "print(Path('/tmp/spi-e2e-db-transaction').is_file())")
            wait_for(lambda: compose.run("exec", "-T", "api", "python", "-c", check) == "True", timeout=15)
            compose.run("kill", "-s", "SIGKILL", "api")
        finally:
            resumed.set()
        assert upload_attempt.result(timeout=20) in (400, 502, 503)
    compose.run("start", "api")
    wait_for(lambda: client.get("/api/health").status_code == 200)
    assert status(client.get("/api/auth/me"), 200).json()["display_name"] == "Alice"
    assert status(client.get("/api/uploads"), 200).json() == before
    compose.run("run", "--rm", "--no-deps", "-e", "SPI_AUTH_SECRET_FILE=/local-secret/not-a-secret",
                "api", expected=1)
    compose.run("run", "--rm", "--no-deps", "-e", "SPI_AUTH_SECRET_FILE=", "-e", "SPI_AUTH_SECRET=invalid",
                "api", expected=1)
    print("PASS API SIGKILL with partial multipart and open SQLite write, rollback, no upload and invalid secret refusal",
          flush=True)


def interrupted_export(compose, client, path, directory, expected):
    started = Event()

    def download():
        started.set()
        try:
            return client.get(path + "/export/project.zip").status_code
        except httpx.TransportError:
            return 502

    compose.run("pause", "sandbox")
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            request = executor.submit(download)
            assert started.wait(5)
            sleep(0.6)
            assert not request.done()
            compose.run("kill", "-s", "SIGKILL", "api")
            assert request.result(timeout=15) in (502, 503)
    finally:
        compose.run("unpause", "sandbox")
    wait_for(lambda: compose.container_health("sandbox") == "healthy")
    compose.run("start", "api")
    wait_for(lambda: client.get("/api/health").status_code == 200)
    assert export(client, path, directory / "alice-after-interruption.zip") == expected
    print("PASS API interruption during ZIP preparation, successful retry with the same verified snapshot", flush=True)


def unavailable_storage(compose, client):
    compose.run("exec", "-T", "api", "chmod", "000", "/state-tenants")
    try:
        compose.run("exec", "-T", "api", "python", "-m", "spi.ops.runtime", "api-health", expected=1)
        failure = client.get("/api/uploads")
        assert failure.status_code in (500, 503) and "/state-tenants" not in failure.text
    finally:
        compose.run("exec", "-T", "api", "chmod", "700", "/state-tenants")
    compose.run("exec", "-T", "api", "python", "-m", "spi.ops.runtime", "api-health")
    status(client.get("/api/uploads"), 200)
    print("PASS storage unavailable causes readiness failure and recovers after access is restored", flush=True)


def interrupted_backup(compose):
    program = """
from pathlib import Path
from time import sleep
from spi.ops import backup

copy = backup._copy_file

def pause_after_copy(*args, **kwargs):
    copy(*args, **kwargs)
    Path('/tmp/backup-copy-interrupted').touch()
    sleep(60)

backup._copy_file = pause_after_copy
backup.create('interrupted')
"""
    container = compose.run("run", "-d", "--rm", "--no-deps", "--entrypoint", "python", "backup", "-c", program)
    try:
        def copying():
            return subprocess.run(
                ["docker", "exec", container, "test", "-f", "/tmp/backup-copy-interrupted"],
                capture_output=True, timeout=3,
            ).returncode == 0

        wait_for(copying, timeout=20)
        subprocess.run(["docker", "kill", "-s", "SIGKILL", container], check=True, capture_output=True, timeout=10)
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=10)
    compose.run("run", "--rm", "--no-deps", "backup", "verify", "interrupted", expected=1)


def restore_failures(compose, restore_name):
    modifications = """
import io, json, sys, tarfile
from pathlib import Path

base = Path('/backups')
with tarfile.open(base / (sys.argv[1] + '.tar')) as original:
    content = [(item, original.extractfile(item).read()) for item in original]

for scenario in ('hash', 'schema', 'missing', 'sqlite'):
    manifest = json.loads(content[0][1])
    documents = content[1:].copy()
    victim, value = documents[0]
    if scenario == 'hash':
        manifest['files'][victim.name]['sha256'] = 'a' * 64
    elif scenario == 'schema':
        manifest['schema_version'] = 999
    elif scenario == 'missing':
        documents.pop()
    else:
        from hashlib import sha256
        assert victim.name == 'auth/auth.sqlite3'
        broken = b'not a sqlite database'
        manifest['files'][victim.name] = {'bytes': len(broken), 'sha256': sha256(broken).hexdigest()}
        documents[0] = (victim, broken)
    with tarfile.open(base / ('negative-' + scenario + '.tar'), 'w') as target:
        for member, data in ((content[0][0], json.dumps(manifest).encode()), *documents):
            member.size = len(data)
            target.addfile(member, io.BytesIO(data))
"""
    compose.run("run", "--rm", "--no-deps", "--entrypoint", "python", "backup", "-c", modifications, restore_name)
    for name in ("hash", "schema", "missing", "sqlite"):
        compose.run("run", "--rm", "--no-deps", "restore", "negative-" + name, expected=1)
    program = """
from pathlib import Path
from time import sleep
import sys
from spi.ops import restore

original = restore.read_archive
def before_publication(source, stage):
    manifest = original(source, stage)
    Path('/tmp/spi-restore-verified').touch()
    sleep(60)
    return manifest

restore.read_archive = before_publication
restore.restore(Path('/backups') / (sys.argv[1] + '.tar'))
"""
    container = compose.run("run", "-d", "--rm", "--no-deps", "--entrypoint", "python",
                            "restore", "-c", program, restore_name).splitlines()[-1]
    try:
        wait_for(lambda: subprocess.run(["docker", "exec", container, "test", "-f", "/tmp/spi-restore-verified"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0)
    finally:
        subprocess.run(["docker", "kill", container], check=True, stdout=subprocess.DEVNULL)
    compose.run("run", "--rm", "--no-deps", "--entrypoint", "test", "restore", "!", "-e", "/dataset/state")
    print("PASS restore rejects bad SHA-256, schema, missing file and corrupt SQLite; interrupted restore stays unpublished",
          flush=True)


def log_check(compose, forbidden):
    logs = compose.run("logs", "--no-color")
    assert all(secret and secret not in logs for secret in forbidden)
    assert '"request_id":' in logs and '"service": "monitoring"' in logs


def scenario(primary, restored, directory, *, browser=False, encrypted=False):
    primary.start()
    alice, ca = primary.client(directory)
    bob, _ = primary.client(directory)
    with closing(alice), closing(bob):
        private_query = network_contract(alice, primary.origin)
        untrusted_backend(primary)
        network_password = token_urlsafe(23)
        password_admin = token_urlsafe(23)

        def cli(*args):
            return primary.run("exec", "-T", "api", "python", "-m", "spi.auth_cli", *args, "--password-stdin",
                               stdin=password_admin + "\n").splitlines()[-1]

        admin = "operator@example.test"
        cli("bootstrap-admin", "--email", admin, "--display-name", "Synthetic operator")
        invite_alice, invite_bob = [cli("invite", "--admin-email", admin) for _ in range(2)]
        account(alice, primary.origin, "Alice", network_password, invite_alice)
        owner = account(bob, primary.origin, "Bob", network_password, invite_bob)["user_id"]
        solar, rag = directory / "Synthetic Solar.pdf", directory / "Synthetic RAG.pdf"
        processing_fixture(solar)
        rag_fixture(rag)
        status(alice.post("/api/uploads", headers=csrf(alice, primary.origin), data={"domain": "solar"},
                          files={"file": ("invalid.pdf", b"%PDF-1.7\ntruncated", "application/pdf")}), 422)
        crash_parser(primary, alice, solar)
        with ThreadPoolExecutor(max_workers=2) as pool:
            alice_upload = pool.submit(upload, alice, primary.origin, solar, "solar", "Алиса Solar.pdf")
            bob_upload = pool.submit(upload, bob, primary.origin, rag, "rag")
            path_a, path_b = alice_upload.result(), bob_upload.result()
        worker_recovery(primary, bob, primary.origin, path_b, owner, path_b.rsplit("/", 1)[1], alice, path_a)
        results_a = status(alice.get(path_a + "/results"), 200).json()
        assert len(results_a) == 4
        original_a = status(alice.get(path_a + "/file"), 200).content
        assert original_a == solar.read_bytes()
        candidate = next(item for item in results_a if item["raw"]["value"] == "15.5")
        review = path_a + "/results/" + quote(candidate["result_id"], safe="") + "/review"
        basis = status(alice.get(review), 200).json()["basis"]
        approved = status(alice.post(review, headers=csrf(alice, primary.origin), json={
            "basis": basis, "expected_revision": 0, "action": "approve", "evidence_mode": "text",
            "warnings_acknowledged": True,
        }), 201)
        assert approved.json()["status"] == "approved"
        status(alice.post(review, headers=csrf(alice, primary.origin), json={
            "basis": basis, "expected_revision": 0, "action": "approve", "evidence_mode": "text",
            "warnings_acknowledged": True,
        }), 409)
        history = status(alice.get(review + "/history"), 200).json()
        assert len(history) == 1
        snapshot_a = export(alice, path_a, directory / "alice-before.zip")
        truncated_zip = directory / "truncated.zip"
        truncated_zip.write_bytes((directory / "alice-before.zip").read_bytes()[:100])
        try:
            verify(truncated_zip)
        except BadZipFile:
            pass
        else:
            raise AssertionError("Truncated ZIP unexpectedly verified")
        if browser:
            browser_check(directory, ca, primary.origin, network_password, path_a)
        cookie_name = "__Host-spi-session"
        logout_cookie = alice.cookies[cookie_name]
        status(alice.post("/api/auth/logout", headers=csrf(alice, primary.origin)), 204)
        status(alice.get("/api/auth/me", headers={"Cookie": f"{cookie_name}={logout_cookie}"}), 401)
        assert status(bob.get("/api/uploads"), 200).json()[0]["upload_id"] == path_b.rsplit("/", 1)[1]
        for endpoint in ("/file", "/results", "/export/project.zip"):
            status(bob.get(path_a + endpoint), 404)
        results_b = status(bob.get(path_b + "/results"), 200).json()
        assert len(results_b) == 16
        rag_review = path_b + "/results/" + quote(results_b[0]["result_id"], safe="") + "/review"
        rag_basis = status(bob.get(rag_review), 200).json()["basis"]
        proposal = status(bob.post(rag_review, headers=csrf(bob, primary.origin), json={
            "basis": rag_basis, "expected_revision": 0, "action": "propose_correction",
            "correction": {"context": {"top_k": 3}}, "reason": "Synthetic retrieval setting",
        }), 201).json()
        assert proposal["status"] == "correction_proposed"
        decision = status(bob.post(rag_review, headers=csrf(bob, primary.origin), json={
            "basis": rag_basis, "expected_revision": 1, "action": "approve_correction",
            "evidence_mode": "text", "warnings_acknowledged": True,
        }), 201).json()
        assert decision["status"] == "approved_corrected"
        assert decision["decision"]["reviewed_result"]["metric"] == results_b[0]["metric"]
        rag_history = status(bob.get(rag_review + "/history"), 200).json()
        snapshot_b = export(bob, path_b, directory / "bob-before.zip")
        primary.run("restart", "api", "worker")
        wait_for(lambda: bob.get("/api/auth/me").status_code == 200)
        assert status(bob.get(path_b + "/results"), 200).json() == results_b
        account(alice, primary.origin, "Alice", network_password)
        assert status(alice.get(review + "/history"), 200).json() == history
        disk_and_proxy_failure(primary, alice, primary.origin, solar)
        interrupted_writes(primary, alice, primary.origin, solar)
        interrupted_export(primary, alice, path_a, directory, snapshot_a)
        unavailable_storage(primary, alice)
        assert status(alice.get(review + "/history"), 200).json() == history
        old_cookie = alice.cookies[cookie_name]
        generated_secret = primary.run("exec", "-T", "api", "cat", "/local-secret/auth")
        log_check(primary, [invite_alice, invite_bob, network_password, password_admin,
                            old_cookie, generated_secret, private_query, "Алиса Solar.pdf", "Synthetic RAG.pdf"])
        primary.run("run", "--rm", "--no-deps", "backup", "create", "must-refuse-active", expected=1)
        local_alert(primary, directory)
        primary.run("stop", "proxy", "worker", "api")
        output = primary.run("run", "--rm", "--no-deps", "backup", "create", "rehearsal")
        manifest = json.loads(output.splitlines()[-1])
        assert manifest["counts"] == {"jobs": 2, "review_events": 3, "uploads": 2, "users": 3}
        assert manifest["configuration"]["session_policy"] == "excluded"
        assert {sha256(solar.read_bytes()).hexdigest(), sha256(rag.read_bytes()).hexdigest()} <= {
            row["sha256"] for row in manifest["files"].values()
        }
        interrupted_backup(primary)
        primary.run("run", "--rm", "--no-deps", "backup", "verify", "rehearsal")
        restore_name = encrypted_rehearsal(primary, directory) if encrypted else "rehearsal"
        # Copy into an intentionally incomplete artifact in the disposable backup volume.
        primary.run("run", "--rm", "--no-deps", "--entrypoint", "python", "backup", "-c",
                    "from pathlib import Path; Path('/backups/truncated.tar').write_bytes("
                    "Path('/backups/rehearsal.tar').read_bytes()[:2048])")
        primary.run("down")  # Release the network before starting the other project.
        restored.run("run", "--rm", "--no-deps", "restore", "truncated", expected=1)
        restore_failures(restored, restore_name)
        restored.run("run", "--rm", "--no-deps", "restore", restore_name, "--dry-run")
        restored.run("run", "--rm", "--no-deps", "restore", restore_name)
        restored.run("run", "--rm", "--no-deps", "restore", restore_name, expected=1)
        restored.start()
        with closing(restored.client(directory)[0]) as restored_alice, closing(restored.client(directory)[0]) as restored_bob:
            status(restored_alice.get("/api/auth/me", headers={"Cookie": f"{cookie_name}={old_cookie}"}), 401)
            account(restored_alice, restored.origin, "Alice", network_password)
            account(restored_bob, restored.origin, "Bob", network_password)
            assert status(restored_alice.get(path_a + "/file"), 200).content == original_a
            assert status(restored_bob.get(path_b + "/file"), 200).content == rag.read_bytes()
            assert status(restored_alice.get(path_a + "/results"), 200).json() == results_a
            assert status(restored_bob.get(path_b + "/results"), 200).json() == results_b
            assert status(restored_alice.get(review + "/history"), 200).json() == history
            assert export(restored_alice, path_a, directory / "alice-after.zip") == snapshot_a
            assert status(restored_bob.get(rag_review + "/history"), 200).json() == rag_history
            assert export(restored_bob, path_b, directory / "bob-after.zip") == snapshot_b
            status(restored_bob.get(path_a + "/file"), 404)
            status(restored_bob.get(path_a + "/export/project.zip"), 404)
            status(restored_alice.get(path_b + "/results"), 404)
    print("PASS Alice/Bob, scientific results (4 Solar / 16 RAG), two SHA-256 originals, ZIP and review history", flush=True)
    print("PASS active backup refusal, SIGKILL before backup publication, cold manifest, corrupt and conflicting restore refusal",
          flush=True)


def browser_check(directory, ca, origin, password, upload_route):
    work = directory / "browser"
    work.mkdir()
    for database in (work / ".pki/nssdb", work / ".local/share/pki/nssdb"):
        database.mkdir(parents=True)
        subprocess.run(["certutil", "-N", "--empty-password", "-d", f"sql:{database}"], check=True)
        subprocess.run(["certutil", "-A", "-d", f"sql:{database}", "-n", "spi-synthetic-ca",
                        "-t", "C,,", "-i", str(ca)], check=True)
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = env.get("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache/ms-playwright"))
    env["HOME"] = str(work)
    subprocess.run(["node", "tests/deployment/production.mjs"], cwd=ROOT / "frontend_redezigned",
                   input=json.dumps({"origin": origin, "password": password, "upload": upload_route.rsplit("/", 1)[1]}),
                   text=True, env=env, check=True, timeout=75)


def main():
    if not __debug__:
        raise RuntimeError("Deployment assertions must be enabled")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--browser", action="store_true", help="Use Chromium with an isolated NSS trust store on Linux")
    parser.add_argument("--encrypted", action="store_true", help="Exercise a local repository using restic from PATH")
    args = parser.parse_args()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    suffix = token_hex(4)
    primary = Compose(f"spi-e2e-{suffix}", port, f"spi-e2e-{suffix}-backups")
    restored = Compose(f"spi-restore-{suffix}", port, f"spi-e2e-{suffix}-backups")
    try:
        if args.build:
            primary.run("build", timeout=600)
        with TemporaryDirectory(prefix="spi-compose-e2e-") as work:
            directory = Path(work)
            blank = directory / "empty.env"
            blank.touch()
            environment = {key: value for key, value in os.environ.items() if not key.startswith("SPI_")}
            environment.update({f"SPI_HOST_{key}": f"/tmp/spi-e2e/{key.lower()}"
                                for key in ("DATASET", "BACKUPS", "PROXY_DATA", "AUTH_SECRET_FILE")})
            for image in ("RUNTIME", "PROXY"):
                environment[f"SPI_{image}_REPOSITORY"] = f"registry.example.org/spi/{image.lower()}"
                environment[f"SPI_{image}_DIGEST"] = "sha256:" + "0" * 64
            missing_tls = subprocess.run(
                ["docker", "compose", "--env-file", str(blank), "-f", "compose.yaml", "-f", "compose.production.yaml",
                 "config", "--quiet"], cwd=ROOT, env=environment, capture_output=True, text=True,
            )
            assert missing_tls.returncode and any(key in missing_tls.stderr for key in (
                "SPI_PUBLIC_DOMAIN", "SPI_PUBLIC_ORIGIN", "SPI_AUTH_ALLOWED_ORIGINS",
            ))
            try:
                scenario(primary, restored, directory, browser=args.browser, encrypted=args.encrypted)
            except (AssertionError, OSError, subprocess.SubprocessError):
                for compose in (primary, restored):
                    with suppress(AssertionError, OSError, subprocess.SubprocessError):
                        lines = compose.run("logs", "--no-log-prefix", "--tail", "10", "monitoring")
                        if lines:
                            print(lines, file=sys.stderr)
                raise
    finally:
        # Project names and volumes are generated only for this test invocation.
        restored.run("down", "-v")
        primary.run("down", "-v")


if __name__ == "__main__":
    main()
