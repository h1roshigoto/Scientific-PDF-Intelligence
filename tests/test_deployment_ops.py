import errno
import socket
import struct
from datetime import datetime, timedelta, timezone
from time import monotonic

import pytest

from spi import pdf_boundary
from spi.ops.alerts import alerts
from spi.ops.backup import safe_member
from spi.ops.production_check import evaluate
from spi.ops.release import exception_map, read_json, scan_result, sha256_file
from spi.ops.state import disk_full
from spi.storage import Store


@pytest.mark.parametrize("invalid", [
    "/auth/auth.sqlite3", "../auth/auth.sqlite3", "auth//auth.sqlite3",
    "auth\\auth.sqlite3", "tenants/users/evil/metadata/uploads.sqlite3",
])
def test_backup_member_rejects_noncanonical_paths(invalid):
    with pytest.raises(ValueError):
        safe_member(invalid)


def test_public_gate_requires_real_release_and_primary_evidence(tmp_path):
    assert evaluate()["readiness"] == "NOT READY"
    assert evaluate()["controls"]["security_scans"]["status"] == "BLOCKED"
    now = datetime.now(timezone.utc)
    context = {
        "environment": "public-production", "environment_id": "deployment-0001", "revision": "f" * 40,
        "public_origin": "https://spi.production.org", "hostname": "spi.production.org",
        "image_digests": {"runtime": "sha256:" + "a" * 64, "proxy": "sha256:" + "b" * 64},
    }
    fake = {"status": "PASS"}
    artifact = tmp_path / "public-tls.log"
    artifact.write_text("Synthetic primary evidence", encoding="utf-8")
    record = {"outcome": "verified", "operator": "Test operator", "reference": "https://spi.production.org/log",
              "checked_at": now.isoformat(), "artifact": artifact.name, "sha256": sha256_file(artifact)}

    def check(controls, **overrides):
        return evaluate({**context, **overrides, "schema_version": 2, "controls": controls},
                        expected=context, evidence_dir=tmp_path, now=now)

    result = check({"public_tls": record})
    assert result["readiness"] == "NOT READY"
    assert result["controls"]["public_tls"]["verification"] == "operator-attested"
    assert result["controls"]["security_high"]["status"] == "BLOCKED"
    for invalid in (fake, {**record, "status": "PASS"}, {**record, "outcome": "UNKNOWN"},
                    {**record, "reference": "operator log"}, {**record, "artifact": "missing.log"},
                    {**record, "checked_at": (now - timedelta(days=8)).isoformat()}):
        with pytest.raises(ValueError):
            check({"public_tls": invalid})
    for name in ("unknown", "security_scans", "security_high"):
        with pytest.raises(ValueError):
            check({name: record})
    for change in ({"revision": "a" * 39}, {"revision": "a" * 40}, {"image_digests": {"runtime": "latest"}},
                   {"hostname": "localhost", "public_origin": "https://localhost:8443"},
                   {"public_origin": "https://other.production.org"}):
        with pytest.raises(ValueError):
            check({}, **change)
    conflict = tmp_path / "conflicting.json"
    conflict.write_text('{"public_tls":{"outcome":"failed"},"public_tls":{"outcome":"verified"}}', encoding="utf-8")
    with pytest.raises(ValueError):
        read_json(conflict)


