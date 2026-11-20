"""Build the offline demo PDFs from the same fixture snapshot read by the frontend."""
import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import BaseDocTemplate, Frame, LongTable, PageTemplate, Paragraph, Spacer, TableStyle


FIXTURES = Path(__file__).resolve().parent
DOMAINS = {'solar': 'Solar', 'rag': 'RAG'}
INK = colors.HexColor('#101D35')
BLUE = colors.HexColor('#0866BE')
PALE = colors.HexColor('#F3F7FC')
MUTED = colors.HexColor('#4D6079')

LABELS = {
    'document_id': 'Идентификатор файла', 'title': 'Название', 'publication_id': 'Публикация',
    'page_count': 'Страниц', 'pages_needing_ocr': 'Страницы в очереди OCR',
    'processing_status': 'Состояние обработки', 'year': 'Год', 'authors': 'Авторы', 'doi': 'DOI',
    'page': 'Страница PDF', 'section': 'Раздел', 'table': 'Таблица', 'figure': 'Рисунок',
    'row': 'Строка', 'column': 'Столбец', 'fragment': 'Фрагмент', 'origin': 'Происхождение',
    'cited_publication_id': 'Цитируемая публикация', 'device_label': 'Прибор', 'architecture': 'Архитектура',
    'material_name_raw': 'Материал', 'measurement_type': 'Тип измерения', 'scan_direction': 'Направление развёртки',
    'aggregation': 'Агрегация', 'spectrum': 'Спектр', 'light_intensity': 'Интенсивность света',
    'intensity_unit': 'Единица интенсивности', 'temperature': 'Температура', 'layers': 'Слои',
    'layer_order': 'Порядок слоя', 'layer_function': 'Функция', 'layer_material': 'Материал слоя',
    'layer_thickness': 'Толщина', 'thickness_unit': 'Единица толщины', 'deposition_method': 'Нанесение',
    'annealing_temperature': 'Температура отжига', 'annealing_time': 'Время отжига', 'notes': 'Примечания',
    'method': 'Метод', 'generator_model': 'Генератор', 'dataset_name': 'Набор данных', 'label': 'Метка',
}
STATUS = {'verified': 'Сверено', 'extracted': 'Извлечено', 'needs_review': 'Нужна проверка'}
CHECKS = {
    'publication_scope': 'Допуск публикации', 'confirmed_duplicate': 'Подтверждённый повтор',
    'pce_check_pass': 'Формула PCE сошлась', 'pce_mismatch': 'Расхождение PCE',
    'pce_not_evaluable': 'PCE нельзя проверить', 'field_not_reported': 'Не указано',
    'field_extraction_failed': 'Указано, но не извлечено', 'field_undetermined': 'Наличие не установлено',
    'supplement_unavailable': 'Приложение не предоставлено',
}


def register_fonts(explicit):
    candidates = [explicit] if explicit else [Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts',
                                               Path('/usr/share/fonts/truetype/dejavu')]
    for directory in candidates:
        regular, bold = (directory / name for name in ('DejaVuSans.ttf', 'DejaVuSans-Bold.ttf'))
        if regular.is_file() and bold.is_file():
            pdfmetrics.registerFont(TTFont('Report', str(regular)))
            pdfmetrics.registerFont(TTFont('Report-Bold', str(bold)))
            pdfmetrics.registerFontFamily('Report', normal='Report', bold='Report-Bold')
            return
    raise FileNotFoundError('Install DejaVu Sans or pass --font-dir with its regular and bold .ttf files.')


def value_text(value):
    if value is None or value == '':
        return 'Нет данных'
    if isinstance(value, bool):
        return 'Да' if value else 'Нет'
    if isinstance(value, list):
        return '\n'.join(value_text(item) for item in value) or 'Нет записей'
    if isinstance(value, dict):
        return '\n'.join(f'{LABELS.get(key, key)}: {value_text(item)}' for key, item in value.items()) or 'Нет данных'
    return str(value)


def measurement(pair):
    value = pair.get('value')
    return 'Нет данных' if value is None or value == '' else f"{value}{' ' + pair['unit'] if pair.get('unit') else ''}"


def blocks(*parts):
    return '\n'.join(part for part in parts if part)


def choose_scope(snapshot, domain, document_id=None):
    def in_scope(row):
        return domain == 'all' or row['domain'] == domain

    documents = [doc for doc in snapshot['documents'] if in_scope(doc) and
                 (document_id is None or doc['document_id'] == document_id)]
    results = [row for row in snapshot['results'] if in_scope(row) and
               (document_id is None or row['document_id'] == document_id)]
    record_ids = {row['result_id'] for row in results}
    issues = [issue for issue in snapshot['issues'] if in_scope(issue) and (document_id is None or
              record_ids.intersection(issue.get('result_ids', [])) or
              any(ref.get('document_id') == document_id for ref in issue.get('evidence_refs', [])))]
    return documents, results, issues


