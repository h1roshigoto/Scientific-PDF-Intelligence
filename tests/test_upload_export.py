import asyncio
import csv
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from hashlib import sha256
from io import BytesIO, StringIO
from threading import Event
from zipfile import ZipFile, ZipInfo

import pymupdf
import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from spi import upload_api, upload_export
from spi.api import app
from spi.release_readiness import audit_store
from spi.storage import Store
from spi.upload_export_models import MANIFEST, MEMBERS, ROOT
from spi.upload_export_verify import verify
from spi.upload_processing import UploadProcessingStore
from spi.upload_queue import UploadQueue
from spi.upload_store import UploadStore
from test_batch_table_proposals import rag_multilevel_pdf
from test_upload_processing import get, start, upload, worker
from test_upload_reviews import payload, read, review_path, snapshot, solar, write  # noqa: F401
from test_uploads import TOKEN, pdf_bytes, private_uploads  # noqa: F401

HEADER = "X-SPI-Snapshot-SHA256"
ROUTES = {
    "manifest": ("application/json", "manifest-preview.json"),
    "reviewed-results.csv": ("text/csv; charset=utf-8", "reviewed_results.csv"),
    "reviewed-results.json": ("application/json", "reviewed_results.json"),
    "scientific-passport.pdf": ("application/pdf", "scientific-passport.pdf"),
    "project.zip": ("application/zip", None),
}


def request_export(client, url, name="manifest", *, expected=None, status=200, code=None, headers=None):
    headers = dict(TOKEN if headers is None else headers)
    if expected is not None:
        headers[HEADER] = expected
    response = client.get(f"{url}/export/{name}", headers=headers)
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    if code is not None:
        assert response.json() == {"detail": code}
    if status == 200:
        content_type, filename = ROUTES[name]
        assert response.headers["content-type"] == content_type
        disposition = response.headers["content-disposition"]
        if filename:
            assert disposition.endswith(f'"{filename}"')
        else:
            assert disposition.startswith('attachment; filename="spi-project-')
            assert disposition.endswith(f'-{url.rsplit("/", 1)[1]}.zip"')
        if expected:
            assert response.headers[HEADER] == expected
    return response


def approve(client, url, row, **changes):
    path = review_path(url, row)
    return write(client, path, payload(read(client, path), **changes))


def reject(client, url, row):
    path = review_path(url, row)
    return write(client, path, payload(read(client, path), "reject", reason="Другая измерительная серия"))


def propose(client, url, row, correction):
    path = review_path(url, row)
    return write(client, path, payload(read(client, path), "propose_correction", correction=correction,
                                      reason="Сверено с таблицей и условиями измерения"))


def approve_correction(client, url, row, **changes):
    path = review_path(url, row)
    return write(client, path, payload(read(client, path), "approve_correction", **changes))


def unpack(content):
    with ZipFile(BytesIO(content)) as archive:
        assert set(archive.namelist()) == {ROOT + name for name in (*MEMBERS, MANIFEST)}
        return {name: archive.read(ROOT + name) for name in (*MEMBERS, MANIFEST)}


def csv_rows(data):
    reader = csv.DictReader(StringIO(data.decode("utf-8-sig"), newline=""))
    assert tuple(reader.fieldnames) == upload_export.CSV_COLUMNS
    return list(reader)


def pdf_text(data):
    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        return "".join(page.get_text() for page in pdf)


def edit_job(processing, **changes):
    queue = UploadQueue(processing.root.parent)
    with queue._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        job = queue._get(db, processing.upload.upload_id)
        job.processing = job.processing.model_copy(update=changes)
        queue._save(db, job)
    return queue


def mutate_event(processing, result_id, change):
    with closing(sqlite3.connect(processing.root / "reviews.sqlite3")) as db, db:
        row = db.execute("SELECT revision, event FROM review_events WHERE result_id = ? ORDER BY revision DESC",
                         (result_id,)).fetchone()
        event = json.loads(row[1])
        change(event)
        db.execute("UPDATE review_events SET event = ? WHERE result_id = ? AND revision = ?",
                   (json.dumps(event), result_id, row[0]))


