"""Verify and restore a cold backup to a clean dataset."""

import argparse
import json
import os
import sqlite3
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time

from spi.upload_store import private_path

from .backup import archive_path, read_archive, sync_directory
from .runtime import STATE
from .state import cold_lock
from .telemetry import event, snapshot


def restore(source, destination=STATE, *, dry_run=False):
    root = destination.parent
    coord = private_path(root, Path("coord"), directory=True)
    coord.mkdir(mode=0o700, exist_ok=True)
    with cold_lock(coord, exclusive=True, create=True), TemporaryDirectory(prefix=".spi-restore-", dir=root) as work:
        private_path(root, Path(destination.name), directory=True)
        if destination.exists() and not dry_run:
            raise FileExistsError("restore_target_exists")
        stage = Path(work)
        manifest = read_archive(source, stage)
        if not dry_run:
            (stage / "tenants").mkdir(mode=0o700, exist_ok=True)
            for directory, _, _ in os.walk(stage, topdown=False):
                sync_directory(directory)
            os.rename(stage, destination)
            sync_directory(root)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        manifest = restore(archive_path(args.name), dry_run=args.dry_run)
        snapshot("restore", {"last_result": "verified" if args.dry_run else "restored", "timestamp": time()})
        print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    except (OSError, RuntimeError, ValueError, tarfile.TarError, sqlite3.Error, TypeError, KeyError, AttributeError) as exc:
        snapshot("restore", {"last_result": "failed", "timestamp": time()})
        event("restore", "restore_failed", level="ERROR", error_type=type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
