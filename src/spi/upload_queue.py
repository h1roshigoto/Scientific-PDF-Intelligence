"""Durable upload jobs and a crash-released, cross-process lock per document."""

import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_hex

from pydantic import BaseModel, Field

from .models import UPLOAD_ID_PATTERN, Domain, StoredUpload, UploadProcessing
from .upload_store import private_path, private_sqlite_path


class UploadJob(BaseModel):
    domain: Domain
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    processing: UploadProcessing
    model_requests: int = Field(default=0, ge=0)

    def matches(self, upload: StoredUpload) -> bool:
        return (self.processing.upload_id, self.domain, self.sha256) == (
            upload.upload_id, upload.domain, upload.sha256,
        )


class UploadQueue:
    def __init__(self, root: Path):
        self.root = root

    @contextmanager
    def _connect(self):
        path = private_sqlite_path(self.root, Path("queue.sqlite3"))
        self.root.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=15)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                upload_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                updated_at TEXT NOT NULL, metadata TEXT NOT NULL
            )""")
            yield db

    @staticmethod
    def _get(db, upload_id: str) -> UploadJob | None:
        row = db.execute("SELECT metadata FROM jobs WHERE upload_id = ?", (upload_id,)).fetchone()
        if not row:
            return None
        job = UploadJob.model_validate_json(row[0])
        if job.processing.upload_id != upload_id or job.processing.job_id is None:
            raise ValueError("Invalid upload job identity")
        return job

    @staticmethod
    def _save(db, job: UploadJob):
        status = job.processing
        db.execute("""INSERT INTO jobs VALUES (?, ?, ?, ?)
            ON CONFLICT(upload_id) DO UPDATE SET status = excluded.status,
            updated_at = excluded.updated_at, metadata = excluded.metadata""",
                   (status.upload_id, status.status, status.updated_at.isoformat(), job.model_dump_json()))

    def get(self, upload_id: str) -> UploadJob | None:
        with self._connect() as db:
            return self._get(db, upload_id)

    @contextmanager
    def review_snapshot(self, upload_id: str, *, write: bool = False):
        # Keep the job stable until the review transaction commits. The lock
        # order is always queue -> review database. Reviews never take a worker lock.
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield self._get(db, upload_id)

    def enqueue(self, upload: StoredUpload) -> UploadJob:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._get(db, upload.upload_id)
            if current and not current.processing.retry_allowed:
                return current
            job = UploadJob(domain=upload.domain, sha256=upload.sha256, processing=UploadProcessing(
                upload_id=upload.upload_id, job_id=token_hex(16), status="queued", step="queued",
                updated_at=datetime.now(timezone.utc),
            ))
            self._save(db, job)
            return job

    @contextmanager
    def _worker_lock(self, upload_id: str):
        if not re.fullmatch(UPLOAD_ID_PATTERN, upload_id):
            raise ValueError("Invalid queued upload ID")
        locks = private_path(self.root, Path("locks"), directory=True)
        locks.mkdir(exist_ok=True)
        path = private_sqlite_path(locks, Path(f"{upload_id}.sqlite3"))
        # The connection owns the write reservation for the entire job. SQLite and
        # the OS release it on process exit, even after a hard kill.
        with closing(sqlite3.connect(path, timeout=0)) as lock:
            try:
                lock.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode != sqlite3.SQLITE_BUSY:
                    raise
                yield False
            else:
                yield True

    @contextmanager
    def claim(self):
        with self._connect() as db:
            candidates = db.execute("""SELECT upload_id FROM jobs
                WHERE status IN ('queued', 'processing') ORDER BY updated_at, upload_id""").fetchall()
        for (upload_id,) in candidates:
            with self._worker_lock(upload_id) as acquired:
                if not acquired:
                    continue
                with self._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    job = self._get(db, upload_id)
                    if job is None or job.processing.status not in ("queued", "processing"):
                        continue
                    recovering = job.processing.status == "processing"
                    job.processing = UploadProcessing(
                        upload_id=upload_id, job_id=job.processing.job_id, status="processing",
                        step="validating_input", updated_at=datetime.now(timezone.utc),
                        diagnostic="worker_recovered" if recovering else None,
                        mode=job.processing.mode,
                        recoveries=job.processing.recoveries + int(recovering),
                    )
                    self._save(db, job)
                yield job
                return
        yield None

    def update(self, job: UploadJob, *, reserve_model_request: bool = False, **changes) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._get(db, job.processing.upload_id)
            if (current is None or current.processing.job_id != job.processing.job_id
                    or current.processing.status != "processing"):
                raise RuntimeError("Upload job is no longer owned by this worker")
            if reserve_model_request:
                job.model_requests += 1
            job.processing = UploadProcessing.model_validate({
                **job.processing.model_dump(), **changes, "updated_at": datetime.now(timezone.utc),
            })
            self._save(db, job)
