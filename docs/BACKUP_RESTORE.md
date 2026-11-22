# Cold backup и проверка восстановления

Backup содержит auth SQLite с хешами паролей, приглашениями и квотами, PDF
пользователей и tenant SQLite, JSON научных результатов, issues, checkpoints,
review events, actor associations и migration receipts. Копия auth DB
исключает сессии. Auth secret, Caddy TLS state и GigaChat credentials хранятся
отдельно. ZIP формируются из сохранённых исходных данных.

## Создать и проверить

```bash
bash deploy/scripts/cold-backup.sh /srv/spi/compose.env spi-2026-11-22T0010Z
docker compose --env-file /srv/spi/compose.env -f compose.yaml -f compose.production.yaml run --rm --no-deps backup verify spi-2026-11-22T0010Z
```

Скрипт останавливает Caddy для maintenance, затем штатно останавливает API
и worker. Backup требует эксклюзивный файловый lock, который несовместим с
работающими писателями Compose. Перед копией остановите внешние CLI, если
они запускались напрямую с доступом к тому же dataset. Утилита сохраняет
SQLite с помощью SQLite backup API, проверяет владельцев, инвентаризирует
PDF и научные данные и повторно проверяет готовый архив перед публикацией.
Незавершённые multipart-файлы и незафиксированные оригиналы исключаются.
При повреждении SQLite остановите эксплуатацию и примените процедуру
инцидента. Если после аварии остался hot journal, штатно восстановите SQLite
на том же хосте перед backup либо используйте последнюю проверенную копию.

В `manifest.json` записаны ID и UTC-время, версия приложения, версия формата,
SQLite `user_version`, количество пользователей, PDF, jobs и review events,
относительные имена, байты и SHA-256 каждого файла, политика восстановления,
признак cold snapshot и результат проверки. Полный manifest содержит
внутренние идентификаторы. Храните его вместе с архивом.

## Шифрование и копии вне хоста

P0 №32 добавляет проверяемый restic workflow с репозиторием версии 2.
Храните ключ restic, ciphertext и исходный dataset в отдельных каталогах.
Ограничьте доступ к plaintext staging, архивам, журналу и файлу пароля.
На Windows ограничьте NTFS ACL учётной записью службы или оператора.

```bash
umask 077
restic_args=(--repo /secure/spi-restic --password-file /secure/keys/spi-restic \
  --scratch /secure/backup-scratch --report /secure/spi-restic-status.json)
python -m spi.ops.encrypted_backup "${restic_args[@]}" init
python -m spi.ops.encrypted_backup "${restic_args[@]}" backup /srv/spi/backups/rehearsal.tar
python -m spi.ops.encrypted_backup "${restic_args[@]}" verify
python -m spi.ops.encrypted_backup "${restic_args[@]}" restore "$SNAPSHOT_ID" /secure/rehearsal/snapshot.tar
python -m spi.ops.encrypted_backup "${restic_args[@]}" retention --daily 7 --weekly 4 --monthly 12
```

Отчёт содержит время и размер последней копии, последнюю проверку данных,
время и SHA-256 извлечённого TAR, признаки шифрования и план retention.
Полная репетиция дополнительно выполняет `spi.ops.restore` и проверяет
приложение с отдельным dataset. Локальный E2E проверяет отказ с неверным
ключом и восстановление SHA-256 с правильным ключом.

Перед использованием off-host назначьте одобренное хранилище и скопируйте
ciphertext закрытого локального репозитория или настройте штатный `restic copy`.
Проверьте восстановление из самого внешнего назначения. Политика wrapper
планирует 7 daily, 4 weekly и 12 monthly, сохраняет два recovery points и
отклоняет план, если теряется последнее восстановленное состояние. Для
ручного удаления выполните свежий dry-run, проверьте выбранные snapshot ID
и восстановление вне сервера. Автоматическое удаление отсутствует.

