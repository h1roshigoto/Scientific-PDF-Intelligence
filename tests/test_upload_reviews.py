import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier, Event

import pytest
from fastapi.testclient import TestClient

from spi.api import app
from spi.models import ResultRecord
from spi.upload_processing import UploadProcessingStore
from spi.upload_queue import UploadQueue
from spi.upload_reviews import UploadReviews, source_result_hash
from spi.upload_store import UploadStore
from test_batch import make_pdf
from test_upload_processing import fake_model, get, start, upload, worker  # noqa: F401
from test_uploads import TOKEN, pdf_bytes, private_uploads  # noqa: F401


def review_path(url, row):
    return f"{url}/results/{row['result_id']}/review"


def read(client, path):
    response = client.get(path, headers=TOKEN)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def payload(detail, action="approve", **changes):
    body = {"basis": detail["basis"], "expected_revision": detail["revision"], "action": action}
    if action in {"approve", "approve_correction"}:
        body["evidence_mode"] = "text"
    return {**body, **changes}


def write(client, path, body, status=201, code=None):
    response = client.post(path, headers=TOKEN, json=body)
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    if code:
        assert response.json() == {"detail": code}
    return response.json()


def snapshot(root):
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def solar(private_uploads, tmp_path, monkeypatch):
    monkeypatch.setenv("SPI_UPLOAD_MAX_BYTES", "65536")
    original_pdf = make_pdf(tmp_path / "D001.pdf", scan=True).read_bytes()
    client = TestClient(app)
    url = upload(client, original_pdf)
    job = start(client, url)
    assert worker().run_one()
    store = UploadStore.from_env()
    record = store.get(job["upload_id"])
    processing = UploadProcessingStore(store.processing, record)
    return client, url, get(client, url, "results"), processing


def test_lifecycle_and_fresh_process_preserve_original_and_science_store(solar, private_uploads):
    client, url, originals, processing = solar
    first, second, third, _ = originals
    approved_path, corrected_path, rejected_path = [review_path(url, row) for row in originals[:3]]
    official = private_uploads["SPI_STATE_DIR"]
    official.mkdir(parents=True)
    for name in ("documents.json", "results.json", "issues.json", "publication_scopes.json"):
        (official / name).write_bytes(b"[]\n")
    official_before = snapshot(official)
    processing_before = snapshot(processing.root)
    pdf_before = client.get(f"{url}/file", headers=TOKEN).content

    pending = read(client, approved_path)
    assert pending["status"] == "pending" and pending["revision"] == 0 and not pending["applicable"]
    assert read(client, f"{approved_path}/history") == read(client, f"{url}/reviewed-results") == []
    decision = write(client, approved_path, payload(pending, reviewer_label="Operator 1"))
    assert decision["status"] == "approved" and decision["original_result"] == first

    pending = read(client, corrected_path)
    write(client, corrected_path, payload(pending, "approve_correction"), 409, "review_proposal_required")
    correction = {"raw": {"value": 0, "unit": None}, "normalized": {"value": None, "unit": None}}
    proposed = write(client, corrected_path, payload(pending, "propose_correction", correction=correction,
                                                     reason="Сверено исходное значение. Единицу установить нельзя."))
    assert proposed["status"] == "correction_proposed" and proposed["revision"] == 1
    assert proposed["original_result"] == second
    assert proposed["decision"]["reviewed_result"]["raw"] == correction["raw"]
    assert proposed["decision"]["reviewed_result"]["context"]["unit_status"] == "unconfirmed"
    assert [r["original_result"] for r in read(client, f"{url}/reviewed-results")] == [first]
    fixed = write(client, corrected_path, payload(proposed, "approve_correction"))
    assert fixed["status"] == "approved_corrected" and fixed["revision"] == 2
    assert fixed["decision"]["reviewed_result"]["extraction_status"] == second["extraction_status"]
    assert fixed["decision"]["reviewed_result"]["source"] == second["source"]

    rejected = write(client, rejected_path, payload(read(client, rejected_path), "reject", reason="Другой прибор"))
    assert rejected["status"] == "rejected" and rejected["original_result"] == third
    assert len(read(client, f"{url}/reviews")["reviews"]) == 4
    verified = read(client, f"{url}/reviewed-results")
    assert [r["original_result"] for r in verified] == [first, second]
    assert verified[1]["reviewed_result"]["raw"] == correction["raw"]

    reconsidered = write(client, rejected_path, payload(rejected, reason="Прибор уточнён"))
    assert reconsidered["status"] == "approved" and reconsidered["revision"] == 2
    write(client, corrected_path, payload(fixed, "reject", reason="Требуется повторная сверка"))
    history_url = f"{corrected_path}/history"
    history = read(client, history_url)
    assert [r["action"] for r in history] == ["propose_correction", "approve_correction", "reject"]
    assert [r["previous_revision"] for r in history] == [0, 1, 2]
    assert [r["revision"] for r in history] == [1, 2, 3]
    assert all(event["original_result"] == second for event in history)
    assert history[2]["previous_decision"] == history[1]["decision"]
    assert history[0]["correction"]["raw"] == correction["raw"]
    assert all(event["decision"]["reviewed_at"].endswith("Z") for event in history)
    restarted = subprocess.run([sys.executable, "-c", """
import json, os
from fastapi.testclient import TestClient
from spi.api import app
response = TestClient(app).get(os.environ['SPI_TEST_HISTORY'], headers={'Authorization': 'Bearer ' + os.environ['SPI_UPLOAD_TOKEN']})
response.raise_for_status()
print(json.dumps(response.json(), ensure_ascii=True))
"""], env={**os.environ, "SPI_TEST_HISTORY": history_url}, check=True, capture_output=True, text=True, timeout=20)
    assert json.loads(restarted.stdout) == history
    assert snapshot(official) == official_before
    after = snapshot(processing.root)
    assert after.pop(Path("reviews.sqlite3"))
    assert after == processing_before
    assert get(client, url, "results") == originals
    assert client.get(f"{url}/file", headers=TOKEN).content == pdf_before