def result_cells(row):
    metric = row['metric']
    name = metric['name'] + (f"@{metric['k']}" if metric.get('k') is not None else '')
    if metric.get('variant'):
        name += f" / {metric['variant']}"
    context = dict(row.get('context', {}))
    if isinstance(context.get('layers'), list):
        context['layers'] = sorted(context['layers'], key=lambda layer: layer.get('layer_order', float('inf')))
    return [
        blocks(name, row['result_id'], STATUS.get(row.get('extraction_status'), value_text(row.get('extraction_status')))),
        blocks('Исходное:', measurement(row['raw']), 'Нормализованное:', measurement(row['normalized'])),
        blocks('Файл: ' + row['document_id'], value_text(row['source']), value_text(context) if context else '',
               f"Примечание: {row['note']}" if row.get('note') else ''),
    ]


def issue_cells(issue):
    declared, expected = issue.get('observed'), issue.get('expected')
    if issue['rule_id'] == 'publication_scope':
        declared = {'out_of_scope': 'Вне области кейса', 'in_scope': 'В области кейса',
                    'needs_review': 'Допуск нужно проверить'}.get(declared, declared)
    pce = issue['rule_id'] in ('pce_mismatch', 'pce_check_pass')
    return [
        blocks(CHECKS.get(issue['rule_id'], issue['rule_id']), issue['issue_id'],
               f"Правило: {issue['rule_id']}", f"Уровень: {issue['severity']}"),
        blocks(issue['reason'], ('Заявленный PCE: ' if pce else 'Наблюдение: ') + declared if declared else '',
               ('Расчётный PCE: ' if pce else 'Ожидаемое: ') + expected if expected else '',
               'Записи: ' + ', '.join(issue['result_ids']) if issue.get('result_ids') else '',
               value_text(issue['evidence_refs']) if issue.get('evidence_refs') else ''),
    ]


