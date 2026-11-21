import fitz
from spi.pdf import import_pdf, parse_pdf
from spi.storage import Store


def test_import_marks_blank_page_for_ocr(tmp_path):
    file = tmp_path / "D001.pdf"
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "Table 2  PCE 16.07")
        document.new_page()
        document.save(file)
    store = Store(tmp_path / "state")
    record = import_pdf(store, file, "solar")
    assert record["document_id"] == "solar:D001"
    assert record["pages_needing_ocr"] == [2]
    pages = store.read("solar_D001_pages.json")
    assert pages[0]["page"] == 1
    assert "PCE" in pages[0]["blocks"][0]["text"]


def test_truncated_pdf_marks_missing_page_as_unreadable(tmp_path):
    path = tmp_path / "D002.pdf"
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "Readable first page")
        missing_page_xref = document.new_page().xref
        pdf = document.tobytes()
    end = pdf.index(f"\n{missing_page_xref} 0 obj\n".encode())
    path.write_bytes(pdf[:end])

    record, pages = parse_pdf(path, "rag")

    assert record["page_count"] == 2
    assert record["processing_status"] == "partial"
    assert record["pages_needing_ocr"] == []
    assert pages[0]["read_status"] == "text"
    assert pages[1]["read_status"] == "error"
    assert pages[1]["ocr_status"] == "unknown"
