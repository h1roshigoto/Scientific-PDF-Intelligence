import csv
import json
import sys
from argparse import Namespace
from pathlib import Path

import pymupdf
import pytest

from spi import build_scope_sheet as builder, release_readiness as release, review_scope, store_export
from spi.csv_export import read_submission
from spi.csv_schema import load_schema
from spi.storage import Store


def write_csv(path, fields, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def make_document(root, track, pid):
    path = root / 'data' / track / 'test' / f'{pid}.pdf'
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((40, 65), f'Synthetic {track.upper()} Publication {pid}', fontsize=18)
        pdf.save(path)
    digest = builder.pdf_sha256(path)
    key = f'{track}:{pid}'
    doc = dict(document_id=key, domain=track, publication_id=pid, sha256=digest,
               file_path=str(path), page_count=1)
    queue = dict(document_id=key, domain=track, publication_id=pid, pdf_sha256=digest,
                 pdf_path=path.relative_to(root).as_posix(), page_count='1', preliminary_status='needs_review',
                 agent_proposal='in_scope', reason='Candidate subject', evidence_origin='Synthetic example',
                 native_text_pointers='[]', evidence_pages='[1]', review_start_pages='[1]')
    return doc, queue


def test_scope_sheet_appends_preserving_all_existing_fields(tmp_path):
    _, first = make_document(tmp_path, 'solar', 'P001')
    _, second = make_document(tmp_path, 'solar', 'P002')
    queue, sheet = (tmp_path / name for name in ('queue.csv', 'working-approval.csv'))
    write_csv(queue, first, [first])
    assert builder.build_scope_sheet(sheet, queue_path=queue, root=tmp_path) == (1, None)
    edited = builder.read_approval(sheet)['solar:P001']
    edited.update(human_decision='out_of_scope', human_approved='TRUE', reviewer='Данил', source_pages='[1]',
                  comment='Ручное основание, "цитата"\nВторая строка', scope_basis='Исправленное основание')
    write_csv(sheet, builder.FIELDS, [edited])
    original = sheet.read_bytes()
    with pytest.raises(FileExistsError, match='--append'):
        builder.build_scope_sheet(sheet, queue_path=queue, root=tmp_path)
    assert builder.build_scope_sheet(sheet, queue_path=queue, root=tmp_path, append=True) == (0, None)
    assert original == sheet.read_bytes()

    # An incomplete queue cannot remove rows already reviewed by a human.
    write_csv(queue, second, [second])
    count, backup = builder.build_scope_sheet(sheet, queue_path=queue, root=tmp_path, append=True)
    assert count == 1 and backup.read_bytes() == original
    rows = builder.read_approval(sheet)
    assert rows['solar:P001'] == edited
    assert rows['solar:P002']['agent_proposal'] == 'in_scope'
    assert all(rows['solar:P002'][field] == '' for field in builder.INPUTS)
    after_append = sheet.read_bytes()
    assert builder.build_scope_sheet(sheet, queue_path=queue, root=tmp_path, append=True) == (0, None)
    assert sheet.read_bytes() == after_append


@pytest.mark.parametrize('fault', ['actual_sha', 'queue_sha', 'duplicate', 'write', 'concurrent_edit'])
def test_scope_sheet_failure_preserves_reviews(tmp_path, monkeypatch, fault):
    doc, first = make_document(tmp_path, 'solar', 'P001')
    _, second = make_document(tmp_path, 'solar', 'P002')
    queue, sheet = (tmp_path / name for name in ('queue.csv', 'approval.csv'))
    write_csv(queue, first, [first])
    builder.build_scope_sheet(sheet, queue_path=queue, root=tmp_path)
    original = sheet.read_bytes()
    rows = [first, second]
    expected = ValueError
    match = 'SHA-256'
    if fault == 'actual_sha':
        with Path(doc['file_path']).open('ab') as pdf:
            pdf.write(b'\n% Different PDF bytes\n')
    elif fault == 'queue_sha':
        first['pdf_sha256'] = 'f' * 64
    elif fault == 'duplicate':
        rows.append(dict(first, reason='Contradictory proposed reason'))
        match = 'duplicate publication'
    write_csv(queue, first, rows)
    with monkeypatch.context() as patch:
        if fault == 'write':
            def fail_replace(*args):
                raise OSError('injected write failure')
            patch.setattr(builder.os, 'replace', fail_replace)
            expected, match = OSError, 'injected write failure'
        if fault == 'concurrent_edit':
            edited = dict(builder.read_approval(sheet)['solar:P001'], comment='Edit while the builder was running')
            digest = builder.pdf_sha256

            def edit_sheet(path):
                write_csv(sheet, builder.FIELDS, [edited])
                return digest(path)
            patch.setattr(builder, 'pdf_sha256', edit_sheet)
            match = 'edited during the build'
        with pytest.raises(expected, match=match):
            builder.build_scope_sheet(sheet, queue_path=queue, root=tmp_path, append=True)
    if fault == 'concurrent_edit':
        assert builder.read_approval(sheet) == {'solar:P001': edited}
    else:
        assert sheet.read_bytes() == original
    assert not list(tmp_path.glob('.*.tmp'))
    assert not list(tmp_path.glob('.*.lock'))


def test_rag_pair_requires_one_explicit_shared_criterion(tmp_path):
    docs = {}
    queue = {}
    for pid in ('P007', 'P010'):
        doc, source = make_document(tmp_path, 'rag', pid)
        docs[doc['document_id']] = doc
        queue[doc['document_id']] = source
    baseline = tmp_path / 'baseline.json'
    baseline.write_text('[]', encoding='utf-8')
    sheet = {key: dict(row, human_approved='TRUE', human_decision='in_scope', reviewer='Станислав',
                       comment='Evidence', source_pages='[1]', rag_demo_criterion='external demonstrations')
             for key, row in queue.items()}
    sheet['rag:P010']['human_approved'] = ''
    assert review_scope.validate_review(docs, {}, queue, sheet, baseline, tmp_path) == {}
    sheet['rag:P010'].update(human_approved='TRUE', rag_demo_criterion='another criterion')
    with pytest.raises(ValueError, match='same nonempty'):
        review_scope.validate_review(docs, {}, queue, sheet, baseline, tmp_path)
    sheet['rag:P010']['rag_demo_criterion'] = 'external demonstrations'
    assert review_scope.validate_review(docs, {}, queue, sheet, baseline, tmp_path).keys() == docs.keys()


def make_release(tmp_path, monkeypatch, scenario):
    state = tmp_path / 'state'
    queue_path, approval, baseline = (tmp_path / name for name in ('queue.csv', 'approval.csv', 'baseline.json'))
    contracts = tmp_path / 'contracts'
    solar, rag = (contracts / t / 'submission_template' for t in ('solar', 'rag'))
    store = Store(state)
    baseline.write_text('[]', encoding='utf-8')
    queue, results = [], []
    publications = ('P001', 'P002')
    monkeypatch.setattr(release, 'PUBLICATIONS', publications)
    monkeypatch.setattr(review_scope, 'PUBLICATIONS', publications)
    monkeypatch.setattr(release, 'find_spec', lambda _: object())
    for track, template in (('solar', solar), ('rag', rag)):
        schema = load_schema(track)
        template.mkdir(parents=True)
        for name, table in schema.tables.items():
            write_csv(template / f'{name}.csv', table.columns, [])
        if track == 'rag':
            manifest = {
                'version': schema.version, 'official_rag_template': False,
                'tables': {name: list(table.columns) for name, table in schema.tables.items()},
                'keys': {name: table.key for name, table in schema.tables.items()},
                'enums': {f'{name}.{col}': list(values) for name, table in schema.tables.items()
                          for col, values in table.enums.items()},
            }
            (template / 'schema.json').write_text(json.dumps(manifest), encoding='utf-8')
        checker = template.parent / 'tools' / 'check_submission.py'
        checker.parent.mkdir()
        code = 2 if scenario == 'validator' and track == 'rag' else 0
        checker.write_text(f'raise SystemExit({code})\n', encoding='utf-8')
        for pid in publications:
            doc, review = make_document(tmp_path, track, pid)
            store.save_document(doc, [])
            queue.append(review)
            key, digest = doc['document_id'], doc['sha256']
            store.save_batch_document({'document_id': key, 'sha256': digest, 'status': 'processed',
                                       'pages_needing_ocr': [], 'fragments': {}})
            context = ({'top_k': 20, 'generator_model': 'm', 'dataset_name': 'ds', 'task_type': 'QA',
                        'evaluation_protocol': 'exact match'} if track == 'rag' else
                       {'device_label': 'A', 'measurement_type': 'J-V', 'material_name_raw': 'Absorber A',
                        'layers': [{'layer_role': 'absorber'}]})
            results.append(dict(
                result_id=key, document_id=key, domain=track,
                metric={'name': 'Recall' if track == 'rag' else 'PCE', 'k': 5 if track == 'rag' else None},
                raw={'value': '0.00', 'unit': '%'}, normalized={'value': 0.0},
                source={'page': 1, 'fragment': 'Exact, "quoted"\nvalue', 'origin': 'primary'},
                context=context, extraction_status='verified',
                extraction_metadata={'model_id': 'local', 'method': 'pymupdf_verified_table',
                                     'pdf_sha256': digest, 'schema_version': 'test', 'profile': 'test'},
            ))
            if scenario == 'quality' and track == 'solar' and pid == 'P001':
                results.append(dict(results[-1], result_id=f'{key}:unreviewed', extraction_status='needs_review'))
            if track == 'solar' and pid == 'P001':
                primary = results[-1]
                results.append(dict(primary, result_id=f'{key}:cited', source=dict(primary['source'],
                                    origin='cited', cited_publication_id='Reference 7'), extraction_status='verified'))
    store.write('results.json', results)
    write_csv(queue_path, queue[0], queue)
    builder.build_scope_sheet(approval, queue_path=queue_path, root=tmp_path)
    reviewed = builder.read_approval(approval)
    for key, row in reviewed.items():
        if scenario == 'scope' and key == 'rag:P001':
            continue
        row.update(human_decision='in_scope' if row['publication_id'] == 'P001' else 'out_of_scope',
                   human_approved='TRUE', reviewer='Human reviewer', source_pages='[1]', comment='Evidence')
    write_csv(approval, builder.FIELDS, reviewed.values())
    args = Namespace(root=tmp_path, state=state, queue=queue_path, baseline=baseline, approval=approval,
                     previous_release_state=tmp_path / 'previous', contracts=contracts,
                     solar_template=solar, rag_template=rag)
    return args


@pytest.mark.parametrize('scenario', ['ready', 'scope', 'quality', 'validator'])
def test_release_dry_run_isolated_and_gated(tmp_path, monkeypatch, scenario):
    args = make_release(tmp_path, monkeypatch, scenario)
    originals = {p: p.read_bytes() for p in (*args.state.rglob('*'), args.approval) if p.is_file()}
    run = tmp_path / 'dry-run'
    run.mkdir()
    report = release.audit(args, run)
    assert all(entry['status'] == 'PASS' for entry in report['contracts'].values())
    steps = report['steps']
    assert steps['corpus']['status'] == 'PASS'
    assert {p: p.read_bytes() for p in originals} == originals
    assert not list(run.glob('snapshot-*'))
    if scenario == 'scope':
        assert steps['scope']['status'] == 'BLOCKED' and steps['scope']['pending'] == 1
        registry = (run / 'scope-review-pending.csv').read_text(encoding='utf-8-sig')
        assert 'rag:P001' in registry and 'proposal only: in_scope' in registry
    if scenario == 'quality':
        assert steps['science']['status'] == 'BLOCKED'
        assert report['tracks']['solar']['quality_gate']['needs_review'] == 1
    if scenario in {'scope', 'quality'}:
        assert steps['csv_format']['status'] == steps['solar_validator']['status'] == 'NOT RUN'
    if scenario == 'validator':
        assert steps['solar_validator']['status'] == 'PASS'
        assert steps['rag_validator']['status'] == 'BLOCKED'
    if scenario != 'ready':
        assert 'candidate' not in report and not (run / 'candidate').exists()
        return
    assert all(step['status'] == 'PASS' for step in steps.values())
    assert len(list((run / 'candidate').rglob('*.csv'))) == 14
    for track in ('solar', 'rag'):
        tables = read_submission(load_schema(track), run / 'candidate' / track).tables
        assert [row['in_scope'] for row in tables['publications']] == ['true', 'false']
        assert {row['publication_id'] for row in tables['results']} == {'P001'}
        result = tables['results'][0]
        assert (result['metric_value'], result['metric_unit'], result['raw_value'], result['raw_unit']) == (
            '0.0', None, '0.00', '%',
        )
        assert result['source_fragment'] == 'Exact, "quoted"\nvalue'
        if track == 'rag':
            assert (result['metric_k'], tables['configurations'][0]['top_k']) == ('5', '20')
            assert tables['experiments'][0]['evaluation_protocol'] == 'exact match'
        else:
            assert tables['results'][1]['result_origin'] == 'cited'
            assert tables['results'][1]['cited_publication_id'] == 'Reference 7'
            assert tables['measurements'][0]['measurement_type'] == 'J-V'
            assert tables['materials'][0]['material_name_raw'] == 'Absorber A'
            assert tables['layers'][0]['device_id'] == result['device_id']


def test_all_pdfs_cli_blocks_partial_science_and_preview_keeps_status(tmp_path, monkeypatch, capsys):
    args = make_release(tmp_path, monkeypatch, 'quality')
    store = Store(args.state)
    docs = {doc['document_id']: doc for doc in store.documents()}
    decisions = review_scope.validate_review(docs, {}, builder.read_queue(args.queue),
                                            builder.read_approval(args.approval), args.baseline, args.root)
    store.save_publication_scopes(list(decisions.values()))
    pdfs = args.root / 'data/solar/test'
    output = tmp_path / 'submission'
    monkeypatch.setattr(sys, 'argv', ['store_export', 'solar', str(output), '--pdf-dir', str(pdfs),
                                      '--state', str(args.state), '--all-pdfs'])
    with pytest.raises(SystemExit) as error:
        store_export.main()
    assert error.value.code == 2
    assert 'scientific release gate is blocked' in capsys.readouterr().err
    assert not output.exists()
    preview = store_export.project_submission(store, load_schema('solar'), pdfs, publication_ids=['P001'])
    assert {row['result_id'] for row in preview.tables['results']} == {'solar:P001', 'solar:P001:cited'}
    assert {row.result_id: row.extraction_status for row in store.results('solar')}[
        'solar:P001:unreviewed'
    ] == 'needs_review'

@pytest.mark.parametrize('track', ['solar', 'rag'])
def test_submission_projection_keeps_registered_metadata_and_explicit_result_context(tmp_path, monkeypatch, track):
    args = make_release(tmp_path, monkeypatch, 'ready')
    store = Store(args.state)
    docs = store.documents()
    target = next(doc for doc in docs if doc['domain'] == track and doc['publication_id'] == 'P001')
    target.update(title='Подтвержденная работа', year=2024, DOI='10.1234/test', authors='А. Автор; Б. Автор')
    store.write('documents.json', docs)

    results = store.read('results.json')
    expected_context = (
        {'spread_value': '0.05', 'n_devices': 3, 'time_point': '25', 'time_unit': 'h'}
        if track == 'solar' else
        {'metric_group': 'retrieval', 'metric_direction': 'higher', 'baseline_value': '0.4', 'sample_size': 21}
    )
    for result in results:
        if result['document_id'] == target['document_id']:
            result['context'].update(expected_context)
    store.write('results.json', results)

    all_docs = {doc['document_id']: doc for doc in docs}
    decisions = review_scope.validate_review(
        all_docs, {}, builder.read_queue(args.queue),
        builder.read_approval(args.approval), args.baseline, args.root,
    )
    submission = store_export.project_submission(
        store, load_schema(track), args.root / 'data' / track / 'test',
        publication_ids=['P001', 'P002'],
        reviewed_scopes={(decision.document_id, decision.pdf_sha256): decision
                         for decision in decisions.values()},
    )
    publication, excluded = submission.tables['publications']
    assert publication == {
        'publication_id': 'P001', 'title': 'Подтвержденная работа', 'year': 2024,
        'DOI': '10.1234/test', 'authors': 'А. Автор; Б. Автор',
        'in_scope': True, 'exclusion_reason': None,
    }
    assert excluded['title'] is None and excluded['DOI'] is None and excluded['authors'] is None
    assert excluded['year'] is None

    result = next(row for row in submission.tables['results'] if row['result_origin'] == 'primary')
    assert {key: result[key] for key in expected_context} == expected_context
    output = tmp_path / f'candidate-{track}'
    from spi.csv_export import export_submission
    export_submission(load_schema(track), submission, output, publication_ids=['P001', 'P002'])
    actual = read_submission(load_schema(track), output).tables
    assert actual['publications'][0]['title'] == 'Подтвержденная работа'
    assert actual['publications'][0]['year'] == '2024'
    assert actual['publications'][0]['DOI'] == '10.1234/test'
    assert actual['publications'][0]['authors'] == 'А. Автор; Б. Автор'
    assert all(actual['results'][0][key] == str(value) for key, value in expected_context.items())


def test_opt_in_machine_scope_can_clear_scope_gate_without_human_impersonation(tmp_path, monkeypatch):
    from spi.models import PublicationScope

    args = make_release(tmp_path, monkeypatch, 'scope')
    args.machine_scope = tmp_path / 'machine-scope.json'

    def synthetic_machine_decision(path, root, docs, sheet):
        assert path == args.machine_scope
        key = 'rag:P001'
        return {key: PublicationScope(document_id=key, pdf_sha256=docs[key]['sha256'],
                                      status='in_scope', reason='Two verified synthetic PDF excerpts',
                                      source_pages=[1])}

    monkeypatch.setattr(release, 'machine_decisions', synthetic_machine_decision)
    run = tmp_path / 'machine-audit'
    run.mkdir()
    report = release.audit(args, run)
    assert report['steps']['scope']['status'] == 'PASS'
    assert report['steps']['scope']['pending'] == 0
    assert report['machine_scope']['accepted'] == 1
    assert builder.read_approval(args.approval)['rag:P001']['human_approved'] == ''