def add_issue(processing, row):
    checkpoint = processing.checkpoint()
    checkpoint["science_issues"].append({
        "issue_id": "test-measurement-condition", "domain": "solar", "rule_id": "test_science_warning", "severity": "warning",
        "reason": "Сверить условия измерения", "result_ids": [row["result_id"]],
        "evidence_refs": [{"document_id": row["document_id"], "page": row["source"]["page"], "fragment": "15.5"}],
    })
    processing.save_batch_document(checkpoint)
    counts = UploadQueue(processing.root.parent).get(processing.upload.upload_id).processing.counts
    edit_job(processing, counts=counts.model_copy(update={"issues": counts.issues + 1}))


def test_archive_and_downloads_preserve_scientific_data(solar, private_uploads, tmp_path):
    client, url, automatic, processing = solar
    original, corrected, rejected, pending = automatic
    approve(client, url, original, reviewer_label="Рецензент")
    add_issue(processing, original)
    propose(client, url, corrected, {"raw": {"value": 0, "unit": None}, "normalized": {"value": None, "unit": None}})
    approve_correction(client, url, corrected, reason="Единица неизвестна")
    reject(client, url, rejected)

    official = Store(private_uploads["SPI_STATE_DIR"])
    document = {"document_id": "solar:D001", "domain": "solar", "sha256": "0" * 64}
    official.write("documents.json", [document])
    readiness = list(audit_store(official, {document["document_id"]: document}, {}))
    official_files = snapshot(official.root)
    private_files = snapshot(processing.root.parent)
    originals = snapshot(UploadStore.from_env().files)
    preview = request_export(client, url).json()
    fingerprint = preview["metadata"]["snapshot_sha256"]
    counts = preview["validation"]["review_counts"]
    assert counts == dict(automatic=4, approved=1, approved_corrected=1, pending=1, correction_proposed=0,
                          rejected=1, stale=0, unconfirmed=1)
    assert preview["validation"]["ocr_pending_pages"] == [2]
    assert preview["validation"]["scientific_completeness"] == "unverified"
    assert preview["validation"]["issue_counts"]["total"] >= 1
    package = request_export(client, url, "project.zip", expected=fingerprint)
    files = unpack(package.content)
    path = tmp_path / "private-project.zip"
    path.write_bytes(package.content)
    manifest = verify(path)
    assert manifest.metadata.snapshot_sha256 == fingerprint
    assert manifest.metadata.pdf_sha256 == processing.upload.sha256
    assert manifest.manifest_self_excluded
    assert all(sha256(files[item.name]).hexdigest() == item.sha256 and len(files[item.name]) == item.size_bytes
               for item in manifest.files)

    keys = {original["result_id"], corrected["result_id"]}
    rows = csv_rows(files["reviewed_results.csv"])
    results = json.loads(files["reviewed_results.json"])["results"]
    assert [row["result_id"] for row in rows] == sorted(keys)
    assert [row["decision"]["result_id"] for row in results] == sorted(keys)
    assert {row["decision"]["status"] for row in results} == {"approved", "approved_corrected"}
    for row, item in zip(rows, results, strict=True):
        result = item["reviewed_result"]
        assert row["raw_value"] == str(result["raw"]["value"])
        assert json.loads(row["context_json"]) == result["context"]
        assert row["source_page"] == str(result["source"]["page"]) == "1"
        assert row["source_origin"] == result["source"]["origin"] == "primary"
        assert row["source_result_hash"] == item["decision"]["source_result_hash"]
        assert result["extraction_status"] == item["original_result"]["extraction_status"]
        assert item["ocr_pages"] == [2]
    item = next(row for row in results if row["decision"]["status"] == "approved_corrected")
    assert item["original_result"] == corrected
    assert item["reviewed_result"]["raw"] == {"value": 0, "unit": None}
    assert item["reviewed_result"]["normalized"] == {"value": None, "unit": None}
    assert item["reviewed_result"]["context"]["device_label"] == corrected["context"]["device_label"]
    csv_corrected = next(row for row in rows if row["review_status"] == "approved_corrected")
    assert float(csv_corrected["raw_value"]) == 0
    assert not any(csv_corrected[key] for key in ("raw_unit", "normalized_value", "normalized_unit"))
    issues = json.loads(files["issues.json"])
    assert issues["review_approval_resolves_issues"] is False
    issue = next(issue for issue in issues["issues"] if issue["issue_id"] == "test-measurement-condition")
    assert issue["evidence_refs"][0]["fragment"] == "15.5"
    approved = next(row for row in results if row["decision"]["status"] == "approved")
    assert issue in approved["issues"] and approved["decision"]["warnings_acknowledged"] is False
    history = json.loads(files["review_history.json"])
    assert sorted(history["automatic_results"], key=lambda row: row["result_id"]) == sorted(
        automatic, key=lambda row: row["result_id"])
    assert len(history["events"]) == 4
    assert {event["event"]["action"] for event in history["events"]} == {"approve", "propose_correction", "approve_correction", "reject"}
    assert next(event for event in history["events"] if event["event"]["action"] == "reject")["event"]["original_result"] == rejected
    assert next(row for row in history["current_reviews"] if row["result_id"] == pending["result_id"])["status"] == "pending"

    report = pdf_text(files["scientific-passport.pdf"])
    for text in ("Scientific PDF Intelligence", "Private scientific project", "Рецензент", fingerprint,
                 "Автоматические результаты\n4", "Подтверждённые исходные\n1", "Страниц в ожидании OCR\n1",
                 "approved_corrected", "Единица неизвестна", "test_science_warning", "review_history.json",
                 str(original["raw"]["value"]), "0.0"):
        assert text in report, text
    for route in ("reviewed-results.csv", "reviewed-results.json", "scientific-passport.pdf"):
        standalone = request_export(client, url, route, expected=fingerprint).content
        if route.endswith(".pdf"):
            assert fingerprint in pdf_text(standalone)
            assert all(item["decision"]["review_id"] in pdf_text(standalone) for item in results)
        else:
            assert standalone == files[ROUTES[route][1]]
    local = subprocess.run([sys.executable, "-m", "spi.upload_export_verify", str(path)],
                           text=True, capture_output=True, timeout=20)
    assert local.returncode == 0 and "PASS: 6 files verified by SHA-256" in local.stdout
    assert snapshot(official.root) == official_files
    assert list(audit_store(official, {document["document_id"]: document}, {})) == readiness
    assert snapshot(processing.root.parent) == private_files
    assert snapshot(UploadStore.from_env().files) == originals

    frozen = upload_api._snapshot(processing.upload.upload_id, UploadStore.from_env(), fingerprint)
    first, second = BytesIO(), BytesIO()
    upload_export.build_archive(frozen, first)
    upload_export.build_archive(frozen, second)
    assert first.getvalue() == second.getvalue()


