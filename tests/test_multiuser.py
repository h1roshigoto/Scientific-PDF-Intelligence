import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from secrets import token_urlsafe
from threading import Barrier
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient

from spi import auth_api, auth_store
from spi.api import create_app
from spi.auth_models import AccountInput
from spi.migrate_private_uploads import migrate
from spi.tenant_storage import DAY, tenant_queue, tenant_store
from spi.upload_export import capture_snapshot
from spi.upload_export_verify import verify
from spi.upload_queue import UploadQueue
from spi.upload_reviews import UploadReviews
from spi.upload_store import UploadStore, storage_lease
from spi.upload_worker import MultiuserWorker, UploadWorker
from test_batch import make_pdf
from test_batch_table_proposals import rag_multilevel_pdf
from test_upload_processing import fake_model  # noqa: F401
from test_upload_reviews import payload
from test_uploads import TOKEN, directory_alias, pdf_bytes, post

ORIGIN = "http://127.0.0.1:5173"
PASSWORD = "Synthetic-Coast-Heron!296"
NEW_PASSWORD = "Synthetic-Lantern-Eagle!372"
SNAPSHOT = "X-SPI-Snapshot-SHA256"


@pytest.fixture
def multi(monkeypatch, tmp_path):
    env = {
        "SPI_AUTH_MODE": "multiuser", "SPI_AUTH_LOCAL_DEVELOPMENT": "true",
        "SPI_AUTH_COOKIE_SECURE": "false", "SPI_AUTH_ALLOWED_ORIGINS": ORIGIN,
        "SPI_AUTH_SECRET": token_urlsafe(32), "SPI_AUTH_DB": str(tmp_path / "auth" / "accounts.db"),
        "SPI_MULTIUSER_STORAGE_ROOT": str(tmp_path / "multiuser"),
        "SPI_STATE_DIR": str(tmp_path / "official"),
        "SPI_UPLOAD_ENABLED": "true", "SPI_UPLOAD_TOKEN": TOKEN["Authorization"].split()[1],
        "SPI_UPLOAD_DIR": "", "SPI_UPLOAD_STATE_DIR": "", "SPI_UPLOAD_PROCESS_STATE_DIR": "",
        "SPI_UPLOAD_PROCESS_GIGACHAT": "false", "SPI_AUTH_TRUSTED_PROXIES": "",
        "SPI_AUTH_CSRF_ENABLED": "true", "SPI_PUBLIC_DEMO_ENABLED": "false",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return create_app()


def client(app, *, ip="127.0.0.1", origin=ORIGIN):
    return TestClient(app, base_url=origin, client=(ip, 51000))


def csrf(browser, **headers):
    response = browser.get("/auth/csrf", headers=headers)
    assert response.status_code == 200, response.text
    return {"Origin": str(browser.base_url).rstrip("/"), "X-SPI-CSRF": response.json()["csrf_token"], **headers}


def data(name):
    return {"email": f"{name}@example.test", "display_name": name, "password": PASSWORD}


def login(browser, name, *, password=PASSWORD, **headers):
    return browser.post("/auth/login", headers=csrf(browser, **headers),
                        json={"email": data(name)["email"], "password": password})


def bootstrap(db):
    return db.add_user(AccountInput(**data("Operator")), bootstrap=True)


def register(db, admin, browser, name):
    response = browser.post("/auth/register", headers=csrf(browser),
                            json={**data(name), "invitation_code": db.invite(admin.user_id, 300)})
    assert response.status_code == 201, response.text
    return response.json()["user_id"]


@pytest.fixture
def members(multi):
    db = multi.state.auth
    admin = bootstrap(db)
    alice, bob = client(multi), client(multi)
    owners = [register(db, admin, alice, "Alice"), register(db, admin, bob, "Bob")]
    for browser, name in ((alice, "aLiCe"), (bob, "Bob")):
        response = login(browser, name)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
    return alice, bob, owners, admin


def upload(browser, content, *, domain="solar", filename="private.pdf"):
    return post(browser, content, domain=domain, filename=filename, headers=csrf(browser))


def upload_path(browser, content, **kwargs):
    response = upload(browser, content, **kwargs)
    assert response.status_code == 201, response.text
    return f"/uploads/{response.json()['upload_id']}"


def cli(*args, output_status=0):
    process = subprocess.run([sys.executable, "-m", "spi.auth_cli", *args, "--password-stdin"],
                             input=PASSWORD + "\n", capture_output=True, text=True, timeout=20)
    assert process.returncode == output_status, process.stderr
    return process.stdout.strip()


def test_cli_bootstrap_and_atomic_invitation(multi):
    args = ("bootstrap-admin", "--email", "Operator@example.test", "--display-name", "Operator")
    admin_id = cli(*args)
    cli(*args, output_status=1)
    code = cli("invite", "--admin-email", "operator@example.test")
    db = multi.state.auth
    with db.connect() as conn:
        assert conn.execute("SELECT code_hash FROM invitations").fetchone()[0] != code
        assert conn.execute("SELECT password_hash FROM users").fetchone()[0].startswith("$argon2id$")
    assert db.user(admin_id).role == "admin"

    one, two = client(multi), client(create_app())
    barrier = Barrier(2, timeout=10)

    def redeem(pair):
        browser, name = pair
        headers = csrf(browser)
        barrier.wait()
        return browser.post("/auth/register", headers=headers, json={**data(name), "invitation_code": code})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(redeem, [(one, "Alice"), (two, "Bob")]))
    assert sorted(response.status_code for response in results) == [201, 400]
    assert next(r.json() for r in results if r.status_code == 201)["email_verified"] is False
    expired = db.invite(admin_id, 300)
    with db.connect(write=True) as conn:
        conn.execute("UPDATE invitations SET expires_at = 0 WHERE redeemed_at IS NULL")
    assert one.post("/auth/register", headers=csrf(one),
                    json={**data("Charlie"), "invitation_code": expired}).status_code == 400
    valid = db.invite(admin_id, 300)
    assert one.post("/auth/register", headers=csrf(one), json={
        **data("Charlie"), "password": "passwordpassword", "invitation_code": valid,
    }).json() == {"detail": "weak_password"}
    assert one.post("/auth/register", headers=csrf(one), json={
        **data("OPERATOR"), "invitation_code": valid,
    }).status_code == 409
    assert one.post("/auth/register", headers=csrf(one), json={
        **data("Charlie"), "invitation_code": valid,
    }).status_code == 201


