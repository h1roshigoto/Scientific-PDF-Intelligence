import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from hashlib import sha256
from secrets import token_urlsafe
from threading import Barrier

import httpx
import pymupdf
import pytest
from fastapi.testclient import TestClient

from spi import upload_store
from spi.api import app
from spi.upload_store import UploadStore


TOKEN = {"Authorization": "Bearer synthetic-test-secret-0123456789"}


def pdf_bytes(**save_options):
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "Synthetic upload PDF")
        pdf.new_page()
        return pdf.tobytes(**save_options)


@pytest.fixture
def private_uploads(tmp_path, monkeypatch):
    paths = {
        "SPI_STATE_DIR": tmp_path / "outputs" / "p0-22-release" / "state",
        "SPI_UPLOAD_DIR": tmp_path / "pdfs",
        "SPI_UPLOAD_STATE_DIR": tmp_path / "upload-state",
        "SPI_UPLOAD_PROCESS_STATE_DIR": tmp_path / "processing",
    }
    for name, path in paths.items():
        monkeypatch.setenv(name, str(path))
    monkeypatch.setenv("SPI_UPLOAD_ENABLED", "true")
    monkeypatch.setenv("SPI_UPLOAD_TOKEN", "synthetic-test-secret-0123456789")
    monkeypatch.setenv("SPI_UPLOAD_MAX_BYTES", "4096")
    monkeypatch.setenv("SPI_UPLOAD_MAX_FILES", "10")
    monkeypatch.setenv("SPI_UPLOAD_PROCESS_GIGACHAT", "false")
    monkeypatch.setenv("SPI_UPLOAD_PROCESS_MAX_MODEL_REQUESTS", "8")
    return paths


def post(client, content, *, filename="paper.pdf", domain="solar", mime="application/pdf", headers=TOKEN):
    return client.post("/uploads", headers=headers, data={"domain": domain}, files={"file": (filename, content, mime)})


@pytest.mark.parametrize("domain", ["solar", "rag"])
def test_store_and_reopen_pdf(private_uploads, domain):
    content = pdf_bytes()
    with TestClient(app) as client:
        response = post(client, content, domain=domain, filename="статья.pdf")
        assert response.status_code == 201, response.text
        assert response.headers["cache-control"] == "no-store"
        record = response.json()
        upload_id = record["upload_id"]
        assert record == {
            "upload_id": upload_id,
            "domain": domain,
            "original_filename": "статья.pdf",
            "size_bytes": len(content),
            "sha256": sha256(content).hexdigest(),
            "page_count": 2,
            "status": "stored",
            "created_at": record["created_at"],
        }
        assert datetime.fromisoformat(record["created_at"]).utcoffset().total_seconds() == 0

    assert UploadStore.from_env().get(upload_id).model_dump(mode="json") == record
    with TestClient(app) as new_client:
        response = new_client.get("/uploads", headers=TOKEN)
        assert response.json() == [record]
        assert response.headers["x-spi-upload-max-bytes"] == "4096"
        assert new_client.get(f"/uploads/{upload_id}", headers=TOKEN).json() == record
        original = new_client.get(f"/uploads/{upload_id}/file", headers=TOKEN)
        assert original.status_code == 200
        assert original.headers["content-type"] == "application/pdf"
        assert original.headers["x-content-type-options"] == "nosniff"
        assert original.content == content
        assert new_client.get(f"/uploads/{'0' * 32}", headers=TOKEN).status_code == 404
    assert list(private_uploads["SPI_UPLOAD_DIR"].iterdir()) == [
        private_uploads["SPI_UPLOAD_DIR"] / f"{upload_id}.pdf"
    ]


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer bad-secret"}])
def test_all_operations_require_bearer(private_uploads, headers):
    client = TestClient(app)
    upload_id = "0" * 32
    calls = [post(client, pdf_bytes(), headers=headers)]
    calls += [client.get(url, headers=headers) for url in (
        "/uploads", f"/uploads/{upload_id}", f"/uploads/{upload_id}/file",
        f"/uploads/{upload_id}/processing", f"/uploads/{upload_id}/results", f"/uploads/{upload_id}/issues",
    )]
    calls.append(client.post(f"/uploads/{upload_id}/process", headers=headers))
    assert all(response.status_code == 401 and response.headers["www-authenticate"] == "Bearer" for response in calls)
    assert all(not path.exists() for path in private_uploads.values())


