import fitz
from spi.pdf import import_pdf
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
