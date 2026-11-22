# Развёртывание Scientific PDF Intelligence

Статический React и `/api/*` обслуживает Caddy с одного origin. API и Caddy
соединены внутренней сетью Compose. Worker, PDF sandbox, backup, restore и
monitoring работают без сети. Порты публикует только Caddy. Публичный допуск
определяет [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md).

## Linux

Используйте Linux x86-64 или arm64 с cgroup v2, seccomp и Landlock ABI 3+
(ядро 6.2+ с включённым Landlock), Docker Engine 28+ и Compose 2.36+.
Проверяйте фактические настройки на целевом хосте. SQLite, backup staging и
восстановление требуют локальную файловую систему с POSIX-locking и атомарным
rename. Подходят локальные ext4 или XFS. Для SQLite не используйте SMB и NFS.
Выделите хосту не менее 4 GiB RAM для первоначального стенда и отдельный
дисковый бюджет для данных, backup, Docker и временных файлов.

Создайте отдельный проект Compose и каталоги за пределами Git checkout.
Команды рассчитаны на обычный Docker Engine без user namespace remapping.
При remapping настройте права на соответствующие UID хоста.

```bash
docker version
docker compose version
sudo install -d -m 0711 /srv/spi
sudo install -d -m 0700 -o 10001 -g 10001 /srv/spi/dataset /srv/spi/backups
sudo install -d -m 0700 -o 10002 -g 10002 /srv/spi/proxy
sudo install -d -m 0700 /srv/spi/secrets
sudo sh -c 'umask 077; set -C; python3 -c "import secrets; print(secrets.token_urlsafe(32))" > /srv/spi/secrets/auth'
sudo chown 10001:10001 /srv/spi/secrets/auth
sudo chmod 0400 /srv/spi/secrets/auth
sudo install -m 0600 -o "$(id -u)" -g "$(id -g)" .env.production.example /srv/spi/compose.env
```

Замените `spi.example.org` своим доменом во всех трёх параметрах env-файла.
Назначьте DNS A и при наличии рабочего IPv6 AAAA. Разрешите входящие TCP 80
и 443 к Caddy через firewall и NAT. Прокси должен иметь доступ к DNS и ACME.
Проверьте совпадение публичных адресов, исключите пересечение сетей Compose
с маршрутами хоста. В production публикуйте только порты Caddy.

```bash
spi=(docker compose --env-file /srv/spi/compose.env -f compose.yaml -f compose.production.yaml)
"${spi[@]}" config --quiet
"${spi[@]}" build
"${spi[@]}" up -d --wait
"${spi[@]}" ps
"${spi[@]}" logs --tail 40 api worker proxy monitoring
"${spi[@]}" exec api python -m spi.auth_cli bootstrap-admin --email operator@example.org --display-name Operator
"${spi[@]}" exec api python -m spi.auth_cli invite --admin-email operator@example.org
```

Передайте приглашение получателю по закрытому каналу. CLI выдаёт код с
конечным сроком действия, а адрес получателя вводится при регистрации.
Приложение пока не подтверждает владение email и не отправляет письма.
Для обслуживания работника используйте `docker compose … start worker`
или `restart worker`. Caddy хранит ACME account, ключи и сертификаты на
`/srv/spi/proxy` и автоматически продлевает действующую конфигурацию.
Между релизами сохраняйте UID/GID, пути и постоянный секрет.

## Параметры

