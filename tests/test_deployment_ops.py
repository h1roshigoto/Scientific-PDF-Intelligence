import errno
from datetime import datetime, timezone

import pytest

from spi.ops.backup import safe_member
from spi.ops.production_check import CONTROLS, evaluate
from spi.ops.state import disk_full
from spi.storage import Store


@pytest.mark.parametrize("invalid", [
    "/auth/auth.sqlite3", "../auth/auth.sqlite3", "auth//auth.sqlite3",
    "auth\\auth.sqlite3", "tenants/users/evil/metadata/uploads.sqlite3",
])
def test_backup_member_rejects_noncanonical_paths(invalid):
    with pytest.raises(ValueError):
        safe_member(invalid)


def test_public_gate_requires_every_control_and_public_environment():
    assert evaluate()["readiness"] == "NOT READY"
    record = {"status": "PASS", "reference": "operator log", "checked_at": datetime.now(timezone.utc).isoformat()}
    evidence = {
        "environment": "public-production", "revision": "f" * 40,
        "public_origin": "https://spi.example.org",
        "controls": {item: record for item in CONTROLS},
    }
    assert evaluate(evidence)["readiness"] == "READY"
    for status in ("FAIL", "NOT RUN", "BLOCKED"):
        evidence["controls"]["backup_restore"] = {"status": status}
        assert evaluate(evidence)["readiness"] == "NOT READY"
    evidence["public_origin"] = "https://localhost:8443"
    with pytest.raises(ValueError):
        evaluate(evidence)


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
