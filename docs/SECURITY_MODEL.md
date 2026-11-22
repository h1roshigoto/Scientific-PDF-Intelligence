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
| Malicious PDF and resource exhaustion | Streaming byte limits, PDF signature and structural checks, bounded exports, storage and queue quotas, model and export budgets and process-owned worker slots. | PyMuPDF parses uploads and checks export/review evidence in the API as well as extracting in the worker. SHA-256 verifies identity. Production must constrain both parsing surfaces. |
| Concurrent quota bypass and ZIP abuse | Per-tenant SQLite write transactions for upload count, original PDF bytes, queue depth and retries. Shared windows for upload, review, export and model calls. Snapshot checks bind all requested export formats to the same revision. | Concurrent upload and enqueue tests, independent processes, quota and export rate tests. Disk quotas outside the app must cover results, logs, staging and proxy buffers. |
| Stolen invitation | Random expiring code, only its hash persisted, atomic redemption. Bootstrap and issue operations use local CLI, and issuance requires an active administrator. | Tests exercise expiry, races and replay. A stolen unused code can register an unverified email before expiry. Distribute it privately. |
| Unauthorized scientific review | Account-scoped Store, review revisions and separate authenticated actor audit. | Tests check review writes on foreign resources, preserved historical decisions and offline archive validation. User review is separate from the official scientific release. |
| Worker crash | Reclaimed SQLite slots and per-document locks, replayable jobs, checkpoints and cumulative per-attempt budgets. | Tests hard-kill a real Windows worker after a checkpoint, challenge global and per-user concurrency and resume the same job. |
| GigaChat request abuse | The owner budget is reserved in SQLite before a provider call. The job stores an independent request budget, and retries are bounded. | Concurrency tests cover the shared accounting. A crash can conservatively consume a reservation. Local analysis and CI do not use live GigaChat. |

The suite also verifies migration dry-run, resumed publication, owner
conflicts, content SHA-256, append-only audit history and unchanged snapshot
hash. Password hashing uses the Argon2id defaults of the installed
`argon2-cffi` package. [Argon2 documentation](https://argon2-cffi.readthedocs.io/en/25.1.0/howto.html)

## Production

Before accepting public traffic, complete these infrastructure checks.

1. Serve a single HTTPS origin for the React build and `/api/`. Use a valid
   certificate and restrict the backend listener to loopback or a private network.
   Configure the proxy to overwrite forwarded headers with its directly
   observed client IP and protocol. List each immediate trusted proxy IP
   in `SPI_AUTH_TRUSTED_PROXIES`. Start Uvicorn with
   `--no-proxy-headers --no-access-log` so application trust rules see its
   immediate peer. Test TLS, HSTS, browser cookies, proxy header handling and
   unauthorized origins end to end.
2. Run the API and PDF worker without administrator privileges. Establish
   OS-level isolation for PDF parsing and processing, restricted filesystem
   mounts, low privileges, CPU, memory, disk, wall time and process limits.
   Use an appropriate sandbox or container policy for both the upload
   validation and the extraction surfaces. A separate worker process gives
   application isolation through verified paths. The current worker service
   identity can still read the configured tenant root.
3. Restrict network egress to endpoints necessary for the enabled
   configuration, including GigaChat only when enabled. The host filesystem
   must be local and provide reliable SQLite locks across processes. Apply
   OS disk quotas to originals, queues, processing, temporary files and ZIP
   generation. Budget upload connections and body sizes at the proxy.
4. Restore a tested backup of the account database and tenant root, rehearse
   revocation after restore and document recovery from a corrupt tenant Store.
   Monitor abuse, worker backlog, failures, disk exhaustion and backups
   without logging secrets or private scientific data. The current app has
   no email verification, password recovery or hardened PDF sandbox.

For example, an HTTPS nginx server can map the prefix to a loopback Uvicorn
listener with this location block. Set the server name in
`SPI_AUTH_ALLOWED_ORIGINS` and set the actual proxy IP in
`SPI_AUTH_TRUSTED_PROXIES`. Set the proxy request body limit to the deployment
maximum PDF size plus multipart overhead, accounting for user overrides.
[nginx proxy directive reference](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)

```nginx
location /api/ {
    proxy_pass http://127.0.0.1:8000/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 26m;
    access_log off;
}
```

Serve only the React build distribution and its intended static files.
Set frontend headers including `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`, `X-Frame-Options: DENY` and a frontend CSP.
Start with the following policy and verify PDF.js and downloads in the
deployed browser. Send `Cache-Control: no-store` for the app HTML.

```text
default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'
```

Production TLS, OS sandbox policies, backups and operational monitoring are
deployment work. Local HTTP mode is opt-in and accepts loopback origins
and clients only.
