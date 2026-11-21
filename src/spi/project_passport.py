"""Read-only, paginated passport for the current SPI Store."""

import json
import re
from collections import Counter, defaultdict
from datetime import datetime

import pymupdf

from .models import ResultRecord, ValidationIssue
from .storage import Store


DOMAINS = {"solar": "Solar", "rag": "RAG"}
UNKNOWN = "нет данных"


class PublicationNotFound(ValueError):
    pass


class AmbiguousPublication(ValueError):
    pass


def passport_filename(run_id: str, domain: str, publication_id: str | None) -> str:
    parts = [run_id, domain]
    if publication_id is not None:
        parts.append(publication_id)
    suffix = "_".join(re.sub(r"[^\w.-]+", "_", part)[:80] or "project" for part in parts)
    return f"scientific-pdf-intelligence_passport_{suffix}.pdf"


def _publication_id(document):
    return document.get("publication_id") or document["document_id"].split(":", 1)[-1]


def _scope_status(decision):
    return decision.status if decision else "решение отсутствует"


def _issue_owners(owner: str | None, issue: ValidationIssue, result_owners):
    owners = {owner} if owner else set()
    for result_id in issue.result_ids:
        owners.add(result_owners.get(result_id))
    for ref in issue.evidence_refs:
        if ref.get("document_id"):
            owners.add(ref["document_id"])
        if ref.get("result_id"):
            owners.add(result_owners.get(ref["result_id"], ref.get("document_id")))
    return owners


def _display(value):
    if value is None:
        return UNKNOWN
    if isinstance(value, str):
        return value if value else '""'
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _wrap(text, font, size, width):
    """Use glyph widths, breaking long tokens when there is no word boundary."""
    for paragraph in text.split("\n"):
        if not paragraph:
            yield ""
            continue
        widths = font.char_lengths(paragraph, fontsize=size)
        start = 0
        while start < len(paragraph):
            length, last_space = 0.0, start
            end = start
            while end < len(paragraph) and length + widths[end] <= width:
                length += widths[end]
                end += 1
                if paragraph[end - 1] == " ":
                    last_space = end
            if end < len(paragraph) and last_space > start:
                end = last_space
            if end == start:
                raise ValueError("Passport text does not fit the page")
            yield paragraph[start:end]
            start = end


def _pdf_character(char, font):
    if char == "\n":
        return char
    if ord(char) <= 0xFFFF and (char.isprintable() or char in "\xa0\u202f"):
        if font.has_glyph(ord(char)):
            return char
        # TextWriter may select a fallback glyph unavailable in the primary
        # face. Check its actual PDF mapping before trusting it.
        with pymupdf.open() as probe:
            page = probe.new_page()
            writer = pymupdf.TextWriter(page.rect)
            writer.append((30, 30), f"a{char}z", font=font)
            writer.write_text(page)
            if page.get_text().rstrip("\n") == f"a{char}z":
                return char
    # MuPDF may truncate non-BMP code points or emit a missing glyph.
    return ascii(char)[1:-1]


