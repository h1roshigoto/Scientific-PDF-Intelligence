import json
from hashlib import sha256

import pymupdf
import pytest

from spi.batch import discover, preview, report, run_batch
from spi.batch_checks import check_pce
from spi.batch_evidence import offline_proposals, verify
from spi.batch_pdf import Cell, Fragment
from spi.gigachat import Completion
from spi.gigachat_evidence import Block, EvidenceError
from spi.models import PublicationScope
from spi.storage import Store


def make_pdf(path, *, scan=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((45, 70), "Table 1. Our measurements under simulated AM 1.5G of 100 mW/cm2")
        for y, cells in [(90, ("Sample", "Voc (mV)", "PCE (%)")),
                         (107, ("A1", "1200", "15.5")), (124, ("B1", "1200", "15.6"))]:
            for x, value in zip((45, 135, 215), cells):
                # Centre all the numeric and header strings in their own columns.
                offset = pymupdf.get_text_length(value) / 2 if x > 50 else 0
                page.insert_text((x - offset, y), value)
        if scan:
            image = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, 100, 100), 0)
            image.clear_with(240)
            pdf.new_page().insert_image(pymupdf.Rect(10, 10, 300, 350), pixmap=image)
            pdf.new_page()
        pdf.save(path)
    return path


def test_batch_tracks_sha_ocr_idempotence_and_failure(tmp_path):
    solar = make_pdf(tmp_path / "solar" / "D001.pdf", scan=True)
    rag = make_pdf(tmp_path / "rag" / "D001.pdf")
    broken = solar.with_name("D002.pdf")
    broken.write_bytes(b"damaged")
    other = make_pdf(solar.with_name("D003.PDF"))
    store = Store(tmp_path / "state")
    previewed = preview(discover(solar.parent, limit=2), "solar", store)
    assert [d["status"] for d in previewed["documents"]] == ["pending", "pending"]
    assert store.documents() == []
    first = run_batch(store, discover(solar.parent), "solar", local_only=True)
    assert first["coverage"]["documents"] == {"partial": 1, "failed": 1, "processed": 1}
    assert first["documents"][0]["sha256"] == sha256(solar.read_bytes()).hexdigest()
    assert first["documents"][0]["pages_needing_ocr"] == [2]
    assert [p["read_status"] for p in first["documents"][0]["pages"]] == ["text", "ocr_required", "empty"]
    assert first["documents"][1]["errors"]
    assert first["documents"][2]["path"] == str(other)
    again = run_batch(store, discover(solar.parent), "solar", local_only=True)
    assert not again["requests_this_run"]
    assert first["coverage"] == again["coverage"]
    run_batch(store, [rag], "rag", local_only=True)
    assert {"solar:D001", "rag:D001", "solar:D003"} == {d["document_id"] for d in store.documents()}
    solar_results = [r for r in store.results("solar") if r.document_id == "solar:D001" and r.metric.name == "Voc"]
    assert {r.context["device_label"] for r in solar_results} == {"A1", "B1"}
    assert len({r.result_id for r in solar_results}) == 2


def test_interruption_resumes_fragments_and_uses_hash_cache(tmp_path):
    path = make_pdf(tmp_path / "D001.pdf")
    sibling = path.with_name("D999.pdf")
    sibling.write_bytes(path.read_bytes())
    store = Store(tmp_path / "state")
    calls = []

    def complete(prompt, *, schema):
        calls.append(prompt)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return Completion(model_id="GigaChat-3-Ultra", content=json.dumps({"results": []}))

    with pytest.raises(KeyboardInterrupt):
        run_batch(store, [path, sibling], "solar", local_only=False, complete=complete)
    saved = report(store, "solar")
    assert [d["status"] for d in saved["documents"]] == ["running", "pending"]
    first_doc = saved["documents"][0]
    assert len(first_doc["fragments"]) == 1

    resumed = run_batch(store, [path, sibling], "solar", local_only=False, complete=complete)
    assert all(d["status"] == "partial" for d in resumed["documents"])
    assert all(d["pending_units"] == 0 for d in resumed["documents"])
    assert calls.count(calls[0]) == 1
    assert any(event["cache_hit"] for event in resumed["requests_this_run"])