def test_audit_only_and_proposed_result(solar):
    client, url, rows, _ = solar
    propose(client, url, rows[0], {"context": {"operator_note": "observed"}})
    reject(client, url, rows[1])
    files = unpack(request_export(client, url, "project.zip").content)
    assert csv_rows(files["reviewed_results.csv"]) == []
    assert json.loads(files["reviewed_results.json"])["results"] == []
    history = json.loads(files["review_history.json"])
    assert {item["status"] for item in history["current_reviews"]} == {"pending", "correction_proposed", "rejected"}
    assert {item["event"]["decision"]["status"] for item in history["events"]} == {"correction_proposed", "rejected"}
    assert "zero_approved_results" in pdf_text(files["scientific-passport.pdf"])
    manifest = json.loads(files[MANIFEST])
    assert manifest["validation"]["user_review_status"] == "audit_only"
    assert "zero_approved_results" in {warning["code"] for warning in manifest["validation"]["warnings"]}


@pytest.mark.parametrize("change", ["result_hash", "job"])
def test_stale_decisions_are_only_exported_as_audit(solar, change):
    client, url, rows, processing = solar
    approve(client, url, rows[0])
    preview = request_export(client, url).json()
    old = preview["metadata"]["snapshot_sha256"]
    if change == "result_hash":
        checkpoint = processing.checkpoint()
        candidates = [row for fragment in checkpoint["fragments"].values() for row in fragment.get("results", ())
                      if row["result_id"] == rows[0]["result_id"]]
        assert candidates
        for row in candidates:
            row["note"] = "Automatic result changed"
        processing.save_batch_document(checkpoint)
    else:
        edit_job(processing, status="failed", retry_allowed=True)
        new_job = start(client, url)
        assert new_job["job_id"] != preview["metadata"]["processing_job_id"]
        assert worker().run_one()
    request_export(client, url, "project.zip", expected=old, status=409, code="upload_export_source_changed")
    files = unpack(request_export(client, url, "project.zip").content)
    assert csv_rows(files["reviewed_results.csv"]) == []
    event, = json.loads(files["review_history.json"])["events"]
    assert event["applicability"] == "stale"
    assert event["event"]["decision"]["status"] == "approved"
    counts = json.loads(files[MANIFEST])["validation"]["review_counts"]
    assert counts["stale"] == 1 and counts["approved"] == 0


