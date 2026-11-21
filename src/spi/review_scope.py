"""Validate SHA-bound human scope decisions for the release review."""

import json
from pathlib import Path

import pymupdf
from pydantic import TypeAdapter

from .build_scope_sheet import DISPUTED, IDENTITY, pdf_sha256
from .models import PublicationScope

TRACKS = ('solar', 'rag')
PUBLICATIONS = tuple(f'P{index:03d}' for index in range(1, 91))
FINAL = {'in_scope', 'out_of_scope'}


def current_pdfs(store, root):
    rows = store.documents()
    documents = {doc['document_id']: doc for doc in rows}
    expected = {f'{track}:{pid}' for track in TRACKS for pid in PUBLICATIONS}
    if len(rows) != len(documents) or documents.keys() != expected:
        raise ValueError('Expected unique P001–P090 registrations in each track')
    for track in TRACKS:
        folder = root / 'data' / track / 'test'
        pdfs = [path for path in folder.iterdir() if path.is_file() and path.suffix.lower() == '.pdf']
        if len(pdfs) != len(PUBLICATIONS) or {path.stem for path in pdfs} != set(PUBLICATIONS):
            raise ValueError(f'{track}: PDF folder must contain exactly P001–P090')
        for path in pdfs:
            key = f'{track}:{path.stem}'
            doc = documents[key]
            if (doc['domain'] != track or doc['publication_id'] != path.stem
                    or Path(doc['file_path']).resolve() != path.resolve()):
                raise ValueError(f'{key}: registration has a different identity or path')
            with pymupdf.open(path) as pdf:
                page_count = pdf.page_count
            if pdf_sha256(path) != doc['sha256'] or page_count != doc['page_count']:
                raise ValueError(f'{key}: PDF SHA-256 or page count changed')
    return documents


def validate_pages(pages, limit, key):
    if (not isinstance(pages, list) or not pages
            or any(type(page) is not int or not 1 <= page <= limit for page in pages)
            or len(set(pages)) != len(pages)):
        raise ValueError(f'{key}: source_pages must be unique 1-based integers within 1..{limit}')
    return pages


def approved_decision(row, doc):
    key = doc['document_id']
    approval = row['human_approved'].strip().upper()
    status = row['human_decision'].strip()
    if approval not in {'', 'FALSE', 'TRUE'} or status not in FINAL | {'', 'needs_review'}:
        raise ValueError(f'{key}: unknown human_approved or human_decision')
    if approval != 'TRUE':
        return None
    reviewer, reason = row['reviewer'].strip(), row['comment'].strip()
    if status not in FINAL or not reviewer or not reason:
        raise ValueError(f'{key}: TRUE requires a final decision, named reviewer and factual comment')
    try:
        pages = json.loads(row['source_pages'])
    except json.JSONDecodeError as exc:
        raise ValueError(f'{key}: source_pages must be a JSON list such as [1, 4, 7]') from exc
    return PublicationScope(document_id=key, pdf_sha256=doc['sha256'], status=status,
                            reason=reason, source_pages=validate_pages(pages, doc['page_count'], key))


def validate_review(docs, scopes, queue, sheet, baseline_path, root):
    baseline = TypeAdapter(list[PublicationScope]).validate_json(baseline_path.read_bytes())
    decisions = {row.document_id: row for row in baseline}
    if (len(decisions) != len(baseline) or set(queue) & decisions.keys()
            or set(queue) | decisions.keys() != docs.keys() or sheet.keys() != queue.keys()):
        raise ValueError('Baseline and review queue must cover registrations exactly once and the sheet must match the queue')
    for key, row in decisions.items():
        doc = docs[key]
        digest = doc['sha256']
        if row.status not in FINAL or row.pdf_sha256 != digest or scopes.get((key, digest)) != row:
            raise ValueError(f'{key}: previous approval differs from the registered PDF or Store decision')
        validate_pages(row.source_pages, doc['page_count'], key)

    for key, row in sheet.items():
        doc = docs[key]
        if (any(row[field] != queue[key][field] for field in IDENTITY)
                or row['pdf_sha256'] != doc['sha256'] or row['page_count'] != str(doc['page_count'])
                or (root / row['pdf_path']).resolve() != Path(doc['file_path']).resolve()):
            raise ValueError(f'{key}: scope sheet identity, SHA-256 or source metadata differs from the queue or Store')
        decision = approved_decision(row, doc)
        if decision is not None:
            decisions[key] = decision

    held = DISPUTED & queue.keys() & decisions.keys()
    if held == DISPUTED:
        criteria = {' '.join(sheet[key]['rag_demo_criterion'].split()) for key in DISPUTED}
        if len(criteria) != 1 or '' in criteria:
            raise ValueError('RAG P007/P010 require the same nonempty human-approved rag_demo_criterion')
    elif held:
        for key in held:
            del decisions[key]

    check_scope_conflicts(scopes, docs, decisions)
    return decisions


def check_scope_conflicts(scopes, docs, decisions):
    for (key, digest), scope in scopes.items():
        if scope.status in FINAL and docs.get(key, {}).get('sha256') == digest and decisions.get(key) != scope:
            raise ValueError(f'{key}: saved approval conflicts with the supplied human review')
