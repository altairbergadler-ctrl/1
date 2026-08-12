# Google Sign-In и многопользовательская изоляция

## Назначение и граница доверия

Music Service работает по правилу **shared bytes, private rights**. `Artist`,
`Album`, `Track`, `File`, SHA-1 и Google Drive locations остаются общим
дедуплицированным каталогом. Право увидеть плейлист, source, job или получить
файл определяется отдельно для текущего пользователя.

Регистрация публичная: любой Google-аккаунт с `email_verified=true` при первом
успешном входе создаёт активного пользователя с ролью `user`. После регистрации
пользователь определяется по неизменяемому Google `sub`; смена email у уже
привязанного Google-аккаунта не создаёт второго пользователя. Email остаётся
уникальным ключом только при первичной регистрации и при recovery-привязке
bootstrap owner.

## Два независимых Google OAuth-контура

| Контур | OAuth client | Callback | Scopes | Что сохраняется |
|---|---|---|---|---|
| Google Sign-In | отдельный Web client `Audiofeel Login` | `https://audiofeel.su/api/auth/google/callback` | `openid email profile` | только проверенный `sub`, профиль и собственная серверная сессия |
| Google Drive | отдельный Web client `Audiofeel Drive` | `https://audiofeel.su/api/storage/google/callback` | Drive scopes, включая `drive.file` | зашифрованные infrastructure credentials и Drive account refresh token |

Один Google Cloud project допустим, но client IDs, client secrets и redirect
URI не смешиваются. Drive refresh token, storage account и Drive client secret
никогда не участвуют во входе пользователей. Google access token и ID token
после проверки входа не записываются в БД. Если ID token содержит `at_hash`,
Google access token используется только в памяти для проверки этого claim и
сразу отбрасывается; он не попадает в session, API, cookie или логи.

Официальные ссылки:

