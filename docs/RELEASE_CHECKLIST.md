# Чек-лист релиза

## Кандидат

1. Зафиксируйте точный Git SHA и успешные задания build, runtime controls, Trivy, deployment integration и regression. Для релиза после merge используйте run из `main` на конечном SHA.
2. Скачайте три артефакта одного GitHub Actions run. `spi-oci-images` содержит OCI-архивы обоих образов и сведения о toolchain. `spi-oci-scans-and-sboms` содержит исходные Trivy JSON и CycloneDX. `spi-release-manifest` связывает их с SHA, платформой, digest каждого образа и результатами заданий.
3. Проверьте все CRITICAL, секреты и HIGH по [OCI triage](OCI_VULNERABILITY_TRIAGE.md). Проверьте срок и область каждого утверждённого исключения. Остановите выпуск при результате `RELEASE HARDENING: BLOCKED`.
4. Проверьте SHA-256 14 официальных CSV, синтетические Solar/RAG тесты и ZIP verifier. Проверьте свежую независимую репетицию восстановления.
5. Для утверждённого кандидата перенесите именно проверенные архивы в утверждённый registry. BuildKit сохраняет provenance в OCI-архиве. Она unsigned. Хеши проверяют соответствие байтов артефактам доверенного Actions run.

```bash
skopeo copy --all --preserve-digests oci-archive:runtime.tar "docker://$RUNTIME_REPO:$RELEASE_SHA"
skopeo copy --all --preserve-digests oci-archive:proxy.tar "docker://$PROXY_REPO:$RELEASE_SHA"
docker buildx imagetools inspect "$RUNTIME_REPO@$RUNTIME_DIGEST"
docker buildx imagetools inspect "$PROXY_REPO@$PROXY_DIGEST"
```

Подставьте платформенные `oci_digest` из release manifest в production env. Проверяйте, что registry действительно отдаёт их. Платформенный manifest и индекс архива имеют разные digest.

## Целевая установка

1. На Linux подтвердите cgroup v2, seccomp, Landlock, firewall, доверенный proxy, каталоги и квоты. Настройте резервирование, место, доставку alerts и защищённый ключ.
2. Разверните подтверждённую пару digest в назначенное окно. Проведите DNS/TLS smoke, ручной Alice/Bob E2E с синтетическими данными, автономную проверку двух ZIP и зашифрованный restore в независимой среде.
3. Соберите датированные первичные свидетельства и подтвердите все контроли в [Production readiness](PRODUCTION_READINESS.md). В журнале изменений сохраните решение ответственного оператора, идентификатор среды и ссылку на одобренный release run.
4. При откате верните предыдущие два digest и совместимую конфигурацию. Если состояние несовместимо, используйте проверенный backup в чистый dataset и сохраните прежний dataset для расследования. Процедура приведена в [Operations](OPERATIONS_RUNBOOK.md).
