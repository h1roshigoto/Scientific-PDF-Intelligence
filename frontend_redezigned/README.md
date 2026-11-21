# Scientific PDF Intelligence · Frontend

Интерфейс для просмотра научных показателей, документов, страниц PDF, замечаний и доступных выгрузок.

## Локальный запуск

Требуются Node.js 22.13+ и pnpm 11. Для демонстрации с подготовленным Store используйте [инструкцию Windows / PowerShell](../docs/DEMO_RUNBOOK.md).

```sh
cd frontend_redezigned
pnpm install --frozen-lockfile
pnpm dev --host 127.0.0.1
```

Frontend открывается на `http://127.0.0.1:5173` и по умолчанию работает через реальный API. Vite проксирует `/api/*` на `http://127.0.0.1:8000`. Адрес FastAPI можно задать через `SPI_BACKEND_URL` в `.env.local`, при этом настройки берутся из [.env.example](.env.example).

Для изолированной разработки можно установить `VITE_DATA_SOURCE=mock` в `.env.local` и перезапустить dev-сервер. Тестовые сценарии используют фикстуры из `public/demo`. Production-сборка использует API даже при `VITE_DATA_SOURCE=mock`, а фикстуры в `dist` не поставляются.

## Production

Соберите проект командой `pnpm build`. Для локальной проверки сборки используйте `pnpm preview`. На сервере раздавайте `dist` с SPA fallback и проксируйте `/api/*` к FastAPI, убирая префикс `/api`. Альтернативный публичный адрес API задаётся через `VITE_API_BASE` на этапе сборки. Для API на другом origin настройте CORS на сервере.

- Документы, результаты и замечания запрашиваются без обязательного выбора предметного профиля. Источник открывается по точному ID документа на указанной в результате странице. Нумерация начинается с единицы.
- Сервер возвращает исходные и нормализованные значения, единицы, контекст и статусы. При отсутствии нормализованного значения интерфейс показывает «—».
- `GET /export/{artifact}` читает исторические JSON-файлы. Их содержимое может отличаться от актуального списка. Для CSV укажите `VITE_CSV_ARTIFACT_BASE` только после публикации результатов отдельного экспортёра в подпапках `solar/` и `rag/`. Файлы с одинаковыми именами скачиваются отдельно, с сохранением исходных CSV-колонок и содержимого.
- FastAPI формирует PDF-паспорт проекта через `GET /export/project-passport.pdf`. Установите `VITE_PROJECT_RUN_ID` равным `SPI_RUN_ID` активного Store. По умолчанию фронтенд использует `export/project-passport.pdf` и заголовок `X-SPI-Generated-At`. Их можно настроить через `VITE_PROJECT_PASSPORT_PATH` и `VITE_PROJECT_PASSPORT_GENERATED_AT_HEADER`.
- PDF регистрируются после импорта по инструкции бэкенда. Приём PDF через сайт пока недоступен.

HTTP-маршруты описаны в [контракте API](../docs/API_CONTRACT.md). Схему CSV описывает [паспорт данных](../passport.md).

## Проверки

```sh
pnpm typecheck
pnpm lint
pnpm build
pnpm test
pnpm exec playwright install chromium
pnpm test:e2e
```

Для Playwright установите backend-зависимости командой `python -m pip install -e ..` из папки фронтенда. Playwright запускает Vite на портах 4175–4177 и FastAPI на порту 8182 с временным синтетическим Store. На Windows с установленным Edge можно задать в PowerShell `$env:PLAYWRIGHT_CHANNEL='msedge'`. Результаты проверок доступны в `test-results/`, а HTML-отчёт открывается через `pnpm exec playwright show-report`.
