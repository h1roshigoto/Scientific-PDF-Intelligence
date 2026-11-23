"""Evaluate release-matched evidence from an actual public deployment."""

import argparse
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .release import (DIGEST, IMAGES, RELEASE_TESTS, REVISION, checked_time, exception_map, oci_subject,
                      https_reference, read_json, scan_result, sha256_file)

CONTROLS = (
    "production_images", "non_root", "resource_limits", "trusted_proxy", "public_tls", "cookies_csrf",
    "tenant_isolation", "pdf_isolation", "persistent_storage", "backup_restore", "monitoring",
    "disk_limits", "security_scans", "security_high", "release_tests", "production_e2e",
)
MACHINE_CONTROLS = {"security_scans", "security_high", "release_tests"}
OPERATOR_OUTCOMES = {"verified": "PASS", "failed": "FAIL", "blocked": "BLOCKED"}
CONTEXT_KEYS = {"environment", "environment_id", "public_origin", "hostname", "revision", "image_digests"}
DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
TEST_DOMAINS = (".localhost", ".local", ".invalid", ".test", ".example",
                ".example.org", ".example.com", ".example.net")


def identity(value, *, bundle=False):
    expected = CONTEXT_KEYS | ({"schema_version", "controls"} if bundle else set())
    if set(value) != expected or value["environment"] != "public-production":
        raise ValueError("Invalid production evidence fields or environment")
    domain = value["hostname"]
    if (len(domain) > 253 or not DOMAIN.fullmatch(domain) or domain.endswith(TEST_DOMAINS)
            or domain in {"example.com", "example.net", "example.org"}
            or value["public_origin"] != f"https://{domain}"):
        raise ValueError("Evidence must identify one public HTTPS origin")
    if (not REVISION.fullmatch(value["revision"])
            or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{2,127}", value["environment_id"])):
        raise ValueError("Invalid release or environment identity")
    images = value["image_digests"]
    if set(images) != set(IMAGES) or any(not DIGEST.fullmatch(images[name]) for name in IMAGES):
        raise ValueError("Expected runtime and proxy OCI digests")
    return {key: value[key] for key in CONTEXT_KEYS}


def primary_file(root, filename, digest):
    if (not filename or Path(filename).name != filename or "/" in filename or "\\" in filename
            or not DIGEST.fullmatch(digest)):
        raise ValueError("Invalid primary evidence filename or SHA-256")
    path = root / filename
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0 or sha256_file(path) != digest:
        raise ValueError(f"Invalid or missing primary evidence: {filename}")
    return path


def freshness(value, now, limit):
    timestamp = checked_time(value)
    if not now - limit <= timestamp <= now + timedelta(minutes=5):
        raise ValueError("Stale or future evidence")
    return timestamp


def operator_control(control, record, root, now):
    fields = {"outcome", "operator", "checked_at", "reference", "artifact", "sha256"}
    if control == "backup_restore":
        fields.add("restore_environment_id")
    if set(record) != fields or record["outcome"] not in OPERATOR_OUTCOMES:
        raise ValueError(f"Invalid operator evidence for {control}")
    if not record["operator"].strip():
        raise ValueError(f"Missing operator for {control}")
    https_reference(record["reference"])
    freshness(record["checked_at"], now, timedelta(days=7))
    primary_file(root, record["artifact"], record["sha256"])
    return {**record, "status": OPERATOR_OUTCOMES[record["outcome"]], "verification": "operator-attested"}


def scan_controls(context, directory, exception_file, now):
    release = read_json(directory / "release-manifest.json")
    if (release["revision"] != context["revision"] or release["schema_version"] != 1
            or release["repository"] == "local" or not str(release["workflow_run"]).isdigit()
            or release["exceptions_sha256"] != sha256_file(exception_file)):
        raise ValueError("Scan source does not identify the expected release and workflow")
    freshness(release["generated_at"], now, timedelta(days=3))
    exceptions = exception_map(read_json(exception_file), now.date())
    scans = []
    for name in IMAGES:
        record = release["images"][name]
        archive = primary_file(directory, f"{name}.tar", record["archive_sha256"])
        subject = oci_subject(archive, context["revision"])
        if (subject["oci_digest"] != context["image_digests"][name]
                or any(record[key] != value for key, value in subject.items())):
            raise ValueError("Scan OCI digest does not match the deployment")
        scan_path = primary_file(directory, f"{name}.json", record["scan"]["sha256"])
        result = scan_result(name, read_json(scan_path), subject, exceptions, public=True)
        freshness(result["scan_at"], now, timedelta(days=3))
        scans.append(result)
    reference = https_reference(f"https://github.com/{release['repository']}/actions/runs/{release['workflow_run']}")
    metadata = {"verification": "machine-verified", "reference": reference,
                "report_ids": [result["report_id"] for result in scans]}
    tests = release["tests"]
    tests_pass = set(tests) == RELEASE_TESTS and all(
        test["result"] == "success" and test["image_digests"] == context["image_digests"]
        and test["workflow_run"] == release["workflow_run"] for test in tests.values()
    )
    return {
        "release_tests": {**metadata, "status": "PASS" if tests_pass else "BLOCKED"},
        "security_scans": {**metadata, "status": "BLOCKED" if any(
            report["summary"]["CRITICAL"] or report["secret_count"] for report in scans) else "PASS"},
        "security_high": {**metadata, "status": "BLOCKED" if any(
            any(row["fixed_version"] or row["approval"] != "public" for row in report["high"])
            for report in scans) else "PASS"},
    }


def evaluate(evidence=None, *, expected=None, evidence_dir=None, scans=None, exceptions=None, now=None):
    now = now or datetime.now(UTC)
    report = {"controls": {name: {"status": "BLOCKED" if name in MACHINE_CONTROLS else "NOT RUN",
                                 "verification": "not verified"} for name in CONTROLS}}
    if evidence is not None:
        if expected is None or evidence_dir is None or evidence["schema_version"] != 2:
            raise ValueError("Production evidence requires an independent expected deployment identity")
        context = identity(evidence, bundle=True)
        if context != identity(expected):
            raise ValueError("Evidence belongs to a different deployment")
        report.update(context)
        supplied = evidence["controls"]
        if set(supplied) - (set(CONTROLS) - MACHINE_CONTROLS):
            raise ValueError("Unknown or machine-only production control")
        for name, row in supplied.items():
            result = operator_control(name, row, evidence_dir, now)
            if name == "backup_restore":
                restored = row["restore_environment_id"]
                if not restored.strip() or restored == context["environment_id"]:
                    raise ValueError("Restore rehearsal requires an independent environment")
            report["controls"][name] = result
        if scans is not None:
            if exceptions is None:
                raise ValueError("Versioned OCI exception manifest is required")
            report["controls"].update(scan_controls(context, scans, exceptions, now))
    report["public_production_smoke"] = report["controls"]["production_e2e"]["status"]
    report["readiness"] = ("READY" if all(row["status"] == "PASS" for row in report["controls"].values())
                           else "NOT READY")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--expected-deployment", type=Path)
    parser.add_argument("--scans", type=Path)
    parser.add_argument("--exceptions", type=Path, default=Path("deploy/security/oci-exceptions.json"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        report = evaluate(read_json(args.evidence) if args.evidence else None,
                          expected=read_json(args.expected_deployment) if args.expected_deployment else None,
                          evidence_dir=args.evidence.parent if args.evidence else None,
                          scans=args.scans, exceptions=args.exceptions)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        parser.exit(2, f"Invalid production evidence: {exc}\n")
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"PUBLIC PRODUCTION SMOKE: {report['public_production_smoke']}")
        for key, row in report["controls"].items():
            print(f"{key}: {row['status']} ({row['verification']})")
        print(f"PUBLIC LAUNCH: {report['readiness']}")
    return int(report["readiness"] != "READY")


if __name__ == "__main__":
    raise SystemExit(main())
