"""Filesystem and SQLite primitives used at the deployment boundary."""

import errno
import json
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from spi import __version__
from spi.upload_store import private_path, private_sqlite_path

APP_VERSION = __version__
BACKUP_SCHEMA = 1


def disk_full(exc):
    return (getattr(exc, "errno", None) in (errno.ENOSPC, errno.EDQUOT)
            or getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL)


def files(root, relative=Path()):
    directory = private_path(root, relative, directory=True)
    for entry in sorted(directory.iterdir()):
        key = relative / entry.name
        if entry.is_dir():
            yield from files(root, key)
        else:
            yield private_path(root, key)


def database(path, *, timeout=2, writable=False):
    path = private_sqlite_path(path.parent, path.name)
    mode = "rw" if writable else "ro"
    return closing(sqlite3.connect(path.absolute().as_uri() + f"?mode={mode}", timeout=timeout, uri=True))


def integrity(db, *, full=False):
    pragma = "integrity_check" if full else "quick_check"
    if db.execute(f"PRAGMA {pragma}").fetchone() != ("ok",):
        raise ValueError("sqlite_integrity_failed")
    if db.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("sqlite_foreign_keys_invalid")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version != 0:
        raise ValueError("sqlite_schema_unsupported")
    return version


def write_json(path, value):
    with NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def cold_lock(coord, *, exclusive, create=False):
    import fcntl

    path = private_path(coord, Path("writers.lock"))
    fd = os.open(path, (os.O_CREAT | os.O_RDONLY if create else os.O_RDONLY) | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        yield fd
    finally:
        os.close(fd)