def test_login_rotation_logout_revocation_and_session_storage(multi, members, caplog):
    alice, bob, _, _ = members
    config = multi.state.auth.config
    prior = alice.cookies.get(config.cookie)
    prior_id = prior.split(".")[0]
    missing = login(client(multi), "Nobody")
    wrong = login(client(multi), "Alice", password="wrong")
    assert missing.status_code == wrong.status_code == 401 and missing.json() == wrong.json()
    auth = login(alice, "Alice")
    cookie = alice.cookies.get(config.cookie)
    assert cookie != prior
    assert "HttpOnly" in auth.headers["set-cookie"] and "SameSite=lax" in auth.headers["set-cookie"]
    assert "Domain=" not in auth.headers["set-cookie"] and "Path=/" in auth.headers["set-cookie"]
    assert cookie.split(".")[1] not in auth.text
    assert cookie.encode() not in config.db.read_bytes() and cookie.split(".")[1].encode() not in config.db.read_bytes()
    outsider = client(multi)
    outsider.cookies.set(config.cookie, prior)
    assert outsider.get("/auth/me").status_code == 401
    assert bob.delete(f"/auth/sessions/{auth.json()['session_id']}", headers=csrf(bob)).status_code == 404
    assert alice.get("/auth/me", headers={"X-SPI-Session-ID": prior_id}).status_code == 401
    assert len(alice.get("/auth/sessions").json()) == 1
    assert alice.post("/auth/logout", headers=csrf(alice)).status_code == 204
    assert alice.get("/auth/me").status_code == 401
    outsider.cookies.set(config.cookie, cookie)
    assert outsider.get("/auth/me").status_code == 401
    assert all(secret not in caplog.text for secret in (PASSWORD, cookie, prior))


