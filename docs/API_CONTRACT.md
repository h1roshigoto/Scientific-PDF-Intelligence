# P0 frontend ↔ backend contract

The API serves the internal format. The [data passport](../passport.md) documents the separate CLI and Python CSV export.

The following official Store endpoints are available in private mode:
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

## Authentication modes

`SPI_AUTH_MODE=private` defaults to the existing closed demo. `SPI_AUTH_MODE=multiuser`
requires explicit paths, an allowed Origin list and a server secret. Invalid
multiuser configuration prevents the API and worker from starting. In multiuser
mode `/health` returns only `{"status":"ok"}`. Official Store routes and interactive API
docs are disabled. Browser requests use the same origin and the `/api` prefix.

| Route | Success | Request or response |
| --- | --- | --- |
| `GET /auth/config` | 200 | `{"mode":"private"}` or `{"mode":"multiuser"}` |
| `GET /auth/csrf` | 200 | Issues a short-lived anonymous cookie as needed and returns `csrf_token` bound to that cookie or the current session cookie |
| `POST /auth/register` | 201 | `email`, `password`, `display_name`, `invitation_code`. Returns the user profile |
| `POST /auth/login` | 200 | `email` and `password`. Rotates the presented session and sets the new HttpOnly cookie |
| `GET /auth/me` | 200 | Profile and public `session_id` for binding the current browser view |
| `POST /auth/logout` | 204 | Revokes the session and expires its cookie |
| `GET /auth/sessions` | 200 | Current account's active sessions with time fields and a `current` flag |
| `DELETE /auth/sessions/{session_id}` | 204 | Revokes an account-owned session and clears the cookie if current |
| `POST /auth/change-password` | 204 | `current_password` and `new_password`. Revokes all sessions for the account |
| `GET /account/usage` | 200 | `documents`, `storage_bytes`, `limits`, `active_jobs`, `queued_jobs`, remaining model requests and exports, and `period_seconds` |

Send cookies on multiuser requests. Send the exact allowed browser `Origin`
and the `X-SPI-CSRF` header on POST and DELETE. Obtain a new CSRF value after
login or logout. For browsers with several tabs, pass the public identifier
from `/auth/me` in `X-SPI-Session-ID` on protected calls to reject a stale view
if another tab has rotated the cookie. It cannot authenticate a request.
User and session timestamp fields are Unix seconds. Email verification is false
until a verification workflow exists.

All multiuser responses use `Cache-Control: no-store`. Auth errors contain a
machine-readable `detail` code. An invalid or expired invitation returns 400.
Missing sessions and invalid credentials return 401. Invalid Origin or CSRF
returns 403. Invalid fields return 422. Conflicts return 409. Rate or export
limits return 429 with `Retry-After` in seconds. Unavailable auth storage returns
503. A valid ID for someone else's upload, job or session uses the same 404
as an absent resource. Size violations return 413.

Tenant resolution happens before reading files, results, issues, review
history or export snapshots. A user cannot choose the tenant via the request.
The same rules apply to admins. Review decisions retain the existing scientific
and export contract. The audit envelope stores the account actor separately,
and historical review labels retain their original meaning.

PDF storage quota counts original PDF bytes. Upload count and byte limits,
queue size and retries are enforced with per-tenant SQLite transactions.
Live worker concurrency uses cross-process SQLite locks. Model completions and
export attempts for owned uploads use a UTC day budget. An attempted completion
or export can consume budget after downstream failure. Export previews are
rate limited. Processing rows with status `processing` may represent interrupted
work pending recovery. Scientific output and ZIP schema remain version 1.

## Private PDF uploads

