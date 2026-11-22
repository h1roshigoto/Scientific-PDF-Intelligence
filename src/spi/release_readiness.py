"""Audit the 180-PDF test release and validate a temporary CSV candidate."""

import argparse
from collections import Counter
import csv
from importlib.util import find_spec
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory, mkdtemp

from .automated_scope import machine_decisions
from .batch_checks import quality_gate
from .build_scope_sheet import DISPUTED, cell, read_approval, read_queue
from .csv_export import export_submission, read_submission, validate_submission
from .csv_schema import load_schema
from .review_scope import (
    FINAL, PUBLICATIONS, TRACKS, check_scope_conflicts, current_pdfs, validate_review,
)
from .storage import Store
from .store_export import project_submission, release_quality


def scope_counts(keys, decisions):
    counts = Counter(decisions[key].status if key in decisions else 'needs_review' for key in keys)
    return {status: counts[status] for status in ('in_scope', 'out_of_scope', 'needs_review')}


def audit_store(store, docs, decisions=None):
    for track in TRACKS:
        hashes = {key: doc['sha256'] for key, doc in docs.items() if doc['domain'] == track}
        checkpoints = [row for row in store.batch_documents(track)
                       if row['document_id'] in hashes and row.get('sha256') == hashes[row['document_id']]]
        seen = {row['document_id'] for row in checkpoints}
        if len(seen) != len(checkpoints):
            raise ValueError(f'{track}: duplicate current checkpoints')
        results = [row for row in store.results(track) if row.document_id in hashes]
        issues = list(store.issue_entries(track))
        states = Counter(row['status'] for row in checkpoints)
        has_checkpoint_inventory = seen == hashes.keys()
        has_ocr_inventory = has_checkpoint_inventory and all('pages_needing_ocr' in row for row in checkpoints)
        coverage = quality_gate(hashes, results, checkpoints)
        yield track, {
            'registered': len(hashes),
            'processed': states['processed'],
            'partial': states['partial'],
            'checkpoint_statuses': dict(states),
            'missing_current_checkpoints': sorted(hashes.keys() - seen),
            'results': dict(Counter(row.extraction_status for row in results)),
            'unresolved': coverage['unresolved'] if has_checkpoint_inventory else 'UNKNOWN',
            'ocr_pending_pages': sum(len(set(row['pages_needing_ocr'])) for row in checkpoints)
            if has_ocr_inventory else 'UNKNOWN',
            'validation_issues': dict(Counter(issue.severity for _, issue in issues)),
            'scope': scope_counts(hashes, decisions) if decisions is not None else 'UNKNOWN',
            'quality_gate': release_quality({key: hashes[key] for key in hashes
                                             if key in decisions and decisions[key].status == 'in_scope'},
                                            results, checkpoints, issues) if decisions is not None else 'UNKNOWN',
        }