def test_password_change_revokes_sessions_and_admin_block(multi, members):
    alice, bob, owners, admin = members
    other = client(multi)
    assert login(other, "Alice").status_code == 200
    sessions = alice.get("/auth/sessions").json()
    assert len(sessions) == 2
    second = next(row["session_id"] for row in sessions if not row["current"])
    assert alice.delete(f"/auth/sessions/{second}", headers=csrf(alice)).status_code == 204
    assert other.get("/auth/me").status_code == 401
    assert login(other, "Alice").status_code == 200
    assert alice.post("/auth/change-password", headers=csrf(alice),
                      json={"current_password": PASSWORD, "new_password": NEW_PASSWORD}).status_code == 204
    assert alice.get("/auth/me").status_code == other.get("/auth/me").status_code == 401
    assert login(alice, "Alice").status_code == 401
    assert login(alice, "Alice", password=NEW_PASSWORD).status_code == 200
    cli("block", "--admin-email", admin.email, "--user-id", owners[1])
    assert bob.get("/auth/me").status_code == 401
    assert login(bob, "Bob").json() == {"detail": "invalid_credentials"}


@pytest.mark.parametrize("column", ["expires_at", "last_seen_at"])
def test_session_expires(multi, members, column):
    alice, _, owners, _ = members
    with multi.state.auth.connect(write=True) as conn:
        conn.execute(f"UPDATE sessions SET {column} = 0 WHERE user_id = ?", (owners[0],))
    assert alice.get("/auth/me").status_code == 401


def test_csrf_origin_and_sanitized_validation(multi, members, caplog):
    alice, bob, _, _ = members
    key = csrf(alice)
    for headers, code in [
        ({"Origin": ORIGIN}, "invalid_csrf"),
        ({**key, "X-SPI-CSRF": "a" * 64}, "invalid_csrf"),
        ({**key, "Origin": "https://other.example"}, "invalid_origin"),
        (csrf(bob), "invalid_csrf"),
    ]:
        response = alice.post("/auth/logout", headers=headers)
        assert response.status_code == 403 and response.json() == {"detail": code}
        assert response.headers["cache-control"] == "no-store"
    assert alice.get("/auth/logout").status_code == 405
    assert client(multi).post("/auth/login", headers={"Origin": ORIGIN}, json=data("Alice")).status_code == 403
    secret = "oversized-password-" * 10
    malformed = alice.post("/auth/login", headers=key, json={"email": "bad", "password": secret})
    assert malformed.status_code == 422 and secret not in malformed.text + caplog.text
    assert alice.post("/auth/login", headers=key, content=b"0" * 65537).status_code == 413


def test_login_rate_cross_process_database_and_forwarded_ip(multi, monkeypatch):
    one, two = client(multi), client(create_app())
    # Pin the clock inside both app instances so the test cannot straddle a rate window.
    monkeypatch.setattr(auth_store, "time", lambda: 1_800_000_001)
    for attempt in range(8):
        browser = one if attempt % 2 else two
        assert login(browser, "Unknown", **{"X-Forwarded-For": f"127.0.0.{attempt + 2}"}).status_code == 401
    blocked = login(two, "Unknown")
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) > 0
    for attempt in range(12):
        assert login(one, f"Person{attempt}", **{"X-Forwarded-For": f"127.0.0.{attempt + 2}"}).status_code == 401
    assert login(two, "Different", **{"X-Forwarded-For": "127.0.0.99"}).status_code == 429


