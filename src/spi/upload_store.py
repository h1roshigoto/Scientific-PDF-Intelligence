"""Storage and technical PDF validation for private uploads."""

import json
import os
import re
import sqlite3
import unicodedata
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from secrets import token_hex
from stat import FILE_ATTRIBUTE_REPARSE_POINT, S_ISDIR, S_ISREG
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import BinaryIO

import pymupdf

from . import pdf_boundary
from .models import UPLOAD_ID_PATTERN, Domain, StoredUpload

DEFAULT_UPLOAD_MAX_BYTES = 25 * 1024 * 1024
COPY_CHUNK = 128 * 1024
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# The API shares MuPDF with server-side passport rendering.
mupdf_lock = Lock()


class UploadTooLarge(ValueError):
    pass


class InvalidPdf(ValueError):
    pass


class UploadQuotaFull(ValueError):
    pass


class UnsafePrivatePath(OSError):
    pass


def _path_aliases(path: Path, resolved: Path) -> set[Path]:
    return {Path(os.path.abspath(path)), resolved}


def _overlap(first: set[Path], second: set[Path]) -> bool:
    return any(left.is_relative_to(right) or right.is_relative_to(left)
               for left in first for right in second)


def upload_token() -> str:
    token = os.getenv("SPI_UPLOAD_TOKEN", "")
    if len(token) < 32 or not re.fullmatch(r"[A-Za-z0-9._~+/-]+={0,2}", token):
        raise ValueError("Invalid private upload token")
    return token


def private_path(root: Path, relative: Path, *, directory: bool = False) -> Path:
    """Reject links, junctions and hard-linked files within a private root."""
    if relative.anchor or ".." in relative.parts:
        raise ValueError("Expected a path relative to private storage")
    path = root / relative
    for entry in (*reversed(path.parents), path):
        try:
            info = entry.lstat()
        except FileNotFoundError:
            continue
        is_directory = directory or entry != path
        if (getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
                or (is_directory and not S_ISDIR(info.st_mode))
                or (not is_directory and (not S_ISREG(info.st_mode) or info.st_nlink != 1))):
            raise UnsafePrivatePath("Unsafe private storage path")
    return path


def private_sqlite_path(root: Path, name: Path | str) -> Path:
    name = Path(name)
    for suffix in ("-journal", "-wal", "-shm"):
        private_path(root, name.with_name(name.name + suffix))
    return private_path(root, name)


@contextmanager
def storage_lease(root: Path, *, exclusive: bool = False, create: bool = True):
    """Hold a crash-released shared lease, or require the Store to be offline."""
    if create:
        private_path(root, Path(), directory=True).mkdir(parents=True, exist_ok=True)
    path = private_sqlite_path(root, Path("maintenance.sqlite3"))
    # FastAPI can enter and exit a request dependency on different threads.
    # The connection is only used here, with no concurrent operations.
    with closing(sqlite3.connect(path, timeout=0 if exclusive else 15, check_same_thread=False)) as db:
        db.execute("BEGIN EXCLUSIVE" if exclusive else "BEGIN")
        db.execute("SELECT name FROM sqlite_schema").fetchall()
        yield


