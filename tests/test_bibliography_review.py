import csv
from hashlib import sha256

import pymupdf
import pytest

from spi import bibliography_review as bibliography
from spi.storage import Store


def registered_pdf(tmp_path, store, track, pid, *, title="Embedded title", author="PDF author"):
    folder = tmp_path / "data" / track / "test"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{pid}.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.set_metadata({
            "title": title,
            "author": author,
            "creationDate": "D:20200101000000",
        })
        pdf.save(path)
    digest = sha256(path.read_bytes()).hexdigest()
    docs = store.documents()
    docs.append({
        "document_id": f"{track}:{pid}", "publication_id": pid, "domain": track,
        "file_path": str(path), "sha256": digest, "page_count": 1,
    })
    store.write("documents.json", docs)
    return path


def csv_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def test_candidate_sheet_is_read_only_and_never_approves_embedded_values(tmp_path):
    store = Store(tmp_path / "state")
    registered_pdf(tmp_path, store, "solar", "P001", title="=SUM(1,2)", author="Research Team")
    registered_pdf(tmp_path, store, "rag", "P001", title="RAG Study")
    original_state = (store.root / "documents.json").read_bytes()

    output = tmp_path / "review.csv"
    assert bibliography.write_review_sheet(store, output) == 2
    rows = csv_rows(output)
    assert [row["document_id"] for row in rows] == ["rag:P001", "solar:P001"]
    assert rows[0]["title_candidate"] == "RAG Study"
    assert rows[0]["title_candidate_source"] == "embedded_pdf_metadata"
    assert rows[1]["title_candidate"] == "'=SUM(1,2)"
    assert rows[1]["authors_candidate"] == "Research Team"
    assert all(row["review_status"] == "pending" for row in rows)
    assert all(row["confirmed_title"] == row["confirmed_year"] == row["confirmed_DOI"] == "" for row in rows)
    assert all(row["year_candidate"] == "" and row["year_candidate_source"] == "" for row in rows)
    assert all(row["source_pages"] == row["reviewer"] == "" for row in rows)
    assert (store.root / "documents.json").read_bytes() == original_state

    with pytest.raises(FileExistsError):
        bibliography.write_review_sheet(store, output)
    assert csv_rows(output) == rows


def test_selection_and_determinism(tmp_path):
    store = Store(tmp_path / "state")
    registered_pdf(tmp_path, store, "solar", "P002")
    registered_pdf(tmp_path, store, "solar", "P001")
    registered_pdf(tmp_path, store, "rag", "P001")
    first = tmp_path / "one.csv"
    second = tmp_path / "two.csv"
    assert bibliography.write_review_sheet(store, first, domain="solar") == 2
    assert bibliography.write_review_sheet(store, second, domain="solar") == 2
    assert first.read_bytes() == second.read_bytes()
    assert [row["publication_id"] for row in csv_rows(first)] == ["P001", "P002"]
    third = tmp_path / "one-pdf.csv"
    assert bibliography.main([
        "--state", str(store.root), "--output", str(third),
        "--domain", "rag", "--publication-id", "P001",
    ]) == 0
    assert [row["document_id"] for row in csv_rows(third)] == ["rag:P001"]


@pytest.mark.parametrize("corrupt", ["changed_sha", "missing", "page_count"])
def test_invalid_pdf_blocks_review_sheet_without_touching_state(tmp_path, corrupt):
    store = Store(tmp_path / "state")
    path = registered_pdf(tmp_path, store, "solar", "P001")
    registered_pdf(tmp_path, store, "rag", "P001")
    if corrupt == "changed_sha":
        with path.open("ab") as file:
            file.write(b"\nPDF bytes changed after registration\n")
    elif corrupt == "missing":
        path.unlink()
    else:
        docs = store.documents()
        next(d for d in docs if d["document_id"] == "solar:P001")["page_count"] = 2
        store.write("documents.json", docs)
    original_state = (store.root / "documents.json").read_bytes()
    output = tmp_path / "review.csv"
    with pytest.raises((OSError, ValueError)):
        bibliography.write_review_sheet(store, output)
    assert not output.exists()
    assert (store.root / "documents.json").read_bytes() == original_state


def test_protects_source_and_store_paths_and_duplicate_identity(tmp_path):
    store = Store(tmp_path / "state")
    path = registered_pdf(tmp_path, store, "solar", "P001")
    with pytest.raises(ValueError, match="inside Store"):
        bibliography.write_review_sheet(store, store.root / "review.csv")
    with pytest.raises(ValueError, match="original PDF folder"):
        bibliography.write_review_sheet(store, path.parent / "review.csv")
    assert not (store.root / "review.csv").exists()
    assert not (path.parent / "review.csv").exists()
    docs = store.documents()
    docs.append(dict(docs[0]))
    store.write("documents.json", docs)
    with pytest.raises(ValueError, match="Duplicate document IDs"):
        bibliography.write_review_sheet(store, tmp_path / "review.csv")
    assert not (tmp_path / "review.csv").exists()
