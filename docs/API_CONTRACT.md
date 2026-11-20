# P0 frontend ↔ backend contract

Internal format only: official 7+7 CSV column names must be read from participant templates.

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

Next: official CSV templates and passport; real Solar D001 p5, RAG D001 p6;
OCR review on RAG D002 p8; then rules, batch processing and validation.
