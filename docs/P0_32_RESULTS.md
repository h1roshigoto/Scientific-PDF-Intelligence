# P0 №32. Production hardening and release validation

**RELEASE HARDENING: BLOCKED**

**PUBLIC LAUNCH: NOT READY**

**PUBLIC PRODUCTION SMOKE: NOT RUN**

Base `4fd20ba8e365794a8fcc23740423b55962d690d7`. PR №23 had successful [frontend and backend checks](https://github.com/h1roshigoto/Scientific-PDF-Intelligence/actions/runs/44971700658) and [deployment checks](https://github.com/h1roshigoto/Scientific-PDF-Intelligence/actions/runs/44971700704) on its head. P0 №32 release evidence is produced from the new run and specific OCI digests.

| Local Trivy 0.76.2 | CRITICAL | HIGH | MEDIUM | LOW | UNKNOWN | Secrets |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Base runtime | 0 | 50 | 100 | 81 | 7 | 0 |
| Base proxy | 0 | 2 | 14 | 4 | 1 | 0 |
| Candidate runtime | 0 | 50 | 74 | 79 | 7 | 0 |
| Candidate proxy | 0 | 1 | 13 | 4 | 1 | 0 |

Caddy build metadata and the rescan confirmed the fix for CVE-2026-78661. Debian package revisions eliminated 26 MEDIUM and 2 LOW findings in glibc. The remaining HIGH records, individual packages, layers, advisory sources and residual risks are in [OCI triage](OCI_VULNERABILITY_TRIAGE.md) and the [versioned inventory](../deploy/security/oci-findings.json). The proxy retains CVE-2026-116587 in go-jose/v3. There are no approved exceptions.

## Local verification

- Docker Desktop 29.9.2, Compose 5.6.1, Linux 6.18.58 and cgroup v2. The local and production Compose configurations were rendered. Effective non-root UID, capabilities, no-new-privileges, read-only roots, isolated networking and mounts, CPU, RAM, swap, PID and nofile were checked for the five running services.
- Linux Landlock and seccomp denied unrelated file reads, file writes and mutations, socket creation, fork and exec. The request document remained readable and the inherited output descriptor writable. Unix socket tests checked split frames, size and page limits, invalid JSON and field types, a deadline and disconnect. Disposable tests also exercise parser CPU, address space, file size, descriptors, cgroup CPU and OOM behaviour, and ENOSPC on a limited mount.
- Local Caddy HTTPS used CA and hostname verification. Checks covered spoofed forwarding, an untrusted backend network peer, Host, Origin, secure cookies, CSRF, CORS and concealed ops routes. Parser child SIGKILL generated a recoverable upload failure. Worker SIGKILL after checkpoint preserved the job ID and data.
- Alice and Bob uploaded synthetic Solar and RAG PDFs in parallel. The Compose scenario checks 4 Solar and 16 RAG results, Alice approval, Bob correction, both ZIP archives, tenant isolation, review history and original SHA-256 through restore. PyMuPDF and the scientific rules retain their existing versions and behaviour.
- Cold backup refused an active writer and survived an interrupted backup without a published archive. Restic checked ciphertext, denied a wrong key, restored a byte-identical backup and produced a dry-run retention plan. The restore scenario covers rejected archives, interrupted publication, a separate Compose project and refusal to overwrite a dataset. A backup failure alert arrived at a disposable local receiver.
- Backend and frontend counts and final job status should be matched with the evidence of the PR run. Local Windows verification passed 328 tracked pytest tests, 29 Vitest and 48 Playwright tests in Edge. The three pre-existing untracked files were kept outside the commit.

## Windows commands

Run from the repository root in PowerShell. Install a verified restic binary on PATH for encrypted rehearsal.

```powershell
git fetch origin main
git switch main
git merge --ff-only origin/main
git switch codex/p0-32-release-hardening
docker version
docker compose version
$spi = @('-p','spi-dev','-f','compose.yaml','-f','compose.local.yaml')
docker compose @spi config --quiet
docker compose @spi build
docker compose @spi up --no-build -d --wait
docker compose @spi cp proxy:/data/caddy/pki/authorities/local/root.crt "$env:TEMP/spi-root.crt"
curl.exe --cacert "$env:TEMP/spi-root.crt" https://localhost:8443/api/health
python deploy/scripts/container_check.py --project spi-dev --layers
python deploy/scripts/compose_e2e.py --encrypted
```

```powershell
$scan = Join-Path $env:TEMP 'spi-oci-review'
New-Item -ItemType Directory -Force $scan | Out-Null
$trivy = 'ghcr.io/aquasecurity/trivy:0.76.2@sha256:8f6bb947dbc528902e11f2f5be52f7b519f6a39d5d148f41166f2040499768fb'
foreach ($image in @('runtime','proxy')) {
  docker image save "spi-${image}:local" -o (Join-Path $scan "$image.tar")
  docker run --rm -v "${scan}:/scan" $trivy image --input "/scan/$image.tar" --scanners vuln,secret --format json --output "/scan/$image.json"
  docker run --rm -v "${scan}:/scan" $trivy image --input "/scan/$image.tar" --format cyclonedx --output "/scan/$image.cdx.json"
}
docker compose @spi stop proxy worker api
docker compose @spi run --rm --no-deps backup create rehearsal
docker compose @spi run --rm --no-deps backup verify rehearsal
docker compose @spi down
```

`compose_e2e.py --encrypted` creates and restores a separate disposable repository and dataset. For an operator-managed backup, copy the approved TAR to private staging and use the Python restic commands in [Backup and restore](BACKUP_RESTORE.md). The temporary backup returned by the local stack remains in its local volume after `down`.

## Host controls awaiting evidence

Public DNS/TLS, Linux host firewall and quotas, operator alert routing, validated external encrypted retention and a public authenticated E2E remain `NOT RUN`. The production gate requires evidence for the exact deployment and an independent restore environment. See [Production readiness](PRODUCTION_READINESS.md), [Operations](OPERATIONS_RUNBOOK.md) and the [release checklist](RELEASE_CHECKLIST.md).
