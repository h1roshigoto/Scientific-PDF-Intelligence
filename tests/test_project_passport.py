from datetime import datetime, timedelta

import pymupdf
import pytest
from fastapi.testclient import TestClient

from spi.api import app
from spi.models import PublicationScope
from spi.storage import Store


ENDPOINT = "/export/project-passport.pdf"
RUN = "e2e-dev"
SOLAR_SHA = "a" * 64
RAG_SHA = "b" * 64


@pytest.fixture
def api(tmp_path, monkeypatch):
    store = Store(tmp_path / "state")
    monkeypatch.setenv("SPI_STATE_DIR", str(store.root))
    monkeypatch.setenv("SPI_RUN_ID", RUN)
    return TestClient(app), store


def document(domain, publication, digest, title):
    return {
        "document_id": f"{domain}:{publication}", "publication_id": publication, "domain": domain,
        "sha256": digest, "title": title, "authors": ["И. Иванов"], "year": 2024,
        "doi": "10.1234/paper", "filename": f"{publication}.pdf", "page_count": 3,
        "processing_status": "partial", "pages_needing_ocr": [2],
    }


def result(result_id, doc, *, status="verified", fragment="Фрагмент α ≥ 2 µA·cm⁻²"):
    rag = doc["domain"] == "rag"
    return {
        "result_id": result_id, "document_id": doc["document_id"], "domain": doc["domain"],
        "metric": {"name": "Recall" if rag else "PCE", "k": 5 if rag else None, "variant": "macro"},
        "raw": {"value": "8.75", "unit": "percent"},
        "normalized": {"value": 0.0875 if status == "verified" else None, "unit": "fraction"},
        "source": {"page": 1, "fragment": fragment, "section": "Results", "table": "Table 7", "row": "Ref.",
                   "column": "Metric", "figure": "Fig. 2", "origin": "cited", "cited_publication_id": "Ref123"},
        "context": (
            {"retrieval_top_k": 20, "dataset_name": "QA-Dataset", "task_type": "QA", "method": "RA-CoT",
             "generator_model": "GeneratorA", "evaluation_protocol": "Held-out examples", "experiment": "Exp1"}
            if rag else {"material_name_raw": "Perovskite", "device_label": "Pristine",
                         "layers": [{"role": "absorber", "material_name_raw": "MAPbI3"}],
                         "measurement_type": "JV", "scan_direction": "reverse", "aggregation": "mean"}
        ),
        "extraction_status": status, "note": "Техническая заметка",
        "extraction_metadata": {"model_id": "local", "method": "pymupdf_verified_table",
                                "pdf_sha256": doc["sha256"], "schema_version": "test", "profile": "test"},
    }


def issue(issue_id, domain, results, refs):
    return {
        "issue_id": issue_id, "domain": domain, "rule_id": "table_figure_difference", "severity": "warning",
        "reason": "Измерения различаются", "observed": "8.75 percent", "expected": "9.25 percent",
        "result_ids": results, "evidence_refs": refs,
    }


@pytest.fixture
def populated(api):
    _, store = api
    solar = document("solar", "D001", SOLAR_SHA, "Солнечная статья")
    excluded = document("solar", "D002", "c" * 64, "Публикация вне области")
    rag = document("rag", "D001", RAG_SHA, "Статья о поиске")
    store.save_document(solar, [
        {"page": 1, "read_status": "text", "ocr_status": "not_needed"},
        {"page": 2, "read_status": "ocr_required", "ocr_status": "pending"},
        {"page": 3, "read_status": "error", "ocr_status": "unknown", "error": "Непрочитанная страница"},
    ])
    for doc in (excluded, rag):
        store.save_document(doc, [])
    store.save_publication_scopes([
        PublicationScope(document_id=solar["document_id"], pdf_sha256=SOLAR_SHA, status="in_scope",
                         reason="Подтверждён эксперимент", source_pages=[1]),
        PublicationScope(document_id=excluded["document_id"], pdf_sha256=excluded["sha256"], status="out_of_scope",
                         reason="Отсутствует первичный эксперимент", source_pages=[1, 3]),
        PublicationScope(document_id=rag["document_id"], pdf_sha256=RAG_SHA, status="needs_review"),
    ])
    store.write("results.json", [
        result("solar-metric-1", solar),
        result("solar-metric-2", solar, status="needs_review"),
        result("excluded-row", excluded, status="extracted"),
    ])
    store.save_batch_document({
        "document_id": rag["document_id"], "sha256": RAG_SHA, "status": "partial",
        "pending_units": 3, "fragments": {"unit": {"results": [result("rag-metric-1", rag)]}},
        "science_issues": [issue("rag-checkpoint-issue", "rag", [], [])],
    })
    store.write("issues.json", [
        issue("solar-check", "solar", ["solar-metric-1", "solar-metric-2"],
              [{"document_id": "solar:D001", "page": 1, "fragment": "Доказательство Solar"}]),
        issue("excluded-check", "solar", ["excluded-row"], [{"document_id": "solar:D002", "page": 3}]),
        issue("mixed-check", "solar", ["solar-metric-1", "excluded-row"], []),
        issue("global-check", "solar", [], []),
    ])
    return api


def get(client, **params):
    return client.get(ENDPOINT, params={"run_id": RUN} | params)


def read(response):
    assert response.status_code == 200, response.text
    assert response.headers["Content-Type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")
    assert response.content.rstrip().endswith(b"%%EOF")
    with pymupdf.open(stream=response.content, filetype="pdf") as pdf:
        assert all(abs(page.rect.width - 595) < 1 and abs(page.rect.height - 842) < 1 for page in pdf)
        return "\n".join(page.get_text() for page in pdf), len(pdf)