@pytest.mark.parametrize(("key", "value"), [
    ("SPI_AUTH_MODE", "fallback"),
    ("SPI_AUTH_DB", ""),
    ("SPI_AUTH_SECRET", "A" * 43),
    ("SPI_AUTH_ALLOWED_ORIGINS", "http://*"),
    ("SPI_AUTH_LOCAL_DEVELOPMENT", "false"),
    ("SPI_AUTH_ALLOWED_ORIGINS", "http://192.0.2.10:5173"),
    ("SPI_AUTH_CSRF_ENABLED", "false"),
    ("SPI_RATE_LIMIT_ENABLED", "false"),
    ("SPI_PUBLIC_DEMO_ENABLED", "true"),
    ("SPI_AUTH_TRUSTED_PROXIES", "0.0.0.0/0"),
])
def test_bad_auth_configuration_fails_startup(multi, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        create_app()


def test_production_requires_https_and_exact_proxy_and_host(multi, monkeypatch):
    origin = "https://spi.example.test"
    monkeypatch.setenv("SPI_AUTH_LOCAL_DEVELOPMENT", "false")
    monkeypatch.setenv("SPI_AUTH_COOKIE_SECURE", "true")
    monkeypatch.setenv("SPI_AUTH_ALLOWED_ORIGINS", origin)
    monkeypatch.setenv("SPI_AUTH_TRUSTED_PROXIES", "127.0.0.2")
    secured = create_app()
    bootstrap(secured.state.auth)
    direct = client(secured, origin=origin, ip="127.0.0.3")
    signed_in = login(direct, "Operator")
    assert signed_in.status_code == 200
    assert "__Host-spi-session=" in signed_in.headers["set-cookie"] and "Secure" in signed_in.headers["set-cookie"]
    assert signed_in.headers["strict-transport-security"] == "max-age=31536000"
    assert direct.get("/health", headers={"Host": "other.example.test"}).status_code == 400
    network = client(secured, origin=origin.replace("https", "http"), ip="127.0.0.3")
    forwarded = {"X-Forwarded-For": "203.0.113.3", "X-Forwarded-Proto": "https"}
    assert network.get("/health", headers=forwarded).status_code == 403
    proxy = client(secured, origin=origin.replace("https", "http"), ip="127.0.0.2")
    assert proxy.get("/health", headers=forwarded).json() == {"status": "ok"}
    assert proxy.get("/health", headers={**forwarded, "X-Forwarded-For": "192.0.2.5, 203.0.113.3"}).status_code == 400


def test_every_private_resource_uses_the_owner(multi, members, tmp_path):
    alice, bob, owners, _ = members
    db = multi.state.auth
    official = tmp_path / "official"
    official.mkdir()
    (official / "documents.json").write_text('["private marker"]')
    solar = make_pdf(tmp_path / "solar.pdf").read_bytes()
    rag = rag_multilevel_pdf(tmp_path / "rag.pdf", ("Gemini-2.5", "Flash"), ("Mistral v3-8B Instruct",)).read_bytes()
    a = upload_path(alice, solar, filename="../alice.pdf")
    b = upload_path(bob, rag, domain="rag", filename="bob.pdf")
    assert [row["original_filename"] for row in alice.get("/uploads").json()] == ["alice.pdf"]
    assert [row["original_filename"] for row in bob.get("/uploads").json()] == ["bob.pdf"]
    assert alice.post(a + "/process", headers=csrf(alice)).status_code == 202
    assert bob.post(b + "/process", headers=csrf(bob)).status_code == 202
    worker = MultiuserWorker(db)
    assert worker.run_one() and worker.run_one() and not worker.run_one()
    assert alice.get(a + "/file").content == solar and bob.get(b + "/file").content == rag
    a_results = alice.get(a + "/results").json()
    b_results = bob.get(b + "/results").json()
    assert len(a_results) == 4 and len(b_results) == 16
    review = f"{a}/results/{a_results[0]['result_id']}/review"
    body = payload(alice.get(review).json(), reviewer_label="Unverified display label")
    assert alice.post(review, headers=csrf(alice), json=body).status_code == 201
    target = tenant_store(db.config, owners[0], db.quotas(owners[0]))
    with sqlite3.connect(target.processing / a.split("/")[-1] / "reviews.sqlite3") as conn:
        assert conn.execute("SELECT actor_user_id FROM review_actors").fetchone()[0] == owners[0]
    preview = alice.get(a + "/export/manifest")
    assert preview.status_code == 200, preview.text
    export_headers = {SNAPSHOT: preview.headers[SNAPSHOT]}
    formats = ["/export/reviewed-results.csv", "/export/reviewed-results.json",
               "/export/scientific-passport.pdf", "/export/project.zip", "/export/manifest"]
    for route in formats:
        response = alice.get(a + route, headers=export_headers)
        assert response.status_code == 200, response.text
        assert response.headers[SNAPSHOT] == preview.headers[SNAPSHOT]
        if route.endswith(".zip"):
            archive = tmp_path / "alice.zip"
            archive.write_bytes(response.content)
            assert verify(archive)
    for path in [a + route for route in ["", "/file", "/processing", "/results", "/issues", "/reviews",
                                         "/reviewed-results", *formats]] + [review, review + "/history"]:
        response = bob.get(path, headers=export_headers)
        assert response.status_code == 404 and response.json() == {"detail": "upload_not_found"}
        assert response.headers["cache-control"] == "no-store"
    assert bob.post(a + "/process", headers=csrf(bob)).status_code == 404
    assert bob.post(review, headers=csrf(bob), json=body).status_code == 404
    assert bob.get(b + "/results").json() == b_results
    admin = client(multi)
    assert login(admin, "Operator").status_code == 200
    assert admin.get(a + "/file").status_code == 404
    assert client(multi).get("/uploads", headers=TOKEN).status_code == 401
    assert alice.get("/uploads", headers=TOKEN).status_code == 401
    for path in ["/documents", "/results", "/issues", "/documents/id/file", "/export/project-passport.pdf",
                 "/export/documents.json", "/openapi.json"]:
        assert alice.get(path).status_code == 404
    assert (official / "documents.json").read_text() == '["private marker"]'
    assert alice.get(f"/uploads/{a.split('/')[-1]}../file").status_code == 422
    assert alice.get("/uploads/invalid%27identifier/file").status_code == 422
    original = target.files / (a.split("/")[-1] + ".pdf")
    alias = tmp_path / "linked-source.pdf"
    original.rename(alias)
    os.link(alias, original)
    assert alice.get(a + "/file").status_code == 404


def test_junction_or_symlink_cannot_bridge_namespaces(multi, members):
    alice, bob, owners, _ = members
    a = upload_path(alice, pdf_bytes())
    db = multi.state.auth
    alice_store = tenant_store(db.config, owners[0], db.quotas(owners[0]))
    bob_root = db.config.root / "users" / owners[1]
    directory_alias(bob_root, alice_store.state.parent)
    response = bob.get(a + "/file")
    assert response.status_code == 503 and "path" not in response.text


def test_parallel_uploads_enqueues_and_quota_cleanup(multi, members, tmp_path):
    alice, bob, owners, _ = members
    db = multi.state.auth
    content = pdf_bytes()
    store = tenant_store(db.config, owners[0], db.quotas(owners[0]))
    store.files.mkdir(parents=True)
    leftover = store.files / ".upload-interrupted.part"
    leftover.write_bytes(content)
    with db.connect(write=True) as conn:
        conn.executemany("INSERT INTO user_quotas VALUES (?, ?, ?)", [
            (owners[0], "max_total_storage_bytes", len(content)), (owners[0], "max_queued_jobs", 1),
        ])
    assert upload(alice, b"invalid").status_code == 422
    assert not leftover.exists()
    assert alice.get("/account/usage").json()["documents"] == 0
    other = client(create_app())
    other.cookies.update(alice.cookies)
    barrier = Barrier(2, timeout=10)

    def send(pair):
        browser, index = pair
        headers = csrf(browser)
        barrier.wait()
        return post(browser, content, filename=f"{index}.pdf", headers=headers)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(send, [(alice, 1), (other, 2)]))
    assert sorted(r.status_code for r in responses) == [201, 409]
    used = alice.get("/account/usage").json()
    assert used["documents"] == 1 and used["storage_bytes"] == len(content)
    paths = [f"/uploads/{row['upload_id']}" for row in alice.get("/uploads").json()]
    assert upload(bob, content).status_code == 201
    with db.connect(write=True) as conn:
        conn.execute("DELETE FROM user_quotas WHERE name = 'max_total_storage_bytes'")
    paths.append(upload_path(alice, content))

    def enqueue(pair):
        browser, path = pair
        headers = csrf(browser)
        barrier.wait()
        return browser.post(path + "/process", headers=headers)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(enqueue, zip((alice, other), paths, strict=True)))
    assert sorted(r.status_code for r in responses) == [202, 409]
    assert alice.get("/account/usage").json()["queued_jobs"] == 1


