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

Для локального анализа нужны три процесса в отдельных вкладках PowerShell.
Остановите предыдущий экземпляр FastAPI и из корня репозитория выполните
следующий блок **в обеих вкладках**, где будут работать FastAPI и worker.
Для подготовленного научного Store укажите один и тот же абсолютный
`SPI_STATE_DIR` в обеих вкладках. В примере используется пустой реестр.

```powershell
python -m pip install -e '.[dev]'
$spiLocal = Join-Path $env:LOCALAPPDATA 'ScientificPDFIntelligence'
$env:SPI_STATE_DIR = Join-Path $spiLocal 'local-science-state'
$spiUploads = Join-Path $spiLocal 'private-upload'
$env:SPI_UPLOAD_DIR = Join-Path $spiUploads 'pdfs'
$env:SPI_UPLOAD_STATE_DIR = Join-Path $spiUploads 'metadata'
$env:SPI_UPLOAD_PROCESS_STATE_DIR = Join-Path $spiUploads 'processing'
$env:SPI_UPLOAD_MAX_BYTES = '26214400'
$env:SPI_UPLOAD_MAX_FILES = '100'
$env:SPI_UPLOAD_ENABLED = 'true'
$env:SPI_UPLOAD_PROCESS_GIGACHAT = 'false'
```

**Вкладка 1. FastAPI**

```powershell
$env:SPI_UPLOAD_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
Set-Clipboard -Value $env:SPI_UPLOAD_TOKEN
python -m uvicorn spi.api:app --host 127.0.0.1 --port 8000
```

**Вкладка 2. Worker**

```powershell
$spiSecret = Read-Host 'Вставьте тот же токен загрузки' -AsSecureString
$env:SPI_UPLOAD_TOKEN = [System.Net.NetworkCredential]::new('', $spiSecret).Password
Remove-Variable spiSecret
python -m spi.upload_worker
```

**Вкладка 3. Vite** (из корня репозитория)

```powershell
Set-Location -LiteralPath 'frontend_redezigned'
pnpm.cmd install --frozen-lockfile
$env:SPI_BACKEND_URL = 'http://127.0.0.1:8000'
$env:VITE_DATA_SOURCE = 'api'
$env:VITE_API_BASE = '/api'
pnpm.cmd dev --host 127.0.0.1 --port 5173 --strictPort
```

Лимиты выше задают 25 МиБ на PDF и 100 файлов на хранилище. Выделите три
непересекающихся каталога с доступом только для оператора или служебной
учётной записи. Пути должны находиться за пределами всего checkout, включая
его родительские каталоги, и не пересекаться с `SPI_STATE_DIR` или корнем
статического сайта. Проверяются также цели symlink и junction. Оригиналы
хранятся в `pdfs`, каталог загрузок в `metadata\uploads.sqlite3`, очередь
в `processing\queue.sqlite3`, результаты и checkpoints в
`processing\<upload_id>\`. Оставьте эти каталоги на месте при перезапуске.
Рецензии хранятся в `reviews.sqlite3` внутри каталога загрузки.
Буфер обмена используйте с отключённой историей и синхронизацией. После
входа очистите его командой `Set-Clipboard -Value ' '` в свободной вкладке.

1. Откройте `http://127.0.0.1:5173/documents` или нажмите «Загрузить PDF» на странице «Результаты».
2. В разделе «Мои загрузки» введите токен. Выберите файл и направление Solar или RAG, затем нажмите «Загрузить PDF».
3. Дождитесь `stored` и нажмите «Начать анализ». Дождитесь завершения, затем нажмите «Посмотреть результаты».
4. Выберите запись и нажмите «Открыть источник». Сверьте фрагмент с указанной страницей оригинала.
5. В блоке «Ручная проверка» отметьте сверку значения, единицы и контекста. Если есть научные замечания, ознакомьтесь с ними и отметьте, что учли их. Нажмите «Подтвердить исходное».
6. Для другой записи откройте «Предложить исправление». Укажите изменённые поля и обоснование по PDF, проверьте «Было → Стало» и сохраните предложение. Сверьте предложенную версию с указанной страницей, затем нажмите «Подтвердить исправление».
7. Чтобы отклонить запись, укажите причину и нажмите «Отклонить». Статусы и фильтры доступны в списке научных записей. В карточке раскройте события раздела «История решений» для просмотра исходного evidence, причины и номера ревизии.
8. Перезагрузите страницу и введите токен снова. Решения, замечания и история доступны после перезапуска браузера, FastAPI и worker с прежними приватными каталогами.