@pytest.mark.parametrize("value", ["A" * 31, " " + "A" * 32, "A" * 31 + "!", "а" * 32])
def test_malformed_or_short_token(private_uploads, monkeypatch, value):
    monkeypatch.setenv("SPI_UPLOAD_TOKEN", value)
    response = TestClient(app).get("/uploads", headers=TOKEN)
    assert response.status_code == 503
    assert response.json() == {"detail": "uploads_misconfigured"}
    assert all(not path.exists() for path in private_uploads.values())


def test_urlsafe_token(private_uploads, monkeypatch):
    token = token_urlsafe(32)
    monkeypatch.setenv("SPI_UPLOAD_TOKEN", token)
    assert post(TestClient(app), pdf_bytes(), headers={"Authorization": f"Bearer {token}"}).status_code == 201


def test_disabled_by_default_and_missing_secret(private_uploads, monkeypatch):
    monkeypatch.delenv("SPI_UPLOAD_ENABLED")
    client = TestClient(app)
    assert post(client, pdf_bytes()).json() == {"detail": "uploads_disabled"}
    assert client.get("/uploads", headers=TOKEN).status_code == 503
    monkeypatch.setenv("SPI_UPLOAD_ENABLED", "1")
    monkeypatch.delenv("SPI_UPLOAD_TOKEN")
    assert post(client, pdf_bytes()).json() == {"detail": "uploads_misconfigured"}
    assert all(not path.exists() for path in private_uploads.values())


def test_spoofed_and_unexpected_file_mime(private_uploads):
    client = TestClient(app)
    assert post(client, b"hello", mime="application/pdf").status_code == 422
    assert post(client, pdf_bytes(), filename="paper.bin", mime="text/plain").status_code == 201
    assert client.post("/uploads", headers=TOKEN, content=pdf_bytes()).status_code == 415


@pytest.mark.parametrize(("content", "code"), [
    (b"", "empty_pdf"),
    (b"<html>not PDF</html>", "missing_pdf_signature"),
    (b"%PDF-1.7\ncorrupted", "invalid_pdf"),
])
def test_bad_pdf_has_no_partial_file(private_uploads, content, code):
    response = post(TestClient(app), content)
    assert response.status_code == 422
    assert response.json() == {"detail": code}
    assert not list(private_uploads["SPI_UPLOAD_DIR"].iterdir())
    assert not private_uploads["SPI_UPLOAD_STATE_DIR"].exists()


@pytest.mark.parametrize("password", ["private", ""])
def test_rejects_encrypted_pdf(private_uploads, password):
    content = pdf_bytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw=password)
    response = post(TestClient(app), content)
    assert response.status_code == 422
    assert response.json() == {"detail": "encrypted_pdf"}
    assert not list(private_uploads["SPI_UPLOAD_DIR"].iterdir())


def test_exact_size_limit_and_early_abort_without_content_length(private_uploads, monkeypatch):
    client = TestClient(app)
    content = pdf_bytes()
    monkeypatch.setenv("SPI_UPLOAD_MAX_BYTES", str(len(content)))
    assert post(client, content).status_code == 201
    assert post(client, content + b" ").status_code == 413

    async def send_stream(extra_headers):
        async def body():
            yield b'--test\r\nContent-Disposition: form-data; name="file"; filename="paper.pdf"\r\n\r\n'
            yield content + b" " * 32
            raise AssertionError("Server read past the allowed PDF size")

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as streaming:
            return await streaming.post("/uploads", content=body(), headers={
                **TOKEN, **extra_headers, "Content-Type": "multipart/form-data; boundary=test",
            })

    for extra_headers in ({}, {"Content-Length": "12"}):
        response = asyncio.run(send_stream(extra_headers))
        assert response.status_code == 413, response.text
    assert len(list(private_uploads["SPI_UPLOAD_DIR"].iterdir())) == 1


