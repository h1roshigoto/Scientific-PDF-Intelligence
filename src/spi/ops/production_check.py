"""Evaluate an operator's evidence from an actual public deployment."""

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

CONTROLS = (
    "production_images", "non_root", "resource_limits", "trusted_proxy", "public_tls", "cookies_csrf",
    "tenant_isolation", "pdf_isolation", "persistent_storage", "backup_restore", "monitoring",
    "disk_limits", "security_scans", "production_e2e",
)
STATUSES = {"PASS", "FAIL", "NOT RUN", "BLOCKED"}
BASELINE = {"security_scans": {"status": "BLOCKED", "reference": "docs/P0_31_RESULTS.md"}}


def evaluate(evidence=None):
    evidence = evidence or {}
    results = {"public_origin": evidence.get("public_origin"), "revision": evidence.get("revision"),
               "controls": {}}
    if evidence:
        origin = urlsplit(results["public_origin"] or "")
        if (evidence.get("environment") != "public-production" or origin.scheme != "https" or not origin.hostname
                or origin.username or origin.password or origin.port or origin.path or origin.query or origin.fragment
                or not re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z]{2,63}", origin.hostname)
                or origin.hostname.endswith((".localhost", ".local", ".invalid", ".test", ".example"))
                or not re.fullmatch(r"[a-f0-9]{40}", results["revision"] or "")):
            raise ValueError("Evidence must identify a public HTTPS deployment and a 40-character revision")
    supplied = evidence.get("controls", {})
    if set(supplied) - set(CONTROLS):
        raise ValueError("Unknown production control")
    for control in CONTROLS:
        record = supplied.get(control, BASELINE.get(control, {"status": "NOT RUN"}))
        status = record["status"]
        if status not in STATUSES:
            raise ValueError(f"Unknown status for {control}")
        if status == "PASS" and (not str(record.get("reference", "")).strip()
                                 or not datetime.fromisoformat(record.get("checked_at", "")).tzinfo):
            raise ValueError(f"PASS requires dated evidence for {control}")
        results["controls"][control] = record
    results["public_production_smoke"] = results["controls"]["production_e2e"]["status"]
    results["readiness"] = ("READY" if all(row["status"] == "PASS" for row in results["controls"].values())
                            else "NOT READY")
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        evidence = json.loads(args.evidence.read_text(encoding="utf-8")) if args.evidence else None
        report = evaluate(evidence)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        parser.exit(2, f"Invalid production evidence ({type(exc).__name__})\n")
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"PUBLIC PRODUCTION SMOKE: {report['public_production_smoke']}")
        for key, row in report["controls"].items():
            print(f"{key}: {row['status']}")
        print(f"PUBLIC LAUNCH: {report['readiness']}")
    return int(report["readiness"] != "READY")


if __name__ == "__main__":
    raise SystemExit(main())