Локальный режим использует PyMuPDF и существующие правила извлечения из таблиц
Solar и RAG. Страницы без текстового слоя, требующие OCR, остаются
`ocr_required` с `ocr_status=pending`. Для такой страницы выберите
«Визуально по PDF» и укажите, что на изображении подтверждает запись.
Статус задания `completed` означает техническое завершение. Автоматическое
`extracted` описывает извлечение. `approved` и `approved_corrected` означают
явное решение оператора. Научные замечания и статус охвата сохраняются.

Если сервер сообщает о конфликте ревизий (`409`), нажмите «Обновить данные»,
посмотрите последнее решение и повторите сверку. Пока идёт новый анализ,
сохранение блокируется. Новый ID задания делает предыдущие решения
устаревшими даже для совпадающего значения. В истории остаются прежние
события. Актуальный набор для будущего экспорта отдаёт отдельный маршрут
`GET /uploads/{upload_id}/reviewed-results` с bearer-токеном.

Ctrl+C останавливает worker. После запуска той же командой задачи в очереди
и прерванные задания подхватываются из SQLite. Блокировка документа
освобождается системой при выходе процесса. Worker продолжит с последнего
совместимого checkpoint. Для ошибки с доступным повтором нажмите «Повторить».
Если причина в SHA-256 или отсутствующем оригинале, сначала восстановите
исходный файл. Для одноразового запуска доступно
`python -m spi.upload_worker --once`.

### Опциональный GigaChat

После разрешения передачи фрагментов PDF провайдеру модели задайте переменные
только в процессе worker и перезапустите его.

```powershell
$env:SPI_UPLOAD_PROCESS_GIGACHAT = 'true'
$env:SPI_UPLOAD_PROCESS_MAX_MODEL_REQUESTS = '8'
$spiModelSecret = Read-Host 'GigaChat credentials' -AsSecureString
$env:GIGACHAT_CREDENTIALS = [System.Net.NetworkCredential]::new('', $spiModelSecret).Password
Remove-Variable spiModelSecret
python -m spi.upload_worker
```

