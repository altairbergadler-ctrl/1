# Music Service MVP: handoff для новых чатов

Актуально на: 2026-08-12
Статус: Google Sign-In/multi-user реализованы, развёрнуты и полностью приняты
в production с двумя реальными Google identity. Кодовый production gate пройден
на `aca44956ac111424f60cab5f21e11959e6fa57f9`; этот handoff фиксирует результаты
финальной двухаккаунтной проверки.
Предыдущая production-опора до этой миграции:
`22ea6ee592d9e8f314129fadf79ce276ed57f603`.

## 1. Назначение документа

Этот файл — точка входа для нового Codex/разработчика. Перед работой
нужно также прочитать:

1. `README.md`;
2. `docs/music-service-logic.md`;
3. `docs/music-service-mvp-plan.md`;
4. `docs/google-user-auth.md`;
5. `docs/playlist-import.md`;
6. `docs/google-drive-storage.md`;
7. `docs/provider-health-credential-rotation.md`;
8. `docs/vps-deployment.md`.

Никакие секреты в Git не хранятся. Не выводить в логи и чат значения
`.env`, OAuth codes, provider tokens, passwords и session cookies.

## 2. Что реализовано

- Docker Compose: PostgreSQL, Redis, FastAPI, Celery, Nginx/PWA.
- Alembic: `0009_google_user_auth_contract (head)`; Compose выполняет
  expand/backfill/contract через `app.commands.migrate_database`.
- Read-only монтирование реальной библиотеки через
  `MUSIC_LIBRARY_HOST_PATH`; локально используется `X:/Music`.
- Сканер lossless-файлов с тегами, SHA-1, идемпотентностью,
  обработкой move/retag и удалением устаревших записей каталога.
- MusicBrainz enrichment с Redis cache и rate limit.
- Реальные Spotify OAuth/import и Yandex Music token/import.
- Spotify OAuth-вход с возвратом в PWA, импорт одного доступного плейлиста по
  ссылке и независимый CSV/M3U/текстовый конвертер без provider OAuth.
- Яндекс «Мне нравится» импортируется как стабильный плейлист.
- Каскад matching: ISRC, exact, fuzzy, manual review, MISSING.
- Bit-perfect delivery: HTTP Range, одиночный файл, ZIP_STORED, M3U8.
- Invitation-only Google Sign-In с PKCE/state/nonce/JWKS validation и
  стабильным `google_sub`; публичной регистрации нет.
- Hashed server sessions в HttpOnly/Secure/SameSite cookie, idle/absolute TTL,
  server-side logout/revocation и полноценный CSRF.
- Обязательный `user_id` у sources/playlists/user jobs, cross-user `404` и
  download grant только через собственный READY item; `File`/SHA-1/Drive
  locations остаются общими.
- User-scoped Spotify/Yandex playlist vault и отдельные system Qobuz/Yandex +
  infrastructure Drive vaults.
- PWA с Google login/logout и owner-разделом invitations/disable/revoke;
  session/CSRF tokens не сохраняются в Web Storage.
- `APP_AUTH_TOKEN` изолирован в `/#/recovery` и не принимается обычным API.
- Приватный внешний доступ через Tailscale Serve HTTPS. Funnel не включён.
- Provider Health & Credential Rotation: раздельные account/API/sidecar/worker
  states, encrypted vault, проверка до активации, hot rotation и Celery beat.

## 3. Итоговая приёмка MVP

Финальный прогон 2026-08-09:

