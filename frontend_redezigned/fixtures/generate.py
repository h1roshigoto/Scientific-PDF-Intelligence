"""Generate the explicitly synthetic browser demo using the team's CSV exporter."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf

from spi.csv_export import Submission, export_submission
from spi.csv_schema import load_schema


FIXTURES = Path(__file__).resolve().parent
PUBLIC = FIXTURES.parent / 'public' / 'demo'


def document_id(record):
    return record['document_id'].split(':', maxsplit=1)[1]


def write_pdf(document, results, issues):
    track = document['domain'].upper()
    pid = document_id(document)
    sources = [result['source'] for result in results if result['document_id'] == document['document_id']]
    sources.extend(ref for issue in issues for ref in issue.get('evidence_refs', []) if ref['document_id'] == document['document_id'])
    pdf = pymupdf.open()
    for number in range(1, document['page_count'] + 1):
        page = pdf.new_page(width=612, height=600)
        page.draw_rect(pymupdf.Rect(0, 0, 612, 132), color=None, fill=(0.93, 0.96, 0.99))
        page.insert_text((44, 43), 'SCIENTIFIC PDF INTELLIGENCE / SYNTHETIC DEMO', fontsize=11, color=(0, 0.4, 0.48))
        page.insert_text((44, 81), f'{track} {pid} - Page {number}', fontname='hebo', fontsize=26, color=(0.06, 0.11, 0.21))
        page.insert_text((44, 108), 'Fixture for verifying document identity and page navigation.', fontsize=12)
        page.insert_text((44, 552), 'Illustrative sample. This PDF contains synthetic values only.', fontsize=11, color=(0.23, 0.31, 0.42))
        top = 183
        shown = set()
        for source in sources:
            fragment = source['fragment']
            if source['page'] != number or fragment in shown:
                continue
            label = source.get('table') or source.get('figure') or 'Review evidence'
            page.insert_text((44, top), label, fontname='hebo', fontsize=15)
            page.draw_rect(pymupdf.Rect(44, top + 12, 568, top + 51), color=(0.69, 0.81, 0.94), fill=(0.89, 0.95, 1))
            page.insert_text((56, top + 36), fragment, fontsize=13)
            shown.add(fragment)
            top += 82
    pdf.save(PUBLIC / f'{document["domain"]}-{pid}.pdf', no_new_id=True, deflate=True)
    pdf.close()


def write_csvs(domain, docs, results, issues):
    schema = load_schema(domain)
    tables = {name: [] for name in schema.tables}
    exclusions = {
        ref['document_id']: issue['reason']
        for issue in issues if issue['rule_id'] == 'publication_scope' and issue.get('observed') == 'out_of_scope'
        for ref in issue.get('evidence_refs', [])
    }
    tables['publications'] = [
        {'publication_id': document_id(doc), 'title': doc['title'], 'in_scope': doc['document_id'] not in exclusions,
         'exclusion_reason': exclusions.get(doc['document_id'])} for doc in docs
    ]
    for result in results:
        source, raw, normalized = result['source'], result['raw'], result['normalized']
        tables['results'].append({
            'publication_id': document_id(result), 'result_id': result['result_id'], 'metric_name': result['metric']['name'],
            'raw_value': raw['value'], 'raw_unit': raw.get('unit'), 'normalized_value': normalized.get('value'),
            'normalized_unit': normalized.get('unit'), 'metric_value': normalized.get('value'), 'metric_unit': normalized.get('unit'),
            'result_origin': source['origin'], 'source_page': source['page'], 'source_fragment': source['fragment'],
            'source_table': source.get('table'), 'source_figure': source.get('figure'), 'extraction_status': 'extracted',
            'note': 'Синтетические данные для проверки интерфейса.',
        })
    results_by_id = {row['result_id']: row for row in results}
    for issue in issues:
        records = issue.get('result_ids', [])
        if records:
            first = results_by_id[records[0]]
            publication, source = document_id(first), first['source']
        else:
            source = issue['evidence_refs'][0]
            publication = document_id(source)
        tables['issues'].append({
            'publication_id': publication, 'issue_id': issue['issue_id'], 'record_ids': ';'.join(records),
            'check_name': issue['rule_id'], 'description': issue['reason'], 'source_page': source['page'],
            'source_fragment': source['fragment'],
        })
    with TemporaryDirectory() as temp:
        artifacts = export_submission(schema, Submission(domain, tables), Path(temp) / domain,
                                      publication_ids=[document_id(doc) for doc in docs])
        destination = PUBLIC / 'csv' / domain
        destination.mkdir(parents=True, exist_ok=True)
        for artifact in artifacts.iterdir():
            (destination / artifact.name).write_bytes(artifact.read_bytes())


if __name__ == '__main__':
    demo = json.loads((FIXTURES / 'demo.json').read_text(encoding='utf-8'))
    PUBLIC.mkdir(parents=True, exist_ok=True)
    for document in demo['documents']:
        write_pdf(document, demo['results'], demo['issues'])
    for track in ('solar', 'rag'):
        write_csvs(track, *([row for row in demo[key] if row['domain'] == track] for key in ('documents', 'results', 'issues')))
