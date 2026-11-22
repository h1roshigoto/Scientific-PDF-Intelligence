# Multiuser security model

Multiuser mode provides application-level account and tenant isolation on a
single host. The host operator, local filesystem ACLs, SQLite databases,
TLS terminator and worker deployment are part of the trust boundary.
The private demo and the official scientific Store have separate paths.

## Access matrix

| Resource or operation | Anonymous | Owning account | Other account, including admin | Local operator |
| --- | --- | --- | --- | --- |
| Health, auth mode, CSRF setup | Allowed | Allowed | Allowed | Allowed |
| Register | Valid single-use invitation | Valid single-use invitation | Valid single-use invitation | Via same registration route |
| Profile, sessions and usage | 401 | Allowed | Account-scoped, no access to another account | CLI bootstrap, invitation issuance and account block |
| Uploads and original PDF evidence | 401 | Read and create within quotas | 404 for a foreign upload ID | Filesystem ACLs |
| Processing, retry, results and issues | 401 | Own upload and job | 404 | Worker uses stored owner and verified upload records |
| Reviews, decisions and history | 401 | Own report and its revisions | 404 | No application-level override |
| Manifest, CSV, JSON, passport and ZIP | 401 | Own versioned snapshot | 404 | Offline verification of any supplied archive |
| Official and demo Store routes in multiuser | 404 | 404 | 404 | Use the separate legacy or scientific workflow |
| Legacy import | Unavailable over HTTP | Unavailable over HTTP | Unavailable over HTTP | Explicit source, destination and owner through CLI |

API requests resolve the account from the cookie session and derive the
namespace from a random server-generated ID. The namespace contains the
upload catalog, original PDFs, jobs, checkpoints and review databases.
Uploads and jobs record the owner internally. Cross-tenant filenames are
never resolved from display names. Reviews retain the existing revision rules.
An independent actor row records the authenticated user in the same
transaction as a review event. Legacy event bytes and self-reported labels
are preserved. The official CSV contract and version 1 ZIP verifier are
independent of the account schema.

## Threats and controls

| Threat | Implemented control | Scope and validation |
| --- | --- | --- |
| Credential stuffing and brute force | Argon2id with random salts, rehash on login, weak-password checks, dummy hash for missing accounts, length limits, SQLite windows for account, IP and global login and four cross-process hashing slots | HTTP tests share limits across app instances and cover existing and missing-account errors. Large-scale abuse still requires edge controls. |
| Session theft | HttpOnly, host-only, SameSite=Lax cookies. Production requires Secure and the `__Host-` prefix. SQLite stores a SHA-256 hash of a high-entropy secret. Cookie rotation, idle and absolute timeout, revocation and password-change revocation are enforced. | Tests check cookies, persistence, expiration, rotation, revoke and administrative block. A stolen active cookie retains access until revoked or expired. |
| CSRF and browser cross-origin requests | Exact Origin validation and a server-signed CSRF token bound to a session cookie or a short-lived anonymous cookie. HTTPS, Host allowlist and credentialed CORS use explicit values. | Tests include login, logout, foreign Origin, wrong or absent tokens, invalid Host and forwarded headers. Tokens and passwords are never stored in browser storage by the app. |
| Cross-tenant IDOR | A separate Store per user and a shared ownership layer for reads, processing, reviews and exports. The worker checks upload identity, and the browser binds requests to the current session ID. | HTTP isolation tests cover public routes, resource reads, review changes, download variants and stale session bindings. Browser E2E exercises Alice and Bob in the same tab. |
| Directory traversal, symlink, junction and hard-link abuse | Generated file paths, disjoint data roots and checks of path components, SQLite sidecars and regular-file link counts. | Path tests cover aliases and hard links. ACLs must prevent concurrent path replacement by untrusted local processes. |
| SQL injection and diagnostics | Bound SQLite values, validation of IDs, sanitized error codes, redacted validation bodies and private responses with no-store. | HTTP tests use malformed identifiers and invalid fields. Process and proxy logs must exclude cookies, secrets, request bodies and sensitive query strings. |
| Malicious PDF and resource exhaustion | Streaming byte limits, a networkless Compose parser, bounded exports, quotas, model and export budgets and process-owned worker slots. | Compose delegates all original-document parsing. SHA-256 verifies identity. Standalone private mode uses the original in-process parser. |
| Concurrent quota bypass and ZIP abuse | Per-tenant SQLite write transactions for upload count, original PDF bytes, queue depth and retries. Shared windows for upload, review, export and model calls. Snapshot checks bind all requested export formats to the same revision. | Concurrent upload and enqueue tests, independent processes, quota and export rate tests. Disk quotas outside the app must cover results, logs, staging and proxy buffers. |
| Stolen invitation | Random expiring code, only its hash persisted, atomic redemption. Bootstrap and issue operations use local CLI, and issuance requires an active administrator. | Tests exercise expiry, races and replay. A stolen unused code can register an unverified email before expiry. Distribute it privately. |
| Unauthorized scientific review | Account-scoped Store, review revisions and separate authenticated actor audit. | Tests check review writes on foreign resources, preserved historical decisions and offline archive validation. User review is separate from the official scientific release. |
| Worker crash | Reclaimed SQLite slots and per-document locks, replayable jobs, checkpoints and cumulative per-attempt budgets. | Tests hard-kill a real Windows worker after a checkpoint, challenge global and per-user concurrency and resume the same job. |
| GigaChat request abuse | The owner budget is reserved in SQLite before a provider call. The job stores an independent request budget, and retries are bounded. | Concurrency tests cover the shared accounting. A crash can conservatively consume a reservation. Local analysis and CI do not use live GigaChat. |