def _positive_limit(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if not 0 < value <= 2**53 - 1:
        raise ValueError("Upload limit must be a positive integer")
    return value


def _display_filename(filename: str) -> str:
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    clean = "".join(char for char in basename if unicodedata.category(char) not in {"Cc", "Cf"}).strip()
    return clean[:255] if clean and clean not in {".", ".."} else "document.pdf"


def _pdf_pages(path: Path) -> int:
    if pdf_boundary.enabled():
        result = pdf_boundary.request("pages", path)
        if result["status"] in {"invalid", "encrypted"}:
            raise InvalidPdf("encrypted_pdf" if result["status"] == "encrypted" else "invalid_pdf")
        count = result.get("pages")
        if result["status"] != "ok" or type(count) is not int or not 1 <= count <= pdf_boundary.MAX_PAGES:
            raise pdf_boundary.ParserUnavailable("pdf_parser_protocol")
        return count
    try:
        with mupdf_lock, pymupdf.open(path, filetype="pdf") as pdf:
            if not pdf.is_pdf:
                raise InvalidPdf("invalid_pdf")
            if pdf.needs_pass or pdf.xref_get_key(-1, "Encrypt")[0] != "null":
                raise InvalidPdf("encrypted_pdf")
            if pdf.is_repaired or not pdf.page_count:
                raise InvalidPdf("invalid_pdf")
            return pdf.page_count
    except (pymupdf.FileDataError, RuntimeError):
        # A failed MuPDF open can hold a file handle in the exception traceback on Windows.
        pass
    raise InvalidPdf("invalid_pdf")


@dataclass(frozen=True)
class UploadStore:
    files: Path
    state: Path
    max_bytes: int
    max_files: int
    processing: Path | None = None
    owner_user_id: str | None = None
    max_total_bytes: int | None = None

    @classmethod
    def from_env(cls) -> "UploadStore":
        files_value = os.getenv("SPI_UPLOAD_DIR", "").strip()
        state_value = os.getenv("SPI_UPLOAD_STATE_DIR", "").strip()
        if not files_value or not state_value:
            raise ValueError("Both upload paths must be configured")
        process_value = os.getenv("SPI_UPLOAD_PROCESS_STATE_DIR", "").strip()
        paths = [Path(value) for value in (files_value, state_value, process_value) if value]
        resolved = [path.resolve() for path in paths]
        aliases = [_path_aliases(path, canonical) for path, canonical in zip(paths, resolved)]
        protected = {PROJECT_ROOT, Path(os.getenv("SPI_STATE_DIR", "state"))}
        cwd = Path.cwd()
        checkout = next((parent for parent in (cwd, *cwd.parents) if (parent / ".git").exists()), None)
        if checkout is not None:
            protected.add(checkout)
        for root in {PROJECT_ROOT, checkout} - {None}:
            protected.update(root / name for name in ("src", "tests", "outputs", "data", "frontend_redezigned", "state"))
        protected_aliases = set().union(*(_path_aliases(path, path.resolve()) for path in protected))
        if any(_overlap(current, protected_aliases) or any(_overlap(current, other) for other in aliases[:index])
               for index, current in enumerate(aliases)):
            raise ValueError("Upload paths must be separate from each other and protected paths")
        for path in resolved:
            private_path(path, Path(), directory=True)
        for path in paths:
            private_path(path.absolute(), Path(), directory=True)
        return cls(*resolved[:2], _positive_limit("SPI_UPLOAD_MAX_BYTES", DEFAULT_UPLOAD_MAX_BYTES),
                   _positive_limit("SPI_UPLOAD_MAX_FILES", 100), resolved[2] if process_value else None)

    def _connect(self) -> sqlite3.Connection:
        path = private_sqlite_path(self.state, Path("uploads.sqlite3"))
        self.state.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=15)
        try:
            connection.execute("""CREATE TABLE IF NOT EXISTS uploads (
                upload_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, metadata TEXT NOT NULL
            )""")
        except BaseException:
            connection.close()
            raise
        return connection

    def _record(self, metadata, upload_id):
        upload = StoredUpload.model_validate_json(metadata)
        if upload.upload_id != upload_id or upload.owner_user_id != self.owner_user_id:
            raise ValueError("Upload catalog identity mismatch")
        return upload

    @staticmethod
    def metadata(upload):
        data = upload.model_dump(mode="json")
        if upload.owner_user_id is not None:
            data["owner_user_id"] = upload.owner_user_id
        return json.dumps(data, ensure_ascii=False)

    def list(self) -> list[StoredUpload]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT upload_id, metadata FROM uploads ORDER BY created_at DESC, upload_id DESC")
            return [self._record(metadata, upload_id) for upload_id, metadata in rows]

    def usage(self) -> tuple[int, int]:
        with closing(self._connect()) as db:
            return self._usage(db)

    @staticmethod
    def _usage(db):
        return db.execute("""SELECT count(*), coalesce(sum(json_extract(metadata, '$.size_bytes')), 0)
                          FROM uploads""").fetchone()

    def get(self, upload_id: str) -> StoredUpload | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT metadata FROM uploads WHERE upload_id = ?", (upload_id,)).fetchone()
            if not row:
                return None
            return self._record(row[0], upload_id)

    def file(self, upload: StoredUpload) -> Path | None:
        if not re.fullmatch(UPLOAD_ID_PATTERN, upload.upload_id) or upload.owner_user_id != self.owner_user_id:
            raise ValueError("Invalid upload ID")
        try:
            path = private_path(self.files, Path(f"{upload.upload_id}.pdf"))
        except UnsafePrivatePath:
            return None
        return path if path.is_file() else None

    def save(self, source: BinaryIO, filename: str, domain: Domain) -> StoredUpload:
        private_path(self.files, Path(), directory=True).mkdir(parents=True, exist_ok=True)
        with ExitStack() as resources:
            return self._save(resources, source, filename, domain)

    def _save(self, resources, source, filename, domain):
        db = None
        temporary = None
        published = None
        try:
            used = 0
            if self.owner_user_id is not None:
                db = resources.enter_context(closing(self._connect()))
                db.execute("BEGIN IMMEDIATE")
                count, used = self._usage(db)
                self._remove_interrupted_uploads(db)
                if count >= self.max_files:
                    raise UploadQuotaFull
            with NamedTemporaryFile(dir=self.files, prefix=".upload-", suffix=".part", delete=False) as target:
                temporary = Path(target.name)
                digest = sha256()
                size = 0
                while chunk := source.read(min(COPY_CHUNK, self.max_bytes - size + 1)):
                    if size + len(chunk) > self.max_bytes:
                        raise UploadTooLarge
                    if size == 0 and not chunk.startswith(b"%PDF-"):
                        raise InvalidPdf("missing_pdf_signature")
                    size += len(chunk)
                    digest.update(chunk)
                    target.write(chunk)
                if size == 0:
                    raise InvalidPdf("empty_pdf")
                if self.max_total_bytes is not None and used + size > self.max_total_bytes:
                    raise UploadQuotaFull
                target.flush()
                os.fsync(target.fileno())

            pages = _pdf_pages(temporary)
            if db is None:
                resources.enter_context(storage_lease(self.state))
                db = resources.enter_context(closing(self._connect()))
                db.execute("BEGIN IMMEDIATE")
                if self._usage(db)[0] >= self.max_files:
                    raise UploadQuotaFull
            upload_id = token_hex(16)
            destination = self.files / f"{upload_id}.pdf"
            if destination.exists() or destination.is_symlink():
                raise OSError("Upload ID collision")
            upload = StoredUpload(
                upload_id=upload_id, owner_user_id=self.owner_user_id, domain=domain,
                original_filename=_display_filename(filename), size_bytes=size, sha256=digest.hexdigest(),
                page_count=pages, created_at=datetime.now(timezone.utc),
            )
            db.execute("INSERT INTO uploads VALUES (?, ?, ?)",
                       (upload_id, upload.created_at.isoformat(timespec="microseconds"), self.metadata(upload)))
            os.replace(temporary, destination)
            published = destination
            db.commit()
            return upload
        except BaseException:
            if published is not None:
                published.unlink(missing_ok=True)
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if db is not None:
                db.rollback()
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _remove_interrupted_uploads(self, db):
        # No other uploader can create a staging file before taking this write lock.
        ids = {row[0] for row in db.execute("SELECT upload_id FROM uploads")}
        for path in self.files.iterdir():
            if (re.fullmatch(r"\.upload-[a-z0-9_]+\.part", path.name)
                    or (path.suffix == ".pdf" and re.fullmatch(UPLOAD_ID_PATTERN, path.stem)
                        and path.stem not in ids)):
                private_path(self.files, Path(path.name)).unlink()
