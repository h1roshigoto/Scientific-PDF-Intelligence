from fastapi.testclient import TestClient

from spi.api import app
from spi.storage import Store


def test_read_api_and_internal_json_export_remain_compatible(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("SPI_STATE_DIR", str(state_dir))
    store = Store(state_dir)
    file = tmp_path / "D001.pdf"
    file.write_bytes(b"%PDF-test")
    documents = [
        {"document_id": "solar:D001", "domain": "solar", "filename": "D001.pdf", "file_path": str(file)},
        {"document_id": "rag:D001", "domain": "rag"},
    ]
    store.write("documents.json", documents)
    client = TestClient(app)

    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/documents", params={"domain": "rag"}).json() == documents[1:]
    assert client.get("/documents", params={"domain": "other"}).status_code == 422
    assert client.get("/documents/solar:D001/file").content == file.read_bytes()
    assert client.get("/results", params={"domain": "solar"}).json() == []
    assert client.get("/results/missing").status_code == 404
    assert client.get("/issues", params={"domain": "rag"}).json() == []
    for artifact, expected in (("documents.json", documents), ("results.json", []), ("issues.json", [])):
        assert client.get(f"/export/{artifact}").json() == expected
    assert client.get("/export/results.csv").status_code == 404