| Проверка | Результат |
|---|---|
| `docker compose build` | Успешно для backend/worker/migrate/frontend |
| Docker services | backend/frontend/worker running; PostgreSQL/Redis healthy |
| Celery | worker ping: `pong` |
| Alembic | current = heads = `0005_provider_health_credentials` |
| Music mount | backend и worker: `X:/Music` → `/music/library`, `RW=false` |
| Pytest | `138 passed` с live-PWA, без skip |
| HTTPS/auth | health/login `200`, cookie `Secure; HttpOnly` |
| Yandex import | 6 коллекций без изменений и ошибок |
| Spotify import | 22 обнаружено; 4 доступных без изменений; 18 недоступны из-за Development Mode |
| Real library scan | 2 discovered, 2 unchanged, 0 failed, 0 removed |
| Library snapshot | 2 files, 2 tracks, 2 albums, 55,063,951 bytes |
| Yandex liked playlist | 72 tracks |
| Matching | 1 READY, 71 MISSING, 0 NEEDS_REVIEW |
| Acceptance track | `Slayyyter — DANCE...`: exact, confidence `0.9799`, READY |
| Range | `206`, bytes `0-1023/36684928` |
| M3U8 | `200`, acceptance track present |
| Playlist ZIP | `200`, 2 entries, all ZIP_STORED, FLAC SHA-1 equals source |
| Physical smartphone | PWA/login/download over Tailscale HTTPS and mobile network confirmed by user |

Отдельная приёмка playlist-link/converter 2026-08-11: `249 passed, 5 skipped`,
JavaScript/Python syntax clean, Alembic PostgreSQL current = heads =
`0007_manual_playlist_source`. Подробности: `docs/playlist-import.md`.

Отдельная приёмка Provider Health 2026-08-10 описана в
`docs/provider-health-credential-rotation.md`: backend `212 passed, 5 skipped`,
Qobuz sidecar `9 tests`, Alembic `0005`, Celery `pong`, live Yandex health и
hot rotation без restart.

### 3.1 Google Sign-In / multi-user gate 2026-08-12

- полный Docker pytest с production live-PWA: `293 passed`;
- live-PWA contract на собранном Nginx image: `9 passed`;
- отдельные OIDC/JWKS/state/PKCE/invitation/session/CSRF/IDOR тесты: проходят;
- JavaScript syntax и PWA static contract: проходят;
- реальный PostgreSQL migration test: `0007 → 0008 → backfill → 0009`,
  повторный запуск `already_current`, downgrade `0009 → 0007`;
- при migration/downgrade сохранены исходные source/playlist/item/job IDs,
  `File.sha1` и Drive location; pristine bootstrap user удалён при rollback;
- global Spotify envelope восстановлена после rollback, а на head находится
  только в user vault и migration backup.

Фактическая production-проверка 2026-08-12:

- pre-migration PostgreSQL custom dump создан, `pg_restore --list` прошёл;
  dump восстановлен в отдельную БД, revision и все контрольные counts совпали,
  проверочная БД удалена;
- production мигрирован `0007 -> 0009`: сохранены 2 sources, 4 playlists,
  105 items, 14 jobs, 16 `File` и 16 Drive locations; 7 user jobs получили
  owner, 7 system jobs остались без user; storage account остался один;
- отдельный `Audiofeel Login` client работает с callback
  `/api/auth/google/callback`; существующий Drive client и storage account не
  изменены, Google consent не содержит Drive scopes;
- реальный bootstrap owner прошёл invitation binding и повторный вход по тому
  же `google_sub`; повторные входы не создают дубликаты;
- до invitation второй test-user получил нейтральный HTTP `403`: users count
  остался 1, pending users и live sessions не появились;
- после owner invitation pending-запись имела `google_sub IS NULL`; первый
  успешный вход активировал её и связал второй уникальный `google_sub`;
- роль `user` не получила owner-навигацию; owner-only admin/provider/storage
  endpoints вернули `403`;
- A/B-пробы playlist/items/jobs/download в обе стороны: свои объекты отвечают
  `200`, существующие чужие ID — `404`; списки sources содержат только свои
  записи;
- manual-import второго пользователя создал его собственные source, playlist,
  item и matching job; READY grant у обоих пользователей указывает на тот же
  `Track`/`File`, Range-download обоим вернул `206`, files count остался 16;
- второй Spotify OAuth создал отдельную `(user_id, spotify)` credential; число
  Spotify owners стало 2, а encrypted owner-запись не изменилась;
- disable второго пользователя отозвал все его sessions; уже выданная cookie
  сразу получила `401`, повторный Google login — нейтральный `403` без email;
- Google `at_hash` проверяется с access token только в памяти; access/ID token
  не сохраняются и не возвращаются;
