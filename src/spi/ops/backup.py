"""Create and verify a single-host cold backup of private data."""

import argparse
import json
import os
import re
import shutil
import sqlite3
import tarfile
from contextlib import closing
from datetime import datetime, timezone
from hashlib import file_digest
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from time import time
from uuid import uuid4

from spi.auth_models import USER_ID
from spi.models import UPLOAD_ID_PATTERN
from spi.review_models import ReviewEvent
from spi.upload_processing import UploadProcessingStore
from spi.upload_queue import UploadQueue
from spi.upload_reviews import source_result_hash
from spi.upload_store import UploadStore, _positive_limit, private_path

from .runtime import DATASET, STATE
from .state import APP_VERSION, BACKUP_SCHEMA, cold_lock, database, files, integrity
from .telemetry import event, last_snapshot, snapshot

BACKUPS = Path("/backups")
MANIFEST = "manifest.json"
MANIFEST_MAX_BYTES = 16 * 1024**2
AUTH_DB = Path("auth/auth.sqlite3")
TRANSACTION_FILES = {"maintenance.sqlite3", "batch-lock.sqlite3", "write-lock.sqlite3"}
HEX = r"[a-f0-9]{64}"
NAME = r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}"


def digest(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def archive_path(name):
    if not re.fullmatch(NAME, name):
        raise ValueError("invalid_backup_name")
    return private_path(BACKUPS, Path(f"{name}.tar"))


def safe_member(value):
    if not isinstance(value, str) or len(value) > 240 or "\\" in value or ":" in value:
        raise ValueError("invalid_backup_path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part == ".." for part in path.parts) or not path.parts:
        raise ValueError("invalid_backup_path")
    if path.as_posix() != AUTH_DB.as_posix() and (len(path.parts) < 4
            or path.parts[:2] != ("tenants", "users") or not re.fullmatch(USER_ID, path.parts[2])):
        raise ValueError("invalid_backup_namespace")
    return Path(*path.parts)


def catalog(tenant, owner):
    store = UploadStore(tenant / "uploads", tenant / "metadata", 1, 1, tenant / "processing", owner_user_id=owner)
    db_path = private_path(store.state, Path("uploads.sqlite3"))
    if not db_path.is_file():
        return store, {}
    with database(db_path) as db:
        rows = db.execute("SELECT upload_id, metadata FROM uploads")
        return store, {key: store._record(raw, key) for key, raw in rows}


def jobs_for(store, uploads):
    queue = UploadQueue(store.processing, store.owner_user_id)
    path = private_path(store.processing, Path("queue.sqlite3"))
    jobs = {}
    if path.is_file():
        with database(path) as db:
            for key, status, updated in db.execute("SELECT upload_id, status, updated_at FROM jobs"):
                job = queue._get(db, key)
                if (key not in uploads or not job.matches(uploads[key]) or job.processing.status != status
                        or job.processing.updated_at.isoformat() != updated):
                    raise ValueError("backup_queue_identity")
                jobs[key] = job
    return jobs


def review_count(path, upload, migrated):
    if not path.is_file():
        return 0
    with database(path) as db:
        actors = {}
        if db.execute("SELECT 1 FROM sqlite_schema WHERE name = 'review_actors'").fetchone():
            actors = {(result, revision): actor for result, revision, actor in db.execute(
                "SELECT result_id, revision, actor_user_id FROM review_actors")}
        latest = {}
        count = 0
        for result, revision, raw in db.execute(
                "SELECT result_id, revision, event FROM review_events ORDER BY result_id, revision"):
            event = ReviewEvent.model_validate_json(raw)
            decision = event.decision
            actor = actors.pop((result, revision), None)
            previous = latest.get(result)
            if (event.revision != revision or event.previous_decision != previous
                    or revision != (previous.revision + 1 if previous else 1)
                    or (decision.upload_id, decision.domain, decision.pdf_sha256, decision.result_id)
                    != (upload.upload_id, upload.domain, upload.sha256, result)
                    or decision.source_result_hash != source_result_hash(event.original_result)
                    or (actor != upload.owner_user_id and not (migrated and actor is None))):
                raise ValueError("backup_review_identity")
            latest[result] = decision
            count += 1
        if actors:
            raise ValueError("backup_orphan_review_actor")
        return count


def tenant_files(root, owners, *, live):
    users = private_path(root, Path("tenants/users"), directory=True)
    if not users.exists():
        return
    for tenant in sorted(users.iterdir()):
        owner = tenant.name
        if owner not in owners or not re.fullmatch(USER_ID, owner):
            raise ValueError("backup_unknown_tenant_owner")
        _, uploads = catalog(tenant, owner)
        for source in files(tenant):
            relative = source.relative_to(tenant)
            name, parts = source.name, relative.parts
            if relative == Path(".private-migration.json") or relative == Path("metadata/uploads.sqlite3"):
                yield source
            elif parts[0] == "uploads" and len(parts) == 2 and re.fullmatch(UPLOAD_ID_PATTERN, source.stem) and source.suffix == ".pdf":
                if not live or source.stem in uploads:
                    yield source
            elif relative == Path("processing/queue.sqlite3"):
                yield source
            elif parts[0] == "processing" and len(parts) >= 3 and parts[1] in uploads and (
                    name == "reviews.sqlite3" or source.suffix == ".json" and not name.startswith(".")):
                yield source
            elif not live or not (
                name in TRANSACTION_FILES or name.endswith(("-wal", "-shm", "-journal"))
                or parts[0] == "uploads" and re.fullmatch(r"\.upload-[a-z0-9_]+\.part", name)
                or parts[0] == "processing" and (parts[1] == "locks" or name.startswith("tmp"))
            ):
                raise ValueError("backup_unexpected_tenant_file")


def inspect(root):
    versions, uploads_count, reviews_count, job_count = {}, 0, 0, 0
    with database(root / AUTH_DB) as db:
        versions[AUTH_DB.as_posix()] = integrity(db, full=True)
        owners = {row[0] for row in db.execute("SELECT user_id FROM users")}
        if db.execute("SELECT 1 FROM sessions LIMIT 1").fetchone():
            raise ValueError("backup_contains_sessions")
    members = {root / AUTH_DB, *tenant_files(root, owners, live=False)}
    if set(files(root)) != members:
        raise ValueError("backup_unexpected_file")
    for path in sorted(members):
        if path.suffix == ".sqlite3" and path != root / AUTH_DB:
            with database(path) as db:
                versions[path.relative_to(root).as_posix()] = integrity(db, full=True)
        elif path.suffix == ".json":
            json.loads(path.read_text(encoding="utf-8"))
    users = root / "tenants/users"
    for tenant in sorted(users.iterdir()) if users.exists() else ():
        store, uploads = catalog(tenant, tenant.name)
        jobs = jobs_for(store, uploads)
        receipt = tenant / ".private-migration.json"
        if receipt.is_file():
            marker = json.loads(receipt.read_text(encoding="utf-8"))
            if (set(marker) != {"version", "owner_user_id", "source", "sha256"} or marker["version"] != 1
                    or marker["owner_user_id"] != tenant.name
                    or any(not re.fullmatch(HEX, marker[key]) for key in ("source", "sha256"))):
                raise ValueError("backup_migration_identity")
        originals = set()
        for key, upload in uploads.items():
            original = store.file(upload)
            if original is None or original.stat().st_size != upload.size_bytes or digest(original) != upload.sha256:
                raise ValueError("backup_pdf_sha256")
            originals.add(original)
            processing = UploadProcessingStore(store.processing, upload)
            batch = processing.batch_documents(upload.domain)
            job = jobs.get(key)
            if (batch and (batch[0].get("document_id") != processing.document_id
                           or batch[0].get("domain") != upload.domain
                           or batch[0].get("sha256") not in (None, upload.sha256)
                           or batch[0].get("sha256") is None and batch[0].get("status") not in ("pending", "failed"))
                    or job and job.processing.counts is not None and processing.checkpoint() is None):
                raise ValueError("backup_checkpoint_identity")
            for record in processing.results(upload.domain):
                if (record.document_id != processing.document_id or record.extraction_metadata is None
                        or record.extraction_metadata.pdf_sha256 != upload.sha256):
                    raise ValueError("backup_scientific_identity")
            reviews_count += review_count(processing.root / "reviews.sqlite3", upload, receipt.is_file())
        if store.files.is_dir() and set(store.files.iterdir()) != originals:
            raise ValueError("backup_uncatalogued_original")
        uploads_count += len(uploads)
        job_count += len(jobs)
    return {"users": len(owners), "uploads": uploads_count, "jobs": job_count, "review_events": reviews_count}, versions


def _copy_file(source, destination, *, auth=False):
    destination.parent.mkdir(mode=0o700, exist_ok=True, parents=True)
    if source.suffix == ".sqlite3":
        with database(source) as before, closing(sqlite3.connect(destination)) as after:
            before.backup(after)
            after.execute("PRAGMA journal_mode=DELETE")
            if auth:
                after.execute("PRAGMA secure_delete=ON")
                with after:
                    after.execute("DELETE FROM sessions")
    else:
        shutil.copyfile(source, destination)
    destination.chmod(0o600)


def create(name, *, source=STATE, destination=None):
    destination = destination or archive_path(name)
    with cold_lock(source.parent / "coord", exclusive=True), TemporaryDirectory(
            prefix=".spi-backup-", dir=destination.parent) as scratch:
        area = Path(scratch)
        tree = area / "snapshot"
        with database(source / AUTH_DB) as db:
            owners = {row[0] for row in db.execute("SELECT user_id FROM users")}
        _copy_file(source / AUTH_DB, tree / AUTH_DB, auth=True)
        for entry in tenant_files(source, owners, live=True):
            _copy_file(entry, tree / entry.relative_to(source))
        counts, versions = inspect(tree)
        manifest = {
            "backup_id": name, "created_at_utc": datetime.now(timezone.utc).isoformat(), "app_version": APP_VERSION,
            "schema_version": BACKUP_SCHEMA, "sqlite_user_versions": versions, "counts": counts,
            "configuration": {"mode": "multiuser", "consistency": "cold", "session_policy": "excluded"},
            "verification": "passed", "restore_policy": "empty dataset only, verify before publication",
            "files": {p.relative_to(tree).as_posix(): {"bytes": p.stat().st_size, "sha256": digest(p)}
                      for p in files(tree)},
        }
        temporary = area / "snapshot.tar"
        manifest_path = area / MANIFEST
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with tarfile.open(temporary, "w") as archive:
            archive.add(manifest_path, MANIFEST, recursive=False)
            for relative in manifest["files"]:
                archive.add(tree / relative, relative, recursive=False)
        temporary.chmod(0o600)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        with TemporaryDirectory(dir=area) as verify:
            read_archive(temporary, Path(verify))
        os.link(temporary, destination)
        sync_directory(destination.parent)
    return manifest


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_archive(source, target):
    maximum = _positive_limit("SPI_BACKUP_MAX_BYTES", 64 * 1024**3)
    if source.stat().st_size > maximum:
        raise ValueError("backup_size_limit")
    with tarfile.open(source, mode="r:") as archive:
        first = archive.next()
        if first is None or first.name != MANIFEST or not first.isfile() or first.size > MANIFEST_MAX_BYTES:
            raise ValueError("backup_manifest_missing")
        manifest = json.load(archive.extractfile(first))
        if (manifest.get("schema_version") != BACKUP_SCHEMA or manifest.get("app_version") != APP_VERSION
                or not re.fullmatch(NAME, manifest.get("backup_id", ""))
                or manifest.get("verification") != "passed"
                or manifest.get("configuration") != {"mode": "multiuser", "consistency": "cold", "session_policy": "excluded"}):
            raise ValueError("backup_schema_unsupported")
        expected = dict(manifest["files"])
        total = 0
        for member in iter(archive.next, None):
            relative = safe_member(member.name)
            entry = expected.pop(member.name, None)
            if (not member.isfile() or member.sparse or not isinstance(entry, dict) or member.size != entry["bytes"]
                    or not re.fullmatch(HEX, entry["sha256"])):
                raise ValueError("backup_unexpected_member")
            total += member.size
            if total > maximum:
                raise ValueError("backup_size_limit")
            path = target / relative
            path.parent.mkdir(mode=0o700, exist_ok=True, parents=True)
            with archive.extractfile(member) as src, path.open("xb") as dst:
                shutil.copyfileobj(src, dst, length=128 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            if digest(path) != entry["sha256"]:
                raise ValueError("backup_sha256_mismatch")
        if expected:
            raise ValueError("backup_member_missing")
    counts, versions = inspect(target)
    if manifest["counts"] != counts or manifest["sqlite_user_versions"] != versions:
        raise ValueError("backup_semantic_manifest_mismatch")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "verify"))
    parser.add_argument("name", nargs="?")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        name = args.name or f"spi-{uuid4().hex}"
        if args.action == "verify":
            with TemporaryDirectory(prefix=".spi-verify-", dir=BACKUPS) as temporary:
                report = read_archive(archive_path(name), Path(temporary))
        else:
            report = create(name)
            snapshot("backup", {**last_snapshot("backup"), "last_success_timestamp": time(), "last_result": "passed"})
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    except (OSError, RuntimeError, ValueError, tarfile.TarError, sqlite3.Error, TypeError, KeyError, AttributeError) as exc:
        old = last_snapshot("backup")
        snapshot("backup", {**old, "last_result": "failed", "failures": old.get("failures", 0) + 1})
        event("backup", "backup_failed", level="ERROR", error_type=type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
