"""Storage and technical PDF validation for private uploads."""

import os
import re
import sqlite3
import unicodedata
from contextlib import closing
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
    if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{32,}", token):
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


def private_sqlite_path(root: Path, name: Path) -> Path:
    for suffix in ("-journal", "-wal", "-shm"):
        private_path(root, name.with_name(name.name + suffix))
    return private_path(root, name)


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

    def list(self) -> list[StoredUpload]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT metadata FROM uploads ORDER BY created_at DESC, upload_id DESC")
            return [StoredUpload.model_validate_json(row[0]) for row in rows]

    def get(self, upload_id: str) -> StoredUpload | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT metadata FROM uploads WHERE upload_id = ?", (upload_id,)).fetchone()
            if not row:
                return None
            upload = StoredUpload.model_validate_json(row[0])
            if upload.upload_id != upload_id:
                raise ValueError("Upload catalog ID mismatch")
            return upload

    def file(self, upload: StoredUpload) -> Path | None:
        if not re.fullmatch(UPLOAD_ID_PATTERN, upload.upload_id):
            raise ValueError("Invalid upload ID")
        try:
            path = private_path(self.files, Path(f"{upload.upload_id}.pdf"))
        except UnsafePrivatePath:
            return None
        return path if path.is_file() else None

    def save(self, source: BinaryIO, filename: str, domain: Domain) -> StoredUpload:
        private_path(self.files, Path(), directory=True).mkdir(parents=True, exist_ok=True)
        temporary = None
        published = None
        try:
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
                target.flush()
                os.fsync(target.fileno())

            pages = _pdf_pages(temporary)
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT count(*) FROM uploads").fetchone()[0] >= self.max_files:
                    raise UploadQuotaFull
                upload_id = token_hex(16)
                destination = self.files / f"{upload_id}.pdf"
                if destination.exists() or destination.is_symlink():
                    raise OSError("Upload ID collision")
                upload = StoredUpload(
                    upload_id=upload_id, domain=domain, original_filename=_display_filename(filename),
                    size_bytes=size, sha256=digest.hexdigest(), page_count=pages,
                    created_at=datetime.now(timezone.utc),
                )
                db.execute("INSERT INTO uploads VALUES (?, ?, ?)",
                           (upload_id, upload.created_at.isoformat(timespec="microseconds"), upload.model_dump_json()))
                os.replace(temporary, destination)
                published = destination
            return upload
        except BaseException:
            if published is not None:
                published.unlink(missing_ok=True)
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
