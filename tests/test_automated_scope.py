import json

import pymupdf
import pytest

from spi import automated_scope as auto
from spi.build_scope_sheet import pdf_sha256


SOLAR_1 = "Thin film solar cell devices were fabricated on glass."
SOLAR_2 = "Power conversion efficiency was measured under illumination."
RAG_1 = "The system retrieves external documents for answer generation."
RAG_2 = "Generated answers were evaluated on the question benchmark."


def make_pdf(tmp_path, track="solar", pid="P001"):
    path = tmp_path / "data" / track / "test" / f"{pid}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = (SOLAR_1, SOLAR_2) if track == "solar" else (RAG_1, RAG_2)
    with pymupdf.open() as pdf:
        for line in lines:
            pdf.new_page().insert_text((40, 70), line)
        pdf.save(path)
    digest = pdf_sha256(path)
    key = f"{track}:{pid}"
    row = {
        "document_id": key, "domain": track, "publication_id": pid,
        "pdf_sha256": digest, "pdf_path": path.relative_to(tmp_path).as_posix(),
        "page_count": "2", "title": "Synthetic citation test article",
    }
    doc = {
        "document_id": key, "domain": track, "publication_id": pid,
        "sha256": digest, "page_count": 2, "file_path": str(path),
    }
    return row, doc


def answer(track="solar", status="in_scope"):
    lines = (SOLAR_1, SOLAR_2) if track == "solar" else (RAG_1, RAG_2)
    return {
        "status": status,
        "reason": "Experimental mechanism and evaluation satisfy the supplied criteria.",
        "evidence": [{"page": i, "quote": q} for i, q in enumerate(lines, 1)],
    }


def accepted(row, track="solar"):
    p = answer(track)
    return {
        "document_id": row["document_id"], "pdf_sha256": row["pdf_sha256"],
        "status": "in_scope", "reason": "Two independent Codex scope reviews: " + p["reason"],
        "source_pages": [1, 2], "passes": [p, p],
    }


def save_manifest(path, records):
    path.write_text(json.dumps({
        "policy": auto.POLICY, "model": "gpt-6.2-sol", "records": records,
    }), encoding="utf-8")


def test_checked_pass_requires_two_exact_same_pdf_page_quotes(tmp_path):
    row, _ = make_pdf(tmp_path)
    pages = auto.extract_pages(auto.pdf_for(row, tmp_path))
    assert auto.checked_pass(answer(), pages) == ("in_scope", [1, 2])
    bad = answer()
    bad["evidence"][0]["quote"] = "This fake quote does not appear in the source PDF."
    assert auto.checked_pass(bad, pages) is None
    bad = answer()
    bad["evidence"][1]["page"] = 1
    assert auto.checked_pass(bad, pages) is None
    bad = answer()
    bad["evidence"][1]["page"] = True
    assert auto.checked_pass(bad, pages) is None
    bad = answer()
    bad["evidence"] = bad["evidence"][:1]
    assert auto.checked_pass(bad, pages) is None
    bad = answer()
    bad["evidence"][0]["quote"] = "Thin-film solar cell devices"  # not an exact PDF quote
    assert auto.checked_pass(bad, pages) is None


def test_disagreement_and_unreadable_evidence_abstain(tmp_path, monkeypatch):
    row, _ = make_pdf(tmp_path)
    outcomes = [answer(), answer(status="out_of_scope")]
    monkeypatch.setattr(auto, "call_codex", lambda *args: outcomes.pop(0))
    result = auto.audit_one(row, tmp_path, "gpt-6.2-sol", 10)
    assert result["status"] == "needs_review"
    assert not result["source_pages"]
    assert len(result["passes"]) == 2

    row["pdf_sha256"] = "f" * 64
    result = auto.audit_one(row, tmp_path, "gpt-6.2-sol", 10)
    assert result["status"] == "needs_review"
    assert "SHA-256" in result["reason"]


def test_two_pass_acceptance_and_independent_reverification(tmp_path, monkeypatch):
    row, doc = make_pdf(tmp_path)
    calls = []
    def fake_codex(prompt, model, timeout):
        calls.append((prompt, model, timeout))
        return answer()
    monkeypatch.setattr(auto, "call_codex", fake_codex)
    record = auto.audit_one(row, tmp_path, "gpt-6.2-sol", 123)
    assert record["status"] == "in_scope"
    assert len(calls) == 2 and all("UNTRUSTED" in x[0] for x in calls)
    manifest = tmp_path / "machine.json"
    save_manifest(manifest, [record])
    decisions = auto.machine_decisions(manifest, tmp_path, {row["document_id"]: doc},
                                        {row["document_id"]: row})
    assert decisions[row["document_id"]].source_pages == [1, 2]
    assert decisions[row["document_id"]].status == "in_scope"

    record["passes"][1]["evidence"][0]["quote"] = "Fabricated quotation that isn't in article."
    save_manifest(manifest, [record])
    with pytest.raises(ValueError, match="independent evidence verification"):
        auto.machine_decisions(manifest, tmp_path, {row["document_id"]: doc},
                               {row["document_id"]: row})


def test_machine_manifest_rejects_changed_sha(tmp_path):
    row, doc = make_pdf(tmp_path)
    manifest = tmp_path / "machine.json"
    record = accepted(row)
    record["pdf_sha256"] = "f" * 64
    save_manifest(manifest, [record])
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        auto.machine_decisions(manifest, tmp_path, {row["document_id"]: doc},
                               {row["document_id"]: row})


def test_disputed_rag_pair_is_held_together(tmp_path):
    docs, rows, records = {}, {}, []
    for pid in ("P007", "P010"):
        row, doc = make_pdf(tmp_path, "rag", pid)
        docs[row["document_id"]] = doc
        rows[row["document_id"]] = row
        records.append(accepted(row, "rag"))
    manifest = tmp_path / "machine.json"
    save_manifest(manifest, records[:1])
    assert auto.machine_decisions(manifest, tmp_path, docs, rows) == {}
    save_manifest(manifest, records)
    assert set(auto.machine_decisions(manifest, tmp_path, docs, rows)) == set(docs)


def test_missing_native_text_abstains_without_model_call(tmp_path, monkeypatch):
    row, _ = make_pdf(tmp_path)
    pdf_path = tmp_path / row["pdf_path"]
    with pymupdf.open() as pdf:
        pdf.new_page()  # no native text
        pdf.save(pdf_path)
    row["pdf_sha256"] = pdf_sha256(pdf_path)
    row["page_count"] = "1"
    monkeypatch.setattr(auto, "call_codex", lambda *args: pytest.fail("called on blank page"))
    record = auto.audit_one(row, tmp_path, "gpt-6.2-sol", 10)
    assert record["status"] == "needs_review"


def test_resumable_manifest_requires_matching_model_and_policy(tmp_path):
    path = tmp_path / "out.json"
    save_manifest(path, [])
    assert auto._machine_records(path)["policy"] == auto.POLICY
    path.write_text('{"policy": "untrusted", "records": []}')
    with pytest.raises(ValueError, match="Invalid machine-scope manifest"):
        auto._machine_records(path)
