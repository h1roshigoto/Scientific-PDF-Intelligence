"""Compose startup checks, resource probes and internal monitoring."""

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_urlsafe
from tempfile import NamedTemporaryFile
from time import sleep, time
from urllib.request import urlopen

from spi import pdf_boundary
from spi.auth_config import AuthConfig, flag
from spi.upload_store import _positive_limit, private_path

from .state import cold_lock, database, files, integrity, write_json
from .telemetry import METRICS, event, last_snapshot, snapshot, worker_metric_name

DATASET = Path("/dataset")
STATE = DATASET / "state"


def initialize():
    private_path(DATASET, Path("coord"), directory=True).mkdir(mode=0o700, exist_ok=True)
    with cold_lock(DATASET / "coord", exclusive=False, create=True):
        for relative in ("state/auth", "state/tenants"):
            private_path(DATASET, Path(relative), directory=True).mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.getenv("SPI_AUTH_SECRET_FILE") == "/local-secret/auth":
            path = Path("/local-secret/auth")
            if not path.exists():
                with NamedTemporaryFile("w", dir=path.parent, encoding="ascii", delete=False) as stream:
                    temporary = Path(stream.name)
                    try:
                        stream.write(token_urlsafe(32) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                        try:
                            os.link(temporary, path)
                        except FileExistsError:
                            pass
                    finally:
                        temporary.unlink(missing_ok=True)


def validate_deployment(config):
    profile = os.getenv("SPI_DEPLOYMENT_PROFILE")
    origin = os.getenv("SPI_PUBLIC_ORIGIN")
    domain = os.getenv("SPI_PUBLIC_DOMAIN", "")
    if profile == "production":
        if (not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain)
                or domain.endswith((".localhost", ".local", ".invalid", ".test", ".example"))
                or origin != f"https://{domain}"):
            raise ValueError("invalid_public_domain")
    elif profile != "local" or not re.fullmatch(r"https://localhost:[0-9]{1,5}", origin or ""):
        raise ValueError("invalid_deployment_profile")
    if (config.origins != (origin,) or config.development or not config.secure
            or len(config.trusted_proxies) != 1 or config.trusted_proxies[0].version != 4
            or not pdf_boundary.enabled() or not os.getenv("SPI_AUTH_SECRET_FILE")
            or config.quotas.max_pdf_bytes > pdf_boundary.MAX_PDF_BYTES
            or flag("SPI_UPLOAD_PROCESS_GIGACHAT")):
        raise ValueError("invalid_deployment_security_config")


def storage_ready(roots):
    minimum = _positive_limit("SPI_DISK_MIN_FREE_BYTES", 128 * 1024**2)
    for root in roots:
        private_path(root, Path(), directory=True)
        if (not root.is_dir() or not os.access(root, os.W_OK | os.X_OK)
                or shutil.disk_usage(root).free < minimum):
            raise OSError("storage_unavailable")