- logout и owner recovery revoke немедленно оставили 0 live sessions;
  session и CSRF hashes в PostgreSQL имеют фиксированную длину 32 bytes;
- exact-value scan Caddy/backend/frontend logs после обоих OAuth flows: 0
  совпадений с email, OAuth code/state, Login client credentials; generic email,
  unredacted OAuth query и session-cookie matches также равны 0;
- backend/frontend/PostgreSQL/Redis/sidecars healthy, worker/beat running,
  Celery `pong`, Alembic `0009`, production live-PWA `9 passed`.

Google Sign-In/multi-user gate закрыт полностью. После проверки второй
acceptance-user намеренно оставлен в состоянии `disabled`, его sessions
отозваны; owner остаётся `active`. Acceptance-артефакты добавили один manual
source, playlist, item и job, но не изменили число `File`, SHA-1 или Drive
locations.

Команда полного локального теста с live-PWA:

```powershell
docker compose up -d --build frontend
docker compose run --rm `
  -v "${PWD}/frontend:/frontend:ro" `
  -e DATABASE_URL=sqlite+pysqlite:///:memory: `
  -e REDIS_URL=redis://redis:6379/15 `
  -e MUSIC_LIBRARY_PATH=/tmp/music `
  -e APP_AUTH_TOKEN=test-auth-token-for-pytest `
  -e AUTH_COOKIE_SECURE=false `
  -e MUSICBRAINZ_ENABLED=false `
  -e CELERY_TASK_ALWAYS_EAGER=true `
  -e PWA_ACCEPTANCE_BASE_URL=http://frontend `
  backend pytest -q
