# Допуск к публичной эксплуатации

**PUBLIC LAUNCH: NOT READY**

**PUBLIC PRODUCTION SMOKE: NOT RUN**

В P0 №32 публичный сервер, домен и DNS не предоставлены. Windows Docker Desktop и CI дают свидетельства о конкретных образах. Проверки целевой установки проводит оператор.

| Контроль | Первичное свидетельство |
| --- | --- |
| `production_images`, `non_root`, `resource_limits` | Развёрнутые OCI digest, `docker inspect` и фактические cgroup/UID/capabilities |
| `trusted_proxy`, `public_tls`, `cookies_csrf` | Внешний DNS и TLS, hostname, firewall, HTTPS, HSTS, Host, Origin, CSRF и cookie |
| `tenant_isolation`, `pdf_isolation` | Alice/Bob и реальный seccomp/Landlock на целевом ядре |
| `persistent_storage`, `disk_limits` | Перезапуски, независимые host quota или разделы, тест ENOSPC без потери записи |
| `backup_restore` | Зашифрованный backup и независимый restore с SHA-256, ACL, review history и недействительными старыми сессиями |
| `monitoring` | Тестовая доставка alert до операторского receiver |
| `production_e2e` | Протокол полного публичного smoke, два синтетических PDF и оба ZIP verifier |
| `security_scans`, `security_high`, `release_tests` | Исходные OCI-архивы, Trivy JSON, release manifest и результаты заданий одного доверенного Actions run |

Сохраните JSON с независимо установленной идентичностью целевого развёртывания. Включите `environment: "public-production"`, `environment_id`, `public_origin`, `hostname`, `revision` (40-значный Git SHA) и `image_digests` с ключами `runtime` и `proxy` и платформенными OCI digest. Подтверждайте digest на хосте из image reference и локального registry metadata.

В evidence JSON включите эти же поля, `schema_version: 2` и объект `controls`. Для каждого операторского контроля добавьте `outcome` со значением `verified`, `failed` или `blocked`, имя ответственного в `operator`, UTC-время в `checked_at`, ссылку HTTPS на первичное свидетельство в `reference`, имя локального файла рядом с evidence JSON в `artifact` и его SHA-256 с префиксом `sha256:` в `sha256`. Для `backup_restore` укажите отдельный `restore_environment_id`. Отсутствующий контроль сохраняет `NOT RUN`. Операторские свидетельства действуют семь дней, OCI-сканы три дня. Дубликаты JSON, конфликт идентичности, неверный origin или digest и отсутствие первичного файла завершают проверку ошибкой.

Скачайте исходные OCI-архивы, Trivy и release manifest из одного доверенного Actions run и поместите в один каталог. CLI перепроверяет OCI manifest, config, связь со сканом, политику HIGH и привязку тестов к релизу. Файлы и хеши подтверждают согласованность материала. Источник Actions проверяет оператор. Операторские результаты отображаются как `operator-attested`.

```bash
python -m spi.ops.production_check
python -m spi.ops.production_check --evidence evidence.json \
  --expected-deployment deployment-identity.json --scans ./artifacts --json
```

Код 0 означает `READY`, 1 означает `NOT READY`, 2 означает ошибку свидетельства. Разрешение на публичный запуск фиксирует оператор после проверки первичных материалов. Порядок действий приведён в [Release checklist](RELEASE_CHECKLIST.md).