- [OpenID Connect](https://developers.google.com/identity/openid-connect/openid-connect);
- [OAuth 2.0 for web server applications](https://developers.google.com/identity/protocols/oauth2/web-server);
- [Validate Google ID tokens](https://developers.google.com/identity/sign-in/web/backend-auth).

## Настройка Google Cloud Console

1. Открыть `Google Auth Platform -> Branding` и настроить приложение проекта.
2. В `Audience` выбрать `External` и перевести **Audiofeel Login** в production.
   Режим `Testing` с `Test users` не считается открытой регистрацией и годится
   только для ограниченной staging-приёмки.
3. В `Clients` создать **новый** client типа `Web application` с именем
   `Audiofeel Login`.
4. В Authorized JavaScript origins добавить только `https://audiofeel.su`.
5. В Authorized redirect URIs добавить точно
   `https://audiofeel.su/api/auth/google/callback`.
6. Не добавлять Drive callback в Login client. Не добавлять Login callback в
   существующий Drive client.
7. Сохранить client ID в `GOOGLE_LOGIN_CLIENT_ID`, а client secret — отдельным
   файлом `GOOGLE_LOGIN_CLIENT_SECRET_HOST_FILE`. Значения не печатать в shell,
   логи, issue, handoff или историю команд.

Production-файл secret создаёт `deploy/vps/bootstrap-runtime.sh`; до настройки
он пуст и имеет права `0400`. После безопасной записи client secret backend
нужно пересоздать, не меняя Drive credentials.

## Проверка Google identity

Login использует Authorization Code Flow с PKCE S256. До перенаправления
backend создаёт криптографически случайные `state`, `nonce`, PKCE verifier и
browser binding. В БД находятся только HMAC-хэши state/binding/nonce и
AES-GCM-зашифрованный verifier. Попытка имеет короткий TTL и потребляется
атомарно до обмена code, поэтому повтор state отклоняется даже после неудачного
token exchange.

Callback проверяет:

- точный discovery issuer `https://accounts.google.com` и allowlist HTTPS-host
  для authorization, token и JWKS endpoints;
- `RS256`, `kid` и подпись по текущему Google JWKS; неизвестный `kid` вызывает
  одно принудительное обновление cache;
- `aud`; при нескольких audiences обязателен совпадающий `azp`, а любой
  присутствующий `azp` должен совпадать с Login client ID;
- `exp`, `iat`, допустимый clock skew и то, что token выдан для текущей login
  attempt, а не значительно раньше неё;
- одноразовые state и browser binding, nonce и PKCE verifier;
- `email_verified=true`, корректные `sub` и email.

Данные профиля из браузера не принимаются. Новый подтверждённый identity
атомарно создаёт `active user`; отключённый identity получает нейтральный `403`
и не может повторно зарегистрироваться. Совпадение email с другим уже
привязанным `sub` отклоняется как identity conflict, а не захватывает аккаунт.

## Схема данных

### Identity и sessions

`users`:

- `id`;
- `email`, `email_key` (уникальный нормализованный ключ);
- `google_sub` (`UNIQUE`, `NULL` до первого входа);
- `display_name`;
- `role`: `owner | user`;
- `state`: `pending | active | disabled`;
- `is_bootstrap_owner` (не более одной строки);
- `created_at`, `activated_at`, `last_login_at`.

### Ролевая модель

| Возможность | `user` | `owner` |
|---|---:|---:|
| Вход, личные sources/playlists, import и matching | да | да |
| Личные Qobuz/Yandex acquisition requests и jobs | да | да |
| Личные download grants и OpenSubsonic player keys | да | да |
| Список пользователей, роли, disable и revoke sessions | нет | да |
| System provider credentials и health/rotation | нет | да |
| Library scan и system jobs | нет | да |
| Google Drive infrastructure accounts/migration | нет | да |

Первый публичный вход всегда выдаёт `user`; назначить `owner` может только
действующий `owner` и только уже активному аккаунту. Изменение роли отзывает web sessions цели, bootstrap owner
не понижается, а последний активный owner не может быть понижен или отключён.
Client-side скрытие навигации — только UX: каждое owner-only API независимо
проверяет роль на backend и возвращает `403`.

`user_sessions`:

- случайный UUID записи и `user_id`;
- `kind`: `google | recovery`;
- HMAC-SHA-256 `token_hash`; raw session token существует только в HttpOnly
  cookie;
- HMAC `csrf_hash`;
- `created_at`, `last_seen_at`, absolute `expires_at`, `revoked_at`.

`google_login_attempts` хранит только хэши state/binding/nonce,
зашифрованный PKCE verifier, expiry и consumption state. Device fingerprint,
IP и User-Agent не сохраняются.

### Ownership

| Сущность | Владение | Правило |
|---|---|---|
| `playlist_sources` | обязательный `user_id` | уникальны по `(user_id, service)` |
| `playlists` | обязательный `user_id` | composite FK гарантирует того же владельца, что у source |
| CSV/M3U/TXT import | создавший пользователь | создаёт его `manual` source и playlist |
| Spotify/Yandex playlist credential | `(user_id, provider)` | отдельная AES-GCM envelope на пользователя |
| OpenSubsonic player credential | обязательный `user_id` | отдельный device key, raw показывается один раз |
| import/matching/Qobuz/Yandex job | `scope=user`, обязательный `user_id` | source/playlist composite FK совпадает с owner |
| health/scan/storage job | `scope=system`, `user_id=NULL` | видит только owner |
| `Artist`, `Album`, `Track`, `File` | общие | не копируются на пользователя |
| Google Drive account/location | infrastructure | управляет только owner |

Один `File.sha1` может быть связан с READY items нескольких пользователей.
Download grant существует только через принадлежащий пользователю READY item:
чужой item/playlist/album возвращает `404`, даже если физический `File` общий.

### Credential split

| Vault | Владение | Назначение |
|---|---|---|
| `user_provider_credentials` | пользователь | Spotify и Yandex playlist import |
| `provider_credentials` | система | Qobuz/Yandex acquisition и health/rotation |
| `storage_secrets` | infrastructure | Google Drive OAuth config и account refresh tokens |
| `player_credentials` | пользователь + устройство | отдельный отзываемый OpenSubsonic API key; хранится только HMAC |

Global Spotify credential после backfill удаляется из system vault и
переносится bootstrap owner. Его исходная encrypted envelope временно хранится
в `multitenancy_migration_credentials` только для lossless schema rollback.
Vault-секреты ни одним API response не возвращаются. Единственное исключение —
raw player API key в ответе на создание устройства; list/revoke/admin API его
никогда не повторяют.

## Авторизация endpoints

| Группа | Доступ | Проверка объекта |
|---|---|---|
| `/api/auth/google/*`, `/api/auth/me`, logout | Google session | session state + CSRF для logout |
| `/api/playlists/*`, `/api/sources`, Spotify callback | owner/user | `user_id=current_user.id`; чужой ID → `404` |
| matching/review и user jobs | owner/user | только собственные playlist/item/job |
| Qobuz/Yandex acquisition status/request | owner/user | request/job/playlist принадлежат current user |
| track/album/playlist ZIP/M3U8 | owner/user | только через собственный READY grant |
| `/api/jobs/{id}` | owner/user | собственные jobs; owner дополнительно видит system jobs |
| `/api/admin/*` | owner | role changes, disable, session revocation |
| `/api/providers/*`, `/api/storage/*`, `/api/library/*` | owner | system/infrastructure only |
| `/rest/*` | отдельный active player credential | browse/search/media только через собственный READY grant; foreign ID = missing ID |

Все state-changing browser endpoints требуют одновременно действительную
server session, exact `Origin`/`Referer`, отсутствие cross-site Fetch Metadata и
session-bound `X-CSRF-Token`. `SameSite=Lax` — дополнительный, а не единственный
контроль.

## Sessions и PWA

После callback Music Service ротирует прежнюю session ID и выдаёт собственную
непрозрачную cookie:

- `HttpOnly`, `Secure` в production, `SameSite=Lax`, `Path=/api`;
- default absolute TTL 30 дней, idle TTL 7 дней, touch не чаще 5 минут;
- logout ставит `revoked_at` до удаления cookie;
- role change/disable/revoke sessions немедленно отзывает web sessions; disable
  пользователя также отзывает все его player credentials;
- после рестарта контейнера отозванная cookie остаётся недействительной.

PWA получает CSRF token через `/api/auth/me` и держит его только в памяти.
Session/CSRF/Google/provider tokens не записываются в `localStorage` или
`sessionStorage`. Owner видит раздел «Пользователи»; роль `user` не видит
system/provider/storage navigation и всё равно защищена серверным `403`.

PWA показывает новый player API key один раз и держит его только в памяти до
ухода со страницы. Service worker полностью обходит `/api/` и `/rest/`, поэтому
URL с `apiKey` не попадает в Cache Storage.

### Миграция 0010

`0010_open_subsonic_players` добавляет device credentials, стабильные публичные
UUID для artist/album/song/playlist и playlist sync metadata. Миграция не создаёт
ключи автоматически, не меняет существующие primary IDs, `File.sha1`, local path
или Drive location. После downgrade player credentials утрачиваются и должны
быть созданы заново; каталог и пользовательские плейлисты сохраняются.

## APP_AUTH_TOKEN recovery

`APP_AUTH_TOKEN` не принимается как Bearer и не создаёт обычную user session.
Он доступен только в отдельном `/#/recovery` и API
`/api/auth/recovery/*` для bootstrap/recovery владельца:

- recovery cookie имеет `Path=/api/auth/recovery`, absolute TTL 15 минут и idle
  TTL 5 минут;
- вход ротирует предыдущую recovery session;
- разрешены только замена recovery email bootstrap owner и отзыв его sessions;
- установка новой owner binding отвязывает прежний `sub`, отзывает все sessions
  владельца и удаляет recovery cookie;
- recovery не создаёт новых пользователей и не даёт общий owner-контекст
  обычным API.

Механизм нельзя удалять до завершённой реальной production-приёмки Google
Sign-In и проверенного recovery runbook.

## Миграция 0007 → 0009

Compose запускает `python -m app.commands.migrate_database`:

1. Alembic применяет `0008_google_user_auth_expand` с nullable ownership;
2. legacy plaintext source credentials, если они ещё есть, шифруются;
3. создаётся ровно один pending bootstrap owner;
4. все существующие sources, playlists, user jobs и незавершённые Drive OAuth
   states привязываются к нему;
5. Spotify/Yandex playlist credentials копируются в user vault; global Spotify
   удаляется, global Yandex остаётся acquisition credential;
6. `0009_google_user_auth_contract` проверяет backfill, удаляет legacy auth/token
   columns и переводит ownership в `NOT NULL` с composite FK/check constraints.

IDs playlist/source/item/match/job не меняются. `File`, SHA-1, локальные пути и
Drive locations не копируются и не перемещаются. Повторный запуск на `0009`
возвращает `already_current`.

До миграции безопасно фиксируются только counts пользователей, sources,
playlists, items, jobs и credential records — без значений секретов и email.

## Backup и проверка восстановления

Перед production migration:

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

Проверка реальным restore выполняется в отдельную БД того же PostgreSQL, не
касаясь production database:

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

Имя backup, SHA-256 и успешный restore фиксируются; содержимое credential
таблиц и пользовательские email не выводятся.

## Rollback

До первого identity binding и при единственном pristine bootstrap owner можно
выполнить `alembic downgrade 0007_manual_playlist_source`: encrypted Spotify
envelope восстанавливается в global vault, все исходные IDs и `File` остаются.

После активации identity, появления второго пользователя или duplicate sources
lossless schema downgrade намеренно отказывается выполняться. Production
rollback тогда состоит из остановки writes, checkout предыдущего SHA и
восстановления проверенного pre-migration PostgreSQL dump. `down -v`, удаление
library/Drive objects и автоматическое удаление user data запрещены.

## Production-приёмка

Итерация считается принятой только после проверки:

1. backup создан, `pg_restore` в отдельную БД успешен;
2. Alembic current=head=`0009_google_user_auth_contract`;
3. owner data/IDs/counts и File SHA-1 сохранились;
4. owner входит реальным Google account;
5. новый ранее неизвестный account проходит first login, создаётся как
   `active user` и получает свой `sub` binding;
6. `user` получает `403` на admin/provider/storage/library API и не может сам
   повысить роль;
7. A/B cross-user playlist/source/job/item/download probes дают `404`;
8. общий физический трек выдаётся обоим через разные READY grants без второго
   `File`;
9. manual import и Spotify credentials изолированы по user;
10. role change, disable, revoke sessions и logout действуют немедленно;
11. replay state, bad nonce/audience/azp/signature, expired token,
    `email_verified=false` и CSRF failures отклоняются;
12. Login и Drive OAuth продолжают работать независимыми clients;
13. backend/frontend/worker/beat/PostgreSQL/Redis/sidecars healthy, Celery pong;
14. полный Docker pytest и live-PWA проходят;
15. GitHub branch SHA и deployed release SHA совпадают.

Production snapshot 2026-08-12 относится к предыдущему invitation-only flow:
его isolation, session и IDOR-инварианты проверены с двумя реальными Google
identity. Двусторонние A/B-пробы вернули
`404` для чужих playlist/item/job/download ID, manual import остался личным,
а один общий `File` выдан обоим через разные READY grants без изменения files
count. Второй Spotify OAuth создал отдельную user credential и не изменил
owner envelope. Disable отозвал живую session немедленно (`/api/auth/me` стал
`401`), а повторный Google login отключённого пользователя вернул нейтральный
`403`. Drive account остался healthy и отдельным от Login OAuth. Exact и
generic leak scans после обоих flows дали 0 для email, credentials, OAuth
query values и session cookies.

Автоматизированный production code gate открытой регистрации выполнен на
`8595779f9977611904ede10b909f2a08c2cb904e`: isolated VPS suite `328 passed`,
backup восстановлен в проверочную БД, GitHub/deployed SHA совпали, сервисы
healthy, Celery вернул `pong`, leak/error counters равны `0`. Ручная часть
приёмки остаётся за владельцем: Audience=`External/In production` и вход ранее
неизвестным реальным Google identity по пунктам 5, 6 и 10 выше.
