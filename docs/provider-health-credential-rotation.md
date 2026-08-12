# Provider Health & Credential Rotation

Актуально на: 2026-08-10

Scope: Qobuz и Яндекс Музыка; Spotify включён только в миграцию encrypted vault.
Google Drive и Release 2 не входят в эту итерацию.

## Модель здоровья

Для каждого provider сохраняются четыре независимых компонента:

| Компонент | Что проверяется |
|---|---|
| `account` | активный credential принят аккаунтом |
| `provider_api` | внешний API отвечает независимо от состояния credential |
| `sidecar` | приватный Qobuz sidecar или Yandex signer доступен |
| `worker` | health-check реально выполнился внутри Celery worker |

Допустимые состояния: `healthy`, `expired`, `rate_limited`, `provider_down`,
`not_configured`. Ответы содержат только state, безопасный `detail_code`, время,
latency, retry time и номер версии credential. Токены и provider payload в
health API не входят.

Плановые проверки запускает отдельный Celery beat каждые 1800 секунд. Ручной
`POST /api/providers/{provider}/health-check` создаёт обычный job и имеет
60-секундный cooldown. Если worker-запись старше 5400 секунд, API показывает
`provider_down/worker_stale`.

## Credential vault

System-owned `provider_credentials` хранит только Qobuz/Yandex acquisition
credentials. `user_provider_credentials` хранит отдельно по `(user_id,
provider)` Spotify/Yandex playlist credentials. Обе таблицы содержат только:

- provider и монотонную version;
- AES-256-GCM ciphertext и nonce;
- короткий key ID для fail-closed проверки нужного ключа;
- created/updated/validated timestamps.

Ключи в PostgreSQL не хранятся:

- `provider-credentials.key` шифрует system Yandex и user-owned
  Spotify/Yandex;
- `qobuz-credentials.key` шифрует только Qobuz и доступен Qobuz sidecar;
- backend получает оба ключа для rotation, worker — только
  provider/user-provider key, migration — только этот же key, Qobuz sidecar —
  только Qobuz key;
- оба файла ignored и монтируются read-only как Docker Secrets;
- Qobuz/Yandex account tokens принудительно очищаются из environment сервисов.

База и key files должны резервироваться раздельно. Потеря соответствующего
32-byte key делает ciphertext невосстановимым. Нельзя заменять key file при
существующих записях без отдельной процедуры re-encryption.

## Атомарная ротация

1. Owner-only authenticated same-origin UI отправляет новый system credential
   в JSON body по TLS.
2. Backend берёт provider advisory lock и шифрует временный envelope новой версии.
3. Qobuz sidecar расшифровывает envelope в памяти и выполняет connect; Yandex
   client выполняет `init()` с новым токеном.
4. Только после успешной проверки backend записывает новую encrypted-версию и
   health rows одной DB-транзакцией.
5. При auth/rate-limit/provider/storage ошибке выполняется rollback; прежняя
   версия остаётся активной.
6. Следующая операция backend/worker читает новую версию из БД, поэтому restart
   Docker не нужен.

Ни request body, ни provider response не логируются. API использует нейтральные
ошибки и `Cache-Control: no-store`. PWA использует password inputs, очищает form
сразу после submit, не применяет local/session storage и не кэширует `/api` в
service worker.

## API

- `GET /api/providers/health`;
- `GET /api/providers/{provider}/health`;
- `POST /api/providers/{provider}/health-check` → `202 JobOut`;
- `PUT /api/providers/qobuz/credentials` с `token`, `user_id`;
- `PUT /api/providers/yandex/credentials` с `token`.

Credential endpoints возвращают только provider, configured, version,
updated_at и необязательный безопасный label. User-owned Spotify/Yandex
playlist credentials доступны только своему source flow и API не выдаются.

## Upgrade

Migration `0005_provider_health_credentials` создала исходный vault/health.
При переходе на `0009_google_user_auth_contract` сервис
`app.commands.migrate_database` сначала шифрует оставшиеся legacy source
tokens, затем переносит Spotify и Yandex playlist credentials bootstrap owner.
Global Spotify удаляется из system vault; global Yandex сохраняется как
acquisition credential. Команда выводит только counts и безопасна при
повторном запуске. Qobuz вводится через owner PWA; старые
`QOBUZ_AUTH_TOKEN/QOBUZ_USER_ID` Compose не передаёт контейнерам.

Google Login client secret и `auth.key` не входят ни в один provider vault:
это отдельные Docker secrets, описанные в `docs/google-user-auth.md`.

## Docker-приёмка 2026-08-10

- Alembic: `0005_provider_health_credentials (head)`;
- legacy plaintext source rows: `0`; encrypted migrated rows: `2`;
- backend pytest: `212 passed, 5 skipped`;
- Qobuz sidecar: `9 tests`, OK;
- Compose: backend/frontend/worker/beat/Qobuz/Yandex services running, DB/Redis
  and both sidecars healthy;
- Celery inspect: `pong`;
- manual jobs: Qobuz и Yandex `done`;
- Yandex live components: account/API/sidecar/worker = `healthy`;
- Qobuz без первого UI credential: account/API = `not_configured`,
  sidecar/worker = `healthy`;
- Yandex hot-rotation через PWA nginx origin тем же валидным credential:
  HTTP 200, `Cache-Control: no-store`, version `3`, backend container ID не
  изменился;
- PWA desktop/mobile: экран и формы видимы, три поля имеют `type=password`,
  `autocomplete=off`, пустые values, console errors = `0`.

## Production-проверка user-owned Spotify vault 2026-08-12

После реального Spotify OAuth второго Google-пользователя в vault существуют
две раздельные `(user_id, spotify)` строки. Fingerprint существующего encrypted
owner envelope до и после callback совпал, а новая credential и source
принадлежат только второму user. Ни ciphertext, ни OAuth token, ни email при
проверке не выводились; exact/generic log scan дал 0 совпадений.
