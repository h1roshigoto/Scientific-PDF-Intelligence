# P0 №31. Результаты проверки

Среда локальной проверки — Windows 11, Docker Desktop с Linux Engine 29.9.2,
Compose 5.6.1 и cgroup v2. Тестовые проекты используют синтетические PDF и
случайные приглашения. Публичный сервер и DNS не использовались.

## Инфраструктура

| Компонент | Файлы |
| --- | --- |
| Образы и зависимости | `Dockerfile.api`, `Dockerfile.frontend`, `.dockerignore`, `deploy/runtime.in`, `deploy/requirements.lock` |
| Compose и TLS | `compose.yaml`, `compose.local.yaml`, `compose.production.yaml`, `.env.production.example`, `deploy/Caddyfile`, `deploy/Caddyfile.local`, `deploy/Caddyfile.production` |
| Разбор PDF | `src/spi/pdf_boundary.py`, `src/spi/pdf_layout.py`, `src/spi/ops/pdf_sandbox.py` |
| Startup, метрики, backup и restore | `src/spi/ops/__init__.py`, `runtime.py`, `state.py`, `telemetry.py`, `backup.py`, `restore.py`, `production_check.py` в `src/spi/ops`, `deploy/scripts/cold-backup.sh` |
| Проверки | `.github/workflows/deployment.yml`, `deploy/scripts/container_check.py`, `deploy/scripts/compose_e2e.py`, `deploy/scripts/production_smoke.py`, `tests/test_deployment_ops.py`, `frontend_redezigned/tests/deployment/production.mjs` |
| Эксплуатационные документы | `docs/P0_31_PLAN.md`, `docs/DEPLOYMENT.md`, `docs/OPERATIONS_RUNBOOK.md`, `docs/BACKUP_RESTORE.md`, `docs/PRODUCTION_READINESS.md`, этот отчёт |

Два Linux runtime-образа. Версии базы зафиксированы по digest. Локально
`docker image inspect` показал около 310,6 MB для Python runtime и 85,9 MB
для Caddy со статическим frontend. Worker, sandbox и служебные CLI используют
общий Python-образ с отдельными entry points, правами и mounts. Runtime
API, worker и sandbox работают с UID 10001, Caddy — с UID 10002.

Caddy принимает HTTPS и переписывает `/api/*`. API слушает только внутреннюю
Docker-сеть. Caddy перезаписывает IP и протокол от непосредственного
соединения, удаляет `Forwarded`, сохраняет `Host` и имеет единственный
доверенный /32. Worker и sandbox работают с `network_mode: none`. Sandbox
не получает tenant Store, auth DB или ключи. Лимиты и границы sandbox
описаны в [Security model](SECURITY_MODEL.md), разбиение volumes — в
[Operations](OPERATIONS_RUNBOOK.md).

## Локальная проверка

| Контроль | Результат |
| --- | --- |
| Backend | 324 pytest, включая отказ для traversal и сохранение checkpoint при ENOSPC |
| Frontend | Typecheck, lint, статический build и 29 Vitest прошли, 48 Playwright прошли в Edge; отдельно Linux CI проверил production-сборку в Chromium с локальным CA, PDF.js, Unicode, SPA reload и ZIP |
| Caddy и локальный TLS | Validated CA chain и hostname, SPA/asset cache, HSTS, CSP, Secure HttpOnly cookies, Host, CORS, CSRF и защита forwarded headers |
| Alice и Bob | 4 Solar и 16 RAG записей, отдельные PDF и результаты, ручное review и автономно проверенный ZIP |
| Recovery worker | Конкурентный enqueue дал один job ID. После SIGKILL в сохранённом checkpoint два worker восстановили одну задачу под тем же ID |
| Другие сбои | Проверены SIGKILL API во время multipart и незавершённой SQLite-транзакции, прерывание подготовки ZIP, фактический ENOSPC tmpfs, остановка/restart Caddy, недоступный storage и отказ без требуемых auth/TLS-настроек |
| Контейнеры | `docker inspect` и /proc подтвердили non-root, отсутствие capabilities, ограничения PID, CPU, RAM и nofile. Дополнительный тест подтвердил CPU throttling и cgroup OOM |
| Cold backup | Действующий писатель заблокировал создание копии. Backup включал 3 аккаунта, 2 PDF, 2 jobs и 1 review event. Manifest содержит версии, размеры и SHA-256 файлов. Сессии исключены |
| Restore | Изолированный Compose-проект прошёл dry-run и restore. Усечённый архив и повторное восстановление в существующий dataset были отклонены |
| Целостность после restore | Оба PDF совпали побайтово и по SHA-256. Solar/RAG JSON, review history и export snapshot совпали. Старый cookie вернул 401. Alice и Bob получили 404 при попытке открыть чужой ресурс |
| Логи | В Compose-логах проверяется отсутствие тестовых паролей, приглашений, session cookie, auth secret и приватного query |

Для воспроизведения используйте `python deploy/scripts/compose_e2e.py --build`.
Опция `--browser` предназначена для Linux с Chromium и `certutil`, который
импортирует локальный CA только в выделенное временное NSS-хранилище.
Полные команды приведены в [Windows runbook](DEMO_RUNBOOK.md) и
[Linux deployment](DEPLOYMENT.md).

## Сканы и ограничения

`pip-audit` по фиксированным Python-зависимостям и `pnpm audit --prod`
не сообщили об известных уязвимостях. В Trivy 0.76.2 для текущих баз
результаты распределились так.

| Образ | CRITICAL | HIGH | MEDIUM | LOW | UNKNOWN | Секреты |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Python runtime / Debian 13.7 | 0 | 50 | 100 | 81 | 7 | 0 |
| Caddy 2.11.9 / Alpine 3.24.3 | 0 | 2 | 14 | 4 | 1 | 0 |

Обновление первоначальных баз до Python 3.12.16 / Debian Trixie и
Caddy 2.11.9 / Alpine устранило 8 CRITICAL-срабатываний первоначальных
сканов. У proxy остались HIGH `CVE-2026-78661` в `golang.org/x/net` v0.60.0
и `CVE-2026-116587` в `github.com/go-jose/go-jose/v3` v3.0.5. Trivy указывает
исправление x/net в 0.62.0. У Python высокие результаты относятся к
системным пакетам Debian, в том числе SQLite, OpenSSL и util-linux. Для zlib
образ устанавливает исправленную версию пакета `1:1.3.dfsg+really1.3.1-1+deb13u1`.
CI сохраняет полные отчёты и завершает проверку ошибкой при CRITICAL или
обнаруженном секрете. HIGH остаются видимыми и блокируют публичный допуск
до исправления или обоснованной оценки на целевом релизе.

Научная конфигурация, release gates и 14 официальных CSV сохранены.
GigaChat в Compose выключен. Известные требования публичного допуска —
проверенный домен и TLS, отдельная rehearsal на Linux-хосте, реальные
дисковые квоты, внешние alerts и завершённая оценка OCI findings.
Статусы контроля находятся в [Production readiness](PRODUCTION_READINESS.md).

Источники версий [Python releases](https://www.python.org/doc/versions/), [Caddy releases](https://github.com/caddyserver/caddy/releases/tag/v2.11.9), [Trivy releases](https://github.com/aquasecurity/trivy/releases/tag/v0.76.2).
