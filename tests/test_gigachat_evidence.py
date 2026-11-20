from decimal import Decimal

import pymupdf
import pytest

from spi.gigachat_evidence import (
    RAG,
    SOLAR,
    EvidenceError,
    Extraction,
    make_prompt,
    normalize_cell,
    prepare_source,
    quote_matches,
    verify_extraction,
)


def make_pdf(path, page_number, lines):
    with pymupdf.open() as document:
        for _ in range(page_number):
            page = document.new_page()
        for text, center, baseline in lines:
            width = pymupdf.get_text_length(text, fontsize=7)
            page.insert_text((center - width / 2, baseline), text, fontsize=7)
        document.save(path)


def answer(source, lookup, context="Table 1"):
    return Extraction(
        raw_value=source.target.text, raw_unit=lookup.unit, object_or_method=lookup.subject,
        metric=lookup.metric, context=context, source_block_id=source.target.block_id,
        source_quote=source.target.text,
    )


def test_solar_extracts_verbatim_number_and_header_unit(tmp_path):
    path = tmp_path / "D001.pdf"
    make_pdf(path, 5, [
        ("TABLE 1 | Synthetic", 85, 72),
        ("Sample", 58, 92), ("Jsc", 132, 92), ("Voc (mV)", 191, 92), ("FF", 235, 92),
        ("Pristine", 60, 110), ("12.3", 132, 110), ("1,234", 191, 110), ("0.43", 235, 110),
        ("Pristine", 380, 110), ("9,999", 420, 110),
    ])
    source = prepare_source(path, 5, SOLAR)
    assert source.page == 5
    assert normalize_cell(source, SOLAR) == ("1,234", Decimal("1.234"), "V")
    assert verify_extraction(answer(source, SOLAR), source, SOLAR) == (Decimal("1.234"), "V")
    prompt = make_prompt(SOLAR, source)
    assert "1,234" in prompt
    assert "9,999" not in prompt
    assert "1.234" not in prompt


@pytest.mark.parametrize("misalign", [False, True])
def test_rag_uses_the_model_and_dataset_headings_to_identify_the_metric(tmp_path, misalign):
    path = tmp_path / "D001.pdf"
    centers = (132, 154, 176, 198, 220, 242, 266, 288, 310, 332, 354, 376)
    values = (".321", ".091", ".094", ".152", ".250", ".325") * 2
    headings = [
        ("LLaMA3-8B-Instruct", 187, 80), ("Second model", 321, 80),
        ("Methods", 91, 87),
        ("HotpotQA", 143, 94), ("2Wiki", 187, 94), ("MuSiQue", 231, 94),
        ("HotpotQA", 277, 94), ("2Wiki", 321, 94), ("MuSiQue", 365, 94),
        ("RA-CoT", 90, 135), ("Table 1: Synthetic", 320, 300),
    ]
    headings.extend((metric, x, 107) for metric, x in zip(("EM", "F1") * 6, centers))
    headings.extend(
        (value, x - (7 if misalign and i == 0 else 0), 135)
        for i, (value, x) in enumerate(zip(values, centers))
    )
    make_pdf(path, 6, headings)
    if misalign:
        with pytest.raises(EvidenceError, match="table_column_unresolved"):
            prepare_source(path, 6, RAG)
        return

    source = prepare_source(path, 6, RAG)
    result = answer(source, RAG, "Table 1, LLaMA3-8B-Instruct, HotpotQA")
    assert verify_extraction(result, source, RAG) == (Decimal("0.321"), "")
    other_cell = next(block for block in source.blocks if block.text == ".321" and block != source.target)
    wrong_source = result.model_copy(update={"source_block_id": other_cell.block_id})
    assert quote_matches(wrong_source, source)
    with pytest.raises(EvidenceError, match="wrong_table_cell"):
        verify_extraction(wrong_source, source, RAG)