Для ротации пароля добавьте новый ключ через `restic key add`, проверьте
`check --read-data` и отдельный restore с новым ключом, затем удалите старый
ключ через `restic key remove`. При компрометации master key создайте новый
репозиторий и скопируйте проверенные snapshots. При потере всех пригодных
ключей репозиторий не восстанавливается. Храните проверенную резервную
копию ключа вне dataset и основного сервера.

Ниже сохранён альтернативный ручной workflow age для ранее созданных копий.

Запускайте проверенный cold backup ежедневно в согласованное окно и перед
каждым обновлением. Контролируйте возраст последней успешной копии, объём и
свободное место. Начальная политика — 7 ежедневных и 4 еженедельных архивов
после подтверждения off-host копии. Резервируйте место минимум под три
объёма активного snapshot в момент упаковки и перепроверки.

Создайте отдельную пару ключей `age`. Держите приватный ключ вне сервера, на
защищённом носителе с проверенным доступом ответственных операторов.
На сервере хранится только проверенный публичный recipient. Пример с
файлом recipients, установленным оператором.

```bash
umask 077
sudo age -R /etc/spi/backup-recipients.txt -o /secure/spi-2026-11-22T0010Z.tar.age /srv/spi/backups/spi-2026-11-22T0010Z.tar
sudo sha256sum /secure/spi-2026-11-22T0010Z.tar.age
```

Переносите ciphertext в утверждённое независимое хранилище с защитой от
перезаписи. Сверяйте SHA после переноса и регулярно проверяйте расшифровку на
отдельном сервере. Защитите manifest от подмены независимо от хеша отдельных
файлов. Аутентификация архива достигается проверенной зашифрованной копией
и доверенным контролем происхождения, поскольку SHA-256 файлов можно
пересчитать при подмене всего архива.

## Восстановить и отрепетировать

Создайте новый каталог `SPI_HOST_DATASET` с UID/GID 10001 и отдельный
Compose project name и файл конфигурации. Скопируйте проверенный TAR в
защищённый `SPI_HOST_BACKUPS`. При наличии ciphertext расшифруйте его в этот
каталог с `age -d -i /path/to/identity.txt -o …tar …tar.age` и правами 0600.
Сохраните источник вне нового dataset.

```bash
rehearsal=(docker compose -p spi-rehearsal --env-file /srv/spi-rehearsal/compose.env -f compose.yaml -f compose.production.yaml)
"${rehearsal[@]}" pull
"${rehearsal[@]}" run --rm --no-deps restore spi-2026-11-22T0010Z --dry-run
"${rehearsal[@]}" run --rm --no-deps restore spi-2026-11-22T0010Z
```

Restore требует отсутствия `dataset/state`, сверяет manifest и SHA-256,
проверяет SQLite integrity и версию, PDF, checkpoint, очереди и review
history. Он публикует полностью проверенный каталог атомарным rename на
том же разделе. Для замены действующего deployment направьте конфигурацию
на новый dataset после проверки. Старый dataset сохраните отдельно.
Повторный restore в существующий каталог отклоняется.

На том же хосте проверяйте восстановление в локальном Compose-профиле,
чтобы избежать публикации портов и конфликтов сертификатов. На чистом
prod-хосте установите отдельный секрет согласно Deployment. Оба варианта
требуют повторного логина прежних пользователей. Проверьте, что старый
cookie возвращает 401, приглашения сохраняют срок и статус погашения, а
после backup отозванные приглашения и доступы сверены с операционным журналом.

Сравните SHA-256 оригиналов и export snapshot, научные JSON и историю
решений, actor association, отрицательные проверки Alice/Bob и результат
`python -m spi.upload_export_verify archive.zip`. Зафиксируйте RPO, длительность
восстановления, UTC-время и точный релиз. Backup без успешной отдельной
репетиции не допускает публичный запуск.

Источники [SQLite backup API](https://www.sqlite.org/backup.html), [age](https://github.com/FiloSottile/age).
