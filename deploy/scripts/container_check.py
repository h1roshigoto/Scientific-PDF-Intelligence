"""Verify the effective OCI runtime and application files in built image layers."""

import argparse
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory

SERVICES = {"api": 10001, "worker": 10001, "sandbox": 10001, "proxy": 10002, "monitoring": 10001}
ALLOWED_MOUNTS = {
    "api": {"/state-auth", "/state-tenants", "/coord", "/parser", "/metrics", "/local-secret", "/run/spi-auth-secret"},
    "worker": {"/state-auth", "/state-tenants", "/coord", "/parser", "/metrics", "/local-secret", "/run/spi-auth-secret"},
    "sandbox": {"/parser"}, "proxy": {"/data", "/config"}, "monitoring": {"/metrics"},
}
SECRET = re.compile(rb"(?:-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9_]{32,}|"
                    rb"AKIA[A-Z0-9]{16}|(?:SPI_AUTH_SECRET|GIGACHAT_CREDENTIALS)=[A-Za-z0-9_+/=-]{30,})")


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, encoding="utf-8")


def runtime(compose):
    inspected = {}
    for service, uid in SERVICES.items():
        names = docker(*compose, "ps", "-q", service).splitlines()
        assert len(names) == 1, f"{service} requires one running container"
        name = names[0]
        config = json.loads(docker("inspect", name))[0]
        host = config["HostConfig"]
        assert config["State"]["Running"] and config["Config"]["User"] == f"{uid}:{uid}", service
        assert host["ReadonlyRootfs"] and not host["Privileged"], service
        assert host["CapDrop"] == ["ALL"] and not host["CapAdd"], service
        assert "no-new-privileges:true" in host["SecurityOpt"], service
        assert host["PidMode"] != "host" and host["NetworkMode"] != "host", service
        assert 0 < host["NanoCpus"] <= 1_000_000_000 and 0 < host["Memory"] <= 768 * 1024**2, service
        assert 0 < host["PidsLimit"] <= 64 and {"Name": "nofile", "Hard": 1024, "Soft": 1024} in host["Ulimits"]
        mounts = {mount["Destination"]: mount for mount in config["Mounts"]}
        assert set(mounts) <= ALLOWED_MOUNTS[service] | {"/tmp"}, service
        assert all("docker.sock" not in str(mount) for mount in mounts.values()), service
        assert host["RestartPolicy"]["Name"] == "unless-stopped", service
        if service != "proxy":
            assert not host["PortBindings"], service
            assert all(opt in host["Tmpfs"]["/tmp"] for opt in ("size=", "noexec", "nosuid", "nodev")), service
        cap_script = (
            "id -u; awk '/^(CapEff|CapBnd|NoNewPrivs|Seccomp):/{ print $2 }' /proc/self/status; "
            "cat /sys/fs/cgroup/cpu.max /sys/fs/cgroup/memory.max /sys/fs/cgroup/pids.max "
            "/sys/fs/cgroup/memory.swap.max; ulimit -n"
        )
        values = docker("exec", name, "sh", "-ec", cap_script).splitlines()
        assert values[:5] == [str(uid), "0000000000000000", "0000000000000000", "1", "2"], service
        cpu_quota, cpu_period = map(int, values[5].split())
        assert cpu_quota * 1_000_000_000 == cpu_period * host["NanoCpus"], service
        assert int(values[6]) == host["Memory"] and int(values[7]) == host["PidsLimit"], service
        assert values[8:] == ["0", "1024"], service
        if service in {"worker", "sandbox", "monitoring"}:
            assert host["NetworkMode"] == "none", service
        if service == "monitoring":
            assert mounts["/metrics"]["RW"] is False
        if service in {"api", "worker"}:
            assert mounts["/parser"]["RW"] is False
            secret = "/local-secret" if "/local-secret" in mounts else "/run/spi-auth-secret"
            assert mounts[secret]["RW"] is False
        inspected[service] = config
        print(f"PASS {service} user {uid}, zero capabilities, read-only root, constrained ports, mounts and runtime")
    api = inspected["api"]
    backend = next(iter(api["NetworkSettings"]["Networks"]))
    assert len(api["NetworkSettings"]["Networks"]) == 1
    network = json.loads(docker("network", "inspect", backend))[0]
    assert network["Internal"]
    proxy_ip = inspected["proxy"]["NetworkSettings"]["Networks"][backend]["IPAddress"]
    assert f"SPI_AUTH_TRUSTED_PROXIES={proxy_ip}" in api["Config"]["Env"]
    sandbox = inspected["sandbox"]["Id"]
    probe = """
import errno, os, resource, socket
from pathlib import Path
from spi.ops.pdf_sandbox import restrict_child
base = Path('/tmp/spi-boundary-probe')
base.mkdir()
document = base / 'document'
unrelated = base / 'unrelated'
document.write_text('sample')
unrelated.write_text('private')
with (base / 'result').open('wb') as result:
    restrict_child(document)
    assert document.read_text() == 'sample'
    result.write(b'allowed')
assert resource.getrlimit(resource.RLIMIT_CPU) == (15, 15)
assert resource.getrlimit(resource.RLIMIT_AS) == (384 * 1024**2,) * 2
assert resource.getrlimit(resource.RLIMIT_FSIZE) == (16 * 1024**2,) * 2
assert resource.getrlimit(resource.RLIMIT_NOFILE) == (64, 64)
assert all(not Path(name).exists()
           for name in ('/state-auth', '/state-tenants', '/run/spi-auth-secret', '/local-secret/auth'))
actions = (unrelated.read_bytes, lambda: document.write_text('different'),
           lambda: (base / 'new').touch(), document.unlink, lambda: document.rename(base / 'renamed'),
           lambda: socket.socket(socket.AF_INET), os.fork, lambda: os.execl('/bin/true', 'true'))
for operation in actions:
    try:
        operation()
    except OSError as exc:
        assert exc.errno in (errno.EACCES, errno.EPERM)
    else:
        raise AssertionError('PDF parser sandbox accepted a forbidden operation')
"""
    docker("exec", sandbox, "python", "-c", probe)
    docker("exec", sandbox, "sh", "-ec",
           "cd /tmp/spi-boundary-probe; test \"$(cat document)\" = sample; "
           "test \"$(cat unrelated)\" = private; test \"$(cat result)\" = allowed; "
           "test ! -e new; test ! -e renamed; rm document unrelated result; rmdir /tmp/spi-boundary-probe")
    print("PASS real Landlock, seccomp, parser rlimits, document-only reads and inherited result descriptor")
    program = Path(__file__).with_name("pdf_sandbox_check.py").read_bytes()
    subprocess.run(["docker", "exec", "-i", sandbox, "python", "-"], input=program, check=True)
    images = {config["Config"]["Image"]: config["Image"] for config in inspected.values()}
    for name, image_id in images.items():
        assert docker("image", "inspect", name, "--format", "{{.Id}}").strip() == image_id, (
            f"{name} changed since the containers were started")
    return images, api["Config"]["Image"]