def test_authorization_active_job_and_changed_pdf(solar):
    client, url, _, processing = solar
    for name in ROUTES:
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            request_export(client, url, name, status=401, code="upload_unauthorized", headers=headers)
    request_export(client, "/uploads/" + "0" * 32, status=404, code="upload_not_found")
    request_export(client, "/uploads/invalid", status=422, code="invalid_export_request")
    request_export(client, url, "project.zip", expected="wrong", status=422, code="invalid_export_request")
    path = UploadStore.from_env().file(processing.upload)
    original = path.read_bytes()
    path.write_bytes(original.replace(b"%PDF", b"%PDA", 1))
    for name in ROUTES:
        request_export(client, url, name, status=409, code="upload_export_pdf_changed")
    path.write_bytes(original)
    queue = edit_job(processing, status="failed", retry_allowed=True)
    start(client, url)
    request_export(client, url, status=409, code="upload_export_processing_active")
    with queue.claim() as active_job:
        assert active_job.processing.status == "processing"
        request_export(client, url, "project.zip", status=409, code="upload_export_processing_active")
    other = upload(client, pdf_bytes())
    request_export(client, other, status=409, code="upload_export_analysis_unavailable")


def test_queue_change_before_capture_returns_conflict(solar, monkeypatch):
    client, url, _, processing = solar
    verified = upload_export._verified_original

    def retry_after_hash(*args):
        source = verified(*args)
        edit_job(processing, status="failed", retry_allowed=True)
        start(TestClient(app), url)
        return source

    monkeypatch.setattr(upload_export, "_verified_original", retry_after_hash)
    request_export(client, url, "project.zip", status=409, code="upload_export_source_changed")


def test_concurrent_review_after_capture_does_not_mix_zip_files(solar, monkeypatch):
    client, url, rows, _ = solar
    approved = approve(client, url, rows[0])["decision"]
    fingerprint = request_export(client, url).headers[HEADER]
    rendering = Event()
    continue_rendering = Event()
    build = upload_api.build_archive

    def pause_rendering(*args):
        rendering.set()
        assert continue_rendering.wait(12)
        return build(*args)

    monkeypatch.setattr(upload_api, "build_archive", pause_rendering)
    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(request_export, TestClient(app), url, "project.zip", expected=fingerprint)
        try:
            assert rendering.wait(12)
            reject(client, url, rows[0])
            assert request_export(client, url).headers[HEADER] != fingerprint
        finally:
            continue_rendering.set()
        files = unpack(response.result(timeout=12).content)
    assert csv_rows(files["reviewed_results.csv"])[0]["review_id"] == approved["review_id"]
    assert json.loads(files["reviewed_results.json"])["results"][0]["decision"] == approved
    assert [item["event"]["action"] for item in json.loads(files["review_history.json"])["events"]] == ["approve"]
    report = pdf_text(files["scientific-passport.pdf"])
    assert approved["review_id"] in report and "Отклонённые\n0" in report
    request_export(client, url, "reviewed-results.csv", expected=fingerprint, status=409, code="upload_export_source_changed")