def test_multipart_requires_one_file_and_one_domain(private_uploads):
    client = TestClient(app)
    file = ("paper.pdf", pdf_bytes())
    for fields, status in [
        ([("file", file)], 422),
        ([("file", file), ("domain", (None, "other"))], 422),
        ([("file", file), ("domain", (None, "rag")), ("file", file)], 400),
        ([("file", file), ("domain", (None, "rag")), ("domain", (None, "solar"))], 400),
    ]:
        assert client.post("/uploads", headers=TOKEN, files=fields).status_code == status
    incomplete = b'--test\r\nContent-Disposition: form-data; name="domain"\r\n\r\nsolar'
    assert client.post("/uploads", content=incomplete, headers={
        **TOKEN, "Content-Type": "multipart/form-data; boundary=test",
    }).status_code == 400
    assert not private_uploads["SPI_UPLOAD_DIR"].exists()


def test_traversal_names_and_repeat_upload(private_uploads, tmp_path):
    client = TestClient(app)
    content = pdf_bytes()
    responses = [post(client, content, filename="../../paper.pdf"),
                 post(client, content, filename=r"..\..\paper.pdf")]
    assert all(response.status_code == 201 for response in responses)
    records = [response.json() for response in responses]
    assert {item["original_filename"] for item in records} == {"paper.pdf"}
    assert len({item["upload_id"] for item in records}) == 2
    assert not (tmp_path / "paper.pdf").exists()
    assert client.get("/uploads/..%2Fpaper.pdf/file", headers=TOKEN).status_code == 404


def test_parallel_requests_cannot_overfill_quota(private_uploads, monkeypatch):
    monkeypatch.setenv("SPI_UPLOAD_MAX_FILES", "2")
    content = pdf_bytes()
    barrier = Barrier(5, timeout=10)

    def send(_):
        with TestClient(app) as client:
            barrier.wait()
            return post(client, content)

    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(send, range(5)))
    assert sorted(response.status_code for response in responses) == [201, 201, 409, 409, 409]
    records = UploadStore.from_env().list()
    assert len(records) == 2
    assert {path.name for path in private_uploads["SPI_UPLOAD_DIR"].iterdir()} == {
        f"{record.upload_id}.pdf" for record in records
    }


def test_write_error_rolls_back_file_and_metadata(private_uploads, monkeypatch):
    client = TestClient(app)
    content = pdf_bytes()
    assert post(client, content).status_code == 201
    existing = UploadStore.from_env().list()

    def fail(source, destination):
        raise PermissionError("private path")

    with monkeypatch.context() as scoped:
        scoped.setattr(upload_store.os, "replace", fail)
        response = post(client, content)
        assert response.status_code == 500
        assert response.json() == {"detail": "upload_storage_error"}

    assert UploadStore.from_env().list() == existing
    assert len(list(private_uploads["SPI_UPLOAD_DIR"].iterdir())) == 1


@pytest.mark.parametrize("bad_dir", [
    ".", "..", "src", "tests", "outputs", "data", "frontend_redezigned",
    "outputs/e2e-dev/state/user-files", "data/rag/test",
])
@pytest.mark.parametrize("setting", ["SPI_UPLOAD_DIR", "SPI_UPLOAD_STATE_DIR", "SPI_UPLOAD_PROCESS_STATE_DIR"])
def test_protected_project_trees(private_uploads, monkeypatch, bad_dir, setting):
    monkeypatch.setenv(setting, str(upload_store.PROJECT_ROOT / bad_dir))
    assert post(TestClient(app), pdf_bytes()).status_code == 503
    assert not private_uploads["SPI_UPLOAD_DIR"].exists()
    assert not private_uploads["SPI_UPLOAD_STATE_DIR"].exists()


