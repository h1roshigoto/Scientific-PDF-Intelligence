import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier
from time import monotonic, sleep

import pymupdf
import pytest
from fastapi.testclient import TestClient

from spi import upload_worker
from spi.api import app
from spi.gigachat import Completion, GigaChatError
from spi.upload_store import UploadStore
from test_batch import make_pdf
from test_batch_table_proposals import rag_multilevel_pdf
from test_uploads import TOKEN, directory_alias, pdf_bytes, post, private_uploads  # noqa: F401


def upload(client, content, domain="solar"):
    response = post(client, content, filename="D001.pdf", domain=domain)
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "stored"
    return f"/uploads/{response.json()['upload_id']}"


def start(client, url):
    response = client.post(f"{url}/process", headers=TOKEN)
    assert response.status_code == 202, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def get(client, url, endpoint="processing"):
    response = client.get(f"{url}/{endpoint}", headers=TOKEN)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def worker(**options):
    return upload_worker.UploadWorker(UploadStore.from_env(), **options)


def test_queue_is_explicit_and_two_concurrent_posts_share_job(private_uploads):
    client = TestClient(app)
    url = upload(client, pdf_bytes())
    assert not private_uploads["SPI_UPLOAD_PROCESS_STATE_DIR"].exists()
    assert get(client, url)["status"] == "not_started"
    assert get(client, url, "results") == get(client, url, "issues") == []
    barrier = Barrier(2, timeout=10)

    def send(_):
        with TestClient(app) as other:
            barrier.wait()
            return start(other, url)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(send, range(2))
    assert first == second == start(client, url)
    assert first["status"] == "queued"
    assert first["job_id"] == get(TestClient(app), url)["job_id"]
    assert get(client, url)["counts"] is None


def test_local_results_ocr_evidence_and_official_store_isolation(private_uploads, tmp_path, monkeypatch):
    monkeypatch.setenv("SPI_UPLOAD_MAX_BYTES", "65536")
    official = private_uploads["SPI_STATE_DIR"]
    official.mkdir(parents=True)
    marker = official / "documents.json"
    marker.write_bytes(b'[{"document_id":"solar:D001"}]')
    with pymupdf.open(make_pdf(tmp_path / "D001.pdf", scan=True)) as document:
        contents = document.tobytes(deflate=True)
    rag = rag_multilevel_pdf(tmp_path / "rag.pdf", ("Gemini-2.5", "Flash"), ("Mistral v3-8B Instruct",))
    client = TestClient(app)
    urls = [upload(client, contents), upload(client, contents), upload(client, rag.read_bytes(), "rag")]
    jobs = [start(client, url) for url in urls]
    for job in jobs:
        assert job["status"] == "queued"
        assert worker().run_one()
    assert not worker().run_one()

    for url, job in zip(urls[:2], jobs[:2], strict=True):
        state = get(client, url)
        assert state["job_id"] == job["job_id"]
        assert state["status"] == "needs_review"
        assert state["mode"] == "local"
        assert state["pages_needing_ocr"] == [2]
        assert [(p["read_status"], p["ocr_status"]) for p in state["pages"]] == [
            ("text", "not_needed"), ("ocr_required", "pending"), ("empty", "not_needed"),
        ]
        rows = get(client, url, "results")
        assert state["counts"]["results"] == len(rows) == 4
        assert state["human_review_required"]
        for row in rows:
            assert row["document_id"] == f"solar:{job['upload_id']}"
            assert row["source"]["page"] == 1
            assert row["source"]["origin"] == "primary"
            assert row["source"]["table"] == "Table 1"
            assert row["source"]["fragment"] in {"1200", "15.5", "15.6"}
            assert row["context"]["evidence_blocks"]
            assert row["extraction_status"] == "extracted"
        assert client.get(f"{url}/file", headers=TOKEN).content == contents
        assert start(client, url)["job_id"] == job["job_id"]
        assert get(client, url, "results") == rows
        saved = private_uploads["SPI_UPLOAD_PROCESS_STATE_DIR"] / job["upload_id"] / "documents.json"
        assert json.loads(saved.read_text(encoding="utf-8"))[0]["original_filename"] == "D001.pdf"
    rag_rows = get(client, urls[2], "results")
    assert len(rag_rows) == 16
    assert {r["document_id"] for r in rag_rows} == {f"rag:{jobs[2]['upload_id']}"}
    assert {r["metric"]["name"] for r in rag_rows} == {"EM", "F1"}
    assert all(r["source"]["table"] == "Table 1" and r["context"]["header_path"] for r in rag_rows)
    assert get(client, urls[2])["pages_needing_ocr"] == []
    assert client.get("/documents").json() == [{"document_id": "solar:D001"}]
    assert client.get("/results").json() == client.get("/issues").json() == []
    assert list(official.iterdir()) == [marker]
    assert marker.read_bytes() == b'[{"document_id":"solar:D001"}]'


