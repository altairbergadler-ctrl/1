# VPS deployment: audiofeel.su

Этот профиль разворачивает Music Service рядом с OpenClaw, не публикуя
контейнерные порты напрямую в интернет.

## Изоляция

- frontend слушает только `127.0.0.1:18080`;
- backend слушает только `127.0.0.1:18000`;
- PostgreSQL, Redis и provider sidecars не публикуют host-порты;
- Qobuz и Yandex control networks остаются `internal`;
- для каждого постоянного контейнера заданы лимиты RAM, CPU и PID;
- backend работает без `--reload`, а код берётся из собранного image;
- Uvicorn и весь frontend Nginx access log отключены; Caddy access
  log не включён, а runtime logger фильтрует OAuth query-параметры, поэтому
  `code`, `state` и tokens не попадают в штатные журналы;
- Docker использует отдельный address pool `172.30.0.0/16`, локальный log
  driver с ротацией и не меняет политику IP forwarding, необходимую VPN.

## Подготовка runtime

Из корня checkout на VPS:

```bash
bash deploy/vps/bootstrap-runtime.sh
```

Скрипт идемпотентно создаёт:

- `/etc/audiofeel/music-service.env` с правами `0600`;
- независимые 32-byte provider и server-session keys в
  `/etc/audiofeel/secrets`;
- отдельный пустой `google-login-client-secret` с правами `0400`; после
  создания Login OAuth client он заполняется без вывода значения;
- `/srv/audiofeel/library` и `/srv/audiofeel/staging` как временные зоны
  приёма, а также ограниченный `/srv/audiofeel/cache` для сборки ZIP из Drive;
- случайные PostgreSQL, PWA и внутренние sidecar secrets без вывода значений.

Существующий env и существующие ключи скрипт не заменяет.

## Запуск

До первого запуска релиза с `0008/0009` обязательно создать и восстановить в
отдельную БД PostgreSQL backup по разделу `Backup перед migration` ниже.

```bash
if git rev-parse --verify HEAD >/dev/null 2>&1; then
  release_sha=$(git rev-parse HEAD)
else
  release_sha=$(basename "$(readlink -f /opt/audiofeel/app)")
fi
case "$release_sha" in
  ""|*[!0-9a-f]* ) echo "invalid release SHA" >&2; exit 1 ;;
esac
test "${#release_sha}" -eq 40
sed -i "s/^RELEASE_SHA=.*/RELEASE_SHA=${release_sha}/" \
  /etc/audiofeel/music-service.env
unset release_sha
docker compose \
  --env-file /etc/audiofeel/music-service.env \
  -f docker-compose.yml \
  -f docker-compose.production.yml \
  up -d --build
```

## Приёмка без раскрытия credentials

```bash
docker compose \
  --env-file /etc/audiofeel/music-service.env \
  -f docker-compose.yml \
  -f docker-compose.production.yml \
  ps

curl --fail --silent http://127.0.0.1:18000/api/health
curl --fail --silent --output /dev/null http://127.0.0.1:18080/

docker compose \
  --env-file /etc/audiofeel/music-service.env \
  -f docker-compose.yml \
  -f docker-compose.production.yml \
  exec -T worker \
  celery -A app.workers.celery_app.celery inspect ping --timeout=10
```

`release_sha` из health должен точно совпасть с `git rev-parse HEAD` локально,
на VPS и с SHA remote branch. Это значение не является credential.

Проверка Alembic:

```bash
docker compose \
  --env-file /etc/audiofeel/music-service.env \
  -f docker-compose.yml \
  -f docker-compose.production.yml \
  run --rm --no-deps migrate sh -lc 'alembic current; alembic heads'
```

Для текущего релиза обе команды должны показать
`0012_web_push_subscriptions`. Сам сервис
`migrate` запускает `app.commands.migrate_database`: сначала expand, затем
credential/ownership backfill, после него contract.

## Backup перед migration