def make_report(snapshot, path, domain, document_id=None):
    docs, results, issues = choose_scope(snapshot, domain, document_id)
    metadata = snapshot['passport_snapshot']
    timestamp = datetime.fromisoformat(metadata['generated_at']).astimezone(timezone(timedelta(hours=3), 'МСК'))
    scope = document_id or ('Solar + RAG' if domain == 'all' else DOMAINS[domain])
    styles = {
        'kicker': ParagraphStyle('kicker', fontName='Report-Bold', fontSize=10, leading=14, textColor=BLUE, spaceAfter=5),
        'title': ParagraphStyle('title', fontName='Report-Bold', fontSize=29, leading=34, textColor=INK, spaceAfter=7),
        'subtitle': ParagraphStyle('subtitle', fontName='Report', fontSize=12, leading=17, textColor=MUTED, spaceAfter=11),
        'body': ParagraphStyle('body', fontName='Report', fontSize=9.5, leading=14, textColor=INK, spaceAfter=7),
        'section': ParagraphStyle('section', fontName='Report-Bold', fontSize=15, leading=19, textColor=INK,
                                  spaceBefore=14, spaceAfter=7, keepWithNext=True),
        'detail': ParagraphStyle('detail', fontName='Report-Bold', fontSize=10.5, leading=14, textColor=BLUE,
                                 spaceBefore=9, spaceAfter=5, keepWithNext=True),
        'header': ParagraphStyle('header', fontName='Report-Bold', fontSize=8.7, leading=12, textColor=colors.white),
        'cell': ParagraphStyle('cell', fontName='Report', fontSize=8.7, leading=12, textColor=INK),
    }

    def paragraph(text, style='body'):
        return Paragraph(escape(text).replace('\n', '<br/>'), styles[style])

    frame_height = A4[1] - 36 * mm

    def table(headers, rows, widths):
        columns = [width * mm for width in widths]
        header = [paragraph(label, 'header') for label in headers]
        header_height = max(cell.wrap(width - 12, frame_height)[1] for cell, width in zip(header, columns)) + 10
        style = TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), BLUE),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, PALE]),
            ('LINEBELOW', (0, 0), (-1, -1), 0.25, colors.HexColor('#D9E4F0')),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ])
        blocks = []
        normal_rows = []

        def flush():
            if normal_rows:
                blocks.append(LongTable([header, *normal_rows], colWidths=columns, repeatRows=1, hAlign='LEFT', style=style))
                normal_rows.clear()

        for row in rows:
            cells = [paragraph(text, 'cell') for text in row]
            row_height = max(cell.wrap(width - 12, frame_height)[1] for cell, width in zip(cells, columns)) + 10
            if row_height + header_height <= frame_height:
                normal_rows.append(cells)
                continue
            flush()
            for label, text in zip(headers, row):
                blocks.extend([paragraph(label, 'detail'), paragraph(text)])
            blocks.append(Spacer(1, 2 * mm))
        flush()
        return blocks

    flow = [paragraph('SCIENTIFIC PDF INTELLIGENCE', 'kicker'), paragraph('Паспорт проекта', 'title'),
            paragraph(f'{scope} / Синтетические данные', 'subtitle'),
            paragraph(f"Запуск {metadata['run_id']} / Сформирован {timestamp:%Y-%m-%d %H:%M %Z} / Дата снимка демонстрационной выборки"),
            paragraph('Отчёт содержит все записи выбранной области из демонстрационного снимка.'),
            paragraph('Охват обработки', 'section')]
    coverage = []
    for track, label in DOMAINS.items():
        if domain != 'all' and domain != track:
            continue
        track_docs = [doc for doc in docs if doc['domain'] == track]
        excluded = {ref['document_id'] for issue in issues if issue['rule_id'] == 'publication_scope' and
                    issue.get('observed') == 'out_of_scope' for ref in issue.get('evidence_refs', [])}
        coverage.append([label, str(len(track_docs)), str(sum(doc['document_id'] in excluded for doc in track_docs)),
                         str(sum(len(doc.get('pages_needing_ocr', [])) for doc in track_docs)),
                         str(sum(row['domain'] == track for row in results)), str(sum(issue['domain'] == track for issue in issues))])
    flow.extend(table(['Трек', 'PDF', 'Исключено', 'Страниц на OCR', 'Результатов', 'Проверок'], coverage, [25, 20, 33, 38, 32, 32]))

    flow.append(paragraph('Реестр публикаций и научные записи', 'section'))
    if not docs:
        flow.append(paragraph('В выбранной области нет публикаций.'))
    for doc in docs:
        flow.append(paragraph(f"{doc['document_id']} / {doc['title']}", 'detail'))
        pub_data = {key: value for key, value in doc.items() if key not in ('title', 'domain')}
        for field in ('authors', 'year', 'doi'):
            pub_data.setdefault(field, None)
        flow.append(paragraph(value_text(pub_data)))
        doc_results = [row for row in results if row['document_id'] == doc['document_id']]
        if doc_results:
            flow.extend(table(['Метрика и ID записи', 'Значения', 'Контекст и источник'],
                              [result_cells(row) for row in doc_results], [51, 42, 87]))
        else:
            flow.append(paragraph('В снимке нет строк результатов для этой публикации.'))
        flow.append(Spacer(1, 2 * mm))

    flow.append(paragraph('Проверки, доступность и первоисточники', 'section'))
    if issues:
        flow.extend(table(['Проверка и ID', 'Обоснование и источники'], [issue_cells(issue) for issue in issues], [58, 122]))
    else:
        flow.append(paragraph('Для выбранной области нет зарегистрированных замечаний.'))

    flow.append(paragraph('Контроль состава', 'section'))
    flow.append(paragraph(f'Публикаций: {len(docs)}. Записей результатов: {len(results)}. Проверок: {len(issues)}.'))
    flow.append(paragraph('Номера страниц относятся к страницам исходных PDF и считаются с единицы.'))

    def footer(canvas, doc):
        canvas.setStrokeColor(PALE)
        canvas.line(15 * mm, 15 * mm, 195 * mm, 15 * mm)
        canvas.setFont('Report', 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(15 * mm, 10 * mm, 'Scientific PDF Intelligence / Синтетические данные')
        canvas.drawRightString(195 * mm, 10 * mm, f'Страница {doc.page}')

    doc = BaseDocTemplate(str(path), pagesize=A4, title=f'Паспорт проекта / {scope}', author='Scientific PDF Intelligence')
    frame = Frame(15 * mm, 20 * mm, 180 * mm, frame_height, leftPadding=0, rightPadding=0, bottomPadding=0, topPadding=0)
    doc.addPageTemplates(PageTemplate(id='passport', frames=[frame], onPage=footer))
    doc.build(flow)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--fixture', type=Path, default=FIXTURES / 'demo.json')
    cli.add_argument('--output', type=Path, default=FIXTURES.parent / 'public' / 'demo' / 'passports')
    cli.add_argument('--font-dir', type=Path)
    args = cli.parse_args()
    register_fonts(args.font_dir)
    snapshot = json.loads(args.fixture.read_text(encoding='utf-8'))
    args.output.mkdir(parents=True, exist_ok=True)
    run = snapshot['passport_snapshot']['run_id']
    scopes = [('all', None), ('solar', None), ('rag', None)]
    scopes.extend((doc['domain'], doc['document_id']) for doc in snapshot['documents'])
    for domain, document_id in scopes:
        ident = document_id.split(':', 1)[1] if document_id else ''
        name = f'scientific-pdf-intelligence_passport_{run}_{domain}' + (f'_{ident}' if ident else '')
        safe_name = re.sub(r'[^\w.-]+', '_', name)
        make_report(snapshot, args.output / (safe_name + '.pdf'), domain, document_id)


if __name__ == '__main__':
    main()