def test_upload_export_rate_limits_and_atomic_daily_budget(multi, members, monkeypatch, tmp_path):
    alice, _, owners, _ = members
    db = multi.state.auth
    monkeypatch.setattr(auth_api, "RATE_LIMITS", {**auth_api.RATE_LIMITS, "upload": (1, 60), "export": (2, 60)})
    monkeypatch.setattr(auth_store, "time", lambda now=int(auth_store.time()): now)
    path = upload_path(alice, make_pdf(tmp_path / "test.pdf").read_bytes())
    other = client(create_app())
    other.cookies.update(alice.cookies)
    response = upload(other, pdf_bytes())
    assert response.status_code == 429 and response.headers["retry-after"]
    assert alice.post(path + "/process", headers=csrf(alice)).status_code == 202
    assert MultiuserWorker(db).run_one()
    with db.connect(write=True) as conn:
        conn.execute("INSERT INTO user_quotas VALUES (?, 'max_exports_per_period', 1)", (owners[0],))
    assert alice.get(path + "/export/project.zip").status_code == 200
    spent = other.get(path + "/export/project.zip")
    assert spent.status_code == 429 and spent.json()["detail"] == "export_quota_full"
    limited = other.get(path + "/export/manifest")
    assert limited.status_code == 429 and limited.json()["detail"] == "rate_limited"
    one = db
    two = other.app.state.auth

    def reserve(database):
        try:
            database.consume([("quota_model", owners[0], 1, DAY)])
            return 1
        except auth_store.AuthError as exc:
            assert exc.status == 429
            return 0

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(reserve, [one, two])) == 1
    assert alice.get("/account/usage").json()["model_requests_remaining"] == db.quotas(owners[0]).max_model_requests - 1


