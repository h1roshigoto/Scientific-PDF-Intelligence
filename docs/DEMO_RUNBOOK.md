# Демонстрация Scientific PDF Intelligence на Windows 11

Для запуска нужны Python 3.11+, Node.js 22.13+, pnpm и уже обработанный Store с исходными PDF по путям из `documents.json`. Команды ниже рассчитаны на PowerShell. Замените путь к репозиторию в обеих вкладках на свой.

## 1. FastAPI

```powershell
Set-Location -LiteralPath 'C:\Users\hiros\Dokumente\ChatGPT\Scientific-PDF-Intelligence'
python -m pip install -e '.[dev]'
$env:SPI_STATE_DIR = (Resolve-Path -LiteralPath 'outputs/e2e-dev/state' -ErrorAction Stop).Path
$env:SPI_RUN_ID = 'e2e-dev'
python -m uvicorn spi.api:app --host 127.0.0.1 --port 8000
```

FastAPI читает готовый Store. Для просмотра GigaChat, OCR и повторная обработка корпуса не требуются.

## 2. React и прокси Vite

Во второй вкладке PowerShell откройте папку frontend.

```powershell
Set-Location -LiteralPath 'C:\Users\hiros\Dokumente\ChatGPT\Scientific-PDF-Intelligence\frontend_redezigned'
pnpm.cmd install --frozen-lockfile
notepad.exe .env.local
```

Внесите следующие параметры в `.env.local`. Для этой демонстрации оставьте `VITE_CSV_ARTIFACT_BASE` незаданным.

```dotenv
VITE_DATA_SOURCE=api
VITE_API_BASE=/api
SPI_BACKEND_URL=http://127.0.0.1:8000
VITE_PROJECT_PASSPORT_PATH=export/project-passport.pdf
VITE_PROJECT_RUN_ID=e2e-dev
VITE_PROJECT_PASSPORT_GENERATED_AT_HEADER=X-SPI-Generated-At
```

```powershell
pnpm.cmd dev --host 127.0.0.1 --port 5173 --strictPort
```

Откройте [Scientific PDF Intelligence](http://127.0.0.1:5173/). Vite убирает префикс `/api` перед запросом к FastAPI. Для раздачи production-сборки нужен такой же rewrite и SPA fallback. Переменные `VITE_*` читаются при сборке.

## 3. Проверка перед показом

В отдельной вкладке PowerShell проверьте API через тот же адрес, который использует браузер.

```powershell
$spiApi = 'http://127.0.0.1:5173/api'
Invoke-RestMethod "$spiApi/health"
foreach ($spiRoute in 'documents', 'results', 'issues') {
    $spiRows = Invoke-RestMethod "$spiApi/$spiRoute"
    '{0} = {1}' -f $spiRoute, $spiRows.Count
}
```

1. Откройте «Документы». Поочерёдно откройте Solar D001 и RAG D001. Сверьте содержание первых страниц.
2. В «Результатах» отфильтруйте Solar D001 по `Voc` и откройте детали. В проверенном Store источники этих записей ведут на физическую страницу 5. Для RAG D001 откройте результат и сверяйте номер по его `source.page`. Переключите страницу, откройте PDF в новой вкладке и вернитесь к отфильтрованному списку.
3. В «Проверках» откройте замечание и перейдите к связанному evidence. Значок «Сверено» соответствует сохранённому `verified`, а «Нужна проверка» соответствует `needs_review`.
4. В «Экспорте» скачайте PDF для всего проекта и отдельно для Solar D001 и RAG D001. Проверьте имя файла, время формирования и число страниц. Для проверенного Store числа составили 733, 49 и 511 соответственно. Паспорт включает сохранённые записи для аудита, поэтому счётчик публичного списка может отличаться.
5. Скачайте `passport.md` как схему данных. `results.json` и `issues.json` возвращают исторические файлы Store. Если файлов ещё нет, API возвращает `[]`. Публикация официального CSV зависит от release gate. Загрузка PDF через сайт пока недоступна.

Для проверки прямого скачивания через прокси используйте PowerShell.

```powershell
$spiReport = Join-Path ([System.IO.Path]::GetTempPath()) 'scientific-pdf-intelligence_passport_e2e-dev_all.pdf'
curl.exe --fail --show-error --dump-header - --output "$spiReport" "$spiApi/export/project-passport.pdf?run_id=e2e-dev&domain=all"
```

В заголовках ожидаются `Content-Type: application/pdf`, `Content-Disposition` с именем файла и `X-SPI-Generated-At`. Проверьте, что PDF открывается и содержит последнюю страницу.

## 4. Регрессии

```powershell
Set-Location -LiteralPath 'C:\Users\hiros\Dokumente\ChatGPT\Scientific-PDF-Intelligence'
python -m pytest -q
Set-Location -LiteralPath 'frontend_redezigned'
pnpm.cmd typecheck
pnpm.cmd lint
pnpm.cmd build
pnpm.cmd test
pnpm.cmd exec playwright install chromium
pnpm.cmd test:e2e
```

Если Chromium не скачивается, для Windows можно запустить Playwright через установленный Edge командой `$env:PLAYWRIGHT_CHANNEL = 'msedge'` перед `pnpm.cmd test:e2e`. Playwright поднимает тестовые серверы на `4175`, `4176`, `4177` и `8182`. `real-api.spec.ts` запускает отдельный временный Store с искусственными проверочными записями и PDF.

## Диагностика

| Симптом | Проверка |
| --- | --- |
| Vite не соединяется с API | Сравните `http://127.0.0.1:8000/health` и `http://127.0.0.1:5173/api/health`. Проверьте `SPI_BACKEND_URL`, затем перезапустите Vite. |
| Порт занят | Выполните `Get-NetTCPConnection -LocalPort 8000,5173 -State Listen` и проверьте `OwningProcess`. При запуске на другом порту обновите `SPI_BACKEND_URL` и адрес проверки. |
| `/health` работает, списки пусты | Проверьте абсолютный `SPI_STATE_DIR`, наличие `documents.json` и папки `batch` для подготовленного запуска. |
| Паспорт возвращает 404 или 503 | Сверьте `VITE_PROJECT_RUN_ID` с `SPI_RUN_ID`. Для 404 также проверьте публикацию и домен. После смены переменных перезапустите серверы. |
| Источник возвращает 404 или не читается | Проверьте точный `document_id`, доступность сохранённого `file_path` и открытие оригинального файла. Если Store перенесли на другую машину, старые абсолютные пути могут быть недоступны. |
| Вместо CSV приходит HTML или 404 | После разрешённой публикации проверяйте каждый URL `<VITE_CSV_ARTIFACT_BASE>/<solar или rag>/<имя>.csv`. Ожидаются заголовок CSV и содержимое UTF-8. До публикации оставьте переменную незаданной. |
| pytest заканчивается ошибкой доступа к `pytest-current` | Используйте отдельную папку `python -m pytest -q --basetemp (Join-Path ([System.IO.Path]::GetTempPath()) ('spi-pytest-' + [guid]::NewGuid()))`. |