The suite also verifies migration dry-run, resumed publication, owner
conflicts, content SHA-256, append-only audit history and unchanged snapshot
hash. Password hashing uses the Argon2id defaults of the installed
`argon2-cffi` package. [Argon2 documentation](https://argon2-cffi.readthedocs.io/en/25.1.0/howto.html)

## Compose deployment

Caddy terminates TLS, serves static assets, overwrites forwarded IP and
protocol from the immediate connection, removes `Forwarded`, and preserves
`Host` for the allowlist check. Only its configured IPv4 address is trusted.
Uvicorn disables proxy header processing and access logs. Caddy uses a
dedicated internal backend network and disables HTTP request logs. HTML
and API responses use no-store. The frontend CSP permits same-origin
scripts, workers and connections, inline styles, and blob image or worker
resources. The proxy includes HSTS, framing protection, no-referrer and
nosniff. The local CA and automated public ACME are separate configurations.
See [Deployment](DEPLOYMENT.md).

### PDF parsing surfaces

| Path | Compose execution |
| --- | --- |
| Upload validation, `upload_store._pdf_pages` | The networkless service checks document type, encryption, repair status and page count |
| Human review source, `upload_reviews._check_page` | The service returns page count and whether the selected page has text |
| Private export, `upload_export._verified_original` | The API verifies SHA-256 and delegates PDF structure through `_pdf_pages`; the passport renderer creates a new PDF |
| Worker batch extraction, `batch._process` | The service runs unchanged page and table extraction; the worker validates the JSON layout and runs scientific checks, checkpoints and publication |
| Browser evidence | PDF.js renders the authorized original in the browser under CSP |
| Standalone GigaChat and official/private CLIs | These remain operator-controlled and outside the Compose request path |

The sandbox container has no IP network, auth DB, tenant Store, model
credentials, or Docker socket. It receives document bytes and a bounded
operation through one Unix socket. It serializes requests, places one input
document in tmpfs, and starts a fresh parser child. Each child has a
seccomp syscall allowlist, Landlock restrictions on filesystem writes,
15 seconds CPU, 20 seconds wall time, 384 MiB address space and a
16 MiB output limit. The container additionally caps RAM and PIDs. The
parent discards the parser diagnostic streams. The caller independently
validates reply types, page indices and evidence. The parser child policy
is required at startup, including on Linux environments running Docker
Desktop. The worker deadline is 240 seconds.

The operator and kernel remain trusted. The parser uses a shared process
service and its OS limits also bound document size, page count and throughput.
A hostile PDF can make its own result unavailable, consume one parser
slot until timeout or return fabricated text under compromise. Human
review and application checks still apply. The worker and API share full
tenant storage and auth DB by design. Their scientific JSON processing
remains a trusted application surface and receives bounded data from the
separate parser. GigaChat is disabled in Compose. Enabling external
inference requires an independently reviewed worker egress and secret
configuration with the same parser boundary. Public deployment requires
verification of the sandbox on the actual host.

The production gate also requires a public TLS smoke, host disk controls,
monitoring, scans and a separate restore rehearsal. See
[Production readiness](PRODUCTION_READINESS.md).
