# Допуск к публичной эксплуатации

**PUBLIC LAUNCH: NOT READY**

**PUBLIC PRODUCTION SMOKE: NOT RUN**

Публичный сервер, домен и DNS для этого этапа не предоставлены. Локальные
проверки Compose и GitHub Actions дают свидетельства об изменениях кода и
сборке. Допуск требует результатов на действующей публичной установке.

| Обязательный контроль | Статус production | Свидетельство для PASS |
| --- | --- | --- |
| Production Docker images | NOT RUN | Сборка и указанная пара OCI digests на сервере |
| Non-root API и worker | NOT RUN | `docker inspect` и effective uid/capabilities в контейнере |
| Ограничения ресурсов | NOT RUN | Cgroup CPU, RAM и PID, nofile, deadline и отказ при перегрузке |
| Trusted proxy configuration | NOT RUN | Статический /32, проверка на реальном proxy и закрытый порт API |
| Настоящий HTTPS certificate | NOT RUN | DNS, hostname, публичная trust chain, redirect, HSTS и ACME state |
| Secure cookies и CSRF | NOT RUN | Сквозная браузерная проверка cookie, Host, Origin и CSRF |
| Tenant isolation | NOT RUN | Положительные и отрицательные проверки Alice/Bob на сервере |
| Защита всех PDF parsing paths | NOT RUN | Результаты seccomp/Landlock и инвентаризации разбора на целевом ядре |
| Persistent storage | NOT RUN | Перезапуски и замена образа под тем же dataset |
| Backup и restore | NOT RUN | Cold snapshot и отдельное восстановление с SHA-256, review history и проверкой ACL |
| Monitoring | NOT RUN | Реальные метрики и настройка оповещений на сервере |
| Лимиты диска | NOT RUN | Изоляция разделов или квот и воспроизводимый отказ на host filesystem |
| Security scans | BLOCKED | Разобрать оставшиеся HIGH в OCI-компонентах и пересканировать развёрнутые digests |
| Production E2E | NOT RUN | Протокол публичного smoke и синтетической сквозной проверки |

`PASS` требует проверенного результата и датированного свидетельства.
`FAIL` означает несоответствие при проверке. `NOT RUN` означает отсутствие
проведённой проверки. `BLOCKED` означает обнаруженный запрет на публичный
запуск. Любой статус кроме `PASS` у обязательного пункта сохраняет
`NOT READY`. Локальные результаты и причины блокировки сканов приведены
в [отчёте P0 №31](P0_31_RESULTS.md). Процедуры и границы доверия описаны в
[Security model](SECURITY_MODEL.md) и [Deployment](DEPLOYMENT.md).

```bash
python -m spi.ops.production_check
python -m spi.ops.production_check --evidence /path/to/production-evidence.json --json
```

Команда возвращает 0 только для `READY`, 1 для `NOT READY` и 2 при
невалидном отчёте. Доверенный оператор составляет JSON после проверок
указанной версии и origin. Формат принимает ключи контролей из вывода
`--json`. Пример ниже демонстрирует структуру, с незаполненными контролями
результат остаётся `NOT READY`.

```json
{
  "environment": "public-production",
  "public_origin": "https://spi.example.org",
  "revision": "0123456789abcdef0123456789abcdef01234567",
  "controls": {
    "public_tls": {
      "status": "PASS",
      "reference": "operator report for the named release",
      "checked_at": "2026-11-22T12:00:00Z"
    }
  }
}
```
