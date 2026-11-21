# P0 frontend ↔ backend contract

The API serves the internal format. The [data passport](../passport.md) documents the separate CLI and Python CSV export.

Endpoints:
- GET /health
- GET /documents?domain=solar|rag
- GET /results?domain=solar|rag
- GET /results/{result_id}
- GET /issues?domain=solar|rag
- GET /documents/{document_id}/file
- GET /export/project-passport.pdf?run_id=e2e-dev&domain=all
- GET /export/{artifact} (currently internal JSON only)

A document ID is domain-scoped, e.g. solar:D001. All PDF source pages are 1-based.
The result record includes metric (name, k, variant), raw (value, unit),
normalized (value, unit), source (page, fragment/table/section/figure,
row/column, origin), context, extraction_status and note.
Unknown fields stay null; parsing a PDF alone never verifies a scientific claim.
Page OCR status is independent of scientific extraction_status.

The result list and detail endpoints use the scope decision for the currently
imported PDF SHA-256. An `out_of_scope` decision hides its results and
publication-owned batch issues. The original PDF and saved evidence remain
available in the document registry and local state. Restoring scope for the
same SHA-256 restores the saved results. The internal `/export/{artifact}`
endpoint reads stored JSON files directly. It is a historical artifact reader.

## Project passport

Set `SPI_RUN_ID` to the run represented by `SPI_STATE_DIR`. The API serves
one active Store and uses the configured ID as its label. It returns
`503` when the run is unconfigured and `404` for a different run ID.
Set both variables together when changing runs. For a reproducible report,
point the API at a completed Store whose files are no longer being updated.

The required `run_id` is case-sensitive. The optional `domain` is `all`
(default), `solar`, or `rag`. The optional `publication_id` accepts a PDF
stem in the chosen domain or a full document ID such as `solar:D001`.
A missing publication returns `404`. A bare stem shared by both domains
returns `409` when no domain is specified. Invalid query parameters return
`422`. A generation failure returns `500`.

The response is an A4 PDF attachment with `Content-Type: application/pdf`
and an ISO 8601 UTC `X-SPI-Generated-At` header. The backend reads registered
documents, current results and validation issues, matching checkpoints,
page statuses and scope decisions directly from Store. The report includes
audit records from excluded publications, so its counts can exceed the
public result and issue listings. A single-publication passport includes
issues attributable solely to that publication. The domain or project
passport also includes cross-publication and domain-wide issues.
The scope label reflects the decision for the registered PDF SHA-256.
Result eligibility reflects scope, verification and extraction provenance.
CSV release checks are separate. Escaped Unicode code points in PDF text
preserve control characters and characters that lack a reliable PDF mapping.

### Windows and Vite

From the repository root, serve an existing state in PowerShell.

```powershell
$env:SPI_STATE_DIR = (Resolve-Path 'outputs/e2e-dev/state').Path
$env:SPI_RUN_ID = 'e2e-dev'
python -m uvicorn spi.api:app --host 127.0.0.1 --port 8018
```

Set these values in `frontend_redezigned/.env.local`, then restart Vite.
The download is available in the project passport panel.

```dotenv
VITE_DATA_SOURCE=api
VITE_API_BASE=/api
SPI_BACKEND_URL=http://127.0.0.1:8018
VITE_PROJECT_PASSPORT_PATH=export/project-passport.pdf
VITE_PROJECT_RUN_ID=e2e-dev
VITE_PROJECT_PASSPORT_GENERATED_AT_HEADER=X-SPI-Generated-At
```

With Vite listening on port 5173, check the full report and the two D001
publications through the proxy. Use port 8018 and omit `/api` to check
FastAPI directly.

```powershell
$spiPassport = 'http://127.0.0.1:5173/api/export/project-passport.pdf?run_id=e2e-dev'
curl.exe -fS -D - "$spiPassport&domain=all" -o "$env:TEMP/spi-passport-all.pdf"
curl.exe -fS -D - "$spiPassport&domain=solar&publication_id=D001" -o "$env:TEMP/spi-passport-solar-D001.pdf"
curl.exe -fS -D - "$spiPassport&domain=rag&publication_id=D001" -o "$env:TEMP/spi-passport-rag-D001.pdf"
```

See the [frontend handoff](FRONTEND_HANDOFF.md) for Windows startup, CORS,
D001 source links, and end-to-end verification.