def test_worker_checks_job_owner_and_enforces_model_and_retry_budgets(multi, members, fake_model, tmp_path):
    alice, bob, owners, _ = members
    db = multi.state.auth
    with db.connect(write=True) as conn:
        conn.executemany("INSERT INTO user_quotas VALUES (?, ?, ?)", [
            (owners[0], "max_model_requests", 1), (owners[0], "max_retries", 1),
        ])
    document = make_pdf(tmp_path / "budget.pdf").read_bytes()
    path = upload_path(alice, document)
    other = upload_path(bob, document)
    assert alice.post(path + "/process", headers=csrf(alice)).status_code == 202
    quota = db.quotas(owners[0])
    store = tenant_store(db.config, owners[0], quota)
    queue = tenant_queue(db.config, store, quota)
    upload_id = path.split("/")[-1]
    with queue._connect() as conn:
        (raw,) = conn.execute("SELECT metadata FROM jobs WHERE upload_id = ?", (upload_id,)).fetchone()
        changed = json.loads(raw)
        changed["owner_user_id"] = owners[1]
        conn.execute("UPDATE jobs SET metadata = ? WHERE upload_id = ?", (json.dumps(changed), upload_id))
    worker = MultiuserWorker(db, model_enabled=True)
    _, calls = fake_model
    assert not worker.run_one() and not calls
    assert not (store.processing / upload_id).exists()
    assert bob.get(other + "/processing").json()["status"] == "not_started"
    with queue._connect() as conn:
        conn.execute("UPDATE jobs SET metadata = ? WHERE upload_id = ?", (raw, upload_id))
    assert worker.run_one()
    job = queue.get(upload_id)
    assert job.model_requests == len(calls) == 1
    assert job.processing.diagnostic == "completion:model_request_quota_full"
    assert alice.post(path + "/process", headers=csrf(alice)).status_code == 202
    assert worker.run_one() and len(calls) == 1
    assert alice.post(path + "/process", headers=csrf(alice)).json() == {"detail": "processing_quota_full"}
    assert bob.get("/account/usage").json()["model_requests_remaining"] == db.quotas(owners[1]).max_model_requests