def test_scan_refuses_unapproved_high_available_fix_expiry_and_wrong_image():
    subject = {"config_digest": "sha256:" + "e" * 64, "diff_ids": ["sha256:" + "d" * 64]}
    now = datetime.now(timezone.utc)
    row = {"VulnerabilityID": "CVE-2026-9999", "PkgName": "openssl", "InstalledVersion": "1", "Severity": "HIGH"}
    trivy = {
        "SchemaVersion": 2, "ArtifactType": "container_image", "CreatedAt": now.isoformat(), "ReportID": "test",
        "Metadata": {"ImageID": subject["config_digest"], "DiffIDs": subject["diff_ids"]},
        "Results": [{"Class": "os-pkgs", "Type": "debian", "Vulnerabilities": [row]},
                    {"Class": "lang-pkgs", "Type": "python-pkg"}],
    }
    assert scan_result("runtime", trivy, subject, {})["blockers"]
    exception = {"cve": row["VulnerabilityID"], "package": "openssl", "image": "runtime", "version": "1",
                 "target_type": "debian", "scope": "candidate", "justification": "Reviewed test fixture",
                 "owner": "Maintainer", "advisory": "https://advisory.example.org/cve",
                 "assessed_on": now.date().isoformat(), "review_by": (now.date() + timedelta(days=1)).isoformat(),
                 "invalidated_if": "A fixed version or change of component reachability"}
    manifest = {"schema_version": 1, "exceptions": [exception]}
    approved = exception_map(manifest, now.date())
    assert not scan_result("runtime", trivy, subject, approved)["blockers"]
    assert scan_result("runtime", trivy, subject, approved, public=True)["blockers"]
    row["FixedVersion"] = "2"
    assert scan_result("runtime", trivy, subject, approved)["blockers"]
    with pytest.raises(ValueError):
        exception_map(manifest, now.date() + timedelta(days=1))
    with pytest.raises(ValueError):
        scan_result("runtime", trivy, {**subject, "config_digest": "sha256:" + "f" * 64}, approved)
    row["Severity"] = "CRITICAL"
    trivy["Results"].append({"Class": "secret", "Secrets": [{}]})
    assert len(scan_result("runtime", trivy, subject, approved)["blockers"]) == 2


def test_parser_rejects_oversized_reply_and_disconnect():
    for frame in (struct.pack("!I", pdf_boundary.MAX_RESPONSE_BYTES + 1), b"\x00"):
        sender, client = socket.socketpair()
        with sender, client:
            sender.sendall(frame)
            sender.shutdown(socket.SHUT_WR)
            with pytest.raises(pdf_boundary.ParserUnavailable):
                pdf_boundary.receive(client, pdf_boundary.MAX_RESPONSE_BYTES, monotonic() + 1)


@pytest.mark.parametrize(("setting", "value"), [("SPI_DEPLOYMENT_PROFILE", "production"), ("SPI_DEPLOYMENT", "true")])
def test_production_pdf_cannot_select_local_fallback(monkeypatch, setting, value):
    monkeypatch.setenv(setting, value)
    monkeypatch.delenv("SPI_PDF_SOCKET", raising=False)
    assert pdf_boundary.enabled()
    with pytest.raises(pdf_boundary.ParserUnavailable):
        pdf_boundary.request("pages", b"%PDF")


def test_alerts_detect_stale_health_counters_and_restore():
    now = 100_000_000
    api = {"instance": "sample", "http_5xx": 11, "login_failures": 12, "rate_limit_hits": 15}
    state = {"health": {"last_api_ok": now - 10, "last_sandbox_ok": now - 90},
             "queue": {"queue_depth": 12, "oldest_queued_job_age_seconds": 200, "disk_low": True},
             "backup": {"last_result": "failed"}, "api": api}
    detected = alerts(state, [], {"instance": "sample"}, now, now - 31 * 86400 - 1)
    assert {"sandbox_unavailable", "worker_stalled", "queue_backlog", "queue_age", "disk_low",
            "backup_failed", "backup_overdue", "restore_overdue", "api_5xx_increase",
            "login_abuse", "rate_limit_spike"} <= detected
    assert "api_unavailable" not in detected
    assert "api_5xx_increase" not in alerts(state, ["idle"], api, now, now)


def test_atomic_checkpoint_preserves_previous_file_on_enospc(tmp_path, monkeypatch):
    store = Store(tmp_path)
    old = {"status": "processing", "results": [1, 2, 3]}
    store.write("checkpoint.json", old)

    def fail(_fd):
        raise OSError(errno.ENOSPC, "injected write failure")

    with monkeypatch.context() as fault:
        fault.setattr("spi.storage.os.fsync", fail)
        with pytest.raises(OSError) as error:
            store.write("checkpoint.json", {"status": "completed"})
        assert disk_full(error.value)
    assert store.read("checkpoint.json") == old
    assert list(tmp_path.iterdir()) == [tmp_path / "checkpoint.json"]