```

## 4. Известные ограничения, не дефекты MVP

1. Spotify Development Mode не отдаёт 18 чужих/недоступных
   плейлистов (`403`). Доступные owned/collaborative плейлисты
   импортируются; job завершается с `result_status=partial`.
   Для независимого от Spotify импорта доступен CSV/M3U/текстовый конвертер;
   он не умеет получать треки непосредственно из одной публичной ссылки.
2. В реальных данных не возник естественный `NEEDS_REVIEW`; ручной
   review/resolve покрыт API и Docker-тестами.
3. Tailscale и его Serve-конфигурация установлены на Windows-хосте,
   а не хранятся в репозитории. Точный HTTPS URL получается из
   `tailscale status`; не добавлять его в публичную документацию.
4. Qobuz-интеграция из соседней ветки прошла отдельную hardening-итерацию на
   условиях **RESTRICT** из `docs/qobuz-dl-assessment.md`. `qobuz-dl` и provider
   token находятся только в изолированном sidecar без БД/Redis/локальной
   библиотеки; его HTTPS идёт через точный allowlist proxy. Парольный fallback
   удалён. Worker получает только относительные staging paths, повторно
   проверяет audio/containment и лишь затем переносит файл в библиотеку.
   Автоматически выбираются только однозначные записи; ambiguity и несовпавшие
   version markers не скачиваются. Tier 27 запрашивает максимум и понижается до
   лучшего реально доступного качества. Воспроизводимая проверка и завершённый
   live provider gate зафиксированы в `docs/qobuz-hardening-acceptance.md`.
5. Acquisition через Яндекс.Музыку реализован с политикой best available:
   FLAC предпочтителен, FLAC-in-MP4 перепаковывается без перекодирования, а при
   отсутствии FLAC сохраняется исходный AAC/HE-AAC или MP3. Track id,
   transport, codec, HTTPS-host, контейнер и длительность проверяются до
   импорта. Scanner поддерживает `.m4a`, `.aac`, `.mp3`; PWA показывает
   фактический codec/bitrate. Подробности —
   `docs/yandex-music-api-assessment.md` и
   `docs/yandex-acquisition-acceptance.md`.

## 5. Операционный минимум

```powershell
docker compose up -d
docker compose ps
docker compose exec -T backend alembic current
docker compose exec -T worker celery -A app.workers.celery_app.celery inspect ping --timeout 5
```

После изменения файлов в `X:\Music`:

1. `POST /api/library/scan`;
2. дождаться job через `GET /api/jobs/{id}`;
3. `POST /api/matching/run` для нужного плейлиста;
4. проверить READY/REVIEW/MISSING и delivery.

`.env` должен оставаться ignored. Важные ключи без значений:

- `APP_AUTH_TOKEN`;
- `PUBLIC_ORIGIN`, `AUTH_KEY_HOST_FILE`, `AUTH_COOKIE_SECURE` и session TTL;
- `GOOGLE_LOGIN_CLIENT_ID`, `GOOGLE_LOGIN_CLIENT_SECRET_HOST_FILE`,
  `GOOGLE_LOGIN_REDIRECT_URI`;
- `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REDIRECT_URI`;
- `PROVIDER_CREDENTIAL_KEY_HOST_FILE`, `QOBUZ_CREDENTIAL_KEY_HOST_FILE`;
- `YANDEX_DOWNLOAD_ENABLED`, `YANDEX_SIGNER_URL`, `YANDEX_INTERNAL_TOKEN`,
  `YANDEX_STAGING_PATH`,
  `YANDEX_MAX_TRACKS_PER_RUN`, `YANDEX_REQUEST_DELAY_SECONDS`,
  `YANDEX_BATCH_DELAY_SECONDS` (опционально, см. раздел
  «Яндекс.Музыка: докачка» в README);
- `QOBUZ_ENABLED`, `QOBUZ_INTERNAL_TOKEN` (опционально, см. раздел «Qobuz» в README и
  `docs/qobuz-dl-assessment.md`);
- `MUSIC_LIBRARY_HOST_PATH`, `QOBUZ_STAGING_HOST_PATH`;
- PostgreSQL/Redis settings.

## 6. Следующие итерации

Ни одна из них не входит в `v0.1.0`.

### 6.1 OpenSubsonic compatibility research

Отдельная research/design-итерация, не Release 2. Результат —
`docs/open-subsonic-integration-plan.md`, матрица Symfonium/Ultrasonic/
Amperfy/desktop и выбранный минимальный API-профиль. Код не менять
до согласования плана.

### 6.2 `qobuz-dl` assessment

Выполнен и усилен после review соседней ветки. Audit —
`docs/qobuz-dl-assessment.md`, решение **RESTRICT**. Реализация (2026-08-09):

- `qobuz_sidecar/` — отдельный non-root образ с hash-locked
  `qobuz-dl==0.9.9.10`, token-only auth, обязательными timeout/size limit и
  CONNECT-proxy с точным Qobuz allowlist;
- `backend/app/services/qobuz.py` — приватный sidecar-клиент, каскад
  ISRC → exact → strict fuzzy, защита version markers/ambiguity, повторная
  Mutagen/containment-проверка и перенос без перезаписи;
- `backend/app/api/qobuz.py` — `status`/`connect`/`search`/`download-url`/
  `fetch-missing`/`download-status/{playlist_id}`; один активный qobuz-job,
  только точный повтор идемпотентен, конфликтующий запрос получает 409;
- Qobuz включается серверной конфигурацией на всё время работы стека; PWA
  показывает `включён постоянно` и не предлагает фиктивное ручное подключение;
- `qobuz_download_task` в `backend/app/workers/tasks.py` — pipeline
  staging → verify → library → scan → matching с lease/heartbeat/retry-паттерном
  остальных тасок; этапы и per-track статусы сохраняются в `jobs.payload` и
  восстанавливаются PWA после reload; ошибки конфигурации/авторизации — без retry;
- `provider_attempts` (`0004_provider_attempts`) хранит терминальный результат
  по `provider + fingerprint`: Qobuz не перепроверяет уже обработанное, а один
  запуск проходит все новые MISSING пачками по 25 с паузой между пачками;
- `docker-compose.yml`: staging видят sidecar и worker, `/music/library` rw
  только у worker, backend остаётся read-only и provider token не получает;
- тесты `backend/tests/test_qobuz.py` и `qobuz_sidecar/test_app.py`, включая
  end-to-end fetch-missing на сгенерированном FLAC и сетевую изоляцию.

### 6.3 Яндекс.Музыка как второй provider

Реализована 2026-08-10 как отдельная итерация, не Release 2. Итог:
best-available acquisition **принят с ограничениями**:

- audit upstream и security boundary — `docs/yandex-music-api-assessment.md`;
- Docker/live-приёмка — `docs/yandex-acquisition-acceptance.md`;
- стабильный `yandex-music==3.0.*` остаётся для импорта/поиска;
- подписанный lossless file-info изолирован в non-root `yandex-signer` без
  OAuth token, egress и опубликованного порта;
- worker предпочитает native FLAC/FLAC-in-MP4; MP4 перепаковывается stream
  copy в `.flac`, а при отсутствии FLAC исходный AAC/HE-AAC или MP3 сохраняется
  без транскодирования;
- независимый provider ledger, полный проход плейлиста пачками по 25 и
  per-track job progress сохранены;
- repository default выключен; локальный тестовый стек включён и прошёл
  финальный Docker/PWA-прогон `203 passed` плюс staging-only live FLAC и AAC
  acceptance.

### 6.4 Release 2

Начинать только после отдельного явного указания. Scope зафиксирован в
`docs/music-service-mvp-plan.md`: tracker scraper, qBittorrent automation, scheduled rematching,
Telegram notifications, dedup/upgrade policy и dashboard.

## 7. Ключевая история Git

### OpenSubsonic iteration — локально реализовано, не опубликовано

- additive `/rest/*` adapter и PWA-раздел «Плееры»;
- per-device API keys с one-time display, HMAC storage, revoke и user-disable cascade;
- stable public UUIDs и playlist sync revision на catalog/matching/storage mutations;
- user-scoped browse/search/playlists/artwork/stream/download, `.view`, XML/JSON,
  empty `search3`, Range/HEAD и original bytes без transcoding;
- Alembic head `0010_open_subsonic_players`, проверенный upgrade → downgrade 0009
  → upgrade на изолированном PostgreSQL;
- Caddy `/rest/*` direct proxy и отключённый site access log, поскольку API key
  по протоколу находится в query string;
- review defects исправлены: discovery JSON соответствует OpenSubsonic, `/rest/`
  исключён из browser cache, metadata/delivery используют единый playable source,
  public IDs переживают retag, revision выполняется один раз на transaction;
- локальная проверка: полный backend suite `315 passed, 5 skipped`, PostgreSQL
  migration + idempotent rerun, frontend image/nginx, Compose и Caddy validation прошли;
- production остаётся на `0408391`; deployment, миграция production и real-phone
  Symfonium acceptance не выполнялись.

Перед продолжением прочитать `docs/open-subsonic-integration-plan.md` и
`docs/player-sync-symfonium.md`. Следующий
разрешённый шаг — review/доработка локальной реализации. Публикация и production
по-прежнему требуют отдельного подтверждения.

- `2ac6e91` — Stage 4: matching, delivery, PWA.
- `15eedb3` — live PWA manifest media type.
- `8da6f75` — live Yandex import compatibility.
- `f4b47eb` — Spotify API compatibility.
- `7b43368` — Yandex liked tracks.
- `e8f11c2` — prune missing library files.
- `19b1a7e` — OpenSubsonic research plan.
- `833407d`, `aad1384` — `qobuz-dl` assessment gate and scope decision.
- `b317757` — merge соседней ветки с исходным Qobuz-прототипом; hardening
  выполняется отдельно в `codex/qobuz-hardening`.

## 8. Короткий prompt для нового чата

> Продолжи работу в репозитории Music Service. Сначала прочитай
> `docs/music-service-handoff.md`, `docs/music-service-mvp-plan.md`,
> `docs/music-service-logic.md`, `docs/google-user-auth.md` и `README.md`;
> проверь `git status`, локальный/remote/deployed SHA, Alembic и Docker.
> Google Sign-In использует invitation-only stable `sub`, а Drive OAuth —
> отдельный client. Не начинай Release 2 без моего
> явного указания. Текущий следующий scope бери только из раздела 6
> handoff-документа и после моего выбора.