def test_release_store_unchanged_and_overlapping_configuration_denied(private_uploads, monkeypatch):
    release = private_uploads["SPI_STATE_DIR"]
    release.mkdir(parents=True)
    marker = release / "documents.json"
    marker.write_bytes(b'[{"document_id":"solar:official"}]')
    client = TestClient(app)
    assert post(client, pdf_bytes()).status_code == 201
    assert client.get("/documents").json() == [{"document_id": "solar:official"}]
    for files, state in [
        (release, private_uploads["SPI_UPLOAD_STATE_DIR"]),
        (private_uploads["SPI_UPLOAD_DIR"], release / "nested"),
        (release.parent, private_uploads["SPI_UPLOAD_STATE_DIR"]),
        (private_uploads["SPI_UPLOAD_DIR"], private_uploads["SPI_UPLOAD_DIR"] / "nested"),
        (private_uploads["SPI_UPLOAD_DIR"], private_uploads["SPI_UPLOAD_DIR"]),
        ("", private_uploads["SPI_UPLOAD_STATE_DIR"]),
    ]:
        monkeypatch.setenv("SPI_UPLOAD_DIR", str(files))
        monkeypatch.setenv("SPI_UPLOAD_STATE_DIR", str(state))
        assert post(client, pdf_bytes()).status_code == 503
    assert list(release.iterdir()) == [marker]
    assert marker.read_bytes() == b'[{"document_id":"solar:official"}]'


def directory_alias(alias, target):
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError:
        # Windows can allow directory junctions even without symlink privileges.
        import subprocess
        import sys
        if sys.platform != "win32":
            raise
        subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(target)], check=True)


@pytest.mark.parametrize("setting", ["SPI_UPLOAD_DIR", "SPI_UPLOAD_STATE_DIR", "SPI_UPLOAD_PROCESS_STATE_DIR"])
@pytest.mark.parametrize("protected", ["checkout", "release"])
def test_link_into_protected_store_denied(private_uploads, monkeypatch, tmp_path, setting, protected):
    release = private_uploads["SPI_STATE_DIR"]
    release.mkdir(parents=True)
    alias = tmp_path / "alias"
    directory_alias(alias, release if protected == "release" else upload_store.PROJECT_ROOT)
    monkeypatch.setenv(setting, str(alias / "nested"))
    assert post(TestClient(app), pdf_bytes()).status_code == 503
    assert not list(release.iterdir())


def test_processing_path_cannot_overlap_other_private_roots(private_uploads, monkeypatch):
    for other in ("SPI_UPLOAD_DIR", "SPI_UPLOAD_STATE_DIR", "SPI_STATE_DIR"):
        monkeypatch.setenv("SPI_UPLOAD_PROCESS_STATE_DIR", str(private_uploads[other] / "processing"))
        assert post(TestClient(app), pdf_bytes()).json() == {"detail": "uploads_misconfigured"}


def test_processing_can_be_unconfigured_with_originals_enabled(private_uploads, monkeypatch):
    monkeypatch.delenv("SPI_UPLOAD_PROCESS_STATE_DIR")
    client = TestClient(app)
    response = post(client, pdf_bytes())
    assert response.status_code == 201
    response = client.post(f"/uploads/{response.json()['upload_id']}/process", headers=TOKEN)
    assert response.status_code == 503
    assert response.json() == {"detail": "upload_processing_unconfigured"}


def test_openapi_describes_private_multipart_and_pdf():
    spec = app.openapi()
    create = spec["paths"]["/uploads"]["post"]
    assert create["security"] == [{"HTTPBearer": []}]
    body = create["requestBody"]["content"]["multipart/form-data"]["schema"]
    assert body["required"] == ["file", "domain"]
    assert body["properties"]["domain"]["enum"] == ["solar", "rag"]
    assert body["properties"]["file"]["format"] == "binary"
    assert "201" in create["responses"]
    file = spec["paths"]["/uploads/{upload_id}/file"]["get"]
    assert "application/pdf" in file["responses"]["200"]["content"]
