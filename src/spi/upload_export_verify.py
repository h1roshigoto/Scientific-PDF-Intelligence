"""Offline integrity check for a private ZIP: python -m spi.upload_export_verify <archive.zip>."""

import argparse
import os
import stat
from hashlib import file_digest
from pathlib import Path
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile
from zlib import error as ZlibError

from .upload_export_models import (
    DEFAULT_EXPORT_BYTES, MANIFEST, MEMBERS, ROOT, ExportManifest, IssuesDocument, ReviewedResultsDocument,
    ReviewHistoryDocument,
)

JSON_MODELS = {
    "reviewed_results.json": ReviewedResultsDocument,
    "issues.json": IssuesDocument,
    "review_history.json": ReviewHistoryDocument,
}
MAX_MANIFEST_BYTES = 1024 * 1024


class VerificationError(ValueError):
    pass


def _entries(archive, maximum):
    entries = archive.infolist()
    expected = {ROOT + name for name in (*MEMBERS, MANIFEST)}
    if len(entries) != len(expected) or {entry.filename for entry in entries} != expected:
        raise VerificationError("Unexpected, duplicate or missing ZIP members")
    if sum(entry.file_size for entry in entries) > maximum:
        raise VerificationError("Expanded contents exceed the byte limit")
    for entry in entries:
        if (entry.orig_filename != entry.filename or entry.is_dir() or entry.external_attr & 0x10
                or stat.S_IFMT(entry.external_attr >> 16) not in {0, stat.S_IFREG}
                or entry.flag_bits & 1 or entry.compress_type not in {ZIP_DEFLATED, ZIP_STORED}):
            raise VerificationError("Unsupported ZIP member type")
    return {entry.filename: entry for entry in entries}


def _manifest(archive, entry):
    if entry.file_size > MAX_MANIFEST_BYTES:
        raise VerificationError("Manifest exceeds the byte limit")
    try:
        manifest = ExportManifest.model_validate_json(archive.read(entry))
    except ValueError as exc:
        raise VerificationError("Invalid manifest schema or version") from exc
    for model in (manifest, manifest.metadata, manifest.validation):
        if set(type(model).model_fields) != model.model_fields_set:
            raise VerificationError("Missing manifest fields")
    return manifest


def verify(path: Path, *, max_bytes: int = DEFAULT_EXPORT_BYTES) -> ExportManifest:
    if max_bytes <= 0 or path.stat().st_size > max_bytes:
        raise VerificationError("Archive exceeds the byte limit")
    with ZipFile(path) as archive:
        entries = _entries(archive, max_bytes)
        manifest = _manifest(archive, entries[ROOT + MANIFEST])
        for record in manifest.files:
            entry = entries[ROOT + record.name]
            if record.size_bytes != entry.file_size:
                raise VerificationError(f"Byte size mismatch: {record.name}")
            with archive.open(entry) as source:
                if file_digest(source, "sha256").hexdigest() != record.sha256:
                    raise VerificationError(f"SHA-256 mismatch: {record.name}")
            if record.name in JSON_MODELS:
                try:
                    document = JSON_MODELS[record.name].model_validate_json(archive.read(entry))
                except ValueError as exc:
                    raise VerificationError(f"Invalid JSON contract: {record.name}") from exc
                if ("format" not in document.model_fields_set
                        or document.snapshot_sha256 != manifest.metadata.snapshot_sha256):
                    raise VerificationError(f"Version or snapshot mismatch: {record.name}")
        return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    try:
        maximum = int(os.getenv("SPI_UPLOAD_EXPORT_MAX_BYTES", str(DEFAULT_EXPORT_BYTES)))
        manifest = verify(args.archive, max_bytes=maximum)
    except VerificationError as exc:
        print(f"FAIL: {exc}")
        return 1
    except (OSError, BadZipFile, ValueError, RuntimeError, EOFError, ZlibError) as exc:
        print(f"FAIL: Could not read the private export ({type(exc).__name__})")
        return 1
    print(f"PASS: {len(manifest.files)} files verified by SHA-256 ({manifest.metadata.format})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