def write_review_queue(path, store, queue, sheet, decisions):
    fields = ('document_id', 'domain', 'publication_id', 'pdf_sha256', 'pdf_path', 'title',
              'review_reason', 'evidence_pages', 'review_start_pages', 'proposal_status',
              'scope_status', 'human_approved', 'review_note', 'checkpoint_status',
              'checkpoint_errors', 'ocr_pending_pages')
    batch = {row['document_id']: row for track in TRACKS for row in store.batch_documents(track)}
    with path.open('x', newline='', encoding='utf-8-sig') as file:
        writer = csv.DictWriter(file, fieldnames=fields, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for key, row in sheet.items():
            if key in decisions:
                continue
            source = queue[key]
            checkpoint = batch.get(key, {})
            if checkpoint.get('sha256') != row['pdf_sha256']:
                checkpoint = {}
            proposal = row['agent_proposal']
            review_note = row['comment']
            if key in DISPUTED:
                review_note = 'Review P007 and P010 under the same human-approved criterion. ' + review_note
            writer.writerow(dict(
                {field: row[field] for field in ('document_id', 'domain', 'publication_id', 'pdf_sha256', 'pdf_path')},
                title=cell(row['title']), review_reason=cell(source['reason']), evidence_pages=source['evidence_pages'],
                review_start_pages=row['review_pages'],
                proposal_status=f'proposal only: {proposal}' if proposal in FINAL else 'no final proposal',
                scope_status='needs_review', human_approved=row['human_approved'], review_note=cell(review_note),
                checkpoint_status=checkpoint.get('status', 'UNKNOWN'),
                checkpoint_errors=json.dumps(checkpoint.get('errors'), ensure_ascii=False),
                ocr_pending_pages=json.dumps(checkpoint.get('pages_needing_ocr')),
            ))


def run_validator(track, validator, candidate, pdfs, template, run):
    args = [sys.executable, '-B', '-X', 'utf8', str(validator), str(candidate), str(pdfs)]
    if track == 'rag':
        args += ['--schema', str(template / 'schema.json'), '--json-report', str(run / 'rag-validator.json')]
    process = subprocess.run(args, cwd=run, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             encoding='utf-8', errors='replace', check=False, timeout=300)
    (run / f'{track}-validator.log').write_text(process.stdout, encoding='utf-8')
    review_required = (process.returncode == 2 if track == 'rag' else process.returncode == 0 and any(
        line.startswith('внимание:') for line in process.stdout.splitlines()
    ))
    if review_required:
        return {'status': 'BLOCKED', 'detail': 'Participant checker reported items for human review',
                'exit_code': process.returncode}
    return {'status': 'PASS' if process.returncode == 0 else 'FAIL', 'exit_code': process.returncode}


def state_inventory(state):
    files = {}
    for path in state.rglob('*.json'):
        stat = path.stat()
        files[path.relative_to(state)] = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
    return files


def audit(args, run):
    steps = {name: {'status': 'NOT RUN'} for name in (
        'store', 'corpus', 'scope', 'science', 'csv_format', 'solar_validator', 'rag_validator',
    )}
    report = {'steps': steps, 'tracks': {track: 'UNKNOWN' for track in TRACKS}, 'contracts': {}}
    schemas = {}
    validators = {}
    for track in TRACKS:
        template = getattr(args, f'{track}_template')
        validator = args.contracts / track / 'tools' / 'check_submission.py'
        validators[track] = validator
        try:
            pinned = load_schema(track)
            required = [validator, *(template / f'{name}.csv' for name in pinned.tables)]
            if track == 'rag':
                required.append(template / 'schema.json')
            missing = [str(path) for path in required if not path.is_file()]
            if missing:
                raise FileNotFoundError(', '.join(missing))
            if {path.name for path in template.glob('*.csv')} != {f'{name}.csv' for name in pinned.tables}:
                raise ValueError(f'{template}: unexpected CSV filenames')
            schemas[track] = load_schema(track, template)
            report['contracts'][track] = {'status': 'PASS', 'version': pinned.version,
                                          'files': {f'{name}.csv': list(table.columns)
                                                    for name, table in pinned.tables.items()}}
        except FileNotFoundError as error:
            report['contracts'][track] = {'status': 'BLOCKED', 'detail': str(error)}
        except (OSError, ValueError) as error:
            report['contracts'][track] = {'status': 'FAIL', 'detail': str(error)}

    with TemporaryDirectory(dir=run, prefix='snapshot-') as tmp:
        snapshot = Path(tmp)
        stage = 'store'
        try:
            if not (args.state / 'documents.json').is_file():
                raise FileNotFoundError(args.state / 'documents.json')
            before = state_inventory(args.state)
            shutil.copytree(args.state, snapshot / 'state', ignore=shutil.ignore_patterns('*-lock.sqlite3*'))
            if before != state_inventory(args.state):
                steps[stage] = {'status': 'BLOCKED', 'detail': 'Store changed during snapshot; retry after writes finish'}
                return report
            store = Store(snapshot / 'state')
            scopes = store.publication_scopes()
            docs = {doc['document_id']: doc for doc in store.documents()}
            report['tracks'] = dict(audit_store(store, docs))
            steps[stage] = {'status': 'PASS'}

            stage = 'corpus'
            docs = current_pdfs(store, args.root)
            steps[stage] = {'status': 'PASS'}

            stage = 'scope'
            queue, sheet = read_queue(args.queue), read_approval(args.approval)
            decisions = validate_review(docs, scopes, queue, sheet, args.baseline, args.root)
            if getattr(args, 'machine_scope', None) is not None:
                automated = machine_decisions(args.machine_scope, args.root, docs, sheet)
                overlap = automated.keys() & decisions.keys()
                if any(automated[key] != decisions[key] for key in overlap):
                    raise ValueError('Machine scope conflicts with an already confirmed decision')
                decisions.update(automated)
                report['machine_scope'] = {
                    'mode': 'two-pass evidence-verified model decisions; not human approval',
                    'accepted': len(automated),
                    'remaining': len(sheet.keys() - decisions.keys()),
                }
            if args.previous_release_state.exists():
                if not (args.previous_release_state / 'documents.json').is_file():
                    raise ValueError('Existing release-state has no document registry')
                previous = Store(args.previous_release_state)
                if {doc['document_id']: doc for doc in previous.documents()} != docs:
                    raise ValueError('Existing release-state has a different document registry')
                check_scope_conflicts(previous.publication_scopes(), docs, decisions)
            write_review_queue(run / 'scope-review-pending.csv', store, queue, sheet, decisions)
            pending = len(docs) - len(decisions)
            steps[stage] = {'status': 'BLOCKED' if pending else 'PASS', 'pending': pending,
                            'confirmed': len(decisions), 'confirmed_in_worksheet': len(decisions.keys() & sheet.keys())}

            stage = 'science'
            report['tracks'] = dict(audit_store(store, docs, decisions))
            ready = all(item['quality_gate']['release_ready'] for item in report['tracks'].values())
            steps[stage] = {'status': 'PASS' if ready and not pending else 'BLOCKED',
                            'detail': 'Gate for confirmed in-scope publications only' if pending else 'Full approved scope'}
            if pending or not ready or len(schemas) != len(TRACKS):
                return report
            if find_spec('pypdf') is None:
                steps['csv_format'] = {'status': 'BLOCKED', 'detail': 'Install pypdf for participant evidence checks'}
                return report

            stage = 'csv_format'
            bundles = snapshot / 'candidate'
            bundles.mkdir()
            pdf_dirs = {}
            reviewed = {(key, row.pdf_sha256): row for key, row in decisions.items()}
            for track, schema in schemas.items():
                pdfs = snapshot / f'{track}-pdfs'
                pdfs.mkdir()
                for pid in PUBLICATIONS:
                    shutil.copyfile(docs[f'{track}:{pid}']['file_path'], pdfs / f'{pid}.pdf')
                pdf_dirs[track] = pdfs
                submission = project_submission(store, schema, pdfs, publication_ids=PUBLICATIONS,
                                                reviewed_scopes=reviewed, require_ready=True)
                export_submission(schema, submission, bundles / track, publication_ids=PUBLICATIONS)
                validate_submission(schema, read_submission(schema, bundles / track), publication_ids=PUBLICATIONS)
            steps[stage] = {'status': 'PASS'}

            for track in TRACKS:
                stage = f'{track}_validator'
                steps[stage] = run_validator(track, validators[track], bundles / track, pdf_dirs[track],
                                             getattr(args, f'{track}_template'), run)
            if all(steps[f'{track}_validator']['status'] == 'PASS' for track in TRACKS):
                stage = 'csv_format'
                bundles.rename(run / 'candidate')
                report['candidate'] = str(run / 'candidate')
        except FileNotFoundError as error:
            steps[stage] = {'status': 'BLOCKED', 'detail': str(error)}
        except ValueError as error:
            steps[stage] = {'status': 'BLOCKED' if stage in {'scope', 'corpus'} else 'FAIL', 'detail': str(error)}
        except (OSError, RuntimeError, csv.Error, KeyError, TypeError, subprocess.SubprocessError) as error:
            steps[stage] = {'status': 'FAIL', 'detail': str(error)}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd(), help='Workspace with data/<track>/test')
    parser.add_argument('--state', type=Path, default=Path('outputs/p0-21-test-local/state'))
    parser.add_argument('--queue', type=Path, default=Path('outputs/p0-21-test-local/scope-human-review.csv'))
    parser.add_argument('--baseline', type=Path, default=Path('outputs/test-corpus-intake/previously-reviewed-scopes.json'))
    parser.add_argument('--approval', type=Path, default=Path('outputs/p0-22-release/scope-approval.csv'))
    parser.add_argument('--previous-release-state', type=Path, default=Path('outputs/p0-22-release/release-state'))
    parser.add_argument('--machine-scope', type=Path, help='Opt-in SHA-bound two-pass Codex evidence manifest; never sets human_approved')
    parser.add_argument('--contracts', type=Path, default=Path('outputs/contracts'))
    parser.add_argument('--solar-template', type=Path,
                        default=Path('outputs/p0-19-20261121-052958-94843d/solar-participant-template'))
    parser.add_argument('--rag-template', type=Path, default=Path('outputs/contracts/rag/submission_template'))
    args = parser.parse_args(argv)
    args = argparse.Namespace(**{name: path.resolve() if isinstance(path, Path) else path
                                 for name, path in vars(args).items()})
    run = Path(mkdtemp(prefix='spi-release-')).resolve()
    report = audit(args, run)
    statuses = {entry['status'] for group in ('steps', 'contracts') for entry in report[group].values()}
    status = 'FAIL' if 'FAIL' in statuses else 'PASS' if 'candidate' in report else 'BLOCKED'
    report['status'] = status
    (run / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'{status}: {run / "report.json"}')
    for name, step in report['steps'].items():
        print(f'{name}: {step["status"]}' + (f' — {step["detail"]}' if 'detail' in step else ''))
    return {'PASS': 0, 'BLOCKED': 2, 'FAIL': 1}[status]


if __name__ == '__main__':
    raise SystemExit(main())
