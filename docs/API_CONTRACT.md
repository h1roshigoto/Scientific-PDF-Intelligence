# P0 frontend ↔ backend contract

The API serves the internal format. The [data passport](../passport.md) documents the separate CLI and Python CSV export.

Endpoints:
- GET /health
- GET /documents?domain=solar|rag
- GET /results?domain=solar|rag
- GET /results/{result_id}
- GET /issues?domain=solar|rag
- GET /documents/{document_id}/file
- GET /export/{artifact} (currently internal JSON only)

A document ID is domain-scoped, e.g. solar:D001. All PDF source pages are 1-based.
The result record includes metric (name, k, variant), raw (value, unit),
normalized (value, unit), source (page, fragment/table/section/figure,
row/column, origin), context, extraction_status and note.
Unknown fields stay null; parsing a PDF alone never verifies a scientific claim.
Page OCR status is independent of scientific extraction_status.

See the [frontend handoff](FRONTEND_HANDOFF.md) for Windows startup, CORS,
D001 source links, and end-to-end verification.