def test_scope_explicit_and_versioned(tmp_path):
    path = make_pdf(tmp_path / "D001.pdf")
    store = Store(tmp_path / "state")
    initial = run_batch(store, [path], "solar", local_only=True)
    assert initial["scope_review_queue"] == ["solar:D001"]
    digest = initial["documents"][0]["sha256"]
    for status in ["out_of_scope", "in_scope", "needs_review"]:
        decision = PublicationScope(document_id="solar:D001", pdf_sha256=digest,
                                    status=status, reason="Document reviewed", source_pages=[1])
        store.save_publication_scopes([decision])
        current = run_batch(store, [path], "solar", local_only=True)
        assert current["documents"][0]["scope"]["status"] == status
        assert bool(current["scope_review_queue"]) == (status == "needs_review")
    with pytest.raises(ValueError, match="does not match"):
        store.save_publication_scopes([decision.model_copy(update={"pdf_sha256": "0"*64})])


def test_verifier_requires_exact_quote_row_column_and_known_unit():
    def b(key, text):
        return Block(key, (0, 0, 1, 1), text)

    caption = b("caption", "Table 1: Our measurements")
    first = Cell(b("a", "1200"), (b("row1", "A1"),), (b("col1", "Voc (mV)"),))
    second = Cell(b("b", "1200"), (b("row2", "B1"),), (b("col2", "Voc (mystery)"),))
    blocks = (caption, first.value, *first.row, *first.headers, second.value, *second.row, *second.headers)
    fragment = Fragment("table", 1, blocks, (first, second), caption, "Table 1")
    p1, p2 = offline_proposals(fragment, "solar")

    def check(proposal):
        return verify(proposal, fragment, "solar", "solar:D001", "a"*64, "local")[0]

    assert check(p1).normalized.value == "1.200"
    assert check(p2).extraction_status == "needs_review"
    assert check(p2).normalized.value is None
    with pytest.raises(EvidenceError, match="quote_mismatch"):
        check(p1.model_copy(update={"quote": "1199"}))
    with pytest.raises(EvidenceError, match="wrong_table_cell"):
        check(p1.model_copy(update={"block": "b", "cell": "b"}))
    with pytest.raises(EvidenceError, match="wrong_table_cell"):
        check(p1.model_copy(update={"headers": ["col2"]}))


def test_rag_metric_k_is_distinct_from_retrieval_top_k():
    def block(key, value):
        return Block(key, (0, 0, 1, 1), value)

    caption = block("caption", "Table 3. Our evaluation\nDataset: Benchmark\nGenerator: Model\nRetrieval top-k: 20")
    row = block("row", "Method")
    header = block("col", "Recall@5 (%)")
    value = block("cell", "72")
    cell = Cell(value, (row,), (header,))
    fragment = Fragment("table", 2, (caption, row, header, value), (cell,), caption, "Table 3")
    (proposal,) = offline_proposals(fragment, "rag")
    result, errors = verify(proposal, fragment, "rag", "rag:D101", "b"*64, "local")
    assert not errors
    assert result.metric.k == 5
    assert result.context["top_k"] == "20"
    assert result.normalized.unit == "%"


@pytest.mark.parametrize(("pce", "pin", "comparable", "status"), [
    ("18.4", "100", True, "pass"), ("19.5", "100", True, "fail"),
    ("18.4", None, True, "not_evaluable"), ("18.4", "100", False, "not_evaluable"),
])
def test_pce(pce, pin, comparable, status):
    check = check_pce({"PCE": pce, "Voc": 1.15, "Jsc": 20, "FF": .80}, pin, comparable=comparable)
    assert check["status"] == status
    if status == "fail":
        assert check["published_pce_percent"] == pce
        assert float(check["calculated_pce_percent"]) == 18.4