```bash
install -d -m 0700 /var/backups/audiofeel
backup=/var/backups/audiofeel/pre-google-auth-$(date -u +%Y%m%dT%H%M%SZ).dump
docker compose --env-file /etc/audiofeel/music-service.env \
  -f docker-compose.yml -f docker-compose.production.yml \
  exec -T db sh -lc 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  >"$backup"
chmod 0600 "$backup"
test -s "$backup"
docker compose --env-file /etc/audiofeel/music-service.env \
  -f docker-compose.yml -f docker-compose.production.yml \
  exec -T db pg_restore --list <"$backup" >/dev/null
sha256sum "$backup" >"${backup}.sha256"
```

Проверить dump реальным restore, не затрагивая production DB:

```bash
docker compose --env-file /etc/audiofeel/music-service.env \
  -f docker-compose.yml -f docker-compose.production.yml \
  exec -T db sh -lc 'dropdb -U "$POSTGRES_USER" --if-exists audiofeel_restore_check && createdb -U "$POSTGRES_USER" audiofeel_restore_check'
docker compose --env-file /etc/audiofeel/music-service.env \
  -f docker-compose.yml -f docker-compose.production.yml \
  exec -T db sh -lc 'pg_restore -U "$POSTGRES_USER" -d audiofeel_restore_check --exit-on-error --no-owner --no-privileges' \
  <"$backup"
docker compose --env-file /etc/audiofeel/music-service.env \
  -f docker-compose.yml -f docker-compose.production.yml \
  exec -T db sh -lc 'psql -U "$POSTGRES_USER" -d audiofeel_restore_check -Atc "select version_num from alembic_version"'
docker compose --env-file /etc/audiofeel/music-service.env \
  -f docker-compose.yml -f docker-compose.production.yml \
  exec -T db sh -lc 'dropdb -U "$POSTGRES_USER" audiofeel_restore_check'
```

Фиксируются путь backup, SHA-256 и успешный restore. Counts можно сравнить, но
email и содержимое credential tables выводить нельзя.

## Обновление и откат

Перед обновлением фиксируется Git SHA и снимается проверенный backup PostgreSQL. Затем
образы пересобираются той же командой `up -d --build`. Для отката checkout
возвращается на предыдущий проверенный SHA и команда повторяется. Каталог
`/srv/audiofeel/library`, staging, session/provider keys и volume PostgreSQL не
удаляются.

Lossless schema downgrade до `0007` разрешён только пока существует ровно один
pristine pending bootstrap owner без Google identity. После first login или
добавления второго пользователя rollback выполняется восстановлением
pre-migration dump и предыдущего Git SHA; migration намеренно откажет опасному
downgrade.

Команды `down -v`, очистка `/var/lib/docker` и удаление runtime-каталогов в
штатном обновлении запрещены.

## Публичный доступ

TLS reverse proxy подключается к `127.0.0.1:18080`. До завершения DNS/TLS
приёмки контейнеры остаются доступны только с VPS. Provider account tokens
вводятся позднее через PWA и не хранятся в env.

Google требует два независимых Web OAuth clients:

- `Audiofeel Login`: client ID в защищённом env, client secret в отдельном
  Docker secret, callback
  `https://audiofeel.su/api/auth/google/callback`, scopes
  `openid email profile`;
- `Audiofeel Drive`: config и refresh tokens вводятся owner только через
  `PWA -> Хранилище`, шифруются storage vault, callback
  `https://audiofeel.su/api/storage/google/callback`.

Сначала через отдельный `/#/recovery` с `APP_AUTH_TOKEN` задаётся Google email
bootstrap owner. После успешного Google-входа recovery остаётся только
аварийным механизмом. Все остальные подтверждённые Google identity
регистрируются автоматически как `user`.

Полный Console/setup/session/CSRF/ownership и двухаккаунтный acceptance runbook:
`docs/google-user-auth.md`.

## Исторический двухаккаунтный production gate

## Acquisition queue и Web Push

До запуска релиза создать отдельный VAPID P-256 key, не выводя его содержимое:

```bash
install -d -m 0700 /etc/audiofeel/secrets
test -s /etc/audiofeel/secrets/web-push-vapid-private.pem || \
  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 \
    -out /etc/audiofeel/secrets/web-push-vapid-private.pem
chmod 0400 /etc/audiofeel/secrets/web-push-vapid-private.pem
```

