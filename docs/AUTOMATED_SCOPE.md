# Evidence-verified Codex scope review (opt-in)

The 180-PDF test release has 176 queued scope decisions (88 Solar and 88 RAG).
This command analyzes the **local, original PDFs** using two independent
non-interactive Codex calls per document. It is designed to replace manual
*proposal preparation*, not to claim a human read the paper.

## What it does

- Reads every PDF page locally with PyMuPDF, with explicit page numbers.
- Checks the SHA-256 and page count against the existing scope approval sheet.
- Runs two separate, ephemeral, read-only Codex sessions (normal and skeptical).
- Requires two literal excerpts from **two distinct PDF pages in each pass**,
  correct page numbers, and agreement between both decisions.
- Produces a resumable machine evidence manifest. No rows in
  scope-approval.csv, publication_scopes.json, original PDFs, or batch checkpoints
  are changed. It never writes human_approved=TRUE.
- Abstains (needs_review) on missing PDF text/OCR, overlarge PDFs, unclear scope,
  missing quotes, model disagreement, or model failure.
- Treats RAG P007 and P010 as an indivisible pair under one published
  retrieved-demonstrations criterion. Both must pass evidence checks, or neither
  is accepted by the release audit.

This is an **opt-in machine-review route** to the scope gate. It does not change
the independent scientific completeness gate, PDF quote checking, or the Solar/RAG
participant validators. A machine-reviewed scope is not a human endorsement,
and even a passing machine scope cannot release unverified results.

## Setup (Windows PowerShell)

From the repository containing the 180 private PDFs and Store:

    $root = "C:\Users\hiros\Dokumente\ChatGPT\Scientific-PDF-Intelligence"
    Set-Location $root
    codex --version
    codex login status

Use a code checkout that contains this PR. Avoid switching branches in a dirty
working tree: create a detached worktree after fetching the feature branch.

    git fetch origin codex/p0-machine-scope-evidence-review
    $work = Join-Path $env:TEMP "spi-auto-scope-review"
    git worktree add --detach $work FETCH_HEAD
    $env:PYTHONPATH = Join-Path $work "src"
    $env:PYTHONUTF8 = "1"

### Small pilot (no Store mutation)

    python -B -X utf8 -m spi.automated_scope --root $root --limit 3

The manifest is written to outputs/p0-22-release/machine-scope.json.
Each record includes original SHA-256, the two model responses, exact quotes,
source pages, and an explicit status. Failed or uncertain records remain
needs_review; they are not silently promoted.

### Remaining papers (resumable)

    python -B -X utf8 -m spi.automated_scope --root $root

On re-run, existing records are kept. To retry abstentions with the same model:

    python -B -X utf8 -m spi.automated_scope --root $root --retry-unresolved

Do not set human_approved in scope-approval.csv. The local Codex sign-in may incur
model usage costs. PDF text is sent to the selected Codex model; keep this
workflow on a trusted computer with permission to process the papers.

### Audit with the machine evidence (read-only snapshot)

    python -B -X utf8 -m spi.release_readiness --root $root --machine-scope outputs/p0-22-release/machine-scope.json

Inspect the emitted report.json. It identifies accepted machine decisions and
remaining scope cases separately; science can remain BLOCKED, especially while
all 180 checkpoints are partial. Do not report final 14 CSV readiness unless
the complete release audit and both original validators pass.

## Limitations

- Scanned/near-empty pages require OCR, and are **not** auto-approved by this
  version. Do not upload arbitrary OCR sidecars as verifiable citations.
- Two agreement passes are not statistically independent model families and
  are not a guarantee of scientific correctness. Literal quotes prove textual
  grounding, not the logic of the classification.
- Exclusions require affirmative evidence of a scope mismatch. Missing fields
  or a missing search term alone cannot justify out_of_scope.
- This does not batch-reprocess missing scientific measurements or add data to
  the participant CSVs. Those are a separate evidence-and-extraction workstream.
