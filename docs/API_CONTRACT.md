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

## Private PDF uploads

All upload routes require `Authorization: Bearer <token>`. The browser uses
the same-origin Vite proxy, so it calls `/api/uploads`. Set
`SPI_UPLOAD_ENABLED=true` and a server-only `SPI_UPLOAD_TOKEN` of at least
32 characters. Generate it with `secrets.token_urlsafe(32)`.

| Route | Success response |
| --- | --- |
| `POST /uploads` | `201 Created` with upload metadata |
| `GET /uploads` | `200 OK` with an array of metadata, newest first, and `X-SPI-Upload-Max-Bytes` |
| `GET /uploads/{upload_id}` | `200 OK` with metadata |
| `GET /uploads/{upload_id}/file` | `200 OK` with the original bytes as an `application/pdf` attachment |
| `POST /uploads/{upload_id}/process` | `202 Accepted` with `upload_id`, `job_id`, and `status`. The request has no body. |
| `GET /uploads/{upload_id}/processing` | `200 OK` with the processing status, current step, and diagnostics |
| `GET /uploads/{upload_id}/results` | `200 OK` with an array of private scientific result records |
| `GET /uploads/{upload_id}/issues` | `200 OK` with an array of private validation and extraction issues |

Submit `multipart/form-data` with exactly one `file` and one `domain`
(`solar` or `rag`). Validity is checked from the file bytes regardless of
the file part's MIME type or extension. The API limits the PDF part and total
multipart body as bytes arrive, without using `Content-Length` for validation.
The default file limit is 25 MiB (`SPI_UPLOAD_MAX_BYTES`). The default
store-wide quota is 100 files (`SPI_UPLOAD_MAX_FILES`). Repeated names are
allowed. Each accepted request gets its own opaque ID.

Metadata fields are `upload_id` (32 hexadecimal characters), `domain`,
`original_filename` (display basename), `size_bytes` (positive integer),
`sha256` (hexadecimal digest), `page_count` (positive integer), `status`
(`stored`), and `created_at` (UTC ISO 8601). Upload responses use
`Cache-Control: no-store` and contain no server paths.

| HTTP status | Meaning |
| --- | --- |
| `400` | Malformed multipart body or repeated parts |
| `401` | Missing or invalid bearer token |
| `404` | Unknown upload ID or original unavailable |
| `409` | File count quota reached |
| `413` | PDF or multipart body exceeds its byte limit |
| `415` | Request is missing the multipart media type |
| `422` | Invalid fields, ID format, or PDF (empty, missing signature, damaged, encrypted, or zero pages) |
| `500` | Upload storage failure |
| `503` | `uploads_disabled`, `uploads_misconfigured` (including invalid token configuration), or `upload_processing_unconfigured` |

`SPI_UPLOAD_DIR` holds originals named `<upload_id>.pdf`.
`SPI_UPLOAD_STATE_DIR` holds the `uploads.sqlite3` metadata index.
`SPI_UPLOAD_PROCESS_STATE_DIR` holds the processing queue, cross-process
locks and the scientific Store for each `upload_id`. Use three disjoint
directories outside the Git checkout and `SPI_STATE_DIR`. Ancestor and
descendant overlaps are rejected after checking lexical and resolved paths,
including symlinks and junctions. Keep the same directories across restarts.

`stored` confirms that the PDF passed technical upload checks. Its analysis
starts at `not_started`. An explicit POST creates a `queued` job, and a
separate worker moves it to `processing` and then to `completed`,
`needs_review` or `failed`. Duplicate POSTs return the existing job ID and
current status. If `retry_allowed` is true, the next POST creates a new job ID
and reuses compatible checkpoints. A worker restart resumes an interrupted job
with its existing ID once the previous worker's SQLite lock is released.

The status endpoint returns `step`, `updated_at` (UTC ISO 8601), `diagnostic`,
`retry_allowed`, `mode` (local or GigaChat), `coverage`, `recoveries`,
`human_review_required`, and available `counts` and `pages`. Counts stay null
until the worker has a checkpoint summary. Page state and `pages_needing_ocr`
identify pending recognition, which this worker does not perform. Model
timeouts, incomplete page analysis and request-budget exhaustion are retryable
and can leave partial results. Missing credentials with GigaChat enabled fall
back to local extraction for a new job.

Results use the existing scientific record contract with
`document_id = "<domain>:<upload_id>"`, SHA-256, metric, raw and normalized
values, evidence, table context and original primary/cited classification.
The original display filename stays in upload metadata. The issue records
include scientific validation issues and unresolved layout, fragment and page
diagnostics. Available results and issues come from the latest finished
attempt, including partial checkpoints with a summary. While an attempt is
queued or running these endpoints return empty arrays. `completed` marks
technical completion. Private records retain `extracted` or `needs_review`
until human verification. The public registry, results, exports and project
passport continue to read the official scientific Store.
The Windows setup and the requirements for public deployment are in the
[demo runbook](DEMO_RUNBOOK.md#приватная-загрузка-pdf).

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
