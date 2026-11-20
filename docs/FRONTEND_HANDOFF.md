# Передача API Александру

## Запуск на Windows

Из корня репозитория в PowerShell выполните следующие команды. Порты фронтенда приведены для Vite, при другом порте укажите его точный origin.

```powershell
python -m pip install -e '.[dev]'
$env:SPI_STATE_DIR = Join-Path (Get-Location) 'state'
$env:SPI_CORS_ORIGINS = 'http://localhost:5173,http://127.0.0.1:5173'
python -m uvicorn spi.api:app --host 127.0.0.1 --port 8000
```

На новой машине положите оригиналы в `data/solar/dev/D001.pdf` и `data/rag/dev/D001.pdf`. Подготовьте локальное состояние командой `python -m spi.gigachat_pipeline --state state`. Используйте маскированный ввод `GIGACHAT_CREDENTIALS`, а при необходимости задайте `GIGACHAT_CA_BUNDLE_FILE`. Команда использует два заданных PDF, а проверенные ответы повторно читает из кэша. Фронтенд обращается только к SPI API. Ключ GigaChat нужен процессу пайплайна. [Инструкция по безопасному вводу ключа и TLS](GIGACHAT_SMOKE.md#подготовка-и-запуск)

## Маршруты

| Метод и маршрут | Ответ |
| --- | --- |
| `GET /health` | Готовность API |
| `GET /documents` | Документы обоих треков. Поддерживает `?domain=solar` и `?domain=rag` |
| `GET /results?domain=solar` | Результаты Solar |
| `GET /results?domain=rag` | Результаты RAG |
| `GET /results/solar:D001:voc` | Результат Pristine / Voc |
| `GET /results/rag:D001:em` | Результат RA-CoT / LLaMA3-8B-Instruct / HotpotQA / EM |
| `GET /documents/{document_id}/file` | Оригинал PDF с `Content-Type: application/pdf` и `Content-Disposition: inline` |
| `GET /issues?domain=solar` | Замечания из хранилища SPI. Для RAG используйте `domain=rag` |

Для ссылки на доказательство используйте `${apiBase}/documents/${encodeURIComponent(result.document_id)}/file#page=${result.source.page}`. Страницы нумеруются с единицы. Solar открывается по `http://127.0.0.1:8000/documents/solar:D001/file#page=5`, RAG по `http://127.0.0.1:8000/documents/rag:D001/file#page=6`. Фрагмент `#page` обрабатывает PDF-viewer браузера. Сервер возвращает исходный PDF, поэтому для перехода используйте ссылку на файл в браузере.

`raw.value` сохраняет запись из ячейки, включая запятую или начальную точку. `normalized.value` в этих записях — десятичная строка. Для безразмерного значения `unit` равен `null`. `extraction_metadata` содержит фактический идентификатор модели, метод, версию схемы, профиль, SHA-256 PDF и идентификатор проверенного блока.

## Статусы

- `verified` — ответ прошёл строгую схему и проверку полной цитаты, строки, столбца и единицы. В CSV эта запись имеет статус `extracted`.
- `needs_review` — кандидат с непроверенным значением, если ответ прошёл схему и содержит `raw.value`. Интерфейс может показать `note` и целевую таблицу. Для такого кандидата `normalized.value` и `source.fragment` пусты. Кандидат пропускается при CSV-экспорте.
- `extracted` — статус существующих записей SPI. Для отображения подтверждения этого пайплайна проверяйте `verified`.

Если ключ отсутствует, сеть недоступна либо ответ нарушает схему, команда сообщает `needs_review` и причину в JSON-отчёте. При отсутствии пригодного `raw.value` записи в `/results` ещё нет, а запрос детали возвращает `404`.

## Примеры ответов из локального прогона

Маршрут детали возвращает объект, маршрут списка — массив объектов. В выдержках показаны поля для UI.

### `GET /results/solar:D001:voc`

```json
{
  "result_id": "solar:D001:voc",
  "document_id": "solar:D001",
  "domain": "solar",
  "metric": {"name": "Voc", "k": null, "variant": null},
  "raw": {"value": "1,197", "unit": "mV"},
  "normalized": {"value": "1.197", "unit": "V"},
  "source": {
    "page": 5, "fragment": "1,197", "table": "Table 1", "row": "Pristine",
    "column": "Voc", "origin": "primary"
  },
  "context": {"device_label": "Pristine", "measurement_type": "J-V"},
  "extraction_status": "verified",
  "note": null,
  "extraction_metadata": {
    "model_id": "GigaChat-3-Ultra:32.9.16.9",
    "method": "pymupdf_gigachat_json_schema",
    "pdf_sha256": "05df334d346559380be04c4afb90363e714c755330db18998691956e8b865f57",
    "schema_version": "table-cell-v1",
    "profile": "solar-d001-table1-pristine-voc-v1",
    "source_block_id": "p5-b8-l2"
  }
}
```

### `GET /results/rag:D001:em`

```json
{
  "result_id": "rag:D001:em",
  "document_id": "rag:D001",
  "domain": "rag",
  "metric": {"name": "EM", "k": null, "variant": null},
  "raw": {"value": ".204", "unit": null},
  "normalized": {"value": "0.204", "unit": null},
  "source": {
    "page": 6, "fragment": ".204", "table": "Table 1", "row": "RA-CoT",
    "column": "LLaMA3-8B-Instruct / HotpotQA / EM", "origin": "primary"
  },
  "context": {
    "rag_variant": "RA-CoT", "generator_model": "LLaMA3-8B-Instruct",
    "dataset_name": "HotpotQA", "task_type": "multi-hop QA"
  },
  "extraction_status": "verified",
  "note": null,
  "extraction_metadata": {
    "model_id": "GigaChat-3-Ultra:32.9.16.9",
    "method": "pymupdf_gigachat_json_schema",
    "pdf_sha256": "7fcbb21b0bdf5bb222b347d0b5e8d6587aa8af5c5f855f62322d0d7ad5427d3c",
    "schema_version": "table-cell-v1",
    "profile": "rag-d001-table1-ra-cot-hotpotqa-em-v1",
    "source_block_id": "p6-b5-l1"
  }
}
```

## Проверка

После успешной загрузки обоих результатов выполните команды во втором окне PowerShell.

```powershell
$spiApi = 'http://127.0.0.1:8000'
Invoke-RestMethod "$spiApi/documents" | Format-Table document_id, page_count
$spiSolar = Invoke-RestMethod "$spiApi/results?domain=solar"
$spiRag = Invoke-RestMethod "$spiApi/results?domain=rag"
Invoke-RestMethod "$spiApi/results/solar:D001:voc" | ConvertTo-Json -Depth 6
Invoke-RestMethod "$spiApi/results/rag:D001:em" | ConvertTo-Json -Depth 6
if ($spiSolar[0].normalized.value -ne '1.197' -or $spiSolar[0].normalized.unit -ne 'V' -or $spiSolar[0].source.page -ne 5 -or $spiSolar[0].extraction_status -ne 'verified') { throw 'Solar D001 mismatch' }
if ($spiRag[0].normalized.value -ne '0.204' -or $spiRag[0].source.page -ne 6 -or $spiRag[0].extraction_status -ne 'verified') { throw 'RAG D001 mismatch' }
$spiCors = Invoke-WebRequest "$spiApi/results?domain=rag" -Headers @{Origin='http://localhost:5173'}
if ($spiCors.Headers['Access-Control-Allow-Origin'][0] -ne 'http://localhost:5173') { throw 'CORS mismatch' }
```

Откройте обе ссылки `#page` в браузере и сверьте ячейки `Pristine / Voc` и `RA-CoT / LLaMA3-8B-Instruct / HotpotQA / EM`. Повторите команду пайплайна. Для обоих треков ожидаются `cache_hit: true` и прежний идентификатор результата.

## CSV и участнические валидаторы

После обработки обоих D001 загрузите в SPI [решения по предметной области](D001_SCOPE.json). Они приняты по оригиналам и действуют для указанных SHA-256. Solar проверен по изготовлению тонкоплёночных перовскитных элементов и J–V-измерениям, RAG — по поиску во внешнем корпусе, генерации с найденным контекстом и экспериментальной оценке.

`in_scope` переходит в CSV как `true`. `out_of_scope` переходит как `false` с причиной исключения и одной строкой в `publications.csv`. Для `needs_review` экспорт останавливается. Это также состояние по умолчанию для PDF без решения на текущий SHA-256. Для новых статей подготовьте отдельный JSON-массив в том же формате. Для подтверждённого решения укажите основание и номера страниц PDF начиная с единицы. Команда `spi.publication_scope` сверяет SHA-256 и страницы с импортированным документом.

Параметр `--publication-id D001` явно задаёт состав каждого экспорта. При выборе нескольких документов повторите параметр для каждого. Участнические валидаторы получают папки с тем же выбранным набором. В примере PowerShell копирует только D001 каждого трека в отдельный каталог и использует эту копию и при экспорте, и при проверке. Исходные папки `dev` могут содержать другие PDF.

В примере пакеты валидаторов распакованы в `outputs/contracts/solar` и `outputs/contracts/rag`. Укажите фактические пути при другом расположении. Для проверок PDF требуется `pypdf`.

```powershell
python -m pip install 'pypdf>=6,<8'
$spiRun = 'outputs/handoff-check-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
New-Item -ItemType Directory -Path "$spiRun/solar-pdfs", "$spiRun/rag-pdfs" | Out-Null
Copy-Item -LiteralPath 'data/solar/dev/D001.pdf' -Destination "$spiRun/solar-pdfs/D001.pdf"
Copy-Item -LiteralPath 'data/rag/dev/D001.pdf' -Destination "$spiRun/rag-pdfs/D001.pdf"
python -m spi.publication_scope docs/D001_SCOPE.json --state state
if ($LASTEXITCODE -ne 0) { throw 'Scope import failed' }
python -m spi.store_export solar "$spiRun/solar" --state state --pdf-dir "$spiRun/solar-pdfs" --publication-id D001
if ($LASTEXITCODE -ne 0) { throw 'Solar export failed' }
python -m spi.store_export rag "$spiRun/rag" --state state --pdf-dir "$spiRun/rag-pdfs" --publication-id D001 --template-dir outputs/contracts/rag
if ($LASTEXITCODE -ne 0) { throw 'RAG export failed' }
python -X utf8 outputs/contracts/solar/tools/check_submission.py "$spiRun/solar" "$spiRun/solar-pdfs"
if ($LASTEXITCODE -ne 0) { throw 'Solar validation failed' }
python -X utf8 outputs/contracts/rag/tools/check_submission.py "$spiRun/rag" "$spiRun/rag-pdfs"
if ($LASTEXITCODE -ne 0) { throw 'RAG validation failed' }
pytest -q
```

`spi.store_export` требует новый каталог назначения. В каждой выгрузке должно быть семь CSV и одна подтверждённая строка в `results.csv`. Валидаторы должны найти по одной цитате в контрольных PDF. Автоматические тесты используют синтетические PDF и mock-ответы, а CLI-прогон проверяет ответы реального провайдера.
