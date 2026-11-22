"""Validate and copy one offline legacy upload Store to an explicit account."""

import argparse
import json
import os
import re
import shutil
import sqlite3
from contextlib import closing
from hashlib import file_digest, sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from .auth_config import AuthConfig, check_separate
from .auth_store import AuthStore
from .config import load_project_env
from .models import UPLOAD_ID_PATTERN, StoredUpload
from .review_models import ReviewEvent
from .tenant_storage import tenant_lease, tenant_root, tenant_store
from .upload_processing import UploadProcessingStore
from .upload_queue import UploadQueue
from .upload_reviews import source_result_hash
from .upload_store import UploadStore, private_path, private_sqlite_path, storage_lease

MARKER = ".private-migration.json"
TRANSIENT = {"maintenance.sqlite3", "write-lock.sqlite3", "batch-lock.sqlite3"}


def _database(path):
    return sqlite3.connect(path.as_uri() + "?mode=ro", timeout=0, uri=True)


def _integrity(db):
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("SQLite integrity check failed")


def _digest(path):
    with path.open("rb") as source:
        return file_digest(source, "sha256").hexdigest()


def _processing_files(root):
    for entry in sorted(root.iterdir()):
        relative = entry.relative_to(root)
        if entry.is_dir():
            private_path(root, relative, directory=True)
            yield from (relative / item for item in _processing_files(entry))
        else:
            private_path(root, relative)
            if entry.name == "reviews.sqlite3":
                yield relative
            elif entry.suffix == ".json":
                yield relative
            elif (entry.name not in TRANSIENT and not entry.name.startswith("tmp")
                  and not entry.name.startswith("reviews.sqlite3-")):
                raise ValueError("Unexpected file in legacy processing Store")


def _review_rows(path, upload):
    with closing(_database(private_sqlite_path(path.parent, path.name))) as db:
        _integrity(db)
        rows = db.execute("SELECT result_id, revision, event FROM review_events ORDER BY result_id, revision").fetchall()
        if db.execute("SELECT 1 FROM sqlite_schema WHERE name = 'review_actors'").fetchone():
            raise ValueError("Expected historical reviews from the legacy Store")
    latest = {}
    for result_id, revision, raw in rows:
        event = ReviewEvent.model_validate_json(raw)
        basis = event.decision
        if (event.revision != revision or event.previous_decision != latest.get(result_id)
                or (basis.upload_id, basis.domain, basis.pdf_sha256, basis.result_id) !=
                (upload.upload_id, upload.domain, upload.sha256, result_id)
                or basis.source_result_hash != source_result_hash(event.original_result)):
            raise ValueError("Legacy review history failed identity or revision checks")
        latest[result_id] = basis
    return rows