def test_authorization_isolation_and_strict_payload(solar, monkeypatch):
    client, url, rows, processing = solar
    path = review_path(url, rows[0])
    initial = read(client, path)
    body = payload(initial)
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        for endpoint in (path, f"{path}/history", f"{url}/reviews", f"{url}/reviewed-results"):
            unauthorized = client.get(endpoint, headers=headers)
            assert unauthorized.status_code == 401 and unauthorized.headers["cache-control"] == "no-store"
        assert client.post(path, json=body, headers=headers).status_code == 401

    missing = f"/uploads/{'0' * 32}/reviews"
    assert client.get(missing, headers=TOKEN).status_code == 404
    invalid = client.get("/uploads/bad/reviews", headers=TOKEN)
    assert invalid.status_code == 422 and invalid.headers["cache-control"] == "no-store"
    other = upload(client, client.get(f"{url}/file", headers=TOKEN).content)
    start(client, other)
    assert worker().run_one()
    foreign = review_path(other, rows[0])
    assert client.get(foreign, headers=TOKEN).status_code == 404
    assert client.get(f"{foreign}/history", headers=TOKEN).status_code == 404
    write(client, foreign, body, 404, "review_result_not_found")
    for field, value in (("pdf_sha256", "0" * 64), ("source_result_hash", "1" * 64),
                         ("source_job_id", "2" * 32), ("domain", "rag"), ("result_id", "different")):
        write(client, path, {**body, "basis": {**body["basis"], field: value}}, 409, "review_source_changed")

    write(client, path, {**body, "expected_revision": True}, 422, "invalid_review_request")
    write(client, path, {**body, "expected_revision": 9}, 409, "review_revision_conflict")
    write(client, path, payload(initial, "reject"), 422, "invalid_review_request")
    for field in ("document_id", "upload_id", "result_id", "domain", "pdf_sha256", "extraction_metadata"):
        write(client, path, payload(initial, "propose_correction", reason="Synthetic", correction={field: "other"}),
              422, "invalid_review_request")
    write(client, path, payload(initial, "propose_correction", reason="Synthetic",
                                correction={"source": {**rows[0]["source"], "origin": "unknown"}}),
          422, "invalid_review_request")
    assert read(client, f"{path}/history") == []
    monkeypatch.delenv("SPI_UPLOAD_PROCESS_STATE_DIR")
    res = client.get(f"{url}/reviews", headers=TOKEN)
    assert res.status_code == 503 and res.headers["cache-control"] == "no-store"
    assert str(processing.root) not in res.text


