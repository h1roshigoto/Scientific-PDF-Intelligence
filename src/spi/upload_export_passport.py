"""Render a private passport from the same captured data as CSV and JSON."""

from collections import Counter

import pymupdf

from .project_passport import _Report, _display
from .upload_reviews import ReviewError
from .upload_store import _positive_limit, mupdf_lock

MAX_FIELD_CHARS = 12_000
REVIEW_LABELS = {
    "automatic": "Автоматические результаты",
    "approved": "Подтверждённые исходные",
    "approved_corrected": "Подтверждённые исправленные",
    "unconfirmed": "Неподтверждённые",
    "pending": "Ожидают решения",
    "correction_proposed": "Предложены исправления",
    "rejected": "Отклонённые",
    "stale": "Устаревшие решения",
}
WARNING_LABELS = {
    "scientific_completeness_unverified": "Научная полнота не установлена. Статус обработки описывает "
                                          "техническое состояние, а решение рецензента относится к отдельной записи.",
    "private_not_official_submission": "Частные данные загрузки. Пакет не имеет статуса официальной выгрузки.",
    "zero_approved_results": "Архив аудита. Актуальных подтверждённых записей нет.",
    "incomplete_ocr": "Текст части страниц ещё требует OCR.",
    "local_only": "Выполнен локальный анализ. Извлечение с помощью языковой модели не выполнялось.",
    "partial_analysis": "Охват анализа неполон. Состояние отдельных страниц и фрагментов доступно в issues.json.",
    "unresolved_issues": "Подтверждение записи сохраняет научные замечания открытыми.",
}


class _PrivateReport(_Report):
    header_label = "ЧАСТНЫЙ ПАСПОРТ"
    header_width = 84
    data_label = "Данные проекта"

    def __init__(self, pdf):
        self.max_pages = _positive_limit("SPI_UPLOAD_EXPORT_MAX_PDF_PAGES", 200)
        super().__init__(pdf)

    def _new_page(self):
        if len(self.pdf) >= self.max_pages:
            raise ReviewError(413, "upload_export_too_large")
        super()._new_page()

    def row(self, label, value):
        display = _display(value)
        if len(display) > MAX_FIELD_CHARS:
            value = display[:MAX_FIELD_CHARS] + "\n[Сокращено. Полный текст в JSON архива.]"
        super().row(label, value)

    def start(self, section, *, new_page=False):
        if not new_page:
            self.y += 12
        super().start(section, new_page=new_page)

    def private_cover(self, metadata):
        self._bookmark(1, "Титульный лист")
        self.y = 130
        self.page.draw_rect((self.margin, 103, self.margin + 46, 108), color=None, fill=self.accent)
        self._paragraph("Scientific PDF Intelligence", 30, self.bold)
        self._paragraph("Private scientific project", 23, self.regular)
        self._paragraph("Частный паспорт научного проекта", 16, self.bold)
        self.y += 28
        for label, value in (
            ("Исходный файл", metadata.original_filename),
            ("Направление", "Solar" if metadata.domain == "solar" else "RAG"),
            ("Upload ID", metadata.upload_id), ("PDF SHA-256", metadata.pdf_sha256),
            ("Job ID", metadata.processing_job_id), ("Обработка", metadata.processing_status),
            ("Сформирован (UTC)", metadata.exported_at.isoformat()), ("Формат", metadata.format),
            ("Версия приложения", metadata.application_version), ("Снимок SHA-256", metadata.snapshot_sha256),
        ):
            self.row(label, value)


def _overview(report, data, summary):
    report.start("Сводка", new_page=True)
    for key, label in REVIEW_LABELS.items():
        report.row(label, getattr(summary.review_counts, key))
    report.row("Научные замечания", summary.issue_counts.total)
    report.row("Страниц в ожидании OCR", len(summary.ocr_pending_pages))
    report.row("Страницы в ожидании OCR", list(summary.ocr_pending_pages))
    report.row("Режим обработки", summary.processing_mode)
    report.row("Охват извлечения", summary.extraction_coverage)
    report.row("Страниц проанализировано", data.processing.counts.pages_analyzed)
    report.row("Фрагментов в ожидании", data.processing.counts.pending_fragments)
    report.row("Состояние проверки", summary.user_review_status)
    report.row("Научная полнота", "не подтверждена")


def _record(report, result, caption):
    report.table(caption)
    for label, value in (
        ("Метрика", result.metric.name), ("Порог метрики k", result.metric.k), ("Вариант", result.metric.variant),
        ("Исходное значение", result.raw.value), ("Исходная единица", result.raw.unit),
        ("Нормализованное значение", result.normalized.value), ("Нормализованная единица", result.normalized.unit),
        ("Контекст", result.context), ("Страница PDF", result.source.page),
        ("Доказательство", result.source.model_dump(mode="json", exclude_none=True)),
        ("Извлечение", result.extraction_status),
    ):
        report.row(label, value)


def _results(report, rows):
    report.start("Проверенные результаты")
    if not rows:
        report.paragraph(WARNING_LABELS["zero_approved_results"])
    for row in rows:
        decision = row.decision
        report.publication(row.original_result.result_id)
        corrected = decision.status == "approved_corrected"
        _record(report, row.original_result, "Автоматическое извлечение" if corrected else "Подтверждённый оригинал")
        if corrected:
            _record(report, row.reviewed_result, "Подтверждённое исправление")
        report.table("Решение рецензента")
        for label, value in (
            ("Статус", decision.status), ("Review ID", decision.review_id), ("Ревизия", decision.revision),
            ("Время (UTC)", decision.reviewed_at.isoformat()), ("Метка рецензента", decision.reviewer_label),
            ("Основание", decision.reason), ("Режим сверки", decision.evidence_mode),
            ("Обоснование нормализации", decision.normalization_evidence),
            ("Замечания учтены", decision.warnings_acknowledged),
            ("Связанные замечания", [issue.issue_id for issue in row.issues]),
        ):
            report.row(label, value)


def _limitations(report, issues, summary):
    report.start("Замечания и ограничения")
    for warning in summary.warnings:
        report.paragraph(f"{warning.code} · {WARNING_LABELS[warning.code]}")
    report.paragraph("Полный текст доступен в JSON. Символы без поддержки шрифтом отображаются как Unicode escape.")
    for issue in issues:
        report.table(f"{issue.rule_id} · {issue.severity}")
        for label, value in (
            ("ID замечания", issue.issue_id), ("Причина", issue.reason), ("Связанные записи", issue.result_ids),
            ("Ссылки на доказательства", issue.evidence_refs), ("Наблюдение", issue.observed), ("Ожидание", issue.expected),
        ):
            report.row(label, value)


def _history(report, events):
    report.start("История проверки")
    report.paragraph("Полная машиночитаемая история, основания и исходные записи находятся в review_history.json.")
    report.paragraph("Метка рецензента вводится при проверке. Общий токен доступа не удостоверяет личность.")
    report.row("Всего событий", len(events))
    for label, count in sorted(Counter((item.event.action, item.applicability) for item in events).items()):
        report.row(" · ".join(label), count)


def render_passport(data, preview):
    with mupdf_lock, pymupdf.open() as pdf:
        report = _PrivateReport(pdf)
        report.private_cover(preview.metadata)
        _overview(report, data, preview.validation)
        _results(report, data.reviewed_results)
        _limitations(report, data.issues, preview.validation)
        _history(report, data.events)
        return report.finish(no_new_id=True)
