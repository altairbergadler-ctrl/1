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

`provider_credentials` хранит только:

- provider и монотонную version;
- AES-256-GCM ciphertext и nonce;
- короткий key ID для fail-closed проверки нужного ключа;
- created/updated/validated timestamps.

Ключи в PostgreSQL не хранятся:

- `provider-credentials.key` шифрует Yandex и Spotify;
- `qobuz-credentials.key` шифрует только Qobuz и доступен Qobuz sidecar;
- backend получает оба ключа для rotation, worker — только Yandex/Spotify key,
  migration — только Yandex/Spotify key, Qobuz sidecar — только Qobuz key;
- оба файла ignored и монтируются read-only как Docker Secrets;
- Qobuz/Yandex account tokens принудительно очищаются из environment сервисов.

База и key files должны резервироваться раздельно. Потеря соответствующего
32-byte key делает ciphertext невосстановимым. Нельзя заменять key file при
существующих записях без отдельной процедуры re-encryption.

## Атомарная ротация

1. Authenticated same-origin UI отправляет новый credential в JSON body по TLS.
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
updated_at и необязательный безопасный label.

## Upgrade

Migration `0005_provider_health_credentials` создаёт vault/health tables.
После `alembic upgrade head` команда `python -m app.commands.migrate_credentials`
однократно переносит существующие Spotify/Yandex tokens из `playlist_sources`
и зануляет legacy columns. Команда выводит только количество перенесённых строк
и безопасна при повторном запуске. Qobuz при первом переходе вводится через PWA;
старые `QOBUZ_AUTH_TOKEN/QOBUZ_USER_ID` Compose больше не передаёт контейнерам.

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