@pytest.mark.parametrize(("field", "value", "code"), [
    ("document_id", "solar:00000000000000000000000000000000", "upload_export_source_changed"),
    ("result_id", "another-upload:foreign-result", "upload_export_source_changed"),
    ("source.page", 999, "upload_export_invalid_review"),
    ("normalized", {"value": "99999", "unit": "V"}, "upload_export_invalid_review"),
])
def test_corrupt_approved_correction_is_rejected(solar, field, value, code):
    client, url, rows, processing = solar
    row = rows[0]
    propose(client, url, row, {"context": {"note": "Synthetic"}})
    approve_correction(client, url, row)

    def corrupt(event):
        result = event["decision"]["reviewed_result"]
        if field == "source.page":
            result["source"]["page"] = value
        else:
            result[field] = value

    mutate_event(processing, row["result_id"], corrupt)
    request_export(client, url, "project.zip", status=409, code=code)


def test_rag_context_negative_values_csv_safety_and_long_unicode(private_uploads, monkeypatch, tmp_path):
    monkeypatch.setenv("SPI_UPLOAD_MAX_BYTES", "65536")
    path = rag_multilevel_pdf(tmp_path / "synthetic-rag.pdf", ("Gemini-2.5", "Flash"), ("Mistral v3-8B Instruct",))
    client = TestClient(app)
    url = upload(client, path.read_bytes(), domain="rag")
    start(client, url)
    assert worker().run_one()
    row = get(client, url, "results")[0]
    unicode_text = ("Русский текст, Ω и English evidence. " * 400) + "Конец 🧪"
    propose(client, url, row, {
        "metric": {"name": row["metric"]["name"], "k": 7},
        "context": {"top_k": 19, "evaluation_protocol": "topical retrieval + EM", "additional_evidence": unicode_text},
        "source": {**row["source"], "origin": "cited", "cited_publication_id": "Ref-123",
                   "fragment": "\t=1+1 · Доказательство"},
        "raw": {"value": "-3.5e-02", "unit": None}, "normalized": {"value": None, "unit": None},
    })
    approve_correction(client, url, row, reviewer_label="\u200b@name", reason="+1+1")
    files = unpack(request_export(client, url, "project.zip").content)
    result, = json.loads(files["reviewed_results.json"])["results"]
    record, = csv_rows(files["reviewed_results.csv"])
    assert result["original_result"] == row
    assert record["raw_value"] == "-3.5e-02"
    assert record["reviewer_label"] == "'\u200b@name" and record["review_reason"] == "'+1+1"
    assert record["source_fragment"].startswith("'\t=")
    assert record["source_origin"] == "cited" and record["cited_publication_id"] == "Ref-123"
    context = result["reviewed_result"]["context"]
    assert int(record["metric_k"]) == result["reviewed_result"]["metric"]["k"] == 7
    assert json.loads(record["context_json"]) == context and context["top_k"] == 19
    assert context["evaluation_protocol"] == "topical retrieval + EM"
    assert context["additional_evidence"] == unicode_text
    report = pdf_text(files["scientific-passport.pdf"])
    assert "Русский текст" in report and "Сокращено. Полный текст в JSON архива." in report and "-3.5e-02" in report


@pytest.mark.parametrize(("value", "expected"), [
    ("=1", "'=1"), (" \tSafe", "' \tSafe"), ("\ufeff@name", "'\ufeff@name"), ("-1+2", "'-1+2"),
    ("'text", "''text"), (" ＝1", "' ＝1"), ("-3.5e-02", "-3.5e-02"), (-0.2, -0.2), (0, 0), (None, ""),
])
def test_csv_cells(value, expected):
    assert upload_export._csv_cell(value, scientific_value=True) == expected


