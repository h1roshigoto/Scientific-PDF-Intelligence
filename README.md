# Scientific PDF Intelligence

Evidence-first scientific PDF processing for Solar and RAG.

## Backend status (P0 foundation)

Initial internal contract, PDF text blocks with bounding boxes, SHA-256 fingerprints, page-level OCR queue markers, local JSON store, API endpoints, and tests are implemented.
The D001 pipeline verifies GigaChat responses against table cells in both tracks. The store adapter writes seven CSV files per track using the Solar participant format and the RAG adaptation from the participant package. See the [Windows handoff](docs/FRONTEND_HANDOFF.md) for extraction, API setup, and export commands.

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

See [API contract](docs/API_CONTRACT.md).
See the [data passport](passport.md) for CSV schemas and export commands.
All document identifiers contain the domain prefix (e.g. solar:D001).
Sources use 1-based PDF page numbers.
The original PDF paths are registered locally; PDF files, private datasets, API keys and local state are not committed.

Private browser uploads and local analysis are available for Solar and RAG.
Use `SPI_AUTH_MODE=private` for the closed legacy demo with its server-side
bearer token. Use `SPI_AUTH_MODE=multiuser` for invitation-only accounts,
server-side cookie sessions and separate Stores per user. The worker uses the
same authentication mode and paths as FastAPI. After manual review, the upload page can export
current approvals in CSV and JSON, a private PDF passport and a ZIP with
scientific issues, full decision history and offline SHA-256 verification.

For PowerShell commands, first admin setup, migration and backups, see the
[Windows runbook](docs/DEMO_RUNBOOK.md#multiuser-на-windows). For account and API
behavior, see the [API contract](docs/API_CONTRACT.md#authentication-modes).
Public deployment requires operational TLS, PDF execution isolation, backup
validation and monitoring. See the [security model](docs/SECURITY_MODEL.md).

## Next sprint steps

1. Execute OCR on RAG D002 page 8 and visually review its table.
2. Evaluate against dev_gold and integrate with Alexander's UI.

Schedule and ownership: ChatGPT Space sprint, 20–21 November 2026.
# Production infrastructure

For a local Docker Desktop setup with the static production frontend, see
[Windows Compose demo](docs/DEMO_RUNBOOK.md#docker-desktop-локальная-production-сборка).
Linux setup is in [Deployment](docs/DEPLOYMENT.md), with separate
[Operations](docs/OPERATIONS_RUNBOOK.md), [Backup and restore](docs/BACKUP_RESTORE.md)
and [Production readiness](docs/PRODUCTION_READINESS.md).
