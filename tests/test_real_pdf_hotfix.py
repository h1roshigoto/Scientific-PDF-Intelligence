"""Small, independent regressions for geometry observed in real private PDFs."""

import pymupdf
import pytest

from spi.batch import run_batch
from spi.batch_evidence import metric_label
from spi.storage import Store


@pytest.mark.parametrize(("domain", "label", "expected"), [
    ("rag", " Avg F1-Score (↑) ", ("F1", None)),
    ("rag", "Recall＠２０ [％] ↑", ("recall", 20)),
    ("rag", "Normalized discounted cumulative gain@10", ("nDCG", 10)),
    ("rag", "Ave. Time (s)", ("latency", None)),
    ("rag", "Qwen3-32B", None),
    ("rag", "CWQ", None),
    ("solar", "Vₒ꜀ (mV)", None),
    ("solar", "V_OC (mV)", ("Voc", None)),
    ("solar", "R shunt (Ω·cm²)", ("shunt resistance", None)),
    ("solar", "IPCE (%)", ("EQE", None)),
])
def test_metric_label(domain, label, expected):
    assert metric_label(label, domain) == expected


def test_sparse_grid_with_neighbor_table_and_direction_arrows(tmp_path):
    path = tmp_path / "rag.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_font(fontname="sym", fontbuffer=pymupdf.Font("helv").buffer)
        for y, fields in [
            (34, ("Table 1: Work genres of the example characters",)),
            (90, ("Table 2: Our evaluation on ExampleQA",)),
            (112, ("RAG Method", "Accuracy (↑)", "Avg F1-Score (↑)", "Novel score (↓)")),
            (130, ("Baseline", "40%", "-", "2.5")),
            (148, ("Retriever", "80%", "0.72", "1.2")),
            (167, ("Table 3: Performance by dataset",)),
            (185, ("Method", "ExampleQA", "OtherQA")),
            (203, ("Retriever", "0.70", "0.75")),
            (221, ("Baseline", "0.61", "0.58")),
        ]:
            for x, text in zip((45, 160, 260, 365), fields):
                page.insert_text((x, y), text, fontname="sym", fontsize=9)
        doc.save(path)
    store = Store(tmp_path / "state")
    report = run_batch(store, [path], "rag", local_only=True)
    assert [(i["location"], i["reason"]) for i in report["documents"][0]["layout_issues"]] == [
        ("Table 1", "table_outside_metric_scope")
    ]
    rows = store.results("rag")
    assert {(r.metric.name, r.raw.value, r.raw.unit, r.source.row, r.source.table) for r in rows} == {
        ("accuracy", "40", "%", "Baseline", "Table 2"),
        ("accuracy", "80", "%", "Retriever", "Table 2"),
        ("F1", "0.72", None, "Retriever", "Table 2"),
    }
    assert sum(d["code"] == "unknown_metric" and d["table"] == "Table 3"
               for f in report["documents"][0]["fragments"].values()
               for d in f.get("review_details", [])) == 4


def test_local_jv_statement_across_pages(tmp_path):
    path = tmp_path / "solar.pdf"
    with pymupdf.open() as doc:
        p = doc.new_page()
        p.insert_text((58, 120), "Figure 3a shows the J-V characteristics. As fabricated ZnO/ABC/")
        p.insert_text((58, 134), "Mo device shows 11.20% power conversion efficiency")
        p.insert_text((58, 470), "Fig. 3: J-V characteristics of the fabricated device.")
        p = doc.new_page()
        p.insert_text((58, 65), "(PCE) with Jsc = 20.10 mA/cm2, Voc = 780 mV and")
        p.insert_text((58, 79), "FF = 71.45%. These are the measured values.")
        doc.save(path)
    store = Store(tmp_path / "state")
    run_batch(store, [path], "solar", local_only=True)
    assert {(r.metric.name, r.raw.value, r.raw.unit, r.source.page, r.source.figure)
            for r in store.results("solar")} == {
        ("PCE", "11.20", "%", 1, "Fig 3"), ("Jsc", "20.10", "mA/cm2", 2, "Fig 3"),
        ("Voc", "780", "mV", 2, "Fig 3"), ("FF", "71.45", "%", 2, "Fig 3"),
    }