def test_complete_passport_matches_store(populated):
    client, store = populated
    response = get(client, domain="all")
    text, count = read(response)
    assert "Паспорт проекта" in text
    assert "Покрытие Solar" in text and "Покрытие RAG" in text
    assert "Солнечная статья" in text and "И. Иванов" in text
    assert "Фрагмент α ≥ 2 µA·cm⁻²" in text
    assert "Отсутствует первичный эксперимент" in text
    assert "Непрочитанная страница" in text
    assert "В официальной области" in text and "Только для аудита и проверки" in text
    assert "Страницы решения (PDF)\n[1, 3]" in text
    for row in store.results():
        assert row.result_id in text
        assert f"raw.value\n{row.raw.value}" in text
        assert f"raw.unit\n{row.raw.unit}" in text
        assert f"normalized.unit\n{row.normalized.unit}" in text
        assert f"extraction_status\n{row.extraction_status}" in text
    assert "normalized.value\n0.0875" in text and "normalized.value\nнет данных" in text
    assert "metric_k\n5" in text and "context.retrieval_top_k\n20" in text
    assert "source.cited_publication_id\nRef123" in text
    assert "source.page (PDF)\n1" in text
    assert "source.origin\ncited" in text
    assert "context.layers" in text and "MAPbI3" in text
    assert "context.scan_direction\nreverse" in text and "context.aggregation\nmean" in text
    for row in store.issues():
        assert row.issue_id in text
        assert row.reason in text
    assert "Доказательство Solar" in text
    assert "observed\n8.75 percent" in text and "expected\n9.25 percent" in text
    assert f"Страница {count} / {count}" in text
    assert response.headers["Content-Disposition"].endswith('_e2e-dev_all.pdf"')
    timestamp = response.headers["X-SPI-Generated-At"]
    assert datetime.fromisoformat(timestamp).utcoffset() == timedelta(0)
    assert timestamp in text


@pytest.mark.parametrize(("domain", "included", "excluded"), [
    ("solar", ("solar-metric-1", "excluded-row", "mixed-check", "global-check"), ("rag-metric-1", "Статья о поиске")),
    ("rag", ("rag-metric-1", "rag-checkpoint-issue"), ("solar-metric-1", "Солнечная статья", "solar-check")),
])
def test_domain_filter(populated, domain, included, excluded):
    text, _ = read(get(populated[0], domain=domain))
    assert all(value in text for value in included)
    assert all(value not in text for value in excluded)


def test_publication_filter_does_not_leak_other_records_or_cross_document_issues(populated):
    client, _ = populated
    response = get(client, domain="solar", publication_id="D001")
    text, _ = read(response)
    for value in ("solar:D001", "solar-metric-1", "solar-metric-2", "solar-check"):
        assert value in text
    for value in ("rag:D001", "rag-metric-1", "solar:D002", "excluded-row", "excluded-check",
                  "mixed-check", "global-check", "rag-checkpoint-issue"):
        assert value not in text
    assert response.headers["Content-Disposition"].endswith('_e2e-dev_solar_D001.pdf"')
    assert "rag-checkpoint-issue" in read(get(client, domain="rag", publication_id="D001"))[0]
    assert "rag-metric-1" in read(get(client, publication_id="rag:D001"))[0]
    assert get(client, publication_id="D001").status_code == 409
    assert get(client, domain="rag", publication_id="D002").status_code == 404


def test_empty_store_and_run_binding(api, monkeypatch):
    client, _ = api
    text, _ = read(get(client))
    assert "Документы\n0" in text and "Результаты\n0" in text and "Замечания\n0" in text
    assert "Зарегистрированных публикаций нет." in text
    assert get(client, run_id="other").status_code == 404
    assert client.get(ENDPOINT).status_code == 422
    assert get(client, domain="different").status_code == 422
    monkeypatch.delenv("SPI_RUN_ID")
    assert get(client).status_code == 503
    assert client.get("/health").status_code == 200


def test_paginated_report_keeps_all_records_and_long_fragments_without_writing(api):
    client, store = api
    solar = document("solar", "D001", SOLAR_SHA, "Solar")
    store.save_document(solar, [])
    fragment = "Начало\n" + ("длинное доказательство α × 10 " * 240) + "\n" + "X" * 600 + "КонецФрагмента\ue000"
    store.write("results.json", [result(f"result-{index:03}", solar, fragment=fragment if index == 0 else "Evidence")
                                 for index in range(105)])

    def files():
        return {file.relative_to(store.root): file.read_bytes() for file in store.root.rglob("*") if file.is_file()}

    before = files()
    response = get(client)
    text, pages = read(response)
    assert pages > 10
    assert all(f"result-{index:03}" in text for index in range(105))
    assert text.count("доказательство") == 240
    assert text.count("X") == 600
    assert "Начало" in text and "КонецФрагмента" in text
    assert r"\ue000" in text
    assert "(продолжение)" in text
    repeated = get(client)
    assert read(repeated)[0].replace(repeated.headers["X-SPI-Generated-At"], "") == (
        text.replace(response.headers["X-SPI-Generated-At"], "")
    )
    assert files() == before


def test_invalid_store_returns_error_instead_of_partial_pdf(api):
    client, store = api
    store.write("results.json", [{"result_id": "broken"}])
    response = get(client)
    assert response.status_code == 500
    assert response.json()["detail"] == "Could not generate a complete project passport"
