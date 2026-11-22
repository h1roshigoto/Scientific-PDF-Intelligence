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

## Multiuser на Windows

Запустите PowerShell 7 из корня репозитория. Для локального режима используйте
один адрес, `http://127.0.0.1:5173`. Выделите каталоги за пределами репозитория,
официального Store, `outputs` и приватного demo Store.

```powershell
python -m pip install -e '.[dev]'
$spiRoot = Join-Path $env:LOCALAPPDATA 'ScientificPDFIntelligence'
$env:SPI_AUTH_MODE = 'multiuser'
$env:SPI_AUTH_DB = Join-Path $spiRoot 'accounts\auth.sqlite3'
$env:SPI_MULTIUSER_STORAGE_ROOT = Join-Path $spiRoot 'multiuser'
$env:SPI_AUTH_SECRET = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:SPI_AUTH_LOCAL_DEVELOPMENT = 'true'
$env:SPI_AUTH_COOKIE_SECURE = 'false'
$env:SPI_AUTH_ALLOWED_ORIGINS = 'http://127.0.0.1:5173,http://127.0.0.1:8000'
$env:SPI_AUTH_TRUSTED_PROXIES = ''
$env:SPI_AUTH_CSRF_ENABLED = 'true'
$env:SPI_RATE_LIMIT_ENABLED = 'true'
$env:SPI_PUBLIC_DEMO_ENABLED = 'false'
$env:SPI_UPLOAD_PROCESS_GIGACHAT = 'false'
$env:SPI_USER_MAX_UPLOAD_FILES = '100'
$env:SPI_USER_MAX_TOTAL_STORAGE_BYTES = '524288000'
$env:SPI_USER_MAX_PDF_BYTES = '26214400'
$env:SPI_USER_MAX_ACTIVE_JOBS = '1'
$env:SPI_USER_MAX_QUEUED_JOBS = '4'
$env:SPI_USER_MAX_MODEL_REQUESTS = '80'
$env:SPI_USER_MAX_EXPORTS_PER_PERIOD = '30'
$env:SPI_USER_MAX_RETRIES = '3'
$env:SPI_GLOBAL_MAX_ACTIVE_JOBS = '2'
$env:SPI_UPLOAD_PROCESS_MAX_MODEL_REQUESTS = '8'
```

API и worker должны получить один и тот же набор переменных.
Для постоянной установки храните `SPI_AUTH_SECRET` в защищённой конфигурации
процесса и используйте одно значение на всех API-процессах.
Параметры `SPI_USER_*` переопределяются для конкретного аккаунта через
таблицу `user_quotas(user_id, name, value)`. Имя записывается строчными буквами
без префикса `SPI_USER_`, например `max_queued_jobs`. Настройки меняйте
при остановленных API и worker, затем проверьте их в разделе «Аккаунт».

Создайте первого администратора один раз. CLI запросит скрытый пароль и его
подтверждение. Следующая команда запросит пароль администратора и выведет
одноразовый код для передачи пользователю.

```powershell
python -m spi.auth_cli bootstrap-admin --email 'operator@example.test' --display-name 'Operator'
python -m spi.auth_cli invite --admin-email 'operator@example.test' --ttl-seconds 86400
```

Запустите серверы из той же вкладки. Фоновые задания наследуют заданное
окружение. Сценарий работает с локальным извлечением без облачных credentials.

```powershell
$spiRepo = (Get-Location).Path
$spiApi = Start-Job -WorkingDirectory $spiRepo -ScriptBlock {
    python -m uvicorn spi.api:app --host 127.0.0.1 --port 8000 --no-proxy-headers
}
$spiWorker = Start-Job -WorkingDirectory $spiRepo -ScriptBlock {
    python -m spi.upload_worker
}
$spiHealth = $null
for ($spiAttempt = 0; $spiAttempt -lt 30 -and -not $spiHealth; $spiAttempt++) {
    try { $spiHealth = Invoke-RestMethod 'http://127.0.0.1:8000/health' -TimeoutSec 2 }
    catch { Start-Sleep -Seconds 1 }
}
if (-not $spiHealth) { Receive-Job -Job $spiApi -Keep; throw 'FastAPI health check failed' }
Set-Location frontend_redezigned
pnpm.cmd install --frozen-lockfile
$env:SPI_BACKEND_URL = 'http://127.0.0.1:8000'
pnpm.cmd dev --host 127.0.0.1 --port 5173 --strictPort
```

