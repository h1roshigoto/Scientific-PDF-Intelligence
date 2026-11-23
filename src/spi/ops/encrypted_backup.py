"""Create an encrypted restic recovery point from a verified cold backup."""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from .backup import read_archive
from .release import read_json, sha256_file
from .state import write_json

TAG = "spi-cold-v1"
RESTIC_ID = re.compile(r"[0-9a-f]{64}")
ARCHIVE = "snapshot.tar"


class Repository:
    def __init__(self, location, password, executable="restic"):
        self.path = location.resolve()
        if password.is_symlink() or not password.is_file() or not password.stat().st_size:
            raise ValueError("A nonempty password file is required")
        if os.name == "posix" and (stat.S_IMODE(password.stat().st_mode) & 0o077):
            raise ValueError("Password file must have mode 0600 or stricter")
        self.password = password.resolve()
        if self.password.is_relative_to(self.path):
            raise ValueError("Password file must be outside the restic repository")
        self.args = [str(executable), "--no-cache", "-r", str(self.path), "--password-file", str(self.password)]
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("RESTIC_")}

    def run(self, *arguments, **kwargs):
        result = subprocess.run([*self.args, *arguments], env=self.env, check=True,
                                capture_output=True, **kwargs)
        return result.stdout

    def initialize(self):
        self.run("init", "--repository-version", "2")

    def verify(self):
        self.run("check", "--read-data")

    def store(self, archive, scratch):
        if self.path.is_relative_to(archive.parent.resolve()) or self.password.is_relative_to(archive.parent.resolve()):
            raise ValueError("Keep the repository and password outside the plaintext backup directory")
        with TemporaryDirectory(prefix="spi-restic-verify-", dir=scratch) as staging:
            local = Path(staging) / ARCHIVE
            shutil.copyfile(archive, local)
            verified = Path(staging) / "verified"
            verified.mkdir()
            read_archive(local, verified)
            with local.open("rb") as source:
                output = self.run("backup", "--stdin", "--stdin-filename", ARCHIVE, "--host", "spi-cold",
                                  "--tag", TAG, "--json", stdin=source)
        summary, = (row for row in map(json.loads, output.splitlines()) if row["message_type"] == "summary")
        return summary["snapshot_id"]

    def restore(self, snapshot, output, scratch):
        if not RESTIC_ID.fullmatch(snapshot) or output.exists() or output.is_symlink():
            raise ValueError("Restore needs a full snapshot ID and unused destination")
        with TemporaryDirectory(prefix=".spi-restic-", dir=output.parent) as temporary:
            area = Path(temporary)
            self.run("restore", snapshot, "--target", str(area), "--verify")
            archive = area / ARCHIVE
            with TemporaryDirectory(prefix="spi-restic-verify-", dir=scratch) as staging:
                read_archive(archive, Path(staging))
            digest = sha256_file(archive)
            os.link(archive, output)  # Atomically create a new file without clobbering an older backup.
        return digest

    def retention(self, daily, weekly, monthly, last_verified):
        if not RESTIC_ID.fullmatch(last_verified) or min(daily, weekly, monthly) < 1:
            raise ValueError("Retention needs positive counts and a verified snapshot ID")
        self.verify()
        output = self.run("forget", "--dry-run", "--json", "--tag", TAG, "--group-by", "host,paths,tags",
                          "--keep-daily", str(daily), "--keep-weekly", str(weekly), "--keep-monthly", str(monthly),
                          "--keep-last", "2")
        groups = json.loads(output)
        keep = {item["id"] for group in groups for item in (group.get("keep") or [])}
        if len(keep) < 2 or last_verified not in keep:
            raise ValueError("Retention would leave insufficient recovery points or drop the last verified snapshot")
        return {"status": "dry-run", "keep": sorted(keep),
                "proposed_removals": sum(len(group.get("remove") or []) for group in groups)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path, help="Local restic repository outside the dataset")
    parser.add_argument("--password-file", required=True, type=Path)
    parser.add_argument("--scratch", type=Path)
    parser.add_argument("--report", type=Path, help="Private atomic operations report")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("init")
    store = sub.add_parser("backup")
    store.add_argument("archive", type=Path)
    sub.add_parser("verify")
    restore = sub.add_parser("restore")
    restore.add_argument("snapshot")
    restore.add_argument("archive", type=Path)
    retention = sub.add_parser("retention")
    retention.add_argument("--daily", type=int, default=7)
    retention.add_argument("--weekly", type=int, default=4)
    retention.add_argument("--monthly", type=int, default=12)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        repo = Repository(args.repo, args.password_file)
        # Keep operational history with its repository, independent of the cold dataset.
        report = read_json(args.report) if args.report and args.report.exists() else {"encrypted": True}
        timestamp = datetime.now(UTC).isoformat()
        if args.action == "init":
            repo.initialize()
        elif args.action == "backup":
            report["last_backup"] = {"at": timestamp, "snapshot": repo.store(args.archive, args.scratch),
                                     "size_bytes": args.archive.stat().st_size}
        elif args.action == "verify":
            repo.verify()
            report["last_verify"] = timestamp
        elif args.action == "restore":
            report["last_restore"] = {"at": timestamp, "snapshot": args.snapshot,
                                      "archive_sha256": repo.restore(args.snapshot, args.archive, args.scratch)}
        else:
            report["retention"] = repo.retention(
                args.daily, args.weekly, args.monthly, report.get("last_restore", {}).get("snapshot", ""))
        if args.report:
            write_json(args.report, report)
        print(json.dumps(report, sort_keys=True))
    except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        parser.exit(1, f"Encrypted backup operation failed ({type(exc).__name__})\n")


if __name__ == "__main__":
    main()