def test_concurrent_revisions_and_sqlite_rollback(solar):
    client, url, rows, processing = solar
    path = review_path(url, rows[0])
    body = payload(read(client, path))
    barrier = Barrier(2, timeout=10)

    def submit(label):
        with TestClient(app) as other:
            barrier.wait()
            return other.post(path, headers=TOKEN, json={**body, "reviewer_label": label})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ("one", "two")))
    assert sorted(response.status_code for response in results) == [201, 409]
    assert len(history := read(client, f"{path}/history")) == 1
    write(client, path, body, 409, "review_revision_conflict")

    db_path = processing.root / "reviews.sqlite3"
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("""CREATE TRIGGER review_failure AFTER INSERT ON review_events
            BEGIN SELECT RAISE(FAIL, 'synthetic persistence failure'); END""")
    latest = read(client, path)
    rejected = payload(latest, "reject", reason="Синтетическая проверка")
    write(client, path, rejected, 500, "upload_review_storage_error")
    assert read(client, path) == latest
    assert read(client, f"{path}/history") == history
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("DROP TRIGGER review_failure")
    assert write(client, path, rejected)["revision"] == 2


def test_source_page_normalization_warnings_and_ocr(solar):
    client, url, rows, processing = solar
    first = rows[0]
    path = review_path(url, first)
    checkpoint = processing.checkpoint()
    issue = {"issue_id": "synthetic-warning", "domain": "solar", "rule_id": "synthetic",
             "reason": "Проверьте условия измерения", "result_ids": [first["result_id"]]}
    checkpoint["science_issues"].append(issue)
    processing.save_batch_document(checkpoint)
    initial = read(client, path)
    assert initial["ocr_pages"] == [2] and initial["issues"][0]["issue_id"] == issue["issue_id"]
    write(client, path, payload(initial), 422, "review_warnings_unacknowledged")
    write(client, path, payload(initial, "propose_correction", reason="Synthetic",
                                correction={"source": {**first["source"], "page": 4},
                                            "normalized": {"value": None, "unit": None}}), 422, "review_page_outside_pdf")
    write(client, path, payload(initial, "propose_correction", reason="Synthetic",
                                correction={"normalized": {"value": "10000", "unit": "V"}}),
          422, "inconsistent_normalization")
    write(client, path, payload(initial, "propose_correction", reason="Synthetic",
                                correction={"raw": {"value": "100", "unit": None},
                                            "normalized": {"value": "100", "unit": "mystery"}}),
          422, "normalization_evidence_required")
    proposed = write(client, path, payload(initial, "propose_correction", reason="Значение видно на изображении",
                                           correction={"source": {**first["source"], "page": 2, "figure": "Fig. 2"},
                                                       "normalized": {"value": None, "unit": None}}))
    write(client, path, payload(proposed, "approve_correction", warnings_acknowledged=True),
          422, "review_visual_evidence_required")
    write(client, path, payload(proposed, "approve_correction", evidence_mode="visual", warnings_acknowledged=True),
          422, "invalid_review_request")
    approved = write(client, path, payload(proposed, "approve_correction", evidence_mode="visual",
                                           reason="Проверены подпись рисунка и прибор", warnings_acknowledged=True))
    assert approved["status"] == "approved_corrected"
    history = read(client, f"{path}/history")
    assert history[-1]["ocr_pages"] == [2]
    assert history[-1]["issues"] == approved["issues"] == get(client, url, "issues")
    assert read(client, f"{url}/reviewed-results")[0]["issues"] == approved["issues"]


def test_rag_cited_provenance_and_independent_cutoffs(solar):
    client, _, _, _ = solar
    # Start with a processed PDF and a synthetic RAG checkpoint whose evidence uses that PDF.
    url = upload(client, pdf_bytes(), domain="rag")
    job = start(client, url)
    assert worker().run_one()
    uploads = UploadStore.from_env()
    processing = UploadProcessingStore(uploads.processing, uploads.get(job["upload_id"]))
    row = ResultRecord(
        result_id=f"{processing.document_id}:synthetic", document_id=processing.document_id, domain="rag",
        metric={"name": "recall", "k": 3}, raw={"value": "0.21", "unit": "fraction"},
        normalized={"value": "0.21", "unit": "fraction"}, source={"page": 1, "fragment": "Synthetic upload PDF",
        "origin": "cited", "cited_publication_id": "RAG-1"}, context={"top_k": 10},
        extraction_metadata={"model_id": "synthetic", "method": "pymupdf_verified_table",
                             "pdf_sha256": processing.upload.sha256, "schema_version": "test", "profile": "test"},
    )
    processing.write("results.json", [row.model_dump(mode="json")])
    path = review_path(url, row.model_dump())
    initial = read(client, path)
    write(client, path, payload(initial, "propose_correction", reason="Synthetic",
                                correction={"source": {**initial["original_result"]["source"], "origin": "primary"}}),
          422, "invalid_review_request")
    proposed = write(client, path, payload(initial, "propose_correction", reason="Retrieval setting checked",
                                           correction={"context": {"top_k": 25}}))
    corrected = proposed["decision"]["reviewed_result"]
    assert corrected["metric"]["k"] == 3 and corrected["context"]["top_k"] == 25
    assert corrected["source"] == initial["original_result"]["source"]
    assert write(client, path, payload(proposed, "approve_correction"))["status"] == "approved_corrected"