def test_empty_result_and_layout_issue_are_distinct(private_uploads):
    client = TestClient(app)
    empty = upload(client, pdf_bytes())
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((45, 70), "Table 1. Our measurements")
        unresolved = upload(client, pdf.tobytes())
    for url in (empty, unresolved):
        start(client, url)
        assert worker().run_one()
        assert get(client, url, "results") == []
    assert get(client, empty)["status"] == "needs_review"
    assert get(client, empty)["coverage"] == "no_supported_metrics"
    state = get(client, unresolved)
    assert state["status"] == "needs_review"
    assert state["counts"]["unresolved"] == 1
    issues = get(client, unresolved, "issues")
    assert issues[0]["rule_id"] == "table_layout_unresolved"
    assert issues[0]["evidence_refs"][0]["page"] == 1


def test_enabled_model_without_credentials_uses_local(private_uploads, tmp_path, monkeypatch):
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "")
    client = TestClient(app)
    url = upload(client, make_pdf(tmp_path / "D001.pdf").read_bytes())
    start(client, url)
    assert worker(model_enabled=True).run_one()
    assert get(client, url)["diagnostic"] == "model_credentials_missing_local"
    assert len(get(client, url, "results")) == 4


@pytest.fixture
def fake_model(monkeypatch):
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "synthetic")
    calls = []

    class FakeClient:
        fail_at = None

        def __init__(self, settings, *, max_completion_retries):
            assert settings.credentials.get_secret_value() == "synthetic"
            assert max_completion_retries == 0

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def complete_json(self, prompt, *, schema):
            assert schema["type"] == "object"
            calls.append(prompt)
            if len(calls) == self.fail_at:
                raise GigaChatError("completion", "read_timeout")
            candidate = prompt.partition("return it exactly.\n")[2].splitlines()
            results = [json.loads(candidate[0])] if candidate else []
            return Completion(model_id="GigaChat-3-Ultra", content=json.dumps({"results": results}))

    monkeypatch.setattr(upload_worker, "GigaChatClient", FakeClient)
    return FakeClient, calls


@pytest.mark.parametrize("failed_by", ["timeout", "budget"])
def test_model_retry_continues_saved_fragments_without_duplicate_results(
    private_uploads, fake_model, failed_by, tmp_path,
):
    fake, calls = fake_model
    if failed_by == "timeout":
        fake.fail_at = 2
    client = TestClient(app)
    url = upload(client, make_pdf(tmp_path / "D001.pdf").read_bytes())
    assert not calls
    first_attempt = start(client, url)
    assert not calls
    assert worker(model_enabled=True, max_requests=2 if failed_by == "budget" else 8).run_one()
    state = get(client, url)
    assert state["status"] == "failed" and state["retry_allowed"]
    assert state["diagnostic"] == ("completion:read_timeout" if failed_by == "timeout" else "request_budget_exhausted")
    assert state["counts"]["pending_fragments"] > 0
    saved = {row["result_id"] for row in get(client, url, "results")}
    assert saved
    if failed_by == "budget":
        assert len(calls) == 2
    first_call = calls[0]
    retry = start(client, url)
    assert retry["job_id"] != first_attempt["job_id"]
    assert worker(model_enabled=True).run_one()
    state = get(client, url)
    assert state["status"] == "completed"
    assert state["mode"] == "gigachat"
    rows = get(client, url, "results")
    assert len(rows) == len({row["result_id"] for row in rows}) == 4
    assert saved <= {row["result_id"] for row in rows}
    assert calls.count(first_call) == 1
    assert all(row["extraction_status"] == "extracted" for row in rows)


def test_integrity_failure_can_retry_after_original_restored(private_uploads):
    client = TestClient(app)
    content = pdf_bytes()
    url = upload(client, content)
    first = start(client, url)
    path = private_uploads["SPI_UPLOAD_DIR"] / f"{first['upload_id']}.pdf"
    path.write_bytes(content[:-1] + b"!")
    assert worker().run_one()
    assert get(client, url)["diagnostic"] == "original_sha256_mismatch"
    assert get(client, url, "results") == []
    assert not (private_uploads["SPI_UPLOAD_PROCESS_STATE_DIR"] / first["upload_id"]).exists()
    path.write_bytes(content)
    assert start(client, url)["job_id"] != first["job_id"]
    assert worker().run_one()
    assert get(client, url)["coverage"] == "no_supported_metrics"


def test_worker_rejects_catalog_domain_changed_after_enqueue(private_uploads):
    client = TestClient(app)
    url = upload(client, pdf_bytes())
    job = start(client, url)
    catalog = private_uploads["SPI_UPLOAD_STATE_DIR"] / "uploads.sqlite3"
    with closing(sqlite3.connect(catalog)) as db, db:
        (raw,) = db.execute("SELECT metadata FROM uploads WHERE upload_id = ?", (job["upload_id"],)).fetchone()
        metadata = json.loads(raw)
        metadata["domain"] = "rag"
        db.execute("UPDATE uploads SET metadata = ? WHERE upload_id = ?", (json.dumps(metadata), job["upload_id"]))
    assert worker().run_one()
    assert get(client, url)["diagnostic"] == "upload_identity_mismatch"