class _Report:
    """Two-column field tables with rows that can continue on subsequent A4 pages."""

    margin = 44
    divider = 190
    ink = (0.08, 0.17, 0.24)
    muted = (0.33, 0.42, 0.48)
    accent = (0.05, 0.36, 0.42)
    rule = (0.84, 0.89, 0.91)
    pale = (0.94, 0.97, 0.97)
    size = 9
    leading = 13
    header_label = "ПАСПОРТ ПРОЕКТА"
    header_width = 106
    data_label = "Данные Store"

    def __init__(self, pdf):
        self.pdf = pdf
        self.regular = pymupdf.Font("helv")
        self.bold = pymupdf.Font("hebo")
        self.contents = []
        self.characters = {self.regular: {}, self.bold: {}}
        self.section = ""
        self.caption = ""
        self._new_page()

    def _new_page(self):
        self.page = self.pdf.new_page(width=595, height=842)
        self.right = self.page.rect.width - self.margin
        self.bottom = self.page.rect.height - 50
        self.writer = pymupdf.TextWriter(self.page.rect)
        self.page.draw_line((self.margin, 45), (self.right, 45), color=self.rule)
        self._text(self.margin, 33, "SCIENTIFIC PDF INTELLIGENCE", 9, self.bold)
        self._text(self.right - self.header_width, 33, self.header_label, 8, self.bold)
        self.y = 61

    def _text(self, x, y, text, size, font):
        if text.strip():
            self.writer.append((x, y), text, fontsize=size, font=font)

    def _flush(self):
        self.writer.write_text(self.page, color=self.ink)

    def _next_page(self):
        self._flush()
        self._new_page()

    def _lines(self, text, font, size, width):
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        known = self.characters[font]
        for char in set(text).difference(known):
            known[char] = _pdf_character(char, font)
        return _wrap("".join(map(known.__getitem__, text)), font, size, width)

    def _paragraph(self, text, size, font, *, x=None, leading=None):
        x = self.margin if x is None else x
        leading = leading or size * 1.4
        for line in self._lines(text, font, size, self.right - x):
            if self.y + leading > self.bottom:
                self._next_page()
            self._text(x, self.y + size, line, size, font)
            self.y += leading

    def cover(self, run_id, subject, timestamp, version):
        self._bookmark(1, "Титульный лист")
        self.y = 130
        self.page.draw_rect((self.margin, 103, self.margin + 46, 108), color=None, fill=self.accent)
        self._paragraph("Scientific PDF Intelligence", 30, self.bold)
        self._paragraph("Паспорт проекта", 23, self.regular)
        self.y += 30
        self._paragraph(subject, 19, self.bold)
        self.y += 28
        for label, value in (
            ("Run ID", run_id), ("Сформирован (UTC)", timestamp), ("Версия приложения", version),
        ):
            self.row(label, value)
        self.y += 30
        self._paragraph("Обработка и покрытие", 16, self.bold)
        self.y += 8
        self.paragraph(
            "Паспорт отражает сохранённое состояние Store. Полнота зависит от прочитанных "
            "страниц, доступных источников и текущих решений PublicationScope. "
            "Точность по закрытому эталону не оценивалась."
        )

    def start(self, section, *, new_page=False):
        self.section, self.caption = section, ""
        if new_page or self.y + 130 > self.bottom:
            self._next_page()
        self._bookmark(1, section)
        self._paragraph(section, 17, self.bold)
        self.y += 8

    def _bookmark(self, level, title):
        self.contents.append([level, title, self.page.number + 1,
                              {"kind": pymupdf.LINK_GOTO, "page": self.page.number,
                               "to": pymupdf.Point(self.margin, self.y)}])

    def publication(self, document_id):
        if self.y + 145 > self.bottom:
            self._next_page()
        self._bookmark(2, document_id)
        self._paragraph(document_id, 13, self.bold)

    def paragraph(self, text):
        self._paragraph(text, 10, self.regular)
        self.y += 7

    def _table_header(self):
        self.page.draw_rect((self.margin, self.y, self.right, self.y + 19), color=None, fill=self.pale)
        self._text(self.margin + 6, self.y + 13, "Поле", self.size, self.bold)
        self._text(self.divider + 7, self.y + 13, self.data_label, self.size, self.bold)
        self.y += 19

    def table(self, caption):
        if self.y + 84 > self.bottom:
            self._next_page()
            self._paragraph(self.section, 12, self.bold)
        self.caption = caption
        self.y += 7
        self._paragraph(caption, 11, self.bold)
        if self.y + 50 > self.bottom:
            self._continue_table()
        else:
            self._table_header()

    def _continue_table(self):
        self._next_page()
        self._paragraph(self.section, 12, self.bold)
        self._paragraph(f"{self.caption[:80]} (продолжение)", 10, self.bold)
        self._table_header()

    def row(self, label, value):
        left = list(self._lines(label, self.bold, self.size, self.divider - self.margin - 12))
        right = list(self._lines(_display(value), self.regular, self.size, self.right - self.divider - 14))
        count = max(len(left), len(right))
        offset = 0
        while offset < count:
            capacity = int((self.bottom - self.y - 8) // self.leading)
            if capacity < 1 or (offset == 0 and capacity < min(2, count)):
                if self.caption:
                    self._continue_table()
                else:
                    self._next_page()
                continue
            end = min(count, offset + capacity)
            for index in range(offset, end):
                baseline = self.y + 13 + (index - offset) * self.leading
                if index < len(left):
                    self._text(self.margin + 6, baseline, left[index], self.size, self.bold)
                if index < len(right):
                    self._text(self.divider + 7, baseline, right[index], self.size, self.regular)
            self.y += (end - offset) * self.leading + 8
            self.page.draw_line((self.margin, self.y), (self.right, self.y), color=self.rule, width=0.5)
            offset = end

    def finish(self, *, no_new_id=False):
        self._flush()
        self.pdf.set_toc(self.contents)
        for index, page in enumerate(self.pdf, 1):
            page.draw_line((self.margin, 805), (self.right, 805), color=self.rule)
            writer = pymupdf.TextWriter(page.rect)
            writer.append((self.margin, 822), f"Страница {index} / {len(self.pdf)}",
                          fontsize=8, font=self.regular)
            writer.write_text(page, color=self.muted)
        # TextWriter can embed separate copies of a fallback font on many pages.
        return self.pdf.tobytes(garbage=4, deflate=True, no_new_id=no_new_id)


def _rows(report, fields):
    for key, value in fields.items():
        report.row(key, value)


def _page_states(store, document, checkpoint):
    # Batch metadata is tied to the imported SHA. Legacy imports save per-page
    # text and status separately. Keep only the status/diagnostic fields here.
    pages = checkpoint.get("pages") or store.read(f"{document['domain']}_{_publication_id(document)}_pages.json")
    return [{key: page[key] for key in ("page", "read_status", "ocr_status", "error", "analysis_error")
             if key in page} for page in pages]


def _page_summary(pages):
    return dict(Counter(page.get("read_status", "unknown") for page in pages)) or None


def _ocr_pages(document, pages):
    known = document.get("pages_needing_ocr")
    pending = {page["page"] for page in pages if page.get("ocr_status") == "pending"}
    if known is None and not any(p.get("ocr_status") not in {None, "unknown"} for p in pages):
        return None
    return sorted(set(known or []) | pending)


def _results(report, results, documents, scopes):
    for result in results:
        document = documents.get(result.document_id, {})
        digest = document.get("sha256")
        scope = scopes.get((result.document_id, digest))
        metadata = result.extraction_metadata
        official = bool(scope and scope.status == "in_scope" and result.extraction_status == "verified"
                        and metadata and metadata.pdf_sha256 == digest)
        report.table(result.result_id)
        _rows(report, {
            "document_id": result.document_id,
            "PublicationScope": _scope_status(scope),
            "Участие в выгрузке": "В официальной области по сохранённому scope и статусу"
            if official else "Только для аудита и проверки",
            "extraction_status": result.extraction_status,
            "metric.name": result.metric.name,
            "metric.variant": result.metric.variant,
            "metric_k": result.metric.k,
            "raw.value": result.raw.value,
            "raw.unit": result.raw.unit,
            "normalized.value": result.normalized.value,
            "normalized.unit": result.normalized.unit,
            "source.page (PDF)": result.source.page,
        })
        for key, value in result.source.model_dump(exclude={"page"}).items():
            if value is not None or key in ("fragment", "origin"):
                report.row(f"source.{key}", value)
        report.row("note", result.note)
        if result.context:
            # These fields describe this particular result, including links
            # between measurements, configurations, datasets and metrics.
            for key, value in sorted(result.context.items()):
                report.row(f"context.{key}", value)
        else:
            report.row("context", None)
        if metadata:
            _rows(report, {f"extraction_metadata.{key}": value
                           for key, value in metadata.model_dump(exclude_none=True).items()})
        else:
            report.row("extraction_metadata", None)


def render_project_passport(store: Store, *, run_id: str, domain: str, publication_id: str | None,
                            generated_at: datetime, version: str) -> bytes:
    track = None if domain == "all" else domain
    tracks = (track,) if track else tuple(DOMAINS)
    documents = {d["document_id"]: d for d in store.documents() if track is None or d["domain"] == track}
    selected_id = None
    if publication_id is not None:
        matches = [key for key, doc in documents.items()
                   if publication_id in (_publication_id(doc), key)]
        if not matches:
            raise PublicationNotFound("Publication not found in the requested domain")
        if len(matches) > 1:
            raise AmbiguousPublication("Publication ID is ambiguous. Specify solar or rag, or use document_id")
        selected_id = matches[0]
        tracks = (documents[selected_id]["domain"],)

    rows = store.results(track, include_excluded=True)
    result_owners = {row.result_id: row.document_id for row in rows}
    issues = [issue for owner, issue in store.issue_entries(track, include_excluded=True)
              if selected_id is None or _issue_owners(owner, issue, result_owners) == {selected_id}]
    if selected_id is not None:
        documents = {selected_id: documents[selected_id]}
        rows = [row for row in rows if row.document_id == selected_id]

    scopes = store.publication_scopes()
    decisions = {key: scopes.get((key, doc.get("sha256"))) for key, doc in documents.items()}
    checkpoints = {doc["document_id"]: doc for track_name in tracks for doc in store.batch_documents(track_name)
                   if doc["document_id"] in documents
                   and doc.get("sha256") == documents[doc["document_id"]].get("sha256")}
    pages = {key: _page_states(store, doc, checkpoints.get(key, {})) for key, doc in documents.items()}
    ocr = {key: _ocr_pages(doc, pages[key]) for key, doc in documents.items()}
    by_document: dict[str, list[ResultRecord]] = defaultdict(list)
    for row in sorted(rows, key=lambda row: (row.document_id, row.source.page, row.result_id)):
        by_document[row.document_id].append(row)

    subject = " + ".join(DOMAINS[name] for name in tracks)
    if selected_id:
        subject = f"Публикация {selected_id} ({subject})"
    timestamp = generated_at.isoformat()
    with pymupdf.open() as pdf:
        pdf.set_metadata({"title": "Scientific PDF Intelligence — Паспорт проекта", "author": "Scientific PDF Intelligence"})
        report = _Report(pdf)
        report.cover(run_id, subject, timestamp, version)
        report.start("Сводка обработки", new_page=True)
        report.table("Выбранная область")
        _rows(report, {"Документы": len(documents), "Результаты": len(rows), "Замечания": len(issues)})
        for name in tracks:
            keys = [key for key, doc in documents.items() if doc["domain"] == name]
            track_rows = [row for row in rows if row.domain == name]
            report.table(f"Покрытие {DOMAINS[name]}")
            _rows(report, {
                "Документы": len(keys),
                "Документы с результатами": sum(bool(by_document[key]) for key in keys),
                "Результаты": len(track_rows),
                "Статусы результатов": dict(Counter(row.extraction_status for row in track_rows)),
                "Замечания": sum(issue.domain == name for issue in issues),
                "Решения PublicationScope": dict(Counter(_scope_status(decisions[key]) for key in keys)),
                "Страниц по реестру (известно)": sum(documents[key].get("page_count") or 0 for key in keys),
                "Документы без числа страниц": sum(documents[key].get("page_count") is None for key in keys),
                "Состояния чтения страниц": _page_summary([page for key in keys for page in pages[key]]),
                "Страницы в очереди OCR": sum(len(ocr[key] or []) for key in keys),
                "Документы без сведений OCR": sum(ocr[key] is None for key in keys),
            })

        report.start("Реестр публикаций")
        if not documents:
            report.paragraph("Зарегистрированных публикаций нет.")
        for key, document in sorted(documents.items()):
            decision = decisions[key]
            checkpoint = checkpoints.get(key, {})
            report.table(key)
            _rows(report, {
                "document_id": key,
                "publication_id": _publication_id(document),
                "Название": document.get("title"),
                "Авторы": document.get("authors"),
                "Год": document.get("year"),
                "DOI": document.get("doi") or document.get("DOI"),
                "Домен": document["domain"],
                "Файл": document.get("filename"),
                "SHA-256 PDF": document.get("sha256"),
                "Статус обработки": document.get("processing_status"),
                "Статус batch": checkpoint.get("status"),
                "Версия парсера": checkpoint.get("parser_version") or document.get("parser_version"),
                "Версия схемы": checkpoint.get("schema_version"),
                "PublicationScope": _scope_status(decision),
                "Причина решения": decision.reason if decision else None,
                "Страницы решения (PDF)": decision.source_pages if decision else None,
                "Страниц в PDF": document.get("page_count"),
                "Страницы OCR (PDF)": ocr[key],
                "Состояния чтения страниц": _page_summary(pages[key]),
                "Результаты": len(by_document[key]),
            })

        for name in tracks:
            report.start(f"Результаты {DOMAINS[name]}", new_page=True)
            if name == "rag":
                report.paragraph("metric_k задаёт отсечку метрики. Параметр retrieval top_k приведён в context, когда сохранён.")
            groups = [(key, group) for key, group in by_document.items() if group and group[0].domain == name]
            if not groups:
                report.paragraph("Сохранённых результатов нет.")
            for key, group in groups:
                report.publication(key)
                _results(report, group, documents, scopes)

        report.start("Проверки и замечания", new_page=True)
        if selected_id:
            report.paragraph("Включены замечания, относящиеся только к выбранной публикации.")
        if not issues:
            report.paragraph("Сохранённых замечаний в выбранной области нет.")
        for issue in sorted(issues, key=lambda item: (item.domain, item.issue_id)):
            report.table(issue.issue_id)
            _rows(report, issue.model_dump(exclude={"issue_id", "evidence_refs"}))
            for ref in issue.evidence_refs:
                report.row("evidence_refs", ref)

        report.start("Полнота и ограничения", new_page=True)
        report.table("Требуется проверка")
        _rows(report, {
            "Решение scope отсутствует": sorted(key for key, decision in decisions.items() if decision is None),
            "Scope needs_review": sorted(key for key, decision in decisions.items()
                                         if decision and decision.status == "needs_review"),
            "Исключённые публикации": sorted(key for key, decision in decisions.items()
                                            if decision and decision.status == "out_of_scope"),
            "Публикации без результатов": sorted(key for key in documents if not by_document[key]),
            "Результаты без документа в реестре": sorted(row.result_id for row in rows if row.document_id not in documents),
            "Результаты needs_review": sorted(row.result_id for row in rows if row.extraction_status == "needs_review"),
            "Результаты со статусом extracted": sum(row.extraction_status == "extracted" for row in rows),
            "Без указания origin": sum(row.source.origin is None for row in rows),
            "Без фрагмента источника": sum(not row.source.fragment for row in rows),
            "Без normalized.value": sum(row.normalized.value is None for row in rows),
        })
        for key, document in sorted(documents.items()):
            checkpoint = checkpoints.get(key, {})
            report.table(key)
            _rows(report, {
                "Сохранённые статусы страниц": len(pages[key]),
                "Страниц в PDF": document.get("page_count"),
                "Страницы OCR (PDF)": ocr[key],
                "Непрочитанные страницы (PDF)": [
                    page["page"] for page in pages[key] if page.get("read_status") in {"error", "ocr_required"}
                ] if pages[key] else None,
                "Проблемы чтения страниц": [page for page in pages[key] if page.get("error") or page.get("analysis_error")],
                "Покрытие извлечения": checkpoint.get("extraction_coverage"),
                "Ожидающие фрагменты": checkpoint.get("pending_units"),
                "Дополнительные материалы": checkpoint.get("supplement"),
                "Ошибки обработки": checkpoint.get("errors"),
                "Проблемы разметки": checkpoint.get("layout_issues"),
            })
        report.paragraph(
            "Участие в официальной области указано по сохранённым scope, статусу verified "
            "и привязке к текущему PDF. Дополнительные проверки релизной выгрузки CSV выполняются отдельно."
        )
        return report.finish()
