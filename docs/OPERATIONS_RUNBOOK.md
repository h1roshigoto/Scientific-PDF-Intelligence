# Односерверная эксплуатация

## Состояние, доступ и ёмкость

| Volume | Потребители | Содержимое |
| --- | --- | --- |
| `dataset` | initialize, API и worker через subpaths, backup read-only, restore на чистом dataset | `state/auth`, `state/tenants`, `coord` |
| `backups` | backup RW, restore RO | Проверенные TAR и staging |
| `proxy-data`, `proxy-config` | Caddy | Сертификаты, ACME keys и состояние |
| `parser` | Sandbox RW, API и worker RO | Сокет, без PDF-файлов |
| `metrics` | API, worker и ops RW, monitoring RO | Агрегаты и время последнего результата |
| `local-secret` | Только локальный профиль | Случайный постоянный тестовый секрет |

Храните SQLite на локальном диске. Приложение использует штатный SQLite
rollback journal, время ожидания конкурирующей записи 15 секунд и
безожидательные worker locks. Существующие WAL sidecars учитывает SQLite
backup API. Снимок нормализует journal в DELETE. Startup выполняет
`quick_check`, проверку внешних ключей и версии SQLite. Cold snapshot
выполняет полную `integrity_check`. При порче запуск fail-closed.

Compose ограничивает tmpfs, размер процесса, CPU, память, число файлов и
ротацию Docker логов. Исходные PDF ограничены отдельной квотой приложения.
На Linux для production настройте выделенные разделы или XFS project quotas
для dataset, backup, Caddy state и Docker storage. Учитывайте batch JSON,
SQLite journals, checkpoints, промежуточные файлы, паспорта, ZIP, build cache
и Docker logs. Для одного сервера минимальный контроль разделяет ёмкость
dataset, backup и Docker data-root. При project quotas дополнительно
снимайте host-side показатели квоты, поскольку `statvfs` может показывать
свободное место всего раздела. Соберите подтверждение действующих лимитов
и тест отказа на изолированном разделе перед публичным запуском.

Если диск переполнен, остановите proxy, дождитесь остановки писателей,
освободите место в соответствии с политикой хранения и проверьте backup.
Ответ 507 `storage_full` диагностирует исчерпание tmpfs или Storage. Если
запись статуса задачи тоже исчерпала место, останется recoverable
`processing`. После восстановления ёмкости worker повторно захватывает
задачу под тем же ID, использует checkpoint и прежний журнал review.
Проверьте SHA-256 и очередь перед возобновлением запросов.

## Наблюдаемость

```bash
docker compose … ps
docker compose … logs --since 15m --tail 120 monitoring
docker compose … logs --since 15m --tail 120 api worker proxy
```

`GET /api/health` — liveness. Readiness `GET /__ops/ready` доступна только
на loopback внутри API и проверяет auth SQLite, возможность захвата
транзакции, состояние каталогов и свободное место. Worker публикует heartbeat
на idle и при получении задачи. Monitoring каждые 15 секунд объединяет
heartbeat, очередь и возраст backlog, число processing/failed, recoveries,
зарезервированные model requests, свободное место, агрегаты API и события
backup/restore. У каждого worker свой heartbeat, а активные jobs считаются
по SQLite. Monitoring читает только агрегаты из metrics volume. Model count
берётся из текущих записей задач, а в Compose GigaChat выключен.

Порог для idle heartbeat — 30 секунд, processing — deadline задачи + 15
секунд. Отправляйте агрегаты и контейнерные статусы в систему мониторинга
хоста. Настройте alerts на stalled worker, отсутствие samples, свободное
место, нарушение окна backup, рост 5xx и возраст backlog. Старые метрики
остаются с предыдущим timestamp до следующей записи. Caddy не включает
HTTP access/error request logs. API пишет UTC, correlation ID, статус и
длительность, worker пишет только фиксированные диагностические коды и
необязательный opaque job ID. Ограничение Docker local logs — 10 MiB × 3
на контейнер.

## Обновить и откатить

1. Сохраните точный image digest и исходный SHA. Создайте cold backup и
   проверьте копию. Проверьте Compose config и соберите новый тег образов.
2. Прочитайте release notes и проверьте новую версию с копией dataset.
   Текущий формат SQLite `user_version=0`. Миграции должны быть явными,
   версионированными и предварительно отрепетированными.
3. Остановите proxy, затем worker и API с их штатным grace period. При
   необходимости выполните утверждённую миграцию во время maintenance.
4. Запустите `up -d --wait`, проверьте readiness и внешний smoke.
   Зафиксируйте digest, версии SQLite, время и результат.
5. При отказе удерживайте maintenance. Верните предыдущий image digest,
   если его схема и данные совместимы. При несовместимой схеме
   восстановите проверенный снимок в новый dataset и оцените изменения
   после backup до переключения. Исходный каталог сохраните для расследования.

Сбой worker в checkpoint восстанавливается при следующем запуске и
освобождении OS lock. При подозрении на утечку отключите внешний трафик,
сохраните журналы и копию данных. Заблокируйте скомпрометированные аккаунты
существующим `spi.auth_cli block`. Для отзыва отдельных сессий есть экран
аккаунта. Для массового отзыва останавливайте писателей и транзакционно
отзывайте строки `sessions` в auth DB после проверенного backup.
При компрометации секрета создайте новый файл с теми же правами, замените
его и пересоздайте контейнеры. Ротацию секрета проводите вместе с отзывом
сессий, поскольку хеши сессий хранятся независимо от CSRF-ключа.

**`docker compose down -v` удаляет volumes и не должен использоваться на
production данных без проверенного backup и осознанного решения.**

Источники [SQLite WAL](https://www.sqlite.org/wal.html), [Compose services](https://docs.docker.com/reference/compose-file/services/).