def test_worker_rejects_linked_document_processing_directory(private_uploads):
    client = TestClient(app)
    url = upload(client, pdf_bytes())
    job = start(client, url)
    official = private_uploads["SPI_STATE_DIR"]
    official.mkdir(parents=True)
    directory_alias(private_uploads["SPI_UPLOAD_PROCESS_STATE_DIR"] / job["upload_id"], official)
    assert worker().run_one()
    assert get(client, url)["status"] == "failed"
    assert get(client, url, "results") == []
    assert list(official.iterdir()) == []


def test_hard_killed_worker_resumes_from_real_checkpoint(private_uploads, tmp_path):
    client = TestClient(app)
    url = upload(client, make_pdf(tmp_path / "D001.pdf").read_bytes())
    queued = start(client, url)
    signal = tmp_path / "checkpoint-ready"
    script = """
import os
from pathlib import Path
from time import sleep
from spi import batch, upload_worker

save = batch._checkpoint
def pause(store, record):
    save(store, record)
    if record.get('fragments'):
        Path(os.environ['SPI_TEST_CHECKPOINT_READY']).touch()
        sleep(60)

batch._checkpoint = pause
upload_worker.main()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, "--once"], env={**os.environ, "SPI_TEST_CHECKPOINT_READY": str(signal)},
    )
    try:
        deadline = monotonic() + 15
        while not signal.exists() and child.poll() is None and monotonic() < deadline:
            sleep(0.05)
        assert signal.exists()
        running = get(client, url)
        assert running["status"] == "processing"
        assert start(client, url)["job_id"] == queued["job_id"]
        subprocess.run([sys.executable, "-m", "spi.upload_worker", "--once"], check=True, timeout=15)
        assert get(client, url) == running
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
    path = private_uploads["SPI_UPLOAD_PROCESS_STATE_DIR"] / queued["upload_id"]
    checkpoint = path / "batch" / "solar" / f"{queued['upload_id']}.json"
    fragment_ids = set(json.loads(checkpoint.read_text(encoding="utf-8"))["fragments"])
    assert fragment_ids
    subprocess.run([sys.executable, "-m", "spi.upload_worker", "--once"], check=True, timeout=15)
    finished = get(TestClient(app), url)
    assert finished["job_id"] == queued["job_id"]
    assert finished["recoveries"] == 1
    assert finished["status"] == "completed"
    assert len(get(client, url, "results")) == 4
    assert fragment_ids <= set(json.loads(checkpoint.read_text(encoding="utf-8"))["fragments"])

def test_updated_extractor_preserves_previous_checkpoint(private_uploads, tmp_path):
    client = TestClient(app)
    url = upload(client, make_pdf(tmp_path / "D001.pdf").read_bytes())
    first = start(client, url)
    assert worker().run_one()
    root = private_uploads["SPI_UPLOAD_PROCESS_STATE_DIR"] / first["upload_id"]
    checkpoint = next((root / "batch" / "solar").glob("*.json"))
    previous = json.loads(checkpoint.read_text(encoding="utf-8"))
    previous["revision"] += "-old"
    checkpoint.write_text(json.dumps(previous), encoding="utf-8")
    original_results = get(client, url, "results")
    assert get(client, url)["reanalysis_available"]
    assert start(client, url)["job_id"] != first["job_id"]
    assert worker().run_one()
    assert get(client, url, "results") == original_results
    assert not get(client, url)["reanalysis_available"]
    history, = (root / "batch-history").glob("*.json")
    assert json.loads(history.read_text(encoding="utf-8")) == previous


def test_demo_keeps_local_results_when_model_is_unavailable(private_uploads, tmp_path, monkeypatch):
    monkeypatch.setenv("SPI_DEMO_MODEL_FALLBACK", "true")
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "synthetic")

    def unavailable(*args, **kwargs):
        raise GigaChatError("oauth", "unavailable")

    monkeypatch.setattr(upload_worker, "GigaChatClient", unavailable)
    client = TestClient(app)
    path = make_pdf(tmp_path / "D001.pdf")
    with pymupdf.open(path) as pdf:
        pdf[0].insert_text((45, 280), "Our measured power conversion efficiency was 15.5% in this experiment.")
        content = pdf.tobytes()
    url = upload(client, content)
    start(client, url)
    assert worker(model_enabled=True).run_one()
    state = get(client, url)
    assert state["status"] == "completed"
    assert state["diagnostic"] == "model_enrichment_incomplete"
    assert len(get(client, url, "results")) == 4