1. Откройте [локальный интерфейс](http://127.0.0.1:5173/documents).
   Нажмите «У меня есть приглашение», заполните имя, email, пароль с
   подтверждением и код. После регистрации войдите. Email остаётся непроверенным.
2. В «Мои документы» загрузите PDF с направлением Solar или RAG и запустите
   анализ. Дождитесь результатов, откройте научную запись и сверьте значение,
   единицу и контекст со страницей PDF. Подтвердите, исправьте или отклоните запись.
3. Вернитесь к результатам анализа, обновите экран экспорта и скачайте ZIP.
   Проверьте архив офлайн.

```powershell
python -m spi.upload_export_verify 'C:\path\to\spi-project-solar-id.zip'
```

4. В «Аккаунт» проверьте квоты и текущие сессии. Нажмите «Отозвать» у другой
   сессии или «Выйти» для текущей. Оператор может заблокировать пользователя
   с отзывом всех сессий, указав его учетный ID.

```powershell
python -m spi.auth_cli block --admin-email 'operator@example.test' --user-id '<user_id>'
```

После остановки Vite через Ctrl+C остановите фоновые процессы. Если workspace
перезапущен и фоновые задания отсутствуют, повторно задайте окружение для API и
worker.

```powershell
Set-Location $spiRepo
Receive-Job -Job $spiApi,$spiWorker -Keep
Stop-Job -Job $spiApi,$spiWorker
```

### Перенос старого private Store

Сохраните копию исходного каталога и остановите его API и worker. Старый
каталог должен содержать `pdfs`, `metadata` и `processing`. Для иной раскладки
подготовьте копию с этими именами. Подготовьте зарегистрированный аккаунт
в multiuser и выполните перенос до первого обращения к его документам или
квотам. Для получения ID владелец может войти через `POST /auth/login` и запросить
`GET /auth/me` с cookie, минуя экран «Мои документы». ID также можно получить
через локальный read-only запрос к `users` в auth DB.

```powershell
$spiSource = Join-Path $spiRoot 'private-upload'
$spiOwner = '<existing_user_id>'
python -m spi.migrate_private_uploads --source $spiSource --target $env:SPI_MULTIUSER_STORAGE_ROOT --owner-user-id $spiOwner --dry-run
python -m spi.migrate_private_uploads --source $spiSource --target $env:SPI_MULTIUSER_STORAGE_ROOT --owner-user-id $spiOwner
```

Dry-run проверяет SQLite, идентичность заданий и чекпоинтов, ревизии review и
SHA-256 PDF. Для блокировки он может создать служебный SQLite-файл. Реальный
перенос проверяет скопированные данные и публикует каталог одним переименованием.
Исходные файлы остаются доступны. Повтор той же миграции возвращает
`already_imported`. При прерывании повторите команду с тем же владельцем.
Временный каталог `migration-staging` можно очистить после остановки всех
процессов миграции. При ошибке блокировки остановите исходный API, все исходные
worker и операции целевого пользователя, затем повторите dry-run.

Если нужно вернуть доступ к исходному Store, остановите multiuser API и worker,
сохраните целевой namespace как резервную копию и поднимите исходную копию в
`private`. Изменения после переноса останутся в сохранённом namespace. Храните
резервные копии и исходные данные с теми же ограничениями доступа, что и рабочий Store.

### Резервное копирование

Остановите API, worker и все команды управления учётными записями. Копируйте
SQLite auth DB с её каталогом и весь multiuser root в одну резервную копию.
Секрет приложения и настройки сохраняйте отдельно в хранилище секретов.
Для восстановления разверните оба каталога в изолированном окружении,
проверьте SQLite через `PRAGMA integrity_check`, проверьте оригиналы по SHA-256 и
прогоните offline verifier на тестовом экспорте. Перед возвратом в эксплуатацию
отзовите восстановленные сессии, если требуется исключить возобновление
ранее отозванных сессий. Файловая копия работающей SQLite или отдельных каталогов
может содержать несовместимые снимки.

```powershell
$spiBackup = Join-Path $env:LOCALAPPDATA ('SPI-backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $spiBackup
Copy-Item -LiteralPath (Split-Path -Parent $env:SPI_AUTH_DB) -Destination (Join-Path $spiBackup 'accounts') -Recurse
Copy-Item -LiteralPath $env:SPI_MULTIUSER_STORAGE_ROOT -Destination (Join-Path $spiBackup 'multiuser') -Recurse
```

Для production установите `SPI_AUTH_LOCAL_DEVELOPMENT=false`,
`SPI_AUTH_COOKIE_SECURE=true`, задайте внешние HTTPS origins и IP доверенного
reverse proxy. В multiuser официальные маршруты отключены, а включение
`SPI_PUBLIC_DEMO_ENABLED` блокирует старт. Используйте руководство по TLS и
изоляции worker из [модели безопасности](SECURITY_MODEL.md#production).

## Приватная загрузка PDF

### Быстрый запуск на Windows

Из корня worktree установите зависимости, соберите фронтенд и запустите демо. Если зависимости уже установлены и сборка готова, достаточно `python scripts/private_demo.py`.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
pnpm.cmd --dir frontend_redezigned install --frozen-lockfile
pnpm.cmd --dir frontend_redezigned build
.\.venv\Scripts\python.exe scripts/private_demo.py
```

Введите локальный токен длиной от 32 символов в скрытом запросе.
После сообщения `Private demo ready` откройте `http://127.0.0.1:5173/`, нажмите «Загрузить PDF» и введите тот же
токен. Скрипт запускает FastAPI, worker и собранный сайт, используя
`%LOCALAPPDATA%\ScientificPDFIntelligence\demo-upload` для приватных данных.
После остановки через Ctrl+C загрузки и очередь сохраняются в этом каталоге.
Если порты заняты, укажите, например, `--port 5180 --api-port 8180`.

### Cloudflare Access

Создайте self-hosted приложение Access для HTTPS-хоста демо, включая все пути.
Разрешите доступ только участникам демонстрации и сохраните параметры
защищённой браузерной cookie. Настройте именованный Tunnel с сервисом
`http://127.0.0.1:5173` и сохранением исходного Host. Все пользователи,
допущенные к этому приложению Access, работают с одним приватным каталогом.

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[demo-access]'
$env:SPI_DEMO_ACCESS_ORIGIN = 'https://demo.example.org'
$env:SPI_DEMO_ACCESS_TEAM = 'my-team'
$env:SPI_DEMO_ACCESS_AUD = 'application-aud-from-cloudflare'
.\.venv\Scripts\python.exe scripts/private_demo.py --cloudflare-access
```

Во второй вкладке запустите существующий именованный туннель
(`cloudflared tunnel run <имя>` для локально управляемого туннеля).
Откройте HTTPS-адрес и пройдите проверку Access. FastAPI проверяет подпись,
issuer, AUD и срок действия `Cf-Access-Jwt-Assertion`. Для изменяющих запросов
он также проверяет точный Origin и заголовок CSRF.

Для документов, обработанных старой версией извлечения, в «Мои загрузки»
доступна кнопка «Повторить обновлённым анализом». Предыдущий checkpoint
сохраняется в `processing/<upload-id>/batch-history/`. Решения проверки
остаются в истории, а изменения источника требуют новой сверки.

Для дополнительного анализа текста запустите тот же скрипт с `--gigachat`,
установив серверные `GIGACHAT_CREDENTIALS` и `GIGACHAT_CA_BUNDLE_FILE`.
В этом режиме выбранные текстовые фрагменты отправляются в GigaChat.
При недоступности модели локальные кандидаты сохраняются. На страницах
без текстового слоя сайт показывает необходимость OCR.

### Запуск по отдельным процессам

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
$env:SPI_AUTH_MODE = 'private'
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

1. Откройте `http://127.0.0.1:5173/` и нажмите «Загрузить PDF».
2. В разделе «Мои загрузки» введите токен. Выберите файл и направление Solar или RAG, затем нажмите «Загрузить PDF».
3. Дождитесь сохранения PDF и нажмите «Начать анализ». Статус обновляется автоматически. После завершения нажмите «Посмотреть результаты».
4. Выберите запись и нажмите «Открыть источник». Сверьте фрагмент с указанной страницей оригинала.
5. В блоке «Ручная проверка» отметьте сверку значения, единицы и контекста. Если есть научные замечания, ознакомьтесь с ними и отметьте, что учли их. Нажмите «Подтвердить исходное».
6. Для другой записи откройте «Предложить исправление». Укажите изменённые поля и обоснование по PDF, проверьте «Было → Стало» и сохраните предложение. Сверьте предложенную версию с указанной страницей, затем нажмите «Подтвердить исправление».
7. Чтобы отклонить запись, укажите причину и нажмите «Отклонить». Статусы и фильтры доступны в списке научных записей. В карточке раскройте события раздела «История решений» для просмотра исходного evidence, причины и номера ревизии.
8. Вернитесь к списку результатов. В блоке «Экспорт научного проекта» проверьте числа подтверждённых, ожидающих, отклонённых и устаревших записей, сведения об OCR и ограничения охвата. Скачайте CSV, JSON, PDF или полный ZIP. Если рецензия или анализ изменились, нажмите «Обновить данные экспорта».
9. Перезагрузите страницу, введите токен снова и скачайте архив повторно. Решения, замечания и история доступны после перезапуска браузера, FastAPI и worker с прежними приватными каталогами.

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
события. Актуальный набор для программной работы отдаёт отдельный маршрут
`GET /uploads/{upload_id}/reviewed-results` с bearer-токеном.

Ctrl+C останавливает worker. После запуска той же командой задачи в очереди
и прерванные задания подхватываются из SQLite. Блокировка документа
освобождается системой при выходе процесса. Worker продолжит с последнего
совместимого checkpoint. Для ошибки с доступным повтором нажмите «Повторить».
Если причина в SHA-256 или отсутствующем оригинале, сначала восстановите
исходный файл. Для одноразового запуска доступно
`python -m spi.upload_worker --once`.

### Скачать и проверить частный проект из PowerShell

В отдельной вкладке PowerShell используйте тот же токен, что в интерфейсе.
Возьмите ID из адреса `/documents/uploads/{upload_id}` на странице результатов или из
ответа `GET /uploads`. Каталог назначения должен оставаться приватным.

~~~powershell
$spiSecret = Read-Host 'Токен приватной загрузки' -AsSecureString
$spiHeaders = @{ Authorization = 'Bearer ' + [System.Net.NetworkCredential]::new('', $spiSecret).Password }
Remove-Variable spiSecret
$spiUploadId = Read-Host 'ID загрузки'
$spiExport = "http://127.0.0.1:8000/uploads/$spiUploadId/export"
$spiTarget = Join-Path $env:LOCALAPPDATA 'ScientificPDFIntelligence\downloads'
New-Item -Path $spiTarget -ItemType Directory -Force | Out-Null

try {
    $spiPreview = Invoke-RestMethod -Uri "$spiExport/manifest" -Headers $spiHeaders -ErrorAction Stop
    $spiPreview.validation | ConvertTo-Json -Depth 8
    $spiHeaders['X-SPI-Snapshot-SHA256'] = $spiPreview.metadata.snapshot_sha256
    Invoke-WebRequest -Uri "$spiExport/reviewed-results.csv" -Headers $spiHeaders -OutFile (Join-Path $spiTarget 'reviewed_results.csv') -ErrorAction Stop
    Invoke-WebRequest -Uri "$spiExport/reviewed-results.json" -Headers $spiHeaders -OutFile (Join-Path $spiTarget 'reviewed_results.json') -ErrorAction Stop
    Invoke-WebRequest -Uri "$spiExport/scientific-passport.pdf" -Headers $spiHeaders -OutFile (Join-Path $spiTarget 'scientific-passport.pdf') -ErrorAction Stop
    $spiArchive = Join-Path $spiTarget "spi-project-$($spiPreview.metadata.domain)-$spiUploadId.zip"
    Invoke-WebRequest -Uri "$spiExport/project.zip" -Headers $spiHeaders -OutFile $spiArchive -ErrorAction Stop
    python -m spi.upload_export_verify $spiArchive
    if ($LASTEXITCODE -ne 0) { throw 'Проверка ZIP не пройдена' }
    Get-FileHash -LiteralPath $spiArchive -Algorithm SHA256
} finally {
    Remove-Variable spiHeaders
}
~~~

На `409` дождитесь завершения активного задания или обновите предпросмотр.
Пакет без подтверждений доступен для аудита и содержит CSV с заголовками.
Замечания и страницы в ожидании OCR остаются в пакете после ручной проверки.
Для анализа чисел и неизвестных значений используйте типизированный JSON.
Архив содержит конфиденциальные сведения. Для официальной выгрузки
применяйте отдельные схемы и научный gate. [Контракт частного экспорта](API_CONTRACT.md#private-project-export-v1)

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

Если Chromium не скачивается, для Windows можно запустить Playwright через установленный Edge командой `$env:PLAYWRIGHT_CHANNEL = 'msedge'` перед `pnpm.cmd test:e2e`. Playwright использует порты `4175`–`4178` и `8182`–`8183`. Сценарии private и multiuser запускают отдельные FastAPI и worker с временными каталогами и синтетическими PDF. Multiuser-сценарий проверяет два аккаунта, отзыв сессии, изоляцию результатов и ZIP, а также offline verifier. CI не требует секретов GigaChat или приватного корпуса. Для вывода Python в UTF-8 на Windows задайте `$env:PYTHONUTF8 = '1'`.

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
# Docker Desktop. Локальная production-сборка

Запустите Docker Desktop с Linux containers и откройте PowerShell 7 в корне
репозитория. Docker Engine должен поддерживать Landlock ABI 3 и cgroup v2.
Compose использует отдельные volumes для синтетического стенда.

```powershell
docker compose version
docker compose -f compose.yaml -f compose.local.yaml config
docker compose -f compose.yaml -f compose.local.yaml build
docker compose -f compose.yaml -f compose.local.yaml up -d --wait
docker compose -f compose.yaml -f compose.local.yaml ps
docker compose -f compose.yaml -f compose.local.yaml logs --tail 100
```

Скопируйте CA этого Compose-проекта для проверки TLS. Используйте его в
выделенном тестовом профиле или отдельном клиенте. На машине с пользовательскими
данными не добавляйте тестовый CA в общий trust store.

```powershell
$spiCa = Join-Path ([System.IO.Path]::GetTempPath()) 'spi-compose-local-ca.crt'
docker compose -f compose.yaml -f compose.local.yaml cp proxy:/data/caddy/pki/authorities/local/root.crt $spiCa
curl.exe --fail --cacert $spiCa https://localhost:8443/api/health
docker compose -f compose.yaml -f compose.local.yaml exec api python -m spi.auth_cli bootstrap-admin --email operator@example.test --display-name Operator
docker compose -f compose.yaml -f compose.local.yaml exec api python -m spi.auth_cli invite --admin-email operator@example.test
```

Откройте [Документы](https://localhost:8443/documents) в тестовом браузере
с доверием этому локальному CA. Нажмите «У меня есть приглашение»,
зарегистрируйте тестовый аккаунт и войдите. Загрузите синтетический PDF,
выберите Solar или RAG, нажмите «Начать анализ», проверьте evidence и
подтвердите только проверенную запись. На странице результатов скачайте ZIP.
`python -m spi.upload_export_verify file.zip` проверяет архив автономно.

Cold backup и restore в отдельный локальный проект. Для первого прогона
используйте новые и уникальные имена проекта и backup. Команды
сохраняют volumes при остановке.

```powershell
$env:SPI_LOCAL_BACKUP_VOLUME = 'spi-demo-rehearsal-copies'
$spiLocal = @('-f', 'compose.yaml', '-f', 'compose.local.yaml')
docker compose @spiLocal up -d --wait
docker compose @spiLocal stop proxy worker api
docker compose @spiLocal run --rm --no-deps backup create rehearsal
docker compose @spiLocal run --rm --no-deps backup verify rehearsal
docker compose @spiLocal down
docker compose -p spi-demo-restored @spiLocal run --rm --no-deps restore rehearsal --dry-run
docker compose -p spi-demo-restored @spiLocal run --rm --no-deps restore rehearsal
docker compose -p spi-demo-restored @spiLocal up -d --wait
docker compose -p spi-demo-restored @spiLocal ps
```

Для восстановленного проекта скопируйте его CA и снова войдите.
Проверьте оригинал, результаты, review history, экспорт и второй аккаунт.
Остановите проекты с `docker compose … down`. **`docker compose down -v`
удаляет volumes и не должен использоваться на production данных без
проверенного backup и осознанного решения.**

Автоматическая проверка на одноразовых Compose projects, где скрипт удаляет
только созданные им тестовые volumes.

```powershell
python -m pip install -e '.[dev]'
python deploy/scripts/compose_e2e.py --build
```