| Переменная | Пример / поведение |
| --- | --- |
| `SPI_PUBLIC_DOMAIN` | `spi.example.org`, публичный DNS hostname |
| `SPI_PUBLIC_ORIGIN`, `SPI_AUTH_ALLOWED_ORIGINS` | Оба `https://spi.example.org` |
| `SPI_PROXY_IP` → `SPI_AUTH_TRUSTED_PROXIES` | `10.93.31.2`, единственный доверенный Caddy |
| `SPI_BACKEND_SUBNET`, `SPI_BACKEND_DYNAMIC_RANGE`, `SPI_EDGE_SUBNET` | `10.93.31.0/28`, `10.93.31.8/29`, `10.93.31.16/28`. IP Caddy находится вне диапазона динамических адресов |
| `SPI_AUTH_MODE`, `SPI_AUTH_LOCAL_DEVELOPMENT`, `SPI_AUTH_COOKIE_SECURE` | Compose фиксирует `multiuser`, `false`, `true` |
| `SPI_PUBLIC_DEMO_ENABLED`, `SPI_RATE_LIMIT_ENABLED`, `SPI_AUTH_CSRF_ENABLED` | Compose фиксирует `false`, `true`, `true` |
| `SPI_HOST_AUTH_SECRET_FILE` → `SPI_AUTH_SECRET_FILE` | Файл `/srv/spi/secrets/auth`, примонтирован read-only в `/run/spi-auth-secret`. `SPI_AUTH_SECRET` поддерживается в прежних standalone workflows |
| `SPI_HOST_DATASET`, `SPI_HOST_BACKUPS`, `SPI_HOST_PROXY_DATA` | Три отдельных каталога или раздела вне Git |
| `SPI_AUTH_DB`, `SPI_MULTIUSER_STORAGE_ROOT` | Фиксированные пути в контейнере `/state-auth/auth.sqlite3` и `/state-tenants` |
| `SPI_GLOBAL_MAX_ACTIVE_JOBS`, `SPI_USER_MAX_ACTIVE_JOBS`, `SPI_USER_MAX_QUEUED_JOBS` | `2`, `1`, `4` |
| `SPI_USER_MAX_UPLOAD_FILES`, `SPI_USER_MAX_TOTAL_STORAGE_BYTES`, `SPI_USER_MAX_PDF_BYTES` | `100`, `524288000`, `26214400`. Параметр оригиналов называется `SPI_USER_MAX_TOTAL_STORAGE_BYTES` |
| `SPI_PROXY_UPLOAD_BYTES` | `26345472`, совпадает с лимитом PDF плюс 64 KiB multipart |
| `SPI_DISK_MIN_FREE_BYTES`, `SPI_WORKER_JOB_TIMEOUT` | `134217728`, Compose фиксирует 240 секунд на задачу |
| `SPI_UPLOAD_PROCESS_GIGACHAT` | Compose фиксирует `false`. Для standalone private mode см. [GIGACHAT_SMOKE.md](GIGACHAT_SMOKE.md) |
| `SPI_IMAGE_TAG` | `local` для теста. Для обновлений используйте отдельный тег релиза и сохраняйте digest |

Конфигурация запуска отклоняет отсутствующий или небезопасный секрет, неверный
origin, несколько или неединичный IP прокси и превышение лимита sandbox.
Compose передаёт frontend только `VITE_DATA_SOURCE=api` и `VITE_API_BASE=/api`.
Контейнеры API и worker разделяют auth и tenant volumes, поскольку worker
читает владельцев, квоты и очередь. PDF sandbox монтирует только сокет.
См. [SECURITY_MODEL.md](SECURITY_MODEL.md) для оставшихся границ доверия.

## Production smoke

Запускайте проверки после предоставления настоящего сервера, домена и разрешения.

```bash
python deploy/scripts/production_smoke.py https://spi.example.org --authorized
python deploy/scripts/container_check.py --project scientific-pdf-intelligence --profile production --env-file /srv/spi/compose.env --layers
python -m spi.ops.production_check --evidence /path/to/production-evidence.json --json
```

1. С внешней машины проверьте DNS, цепочку сертификата и hostname, HTTP →
   HTTPS, HSTS, Secure HttpOnly CSRF cookie, отказ без CSRF, Host allowlist,
   overwrite forwarded headers и отсутствие официальных API. Первый скрипт
   делает эти сетевые проверки с обычным TLS trust store.
2. С внешней машины проверьте, что API port 8000 и Caddy admin 2019 недоступны.
   На сервере проверьте отсутствие published ports worker, БД и мониторинга,
   единственный доверенный IP, capabilities, mounts, PID, RAM и CPU.
3. Выдайте два синтетических приглашения, зарегистрируйте Alice и Bob,
   проверьте Secure HttpOnly session cookie, login/logout, загрузку PDF,
   обработку, evidence, ручное решение, ZIP и автономный verifier. Проверьте
   отказ доступа Bob к PDF и ZIP Alice и обратный отказ.
4. Перезапустите worker, API и Caddy по очереди. Проверьте стабильность job ID,
   результата, review history и сессий. Проверьте monitoring, полный scan
   runtime images, реальные дисковые квоты и восстановление по
   [BACKUP_RESTORE.md](BACKUP_RESTORE.md) на отдельном хосте или dataset.

Публичный smoke совмещает все четыре группы. Сетевой скрипт сам по себе
проверяет только внешний edge. Зафиксируйте результаты каждого контроля,
SHA релиза, публичный origin и ссылки на эксплуатационные свидетельства.

Источники по конфигурации [Caddy reverse proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy), [Compose networking](https://docs.docker.com/compose/how-tos/networking).