Доступны существующие `GIGACHAT_SCOPE` и `GIGACHAT_CA_BUNDLE_FILE`.
Лимит от 1 до 32 запросов на задание включает попытки, прерванные аварией.
Ответ модели ограничен 700 токенами. Таймауты адаптера составляют 12 секунд
на подключение и 30 секунд на чтение. Явный повтор выдаёт новый бюджет и
использует уже сохранённые фрагменты. Если credentials отсутствуют,
новое задание выполняется локально с диагностикой.

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
$spiJob = "$spiUploadApi/$($spiRecord.upload_id)"
Invoke-RestMethod -Uri "$spiJob/process" -Headers $spiHeaders -Method Post
# После завершения обработки:
Invoke-RestMethod -Uri "$spiJob/processing" -Headers $spiHeaders
Invoke-RestMethod -Uri "$spiJob/results" -Headers $spiHeaders
Invoke-RestMethod -Uri "$spiJob/issues" -Headers $spiHeaders
Invoke-RestMethod -Uri "$spiJob/reviews" -Headers $spiHeaders
Invoke-RestMethod -Uri "$spiJob/reviewed-results" -Headers $spiHeaders
$spiCopy = Join-Path $env:TEMP ("spi-" + $spiRecord.upload_id + ".pdf")
Invoke-WebRequest -UseBasicParsing -Uri "$spiUploadApi/$($spiRecord.upload_id)/file" -Headers $spiHeaders -OutFile $spiCopy
Invoke-Item -LiteralPath $spiCopy
Remove-Variable spiHeaders,spiSecureToken
```

Этот режим рассчитан на loopback или закрытую сеть с доверенными операторами.
Один общий токен даёт доступ ко всем файлам и рецензиям приватного хранилища.
Метка оператора вводится вручную. Журнал фиксирует действия с общим доступом.
Перед публикацией нужны TLS, отдельные пользовательские сессии, проверка
владения на всех приватных маршрутах, лимиты частоты и параллельности,
квоты по пользователю, политика хранения и удаления данных и изоляция
ресурсов worker для недоверенных PDF. Очередь и блокировки рассчитаны
на процессы с общей локальной файловой системой.

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

Если Chromium не скачивается, для Windows можно запустить Playwright через установленный Edge командой `$env:PLAYWRIGHT_CHANNEL = 'msedge'` перед `pnpm.cmd test:e2e`. Playwright поднимает тестовые серверы на `4175`, `4176`, `4177` и `8182`. Сценарий проверки запускает FastAPI и отдельный worker с временными каталогами и синтетическим PDF. Он проверяет подтверждение, исправление, отклонение, историю, повторный вход и восстановление после реального конфликта ревизий. CI не требует секретов GigaChat или приватного корпуса.

Для отдельного Windows smoke-test поднимите три процесса командами выше и
проверьте шаги 1–5 с локальной научной публикацией, которую можно использовать
для теста. Сохраните SHA-256, итоговый статус, число записей и страницу
одного подтверждаемого фрагмента. Остановите и запустите worker снова,
чтобы проверить поведение очереди независимо от синтетического CI.

## Диагностика

| Симптом | Проверка |
| --- | --- |
| Vite не соединяется с API | Сравните `http://127.0.0.1:8000/health` и `http://127.0.0.1:5173/api/health`. Проверьте `SPI_BACKEND_URL`, затем перезапустите Vite. |
| Порт занят | Выполните `Get-NetTCPConnection -LocalPort 8000,5173 -State Listen` и проверьте `OwningProcess`. При запуске на другом порту обновите `SPI_BACKEND_URL` и адрес проверки. |
| `/health` работает, списки пусты | Проверьте абсолютный `SPI_STATE_DIR`, наличие `documents.json` и папки `batch` для подготовленного запуска. |
| `503 uploads_misconfigured` или `upload_processing_unconfigured` | Сверьте три абсолютных приватных пути и токен длиной не менее 32 символов. |
| Задание остаётся в очереди | Проверьте, что worker запущен с теми же каталогами и видит `processing\queue.sqlite3`. |
| `original_sha256_mismatch` | Восстановите исходные байты PDF из проверенного источника и повторите задание. |
| `completion:read_timeout` или `request_budget_exhausted` | Проверьте доступность GigaChat или лимит запросов и повторите задание. |
| Паспорт возвращает 404 или 503 | Сверьте `VITE_PROJECT_RUN_ID` с `SPI_RUN_ID`. Для 404 также проверьте публикацию и домен. После смены переменных перезапустите серверы. |
| Источник возвращает 404 или не читается | Проверьте точный `document_id`, доступность сохранённого `file_path` и открытие оригинального файла. Если Store перенесли на другую машину, старые абсолютные пути могут быть недоступны. |
| Вместо CSV приходит HTML или 404 | После разрешённой публикации проверяйте каждый URL `<VITE_CSV_ARTIFACT_BASE>/<solar или rag>/<имя>.csv`. Ожидаются заголовок CSV и содержимое UTF-8. До публикации оставьте переменную незаданной. |
| pytest заканчивается ошибкой доступа к `pytest-current` | Используйте отдельную папку `python -m pytest -q --basetemp (Join-Path ([System.IO.Path]::GetTempPath()) ('spi-pytest-' + [guid]::NewGuid()))`. |
