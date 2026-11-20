# Пакетная обработка Solar и RAG

## Подготовка на Windows

Команды рассчитаны на PowerShell и запускаются из корня репозитория. Нужен Python 3.11 или новее.

```powershell
python -m pip install -e ".[dev]"
pytest -q
```

Разложите оригиналы по `data/solar/dev`, `data/rag/dev`, `data/solar/test` и `data/rag/test`. Имя `D001.pdf` даёт идентификатор `solar:D001` или `rag:D001` в зависимости от трека. Для dev, test, локального и модельного прогонов выбирайте отдельные каталоги состояния. Один каталог состояния содержит одну активную версию PDF на идентификатор публикации.

Для модели GigaChat один раз создайте локальный `.env` и сохраните Authorization key с маскированным вводом.

```powershell
if (-not (Test-Path -LiteralPath .env)) { Copy-Item -LiteralPath .env.example -Destination .env }
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\set-gigachat-key.ps1
git check-ignore -- .env
python -c 'from spi.gigachat import GigaChatSettings; print(bool(GigaChatSettings.from_env().credentials.get_secret_value().strip()))'
```

Пакетный CLI, pipeline и smoke test автоматически читают корневой `.env`. В новой сессии PowerShell или Codex повторный ввод ключа не нужен. Для замены ключа снова запустите `scripts/set-gigachat-key.ps1`. При необходимости задайте доверенный PEM через `GIGACHAT_CA_BUNDLE_FILE` в `.env`. FastAPI берёт из этого же файла `SPI_STATE_DIR` и `SPI_CORS_ORIGINS`. Переменные процесса имеют приоритет. Ключ хранится в открытом виде в локальном файле, который игнорирует Git, и не передаётся в GitHub. [Настройка GigaChat и TLS](GIGACHAT_SMOKE.md#подготовка-и-запуск)

Если запускаете FastAPI из разных каталогов, задайте абсолютный `SPI_STATE_DIR`, чтобы всегда использовать одно хранилище. Относительный путь `state` отсчитывается от рабочего каталога процесса.

## Dev и контрольный набор

```powershell
python -m spi.cli data/solar/dev --domain solar --dry-run --limit 4 --state outputs/batch-dev/state
python -m spi.cli data/solar/dev --domain solar --batch --local-only --publication-id D001 --publication-id D006 --publication-id D016 --publication-id D021 --state outputs/batch-dev/state --report outputs/batch-dev/solar.json
python -m spi.cli data/rag/dev --domain rag --batch --local-only --publication-id D001 --publication-id D002 --publication-id D003 --state outputs/batch-dev/state --report outputs/batch-dev/rag.json
python -m spi.cli --domain rag --manifest --state outputs/batch-dev/state
```

`--dry-run` читает имена и SHA-256 файлов и показывает совпадающие checkpoints. При указании `--report` команда записывает только запрошенный отчёт. `--limit` применяется к отсортированному списку после отбора по `--publication-id`. Полное совпадение имён внутри одного трека отклоняется. Для прежнего импорта используйте `python -m spi.cli data/solar/dev --domain solar`. Для прежнего прогона двух D001 используйте `python -m spi.gigachat_pipeline --state state`.

Для финальной отложенной оценки в этой выборке используются Solar D003 и RAG D004. Запускайте их после настройки правил, с другим каталогом состояния. `dev_gold` хранится отдельно от прогонов и читается только при сравнении.

```powershell
python -m spi.batch_benchmark outputs/batch-dev/solar.json C:/case/solar/dev_gold
python -m spi.batch_benchmark outputs/batch-dev/rag.json C:/case/rag/dev_gold
```

Сравнение отдельно считает совпадение метрики, страницы и раздела таблицы или рисунка, совпадение основных связей, чисел и единиц и вхождение цитаты в разметку. Основные связи Solar используют метку устройства. Для RAG нужны метод, генератор и датасет. Сравнение строгих строковых названий чувствительно к сокращениям авторов и разметчиков. Если разметка содержит ссылку на строку без её текста, совпадение цитаты с gold не засчитывается. Уникальные совпадения учитывают происхождение и совместимость агрегации. Скоринг использует опубликованный участникам dev и выполняется отдельно от извлечения.

## Повторный запуск и отчёт

Одинаковая команда с тем же `--state` продолжает работу. Каждый трек хранит документы отдельно, в `batch/solar` и `batch/rag`. Документ проходит состояния `pending`, `running`, `processed`, `partial` либо `failed`. Запись обновляется атомарно до чтения PDF, после импорта и после каждого фрагмента. Прерванный `running` автоматически продолжается после освобождения файловой SQLite-блокировки. Записи успешных фрагментов переиспользуются. Ошибки отдельных документов и ошибки страниц остаются доступны в отчёте. Повторные вызовы с одинаковой версией документа и настройками не создают новых записей.

Ревизия включает SHA-256 PDF, версию геометрического парсера и PyMuPDF, профиль трека, версию схемы и режим. Успешные модельные ответы сохраняются по ревизии и идентификатору фрагмента и снова проходят локальную проверку при использовании кэша. Один процесс обрабатывает один запрос за раз. Процессы с одним каталогом состояния дополнительно защищены блокировкой. Планируйте Solar и RAG последовательно, в том числе при использовании разных каталогов состояния.

Отчёт включает статус и SHA-256 документа, количество и состояния страниц, отдельный список OCR, доказательства, результаты, проверки, ошибки и очередь scope. `requests_this_run` хранит время попыток, модель, токены при наличии, повторы, попадания в кэш и счётчики JSON, цитат и подтверждённых связей. Блок `coverage` агрегирует техническое покрытие и причины ревью. Часть результатов сохраняется с `needs_review`, часть ошибок относится к фрагментам без числовой записи. Отчёт различает нерешённую геометрию уже видимой таблицы, непрочитанные страницы и упоминание supplement в основном PDF при отсутствии его среди обработанных оригиналов. Фиксировать отсутствие конкретного поля можно после отдельной проверки материалов. OCR и загрузка supplement не запускаются автоматически.

## Размер dev-замера и запуск test

```powershell
$spiModelRun = 'outputs/model-dev'
python -m spi.cli data/solar/dev --domain solar --batch --publication-id D001 --max-requests 3 --state "$spiModelRun/state" --report "$spiModelRun/solar-D001.json"
python -m spi.cli data/solar/dev --domain solar --batch --publication-id D021 --max-requests 2 --state "$spiModelRun/state" --report "$spiModelRun/solar-D021.json"
python -m spi.cli data/rag/dev --domain rag --batch --publication-id D001 --max-requests 3 --state "$spiModelRun/state" --report "$spiModelRun/rag-D001.json"
python -m spi.cli data/rag/dev --domain rag --batch --publication-id D003 --max-requests 2 --state "$spiModelRun/state" --report "$spiModelRun/rag-D003.json"
```

Укажите свежий каталог для модельного замера, отдельный от локального прогона. Лимит ограничивает новые запросы генерации за вызов команды. С заданным лимитом HTTP-повторы генерации отключены. Четыре команды вместе ограничены десятью запросами генерации. Обращения к OAuth и чтение кэша не входят в лимит генерации. `config:missing_credentials` означает остановку до сетевого запроса. После ввода ключа можно повторить команды с тем же состоянием. Если провайдер отклоняет строгую схему, используйте `--json-only` в пределах оставшегося бюджета. Локальная проверка Pydantic и проверка доказательств действуют в обоих режимах. Для прогноза полного корпуса измеряйте отдельно локальный разбор страниц, число подходящих фрагментов на PDF и время новых запросов. При одном потоке суммарное время складывается из разбора, генерации и повторов.

Сначала посмотрите состав корпуса и выполните ограниченный запуск после dev-оценки.

```powershell
python -m spi.cli data/solar/test --domain solar --dry-run --state state/test
python -m spi.cli data/rag/test --domain rag --dry-run --state state/test
python -m spi.cli data/solar/test --domain solar --batch --limit 3 --max-requests 10 --state state/test --report outputs/test-solar-pilot.json
python -m spi.cli data/rag/test --domain rag --batch --limit 3 --max-requests 10 --state state/test --report outputs/test-rag-pilot.json
```

После ручной проверки scope сохраните JSON-массив из `PublicationScope` с `document_id`, `pdf_sha256`, `status`, причиной и номерами страниц доказательства. Страницы считаются с единицы. Запись относится к одной версии PDF.

```powershell
python -m spi.publication_scope outputs/test-scope-review.json --state state/test
python -m spi.store_export solar outputs/solar-submission --pdf-dir data/solar/test --all-pdfs --state state/test
python -m spi.store_export rag outputs/rag-submission --pdf-dir data/rag/test --all-pdfs --state state/test
```

Экспорт проверяет совпадение импортированных SHA-256 и явный scope каждого выбранного PDF. Подтверждённое исключение даёт строку публикации с причиной. Научные строки выгружаются для подтверждённых результатов из `in_scope`. Выбор всех PDF позволяет получить 90 строк публикаций на трек, когда в каталоге 90 оригиналов и каждый документ прошёл проверку.

В `issues.csv` попадают научные замечания, связанные хотя бы с одним результатом этой выгрузки. `record_ids` перечисляет через точку с запятой все выгруженные результаты замечания для той же публикации. В `source_page` и `source_fragment` записывается первая доступная пара из номера страницы и дословной цитаты. В конце `description` под строкой `Evidence references (JSON)` сохраняется массив ссылок на все доказательства для этой публикации, включая страницы, таблицы, рисунки и цитаты. Если дословной цитаты нет, поля источника остаются пустыми. Технические замечания без выгруженных результатов доступны в локальном отчёте.

## Проверка извлечения

Локальный режим проверяет восстанавливаемые таблицы и значения из текста, однозначно связанного с трассой рисунка. Режим GigaChat запрашивает короткие предложения по выбранным фрагментам. Проверка связывает значение с дословным текстом, строкой и полным путём заголовка. Неподтверждённую единицу или шкалу оставляют без преобразования. Зависимые условия сохраняются при наличии привязки к результату. Для Solar опубликованная PCE хранится отдельно от вычисленной. Сопоставимость требует одного устройства, измерения, направления и условий. Если источник указывает направление сокращением, оно также доступно в примечании к результату. Цитаты о приборе доступны в `measurement_notes` публикации с номером страницы и координатами. Для RAG `metric_k` и `top_k` относятся к разным полям.

Возможные повторения и различия между источниками сохраняются как issues. `duplicate_of` требует подтверждённой идентичности записи. Сложные таблицы без надёжно восстановленной геометрии попадают в отчёт с номером страницы. Извлечение непривязанных условий, многостраничных таблиц и произвольных рисунков остаётся ограниченным. Метод указывает, получена запись локально или через структурированный ответ модели.