def _inventory(source):
    catalog = private_sqlite_path(source.state, Path("uploads.sqlite3"))
    queue_path = private_sqlite_path(source.processing, Path("queue.sqlite3"))
    with closing(_database(catalog)) as db:
        _integrity(db)
        rows = db.execute("SELECT upload_id, created_at, metadata FROM uploads ORDER BY upload_id").fetchall()
    uploads = {key: source._record(raw, key) for key, _, raw in rows}
    queue = UploadQueue(source.processing)
    jobs = {}
    if queue_path.is_file():
        with closing(_database(queue_path)) as db:
            _integrity(db)
            for upload_id, status, updated in db.execute("SELECT upload_id, status, updated_at FROM jobs"):
                job = queue._get(db, upload_id)
                if (upload_id not in uploads or not job.matches(uploads[upload_id])
                        or job.processing.status != status or job.processing.updated_at.isoformat() != updated
                        or not re.fullmatch(UPLOAD_ID_PATTERN, job.processing.job_id)):
                    raise ValueError("Legacy job identity mismatch")
                jobs[upload_id] = job

    expected_pdfs = {f"{upload_id}.pdf" for upload_id in uploads}
    if source.files.exists():
        for entry in source.files.iterdir():
            private_path(source.files, Path(entry.name))
            if entry.name not in expected_pdfs and not re.fullmatch(r"\.upload-[a-z0-9_]+\.part", entry.name):
                raise ValueError("Uncatalogued file in legacy PDF directory")
    files = {}
    reviews = 0
    for upload_id, upload in uploads.items():
        original = source.file(upload)
        if not original or original.stat().st_size != upload.size_bytes or _digest(original) != upload.sha256:
            raise ValueError("Legacy PDF size or SHA-256 mismatch")
        scientific = UploadProcessingStore(source.processing, upload)
        job = jobs.get(upload_id)
        expected_checkpoint = scientific.root / "batch" / upload.domain / f"{upload_id}.json"
        if (expected_checkpoint.exists() or (job and job.processing.counts is not None)) and not scientific.checkpoint():
            raise ValueError("Legacy checkpoint identity mismatch")
        if scientific.root.exists():
            scientific.upload_results()
            for relative in _processing_files(scientific.root):
                path = private_path(scientific.root, relative)
                if path.name == "reviews.sqlite3":
                    history = _review_rows(path, upload)
                    reviews += len(history)
                    fingerprint = sha256(json.dumps(history, ensure_ascii=False).encode()).hexdigest()
                else:
                    json.loads(path.read_text(encoding="utf-8"))
                    fingerprint = _digest(path)
                files[Path(upload_id) / relative] = fingerprint
    if source.processing.exists():
        for path in source.processing.iterdir():
            if path.name == "locks":
                private_path(source.processing, Path(path.name), directory=True)
            elif path.name.startswith("queue.sqlite3"):
                private_path(source.processing, Path(path.name))
            elif path.name not in uploads:
                raise ValueError("Unknown legacy processing namespace")

    inventory = {"uploads": {key: upload.model_dump(mode="json") for key, upload in uploads.items()},
                 "jobs": {key: job.model_dump(mode="json") for key, job in jobs.items()},
                 "processing": {path.as_posix(): value for path, value in files.items()}}
    fingerprint = sha256(json.dumps(inventory, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return rows, uploads, jobs, files, reviews, fingerprint


def _copy(source, target, rows, uploads, jobs, files, owner):
    target.files.mkdir(parents=True, exist_ok=True)
    with closing(target._connect()) as db, db:
        for upload_id, created, _ in rows:
            record = uploads[upload_id].model_copy(update={"owner_user_id": owner})
            path = target.files / f"{upload_id}.pdf"
            shutil.copyfile(source.file(uploads[upload_id]), path)
            if _digest(path) != record.sha256:
                raise ValueError("Copied PDF SHA-256 mismatch")
            db.execute("INSERT INTO uploads VALUES (?, ?, ?)", (upload_id, created, target.metadata(record)))
    for relative, expected in files.items():
        old = private_path(source.processing, relative)
        new = target.processing / relative
        new.parent.mkdir(parents=True, exist_ok=True)
        if old.name == "reviews.sqlite3":
            with closing(_database(old)) as before, closing(sqlite3.connect(new)) as after:
                before.backup(after)
            history = _review_rows(new, uploads[relative.parts[0]])
            copied = sha256(json.dumps(history, ensure_ascii=False).encode()).hexdigest()
        else:
            shutil.copyfile(old, new)
            copied = _digest(new)
        if copied != expected:
            raise ValueError("Copied scientific data or historical review checksum mismatch")
    queue = UploadQueue(target.processing, owner)
    with queue._connect() as db:
        for job in jobs.values():
            queue._save(db, job.model_copy(update={"owner_user_id": owner}))
    for upload in target.list():
        job = queue.get(upload.upload_id)
        if (job is None) != (upload.upload_id not in jobs) or (job and not job.matches(upload)):
            raise ValueError("Copied ownership verification failed")


def migrate(auth, source_root, owner, *, dry_run):
    config = auth.config
    check_separate(source_root, config.root, config.db)
    source_root = private_path(source_root, Path(), directory=True)
    source = UploadStore(
        private_path(source_root, Path("pdfs"), directory=True),
        private_path(source_root, Path("metadata"), directory=True),
        1, 1, private_path(source_root, Path("processing"), directory=True),
    )
    if not private_sqlite_path(source.state, Path("uploads.sqlite3")).is_file():
        raise ValueError("Legacy uploads.sqlite3 not found")
    user = auth.user(owner)
    if user is None or user.status != "active":
        raise ValueError("Expected an existing active owner")
    source_key = sha256(os.path.normcase(str(source_root.resolve())).encode()).hexdigest()
    target = tenant_root(config, owner)
    locks = private_path(config.root, Path("migration-lock"), directory=True)
    with storage_lease(source.state, exclusive=True), storage_lease(locks, exclusive=True), \
            tenant_lease(config, owner, exclusive=True):
        rows, uploads, jobs, files, reviews, fingerprint = _inventory(source)
        marker = {"version": 1, "owner_user_id": owner, "source": source_key, "sha256": fingerprint}
        report = {"status": "dry_run" if dry_run else "imported", "owner_user_id": owner,
                  "uploads": len(uploads), "jobs": len(jobs), "review_events": reviews,
                  "pdf_bytes": sum(upload.size_bytes for upload in uploads.values()), "sha256": fingerprint}
        if target.parent.is_dir():
            for namespace in target.parent.iterdir():
                other = tenant_root(config, namespace.name)
                receipt = private_path(other, Path(MARKER))
                if receipt.is_file():
                    previous = json.loads(receipt.read_text(encoding="utf-8"))
                    if previous == marker:
                        return {**report, "status": "already_imported"}
                    if previous["source"] == source_key or previous["sha256"] == fingerprint:
                        raise ValueError("Legacy Store already associated with another owner or snapshot")
                catalog = private_sqlite_path(other / "metadata", "uploads.sqlite3")
                if catalog.is_file():
                    with closing(_database(catalog)) as db:
                        if any(row[0] in uploads for row in db.execute("SELECT upload_id FROM uploads")):
                            raise ValueError("An upload ID already exists in the destination installation")
        if target.exists():
            raise ValueError("Target namespace already exists. Use an unused account.")
        if dry_run:
            return report
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = private_path(config.root, Path("migration-staging"), directory=True)
        staging.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=staging, prefix=f"{owner}-") as temporary:
            pending = Path(temporary) / "tenant"
            destination = tenant_store(config, owner, auth.quotas(owner), root=pending)
            _copy(source, destination, rows, uploads, jobs, files, owner)
            with (pending / MARKER).open("x", encoding="utf-8") as receipt:
                json.dump(marker, receipt)
                receipt.flush()
                os.fsync(receipt.fileno())
            with auth.connect(write=True) as db:
                current = auth._user(db, owner)
                if current is None or current.status != "active":
                    raise ValueError("Destination account is no longer active")
                os.rename(pending, target)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Legacy parent of pdfs, metadata and processing")
    parser.add_argument("--target", type=Path, required=True, help="Configured SPI_MULTIUSER_STORAGE_ROOT")
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    load_project_env()
    try:
        config = AuthConfig.from_env()
        if args.target != config.root:
            raise ValueError("Target must match SPI_MULTIUSER_STORAGE_ROOT")
        auth = AuthStore(config)
        print(json.dumps(migrate(auth, args.source, args.owner_user_id, dry_run=args.dry_run)))
    except sqlite3.Error:
        parser.exit(1, "SQLite unavailable or locked. Stop the source API and worker and retry. Check database integrity.\n")
    except (ValueError, OSError):
        parser.exit(1, "Migration refused. Check owner, existing namespaces, source layout and integrity.\n")


if __name__ == "__main__":
    main()