@pytest.mark.parametrize("global_slots", [1, 2])
def test_multiprocess_slots_and_crash_recovery(multi, members, monkeypatch, tmp_path, global_slots):
    monkeypatch.setenv("SPI_GLOBAL_MAX_ACTIVE_JOBS", str(global_slots))
    alice, bob, _, _ = members
    content = make_pdf(tmp_path / "my.pdf").read_bytes()
    a, second, b = upload_path(alice, content), upload_path(alice, content), upload_path(bob, content)
    queued = alice.post(a + "/process", headers=csrf(alice)).json()
    signal = tmp_path / "checkpoint"
    script = """
import os
from pathlib import Path
from time import sleep
from spi import batch, upload_worker

save = batch._checkpoint
def pause(store, record):
    save(store, record)
    if record.get('fragments'):
        Path(os.environ['SPI_TEST_CHECKPOINT_READY']).touch()
        sleep(60)

batch._checkpoint = pause
upload_worker.main()
"""
    child = subprocess.Popen([sys.executable, "-c", script, "--once"],
                             env={**os.environ, "SPI_TEST_CHECKPOINT_READY": str(signal)})
    command = [sys.executable, "-m", "spi.upload_worker", "--once"]
    try:
        deadline = monotonic() + 15
        while not signal.exists() and child.poll() is None and monotonic() < deadline:
            sleep(0.05)
        assert signal.exists()
        assert alice.post(second + "/process", headers=csrf(alice)).status_code == 202
        assert bob.post(b + "/process", headers=csrf(bob)).status_code == 202
        subprocess.run(command, check=True, timeout=20)
        assert alice.get(second + "/processing").json()["status"] == "queued"
        assert bob.get(b + "/processing").json()["status"] == ("queued" if global_slots == 1 else "completed")
    finally:
        child.kill()
        child.wait(timeout=10)
    for _ in range(3):
        subprocess.run(command, check=True, timeout=20)
    result = alice.get(a + "/processing").json()
    assert result["job_id"] == queued["job_id"] and result["recoveries"] == 1
    assert len(alice.get(a + "/results").json()) == 4
    assert len(alice.get(second + "/results").json()) == 4
    assert len(bob.get(b + "/results").json()) == 4


def test_migration_preserves_snapshot_rejects_conflicts_and_recovers(multi, members, tmp_path, monkeypatch):
    alice, _, owners, _ = members
    db = multi.state.auth
    root = tmp_path / "legacy"
    source = UploadStore(root / "pdfs", root / "metadata", 65536, 10, root / "processing")
    content = make_pdf(tmp_path / "legacy.pdf").read_bytes()
    with (tmp_path / "legacy.pdf").open("rb") as pdf:
        original = source.save(pdf, "legacy.pdf", "solar")
    queue = UploadQueue(source.processing)
    queue.enqueue(original)
    assert UploadWorker(source).run_one()
    identity = original.upload_id
    job = queue.get(identity)
    reviews = UploadReviews(source, original, queue.root, job, write=True)
    from spi.review_models import ReviewRequest
    state = reviews.overview().reviews[0]
    detail = reviews.detail(state.result_id)
    body = payload(detail.model_dump(mode="json"), reviewer_label="Historical operator")
    reviews.save(state.result_id, ReviewRequest.model_validate(body))
    expected = capture_snapshot(source, original, queue).preview.metadata.snapshot_sha256
    for path in (root, db.config.root):
        assert path.exists()
    with storage_lease(source.state):
        with pytest.raises(sqlite3.OperationalError):
            migrate(db, root, owners[0], dry_run=True)
    report = migrate(db, root, owners[0], dry_run=True)
    assert report["status"] == "dry_run" and report["uploads"] == report["jobs"] == report["review_events"] == 1
    from spi import migrate_private_uploads as migration
    publish = os.rename

    def fail(*args):
        raise OSError("simulated interruption before publication")

    with monkeypatch.context() as isolated:
        isolated.setattr(migration.os, "rename", fail)
        with pytest.raises(OSError):
            migrate(db, root, owners[0], dry_run=False)
    assert os.rename is publish
    assert migrate(db, root, owners[0], dry_run=False)["status"] == "imported"
    assert migrate(db, root, owners[0], dry_run=False)["status"] == "already_imported"
    with pytest.raises(ValueError, match="already associated"):
        migrate(db, root, owners[1], dry_run=False)
    path = f"/uploads/{identity}"
    assert alice.get(path + "/file").content == content
    assert alice.get(path + "/export/manifest").headers[SNAPSHOT] == expected
    assert alice.get(path + "/processing").json() == job.processing.model_dump(mode="json")
    review = alice.get(f"{path}/results/{state.result_id}/review/history").json()
    assert review[0]["decision"]["reviewer_label"] == "Historical operator"
    output = tmp_path / "migrated.zip"
    output.write_bytes(alice.get(path + "/export/project.zip", headers={SNAPSHOT: expected}).content)
    assert verify(output)
    assert source.file(original).read_bytes() == content
    with pytest.raises(ValueError):
        migrate(db, tmp_path / "official", owners[0], dry_run=True)
