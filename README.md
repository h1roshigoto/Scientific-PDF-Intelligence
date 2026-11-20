# Scientific PDF Intelligence

Evidence-first scientific PDF processing for Solar and RAG.

## Backend status (P0 foundation)

Initial internal contract, PDF text blocks with bounding boxes, SHA-256 fingerprints, page-level OCR queue markers, local JSON store, API endpoints, and tests are implemented.
The CSV exporter validates prepared rows and writes seven files per track. It uses the Solar participant format and the recommended RAG adaptation. Scientific metric extraction, OCR execution, and evaluation against dev_gold are pending.

## Run

Requirements: Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python -m spi.cli data/solar/dev --domain solar
python -m spi.cli data/rag/dev --domain rag
uvicorn spi.api:app --reload
pytest -q
```

API docs: http://127.0.0.1:8000/docs

For the light web frontend, see the [frontend startup and test guide](frontend/README.md).

See [API contract](docs/API_CONTRACT.md).
See the [data passport](passport.md) for CSV schemas and export commands.
All document identifiers contain the domain prefix (e.g. solar:D001).
Sources use 1-based PDF page numbers.
The original PDF paths are registered locally; PDF files, private datasets, API keys and local state are not committed.

## Next sprint steps

1. Extract and manually verify Solar D001 page 5 and RAG D001 page 6 with correct row, column, and object context.
2. Execute OCR on RAG D002 page 8 and visually review its table.
3. Run the participant validators on annotated submissions and integrate with Alexander's UI.

Schedule and ownership: ChatGPT Space sprint, 20–21 November 2026.