def test_pdf_integrity_and_source_hash(solar):
    client, url, rows, processing = solar
    path = review_path(url, rows[0])
    approved = write(client, path, payload(read(client, path)))
    original = UploadStore.from_env().file(processing.upload)
    previous = original.read_bytes()
    original.write_bytes(previous[:-1] + b"!")
    stale = read(client, path)
    assert stale["status"] == "stale" and stale["pdf_status"] == "mismatch"
    write(client, path, payload(approved), 409, "review_pdf_changed")
    assert read(client, f"{url}/reviewed-results") == []
    original.unlink()
    assert read(client, path)["pdf_status"] == "missing"
    assert len(read(client, f"{path}/history")) == 1
    original.write_bytes(previous)

    checkpoint = processing.checkpoint()
    row = next(row for fragment in checkpoint["fragments"].values() for row in fragment["results"]
               if row["result_id"] == rows[0]["result_id"])
    digest = approved["basis"]["source_result_hash"]
    ordered = ResultRecord.model_validate(rows[0])
    assert source_result_hash(ordered.model_copy(update={"context": dict(reversed(list(ordered.context.items())))})) == digest
    row["context"]["device_label"] = "Changed scientific basis"
    processing.save_batch_document(checkpoint)
    changed = read(client, path)
    assert changed["status"] == "stale" and changed["basis"]["source_job_id"] == approved["basis"]["source_job_id"]
    assert changed["basis"]["source_result_hash"] != digest
    write(client, path, payload(approved), 409, "review_source_changed")
    for fragment in checkpoint["fragments"].values():
        fragment["results"] = [row for row in fragment["results"] if row["result_id"] != rows[0]["result_id"]]
    processing.save_batch_document(checkpoint)
    vanished = read(client, path)
    assert vanished["status"] == "stale" and vanished["original_result"] is None
    assert read(client, f"{path}/history")[0]["original_result"] == rows[0]
    assert read(client, f"{url}/reviewed-results") == []


def test_retry_serializes_with_review_and_invalidates_even_identical_source(private_uploads, fake_model, tmp_path, monkeypatch):
    client = TestClient(app)
    url = upload(client, make_pdf(tmp_path / "D001.pdf").read_bytes())
    first = start(client, url)
    assert worker(model_enabled=True, max_requests=2).run_one()
    original = get(client, url, "results")[0]
    path = review_path(url, original)
    initial = read(client, path)
    entered, release, enqueue_entered = Event(), Event(), Event()
    decide, enqueue = UploadReviews._decide, UploadQueue.enqueue

    def wait_before_append(self, *args):
        entered.set()
        assert release.wait(timeout=10)
        return decide(self, *args)

    def observe_enqueue(self, *args, **kwargs):
        enqueue_entered.set()
        return enqueue(self, *args, **kwargs)

    monkeypatch.setattr(UploadReviews, "_decide", wait_before_append)
    monkeypatch.setattr(UploadQueue, "enqueue", observe_enqueue)
    with ThreadPoolExecutor(max_workers=2) as pool:
        approval = pool.submit(write, client, path, payload(initial, warnings_acknowledged=True))
        try:
            assert entered.wait(timeout=10)
            retry = pool.submit(start, TestClient(app), url)
            assert enqueue_entered.wait(timeout=10)
            assert not retry.done()
        finally:
            release.set()
        approved = approval.result(timeout=10)
        second = retry.result(timeout=10)
    assert second["job_id"] != first["job_id"]
    assert read(client, path)["status"] == "stale"
    write(client, path, payload(approved), 409, "review_processing_active")
    queue = UploadQueue(UploadStore.from_env().processing)
    with queue.claim() as running:
        assert running.processing.status == "processing"
        write(client, path, payload(approved), 409, "review_processing_active")
        assert read(client, f"{url}/reviewed-results") == []
    assert worker(model_enabled=True).run_one()
    fresh = read(client, path)
    assert fresh["original_result"] == original
    assert fresh["basis"]["source_result_hash"] == approved["basis"]["source_result_hash"]
    assert fresh["status"] == "stale" and fresh["revision"] == 1
    assert read(client, f"{url}/reviewed-results") == []
    write(client, path, payload(approved), 409, "review_source_changed")
    assert write(client, path, payload(fresh, warnings_acknowledged=True))["revision"] == 2
