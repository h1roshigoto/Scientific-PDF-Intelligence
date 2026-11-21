"""Independently verify real API artifacts downloaded by the browser."""

import csv
import json
import subprocess
import sys
from io import StringIO
from pathlib import Path
from zipfile import ZipFile

import pymupdf


def report_text(content):
    with pymupdf.open(stream=content, filetype="pdf") as pdf:
        return "".join(page.get_text() for page in pdf)


def main():
    archive, csv_path, json_path, pdf_path, reopened = map(Path, sys.argv[1:6])
    rejected = sys.argv[6]
    for path in (archive, reopened):
        subprocess.run([sys.executable, "-m", "spi.upload_export_verify", str(path)], check=True)
    with ZipFile(archive) as first, ZipFile(reopened) as second:
        manifest = json.loads(first.read("spi-project/manifest.json"))
        reopened_manifest = json.loads(second.read("spi-project/manifest.json"))
        assert len(manifest["files"]) == 6
        assert manifest["metadata"]["snapshot_sha256"] == reopened_manifest["metadata"]["snapshot_sha256"]
        counts = manifest["validation"]["review_counts"]
        assert (counts["approved"], counts["approved_corrected"], counts["rejected"], counts["pending"]) == (1, 1, 1, 1)
        json_bytes = first.read("spi-project/reviewed_results.json")
        csv_bytes = first.read("spi-project/reviewed_results.csv")
        assert json_bytes == json_path.read_bytes() == second.read("spi-project/reviewed_results.json")
        assert csv_bytes == csv_path.read_bytes() == second.read("spi-project/reviewed_results.csv")
        rows = list(csv.DictReader(StringIO(csv_bytes.decode("utf-8-sig"), newline="")))
        results = json.loads(json_bytes)["results"]
        assert len(rows) == len(results) == 2
        for row, item in zip(rows, results, strict=True):
            record = item["reviewed_result"]
            assert row["result_id"] == record["result_id"] != rejected
            assert row["raw_value"] == record["raw"]["value"]
            assert row["source_page"] == str(record["source"]["page"])
        assert {row["raw_value"] for row in rows} == {"15.5", "15.7"}
        history = json.loads(first.read("spi-project/review_history.json"))
        assert len(history["automatic_results"]) == 4
        assert any(item["event"]["decision"]["result_id"] == rejected
                   and item["event"]["decision"]["status"] == "rejected" for item in history["events"])
        for content in (pdf_path.read_bytes(), first.read("spi-project/scientific-passport.pdf")):
            text = report_text(content)
            assert manifest["metadata"]["snapshot_sha256"] in text
            assert all(item["decision"]["review_id"] in text for item in results)
            assert "15.5" in text and "15.7" in text and "Отклонённые\n1" in text


if __name__ == "__main__":
    main()