In private mode all upload routes require `Authorization: Bearer <token>`. In
multiuser mode they require a current cookie session and resolve the Store
from its owner. The browser uses the same-origin proxy and calls
`/api/uploads`. For legacy uploads set
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
legacy store-wide quota is 100 files (`SPI_UPLOAD_MAX_FILES`). Multiuser defaults and
per-account overrides are reported by `GET /account/usage`. Repeated names are
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
for their entire lifetime. Human verification has its own contract below.
The public registry, results, exports and project passport continue to read
the official scientific Store.
The Windows setup and the requirements for public deployment are in the
[demo runbook](DEMO_RUNBOOK.md#приватная-загрузка-pdf).

### Private scientific review

Use the same bearer token on each route. Responses, including errors, use
`Cache-Control: no-store`. The shared token authorizes an operator to access the
private store. `reviewer_label` is optional, self-entered audit metadata.

| Route | Success response |
| --- | --- |
| `GET /uploads/{upload_id}/reviews` | Processing state, PDF integrity status, review states and scientific issues |
| `GET /uploads/{upload_id}/results/{result_id}/review` | Original record, current decision, applicability, revision, issues and OCR pages |
| `POST /uploads/{upload_id}/results/{result_id}/review` | `201 Created` with the updated review detail |
| `GET /uploads/{upload_id}/results/{result_id}/review/history` | Events in ascending revision order |
| `GET /uploads/{upload_id}/reviewed-results` | Applicable approved records with `original_result`, `reviewed_result`, `decision`, `issues` and `ocr_pages` |

URL-encode `result_id` as a path segment. A new review state is `pending`
with revision `0`. Copy the `basis` and `revision` from the detail response
into each write. `expected_revision` is the last observed revision.

```json
{
  "basis": {
    "upload_id": "<32-character upload ID>",
    "domain": "solar",
    "result_id": "<result ID>",
    "source_job_id": "<32-character job ID>",
    "pdf_sha256": "<64-character SHA-256>",
    "source_result_hash": "<64-character SHA-256>"
  },
  "expected_revision": 0,
  "action": "approve",
  "evidence_mode": "text",
  "warnings_acknowledged": true,
  "reviewer_label": "Local operator"
}
```

| Action | Decision | Requirements |
| --- | --- | --- |
| `approve` | `approved` | Confirm the original PDF evidence. The reason is optional for text review. |
| `propose_correction` | `correction_proposed` | Supply `correction` and a nonblank `reason` grounded in the PDF. |
| `approve_correction` | `approved_corrected` | Explicitly confirm the current applicable proposal using its revision. The server uses the saved proposal. |
| `reject` | `rejected` | Supply a nonblank `reason`. The automatic record and history are retained. |

Approvals require `evidence_mode` (`text` or `visual`). Visual review requires
a reason. OCR-required pages and pages without extractable text require visual
review. Approval with applicable scientific issues requires
`warnings_acknowledged: true`. The issues remain open and appear alongside the
reviewed records. Review actions certify the operator's comparison with the
original PDF. The server validates integrity, page bounds, and the scientific
structure.

A correction can replace `raw`, `normalized`, and `source`, or update
`metric.name`, `metric.k` and individual `context` keys. Omitted groups and
context keys inherit from the automatic record. `raw` and `normalized` each
accept `value` and `unit`. Keep unknown
units as `null` and use `null` for both normalized fields if a conversion
cannot be established. Changed normalization needs `correction.normalization_evidence`
and a documented unit or RAG evaluation protocol. Known unit conversions must
match the scientific normalization rules. Context retains the instrument,
conditions and other applicable fields unless explicitly edited. Retrieval `top_k`
belongs in context, and `metric.k` is independent. RAG approvals require explicit
primary or cited provenance. Cited evidence needs `cited_publication_id`.
For any source change, send its 1-based PDF `page` with the evidence fields
and explain the change in `reason`. Document, upload and extraction provenance
are immutable. `extraction_metadata` in a corrected record still describes
the source extraction.

The source hash is SHA-256 over the automatic result's JSON with recursively
sorted object keys, compact separators and UTF-8 encoding. It includes raw and
normalized data, metric and `k`, context, evidence and extraction metadata.
The server matches the basis against the current version and hashes the original
PDF before accepting a write. A new job ID invalidates prior decisions even
when the result hash is identical. A changed record or PDF also makes its
decision `stale`. An old decision remains accessible with its original revision
and snapshot. If a record disappeared, detail still serves its history and
returns `original_result: null`. `pdf_status` is `valid`, `missing` or `mismatch`.

Each accepted action adds one revision to a per-result SQLite event log at
`SPI_UPLOAD_PROCESS_STATE_DIR/<upload_id>/reviews.sqlite3`. The latest committed
event is the current decision. Each event keeps the previous and new decisions,
the source snapshot, proposed fields, warnings and OCR context. Queue and review
transactions serialize each write against enqueue, claim and worker status updates.
An append rolls back on write failure. History survives restarts with the same
private directories. The log is append-only at the application layer.

`409` covers a revision conflict, changed basis or PDF, active analysis, or
an unavailable proposal. Refresh detail and make a new decision after checking
the source. A repeated POST with an already used revision returns `409`.
`422` covers invalid input or scientific evidence. `401`, `404` and `503` use
the private upload authentication, lookup and configuration rules. The reviewed
projection omits pending, proposed, rejected and stale decisions. Review does
not change extraction status, publication scope or release readiness.

### Private project export v1

All routes below use the authenticated private upload store. Send the bearer
token in the `Authorization` header. The token grants access to the configured
store. It neither identifies individual users nor authenticates reviewer labels.
Responses use `Cache-Control: no-store`. Keep downloaded data in a private location.

| GET under `/uploads/{upload_id}/export/` | Response |
| --- | --- |
| `manifest` | JSON preview with capture metadata, counts, limitations and available downloads. Checksums are computed when the ZIP is built. |
| `reviewed-results.csv` | `text/csv; charset=utf-8` attachment `reviewed_results.csv` |
| `reviewed-results.json` | `application/json` attachment `reviewed_results.json` |
| `scientific-passport.pdf` | `application/pdf` attachment from the private snapshot |
| `project.zip` | `application/zip` attachment `spi-project-<domain>-<upload_id>.zip` |

Send the preview's `metadata.snapshot_sha256` as `X-SPI-Snapshot-SHA256` on
download requests to pin the version. Each download echoes the captured
fingerprint in the response header. Without a pin, a request captures the
latest eligible snapshot. The fingerprint covers catalog metadata and all
captured scientific and audit data. The UTC export timestamp is separate and
can differ across requests for the same version.

The API streams the original once to verify its registered SHA-256 and checks
its PDF structure and page count before capture. It then reserves the queue
transaction, rechecks the job and original identity, and reads the results,
issues, processing and full review log in queue → reviews order. The queue
reservation ends before validation and rendering. All artifacts in a ZIP use
that captured snapshot even if a newer review commits during rendering.
Active jobs, missing analysis data, changed originals, changed job or pinned
versions, and invalid approved records return `409`. Finished failed attempts
with saved analysis can be exported with their limitations. A finished
snapshot with no current approvals exports a header-only CSV and an audit
package with `zero_approved_results`.

#### CSV and JSON

`spi-private-reviewed-results-v1` is comma-separated UTF-8 with a BOM, CRLF
records and CSV quoting. Rows are unique and sorted by `result_id`. Columns
appear in this order.

~~~text
upload_id,domain,document_id,result_id,review_status,review_revision,review_id,
source_job_id,pdf_sha256,source_result_hash,metric_name,metric_k,metric_variant,
raw_value,raw_unit,normalized_value,normalized_unit,source_page,source_section,
source_table,source_figure,source_fragment,source_origin,cited_publication_id,
context_json,reviewer_label,reviewed_at,review_reason,evidence_mode,warnings_acknowledged,
source_row,source_column
~~~

PDF pages are 1-based. Empty cells represent either null or empty text.
Booleans use `true` and `false`. Text with formula-like prefixes or spreadsheet
control prefixes is escaped with a leading apostrophe. Existing initial
apostrophes are doubled. Zero and plain signed-negative scientific numbers keep
their numeric representation. Use the JSON for fully typed data and exact text.
`metric_k` is a metric cutoff. Retrieval `top_k` stays in `context_json`.

The three UTF-8 JSON documents have top-level `format` and `snapshot_sha256` fields.

| File | Format | Data |
| --- | --- | --- |
| `reviewed_results.json` | `spi-private-reviewed-results-json-v1` | `results` uses the existing `ReviewedResult` model with original and reviewed records, decision, issues and OCR pages. |
| `issues.json` | `spi-private-issues-json-v1` | `issues` carries rule, severity, linked result IDs, evidence references and reason. `processing` tracks page-level coverage. `review_approval_resolves_issues` is false. |
| `review_history.json` | `spi-private-review-history-json-v1` | `automatic_results` preserves all current extraction records. `current_reviews` lists the latest effective status per result, including pending and stale. `events` preserves full review events with their original result snapshots and reasons. |

Only applicable `approved` and `approved_corrected` decisions appear in reviewed
files. In audit history, `current` means the latest decision matches the
current source, `superseded` means a newer decision replaced it on the current
source, and `stale` means its job, PDF or result version no longer applies.
Historical decisions retain their recorded action and status, including
`rejected` and `correction_proposed`. Acceptance of a record leaves scientific
issues, OCR gaps and extraction coverage unchanged. Even technical `completed`
and approval of every current result leave scientific completeness unverified.

#### ZIP integrity and limits

The ZIP contains exactly these files under `spi-project/`.

~~~text
manifest.json
README.md
reviewed_results.csv
reviewed_results.json
issues.json
review_history.json
scientific-passport.pdf
~~~

`manifest.json` uses `spi-private-project-v1`. It includes upload and PDF identity,
processing mode and coverage, UTC export time, application version, review and
issue counts, OCR pages, completeness warnings, and each of the six other
members' relative names, byte sizes and SHA-256 digests. The manifest excludes
its own digest. You may independently compute a checksum of the full ZIP.
Checksums detect changes and do not establish authorship.

~~~powershell
python -m spi.upload_export_verify '.\spi-project-solar-<upload_id>.zip'
~~~

The verifier reads in place without extracting members. Its exit code is `0`
for PASS and `1` for verification failure. It checks the fixed file inventory,
entry types, versions, sizes and SHA-256 digests. The source PDF is available
through the separate authenticated `/file` route. The archive has no official
submission status. The public Store passport, official 14 CSV files and
release readiness gate use their existing contracts.

The configurable defaults are `SPI_UPLOAD_EXPORT_MAX_SNAPSHOT_BYTES=16777216`
for source data and the frozen JSON, `SPI_UPLOAD_EXPORT_MAX_BYTES=33554432` for
both the expanded package and ZIP response, and `SPI_UPLOAD_EXPORT_MAX_PDF_PAGES=200`.
The server returns `413` when a limit is exceeded. Set the matching byte limit
for the verifier if checking an archive produced with a larger limit.
The PDF marks any shortened field. The JSON preserves full Unicode and content.

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