def test_export_temp_cleanup_before_and_during_delivery(solar, monkeypatch):
    client, url, _, processing = solar
    create = upload_api.TemporaryFile
    temporary = []

    def watch(**options):
        file = create(**options)
        temporary.append(file)
        return file

    monkeypatch.setattr(upload_api, "TemporaryFile", watch)
    request_export(client, url, "project.zip")
    assert temporary[-1].closed
    with monkeypatch.context() as limits:
        limits.setenv("SPI_UPLOAD_EXPORT_MAX_PDF_PAGES", "1")
        request_export(client, url, "project.zip", status=413, code="upload_export_too_large")
        assert temporary[-1].closed
    with monkeypatch.context() as limits:
        limits.setenv("SPI_UPLOAD_EXPORT_MAX_BYTES", "50")
        request_export(client, url, "project.zip", status=413, code="upload_export_too_large")
        assert temporary[-1].closed
    with monkeypatch.context() as limits:
        limits.setenv("SPI_UPLOAD_EXPORT_MAX_SNAPSHOT_BYTES", "50")
        request_export(client, url, status=413, code="upload_export_too_large")
    response = upload_api._ProjectResponse(upload_api._snapshot(processing.upload.upload_id, UploadStore.from_env()))

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("Synthetic connection closed")

    with pytest.raises(ClientDisconnect):
        asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}}, None, send))
    assert temporary[-1].closed


def test_export_reopens_in_a_fresh_api_process(solar):
    client, url, rows, _ = solar
    approve(client, url, rows[0])
    fingerprint = request_export(client, url).headers[HEADER]
    process = subprocess.run([sys.executable, "-c", """
import os
from io import BytesIO
from zipfile import ZipFile
from fastapi.testclient import TestClient
from spi.api import app
response = TestClient(app).get(os.environ['SPI_TEST_EXPORT'], headers={
    'Authorization': 'Bearer ' + os.environ['SPI_UPLOAD_TOKEN'],
    'X-SPI-Snapshot-SHA256': os.environ['SPI_TEST_SNAPSHOT'],
})
response.raise_for_status()
with ZipFile(BytesIO(response.content)) as archive:
    print(archive.read('spi-project/reviewed_results.json').decode('utf-8'))
"""], env={**os.environ, "SPI_TEST_EXPORT": url + "/export/project.zip", "SPI_TEST_SNAPSHOT": fingerprint},
        check=True, encoding="utf-8", capture_output=True, timeout=20)
    assert json.loads(process.stdout) == request_export(client, url, "reviewed-results.json", expected=fingerprint).json()


def test_verifier_rejects_corruption_and_unexpected_entries(solar, tmp_path):
    client, url, _, _ = solar
    original = request_export(client, url, "project.zip").content
    files = unpack(original)
    path = tmp_path / "verify.zip"
    path.write_bytes(original)
    assert verify(path).metadata.official_submission is False

    def repack(*, updates=None, extra=None):
        with ZipFile(path, "w") as archive:
            for name, content in {**files, **(updates or {})}.items():
                archive.writestr(ROOT + name, content)
            if extra:
                archive.writestr(extra, b"unexpected")

    def fails():
        process = subprocess.run([sys.executable, "-m", "spi.upload_export_verify", str(path)], text=True,
                                 capture_output=True, timeout=20)
        assert process.returncode == 1 and process.stdout.startswith("FAIL:")

    path.write_bytes(original[:80])
    fails()
    bad_csv = bytearray(files["reviewed_results.csv"])
    bad_csv[-3] ^= 1
    repack(updates={"reviewed_results.csv": bad_csv})
    fails()
    for extra in (ROOT + "unexpected.xml", ROOT + "../escape.txt", "/escape.txt"):
        repack(extra=extra)
        fails()
    symlink = ZipInfo(ROOT + "README.md")
    symlink.create_system = 3
    symlink.external_attr = 0o120777 << 16
    with ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(symlink if name == "README.md" else ROOT + name, content)
    fails()
    manifest = json.loads(files[MANIFEST])
    manifest["metadata"]["format"] = "spi-private-project-v2"
    repack(updates={MANIFEST: json.dumps(manifest).encode()})
    fails()
    manifest = json.loads(files[MANIFEST])
    del manifest["checksum_algorithm"]
    repack(updates={MANIFEST: json.dumps(manifest).encode()})
    fails()
    assert list(tmp_path.glob("*.txt")) == []