В защищённом `/etc/audiofeel/music-service.env` задаются
`ACQUISITION_ENABLED=true`, batch size/dispatcher interval,
`WEB_PUSH_ENABLED=true`, host path ключа и VAPID subject. Значения env и
private key в журнал не выводятся. Проверка Compose выполняется через
`docker compose ... config -q`.

Worker должен слушать очереди `celery,acquisition`, а beat периодически
запускать `acquisition_dispatch`. Фактическая отправка Push возможна только
после того, как пользователь разрешит уведомления в PWA. Отдельное
Google/Firebase приложение не нужно. Полный gate:
`docs/acquisition-workflow.md`.

## OpenSubsonic deployment gate

Gate выполнен 2026-08-12 на code release
`26f1bd513f22a0ed98afe2628ce440b8c644a371`:

1. PostgreSQL custom dump, `pg_restore --list` и пробное восстановление.
2. Upgrade до `0010_open_subsonic_players`; проверить, что player key rows не
   создавались автоматически и File/SHA-1/Drive rows не изменились.
3. Caddy route `/rest/*` напрямую на backend. Site access log должен быть выключен:
   OpenSubsonic API key передаётся в query string.
4. Backend/PWA regression suite, health всех контейнеров и проверка deployed SHA.
5. Только затем отдельная real-phone Symfonium acceptance по
   `docs/player-sync-symfonium.md`.

Фактический результат:

- custom dump создан, `pg_restore --list` прошёл, restore в отдельную БД вернул
  `0009_google_user_auth_contract`, проверочная БД удалена;
- migration достигла `0010_open_subsonic_players`, повторный запуск вернул
  `already_current`; автоматически создано `0` player credentials;
- counts и контрольные digest для 143 `File` и 143 Drive location совпали до и
  после migration, все public ID/sync metadata заполнены;
- Caddy `/rest/*`, discarded site log, Uvicorn `--no-access-log` и Nginx
  `access_log off` применены; backend/frontend healthy, Celery вернул `pong`;
- public health/deployed SHA, OpenSubsonic discovery и live-PWA `12 passed`;
  query-secret log hits в Caddy/backend — `0`.

Real-phone Symfonium acceptance в этот gate не входит и остаётся невыполненной.

Rollback приложения выполняется вместе с downgrade до 0009; device keys после
этого утрачиваются и создаются заново. Multi-user downgrade ниже 0009 остаётся
запрещён прежним production-контрактом.

На 2026-08-12 в production выполнен прежний invitation-only runbook: test-user allowlist,
неприглашённый `403` без DB side effects, owner invitation, first-login binding,
двусторонние IDOR-пробы, shared-File Range delivery, независимый Spotify vault,
disable/revoke и повторный `403`. Drive account после проверки остался enabled
и healthy. В логах не найдено exact/generic email, unredacted OAuth query values
или session-cookie values.

Открытая регистрация и RBAC развёрнуты в production на
`8595779f9977611904ede10b909f2a08c2cb904e`. Изолированный VPS suite дал
`328 passed`; backup прошёл пробное восстановление; deployed/GitHub SHA,
Alembic `0010`, health контейнеров, Celery `pong` и нулевые error/query-secret
counters подтверждены. Для ручной identity-приёмки проверить:

1. **Audiofeel Login** имеет Audience=`External` и publishing status production;
   Drive OAuth client и его Test users не изменять.
2. Ранее неизвестный подтверждённый Google account создаётся как `active user`
   без предварительной строки в `users`.
3. Новый `user` получает `403` на `/api/admin/*`, `/api/providers/*`,
   `/api/storage/*` и `/api/library/*`, но работает со своими playlist/player API.
4. Owner promotion/demotion отзывает прежнюю web session, self/bootstrap/last
   owner protections возвращают `409`.
5. Disabled identity повторно получает `403`; cross-user ID остаются `404`.
6. В Caddy/backend/frontend logs отсутствуют email, OAuth code/state, ID/access
   token, session cookie и CSRF values; GitHub и deployed SHA совпадают.

После появления второго identity schema downgrade больше не применяется:
rollback выполняется только через остановку writes, предыдущий release SHA и
проверенный pre-migration PostgreSQL dump. Acceptance-user после проверки
оставлен `disabled`, его sessions отозваны.