def auth_ready(config):
    storage_ready((config.db.parent, config.root))
    with closing(sqlite3.connect(config.db.absolute().as_uri() + "?mode=rw", timeout=1, uri=True)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("SELECT user_id FROM users LIMIT 1").fetchone()
        db.rollback()
    return True


def startup(config):
    validate_deployment(config)
    storage_ready((config.db.parent, config.root))
    for root in (config.db.parent, config.root):
        for path in files(root):
            if path.suffix == ".sqlite3":
                with database(path, writable=True) as db:
                    integrity(db)
    if pdf_boundary.request("ping") != {"status": "ok"}:
        raise OSError("pdf_parser_unavailable")


def serve(command):
    if not command:
        raise ValueError("missing_command")
    os.umask(0o077)
    with cold_lock(Path("/coord"), exclusive=False) as lease:
        startup(AuthConfig.from_env())
        os.set_inheritable(lease, True)
        if command[:3] == ["python", "-m", "uvicorn"]:
            path = Path("/tmp/spi-logging.json")
            write_json(path, {
                "version": 1, "disable_existing_loggers": False,
                "formatters": {"safe": {"()": "spi.ops.telemetry.SafeFormatter"}},
                "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "safe"}},
                "root": {"level": "INFO", "handlers": ["console"]},
                "loggers": {"uvicorn": {"level": "INFO", "handlers": ["console"], "propagate": False}},
            })
            command.extend(("--log-config", str(path)))
        os.execvp(command[0], command)


def probe_worker(worker):
    age = time() - worker.get("sample_time", 0)
    processing = worker.get("state") == "processing"
    limit = _positive_limit("SPI_WORKER_JOB_TIMEOUT", 240) + 15 if processing else 30
    return processing and age < limit or worker.get("state") == "idle" and age < limit


def queue_metrics(root):
    now = time()
    totals = {"queue_depth": 0, "oldest_queued_job_age_seconds": 0, "processing_failures": 0,
              "worker_recoveries": 0, "model_requests": 0, "processing": 0}
    users = private_path(root, Path("users"), directory=True)
    if not users.is_dir():
        return totals
    for directory in users.iterdir():
        path = private_path(users, Path(directory.name, "processing", "queue.sqlite3"))
        if not path.is_file():
            continue
        with database(path) as db:
            rows = db.execute("""SELECT status, min(updated_at), count(*),
                sum(json_extract(metadata, '$.processing.recoveries')),
                sum(json_extract(metadata, '$.model_requests')) FROM jobs GROUP BY status""")
            for status, oldest, count, recoveries, requests in rows:
                totals["worker_recoveries"] += recoveries or 0
                totals["model_requests"] += requests or 0
                if status == "queued":
                    age = now - datetime.fromisoformat(oldest).astimezone(timezone.utc).timestamp()
                    totals["queue_depth"] += count
                    totals["oldest_queued_job_age_seconds"] = max(totals["oldest_queued_job_age_seconds"], int(age))
                elif status == "failed":
                    totals["processing_failures"] += count
                elif status == "processing":
                    totals["processing"] += count
    return totals


def collect_storage(config):
    try:
        usage = shutil.disk_usage(config.root)
        snapshot("queue", {"sample_time": time(), "disk_free_bytes": usage.free, "disk_used_bytes": usage.used,
                           "disk_low": usage.free < _positive_limit("SPI_DISK_MIN_FREE_BYTES", 128 * 1024**2),
                           "queue_available": True, **queue_metrics(config.root)})
    except (OSError, ValueError, sqlite3.Error):
        snapshot("queue", {"sample_time": time(), "queue_available": False})


def monitoring():
    while True:
        active = [worker["state"] for path in METRICS.glob("worker-*.json")
                  if probe_worker(worker := last_snapshot(path.stem))]
        event("monitoring", "sample", active_workers=len(active),
              worker_state="processing" if "processing" in active else "idle" if active else "stalled",
              queue=last_snapshot("queue"), api=last_snapshot("api"),
              backup=last_snapshot("backup"), restore=last_snapshot("restore"))
        sleep(15)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("initialize", "serve", "api-health", "worker-health", "monitor"))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.action == "initialize":
            initialize()
        elif args.action == "serve":
            serve(args.command)
        elif args.action == "api-health":
            with urlopen("http://127.0.0.1:8000/__ops/ready", timeout=3) as response:
                if response.status != 200:
                    raise ValueError("api_unavailable")
        elif args.action == "worker-health":
            queue = last_snapshot("queue")
            if not probe_worker(last_snapshot(worker_metric_name())) or not queue.get("queue_available"):
                raise ValueError("worker_unavailable")
        else:
            monitoring()
    except (OSError, ValueError, sqlite3.Error) as exc:
        event("runtime", "operations_check_failed", level="ERROR", action=args.action, error_type=type(exc).__name__)
        sys.exit(1)


if __name__ == "__main__":
    main()
