"""Construct tenant Stores exclusively from server-issued user IDs."""

import re
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from .auth_models import USER_ID
from .upload_queue import UploadQueue
from .upload_store import UploadStore, private_path, private_sqlite_path, storage_lease

DAY = 86400


def tenant_root(config, user_id):
    if not re.fullmatch(USER_ID, user_id):
        raise ValueError("Invalid account ID")
    return private_path(config.root, Path("users", user_id), directory=True)


@contextmanager
def tenant_lease(config, user_id, *, exclusive=False):
    tenant_root(config, user_id)
    locks = private_path(config.root, Path("maintenance", user_id), directory=True)
    with storage_lease(locks, exclusive=exclusive):
        yield


def tenant_store(config, user_id, quota, *, root=None):
    root = tenant_root(config, user_id) if root is None else root
    return UploadStore(
        private_path(root, Path("uploads"), directory=True),
        private_path(root, Path("metadata"), directory=True),
        quota.max_pdf_bytes, quota.max_upload_files,
        private_path(root, Path("processing"), directory=True),
        owner_user_id=user_id, max_total_bytes=quota.max_total_storage_bytes,
    )


def tenant_queue(config, store, quota):
    return UploadQueue(store.processing, store.owner_user_id, quota=quota, job_budget=config.job_requests)


@contextmanager
def worker_slot(root, limit):
    """Claim one process-owned slot, released by the OS even on hard kill."""
    private_path(root, Path(), directory=True).mkdir(parents=True, exist_ok=True)
    for index in range(limit):
        path = private_sqlite_path(root, Path(f"{index}.sqlite3"))
        with closing(sqlite3.connect(path, timeout=0)) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode != sqlite3.SQLITE_BUSY:
                    raise
            else:
                yield True
                return
    yield False
