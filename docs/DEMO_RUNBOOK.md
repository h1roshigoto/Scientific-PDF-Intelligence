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
5. Скачайте `passport.md` как схему данных. `results.json` и `issues.json` возвращают исторические файлы Store. Если файлов ещё нет, API возвращает `[]`. Публикация официального CSV зависит от release gate.

Для проверки прямого скачивания через прокси используйте PowerShell.

```powershell
$spiReport = Join-Path ([System.IO.Path]::GetTempPath()) 'scientific-pdf-intelligence_passport_e2e-dev_all.pdf'
curl.exe --fail --show-error --dump-header - --output "$spiReport" "$spiApi/export/project-passport.pdf?run_id=e2e-dev&domain=all"
```

В заголовках ожидаются `Content-Type: application/pdf`, `Content-Disposition` с именем файла и `X-SPI-Generated-At`. Проверьте, что PDF открывается и содержит последнюю страницу.

## Приватная загрузка PDF

Загрузка по умолчанию выключена. Для запуска в локальном режиме остановите FastAPI
и выполните в его вкладке PowerShell из корня репозитория следующие команды.
Путь для чтения научного Store можно оставить из шага 1. Если
готового Store нет, укажите отдельный локальный каталог для пустого реестра.

```powershell
python -m pip install -e '.[dev]'
$spiLocal = Join-Path $env:LOCALAPPDATA 'ScientificPDFIntelligence'
# Только если готового научного Store нет:
# $env:SPI_STATE_DIR = Join-Path $spiLocal 'local-science-state'
$spiUploads = Join-Path $spiLocal 'private-upload'
$env:SPI_UPLOAD_DIR = Join-Path $spiUploads 'pdfs'
$env:SPI_UPLOAD_STATE_DIR = Join-Path $spiUploads 'metadata'
$env:SPI_UPLOAD_MAX_BYTES = '26214400'
$env:SPI_UPLOAD_MAX_FILES = '100'
$env:SPI_UPLOAD_ENABLED = 'true'
$env:SPI_UPLOAD_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
Set-Clipboard -Value $env:SPI_UPLOAD_TOKEN
python -m uvicorn spi.api:app --host 127.0.0.1 --port 8000
```

Оригиналы сохраняются в `pdfs` под серверными именами, метаданные — в
`metadata\uploads.sqlite3`. Оставьте оба каталога на месте при перезапуске.
Лимиты выше задают максимум 25 МиБ на PDF и 100 файлов на хранилище. Размещайте
каталоги в доступной только оператору папке, за пределами checkout и каталога
статического сайта. Для передачи токена используйте буфер обмена с
выключенными историей и синхронизацией. После входа очистите его командой
`Set-Clipboard -Value ' '`. Токен нужен только процессу FastAPI и открытой
вкладке браузера. Frontend использует настройки прокси из шага 2.

1. Откройте `http://127.0.0.1:5173/documents` или нажмите «Загрузить PDF» на странице «Результаты».
2. В разделе «Мои загрузки» введите токен. Выберите файл и направление Solar или RAG, затем нажмите «Загрузить PDF».
3. После сохранения проверьте имя, направление, число страниц, размер и время загрузки. Нажмите «Открыть оригинал» для просмотра и «Обновить список» для повторного запроса к серверу.
4. После перезагрузки страницы введите токен снова. Файлы и метаданные сохраняются на сервере между его перезапусками.

Статус `stored` подтверждает сохранение и техническую проверку PDF.
Статус `verified` относится к научной записи, проверенной по evidence.
Сохранённый PDF появится в «Моих загрузках». Результаты, отчёты и корпус
научного Store ведутся отдельно.

Для прямой проверки через прокси выполните команды в новой вкладке PowerShell.
Вставьте токен в скрытое поле. Запрос POST получает заголовок через stdin,
поэтому токен не передаётся в аргументах процесса curl.

```powershell
$spiUploadApi = 'http://127.0.0.1:5173/api/uploads'
$spiSecureToken = Read-Host 'Токен загрузки' -AsSecureString
$spiHeaders = @{ Authorization = 'Bearer ' + [System.Net.NetworkCredential]::new('', $spiSecureToken).Password }
$spiPdf = (Resolve-Path -LiteralPath (Read-Host 'Путь к локальному PDF') -ErrorAction Stop).Path
$spiRecord = ("Authorization: " + $spiHeaders.Authorization) |
    curl.exe --fail --show-error --silent --header '@-' --form "file=@$spiPdf" --form 'domain=solar' $spiUploadApi |
    ConvertFrom-Json
$spiRecord
Invoke-RestMethod -Uri $spiUploadApi -Headers $spiHeaders
$spiCopy = Join-Path $env:TEMP ("spi-" + $spiRecord.upload_id + ".pdf")
Invoke-WebRequest -UseBasicParsing -Uri "$spiUploadApi/$($spiRecord.upload_id)/file" -Headers $spiHeaders -OutFile $spiCopy
Invoke-Item -LiteralPath $spiCopy
Remove-Variable spiHeaders,spiSecureToken
```

Этот режим рассчитан на loopback или закрытую сеть с доверенными операторами.
Один общий токен даёт доступ ко всем файлам приватного хранилища.
Перед публикацией нужны TLS, отдельные пользовательские сессии, проверка
владения при чтении метаданных и оригиналов, лимиты частоты и параллельности,
квоты по пользователю и изоляция ресурсов для обработки недоверенных PDF.

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

Если Chromium не скачивается, для Windows можно запустить Playwright через установленный Edge командой `$env:PLAYWRIGHT_CHANNEL = 'msedge'` перед `pnpm.cmd test:e2e`. Playwright поднимает тестовые серверы на `4175`, `4176`, `4177` и `8182`. `real-api.spec.ts` и `real-upload.spec.ts` используют отдельные временные каталоги с искусственными научными записями, PDF и метаданными загрузок.

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