def scan_layers(images):
    for image in sorted(images):
        with TemporaryDirectory(prefix="spi-image-layers-") as temporary:
            archive_path = Path(temporary) / "image.tar"
            docker("image", "save", "-o", str(archive_path), image)
            with tarfile.open(archive_path) as archive:
                manifest = json.load(archive.extractfile("manifest.json"))
                for name in {layer for record in manifest for layer in record["Layers"]}:
                    with tarfile.open(fileobj=archive.extractfile(name), mode="r|*") as layer:
                        for member in layer:
                            path = member.name.removeprefix("./")
                            if not member.isfile() or not path.startswith(("app/", "srv/", "etc/caddy/")):
                                continue
                            parts = path.split("/")
                            assert not any(part.startswith(".env") or part in {".git", "outputs", "node_modules"}
                                           for part in parts), path
                            assert not path.endswith((".pdf", ".zip", ".sqlite3", ".map", ".key")), path
                            if member.size <= 16 * 1024**2:
                                assert not SECRET.search(layer.extractfile(member).read()), f"Secret signature in {path}"
        print(f"PASS image layers {image}: no private inputs, source maps or matching secret signatures")


def exercise_limits(image):
    cpu = """
from pathlib import Path
from time import monotonic
def throttled():
    values = dict(line.split() for line in Path('/sys/fs/cgroup/cpu.stat').read_text().splitlines())
    return int(values['nr_throttled'])
before = throttled()
until = monotonic() + 1.5
while monotonic() < until:
    pass
assert throttled() > before
"""
    options = ("--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true")
    docker("run", "--rm", *options, "--cpus", "0.25", image, "python", "-c", cpu)
    memory = """
from pathlib import Path
import subprocess
import sys
def kills():
    values = dict(line.split() for line in Path('/sys/fs/cgroup/memory.events').read_text().splitlines())
    return int(values['oom_kill'])
before = kills()
result = subprocess.run([sys.executable, '-c', 'buffer=bytearray(160*1024**2)'], timeout=15)
assert result.returncode == -9 and kills() > before
"""
    docker("run", "--rm", *options, "--memory", "64m", "--memory-swap", "64m", image, "python", "-c", memory)
    disk = """
import os
from pathlib import Path
from spi.ops.state import disk_full
from spi.storage import Store
mount = Path('/limited')
store = Store(mount)
store.write('checkpoint.json', {'state': 'original'})
with (mount / 'filler').open('xb') as filler:
    stat = os.statvfs(mount)
    os.posix_fallocate(filler.fileno(), 0, stat.f_bavail * stat.f_frsize)
try:
    store.write('checkpoint.json', {'state': 'new'})
except OSError as exc:
    assert disk_full(exc)
else:
    raise AssertionError('Capacity boundary was not enforced')
assert store.read('checkpoint.json') == {'state': 'original'}
assert set(p.name for p in mount.iterdir()) == {'filler', 'checkpoint.json'}
"""
    docker("run", "--rm", *options, "--tmpfs", "/limited:rw,noexec,nodev,nosuid,size=8m,uid=10001,gid=10001,mode=0700",
           image, "python", "-c", disk)
    print("PASS disposable CPU throttling, cgroup OOM and limited dataset ENOSPC preserving checkpoint")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--profile", choices=("local", "production"), default="local")
    parser.add_argument("--env-file")
    parser.add_argument("--layers", action="store_true")
    args = parser.parse_args()
    compose = ["compose", "-p", args.project, "-f", "compose.yaml", "-f", f"compose.{args.profile}.yaml"]
    if args.env_file:
        compose.extend(("--env-file", args.env_file))
    images, api_image = runtime(compose)
    if args.layers:
        scan_layers(images)
        exercise_limits(api_image)


if __name__ == "__main__":
    main()
