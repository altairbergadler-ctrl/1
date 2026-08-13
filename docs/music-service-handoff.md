# Music Service MVP: handoff для новых чатов

Актуально на: 2026-08-13

## 0. Текущая точка продолжения

Этот раздел — единственный краткий источник текущего операционного состояния.
Нижние разделы сохраняют решения и исторические проверки, но их SHA и counts
не следует считать актуальными без повторной проверки production.

- Работать только на сервере через `ssh openclaw-vps`; production-ссылка:
  `/opt/audiofeel/app`.
- Развёрнутый production SHA: `cd403a2f308e86abcdc9d7441a0e9644d42892db`.
- Последний commit, изменяющий код в `codex/qobuz-hardening`:
  `92f65ffb15ebc1b88739f7b3eb0b3eb342d975b4`. Он опережает production
  небольшим исправлением рейтинга качества и поясняющими комментариями.
- Сам handoff публикуется последующим docs-only commit, поэтому точный tip
  ветки всегда получать через `git ls-remote` и сверять с серверным tree.
- Alembic: `0012_web_push_subscriptions (head)`.
- backend, frontend, PostgreSQL, Redis, Qobuz sidecar и Yandex signer healthy;
  worker/beat/egress running; Celery отвечает `pong`.
- Google Drive: один включённый account, live health = `healthy`,
  detail = `drive_ready`.
- Диск: 79 ГБ всего, 42 ГБ свободно, занято 48%; в локальной библиотеке
  музыкальных файлов нет.
- Legacy Qobuz job №25 завершена (`done`). Unified acquisition jobs №28 и №31
  остаются в `pending + paused`; последняя причина — `disk_guard`. Job №28:
  25/58 позиций, 12 скачанных, 10 загруженных в Drive, 12 локально удалённых
  файлов. Job №31: 0/6. Не возобновлять их без прямого указания пользователя,
  даже если свободное место уже восстановилось.
- Последний полный изолированный server test: `345 passed, 5 skipped`.
- Проверенные резервные копии:
  `/var/backups/audiofeel/pre-acquisition-flow-20260812T233056Z.dump` и
  `/var/backups/audiofeel/pre-qobuz-pause-20260812T203423Z.dump`.

Неподвижные правила текущего flow:

1. Пачка означает 25 позиций плейлиста, а не 25 успешных скачиваний.
2. После скачивания пачка обязана пройти import → scan → Drive upload →
   remote verify → local eviction; только затем двигается cursor и допускается
   следующая пачка.
3. Все затронутые импортом плейлисты автоматически попадают в единую
   provider-neutral очередь. Qobuz и Яндекс проверяются до выбора лучшего
   доступного качества; режим «Обновлять качество» разрешает замену READY-файла
   только фактическим улучшением.
4. Очередь глобально выполняет одну acquisition-пачку и чередует пользователей.
5. Browser Push отправляется только после завершения всех пачек и финального
   matching. Google/Firebase app для этого не требуется: используется VAPID.
6. Не выводить `.env`, OAuth-коды, provider tokens, пароли, cookies и Push
   subscription endpoints. Перед миграцией делать и проверять backup; перед
   deployment сверять remote SHA и tree.

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
8. `docs/acquisition-workflow.md`;
9. `docs/vps-deployment.md`.

Никакие секреты в Git не хранятся. Не выводить в логи и чат значения
`.env`, OAuth codes, provider tokens, passwords и session cookies.

После каждого завершённого этапа нужно обновить раздел 0 тем же commit, что и
код/документацию, либо отдельным handoff-commit сразу после него. Обязательно
разделять GitHub SHA и реально развёрнутый SHA, фиксировать migration, проверки,
backup и безопасно приостановленные jobs. Полную историю чата в новый чат не
переносить: достаточно короткого prompt из раздела 8.

## 2. Что реализовано

- Docker Compose: PostgreSQL, Redis, FastAPI, Celery, Nginx/PWA.
- Alembic: `0012_web_push_subscriptions (head)`; Compose выполняет
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
- Google Sign-In с PKCE/state/nonce/JWKS validation, публичной регистрацией
  подтверждённого identity как `user` и стабильным `google_sub`.
- Hashed server sessions в HttpOnly/Secure/SameSite cookie, idle/absolute TTL,
  server-side logout/revocation и полноценный CSRF.
- Обязательный `user_id` у sources/playlists/user jobs, cross-user `404` и
  download grant только через собственный READY item; `File`/SHA-1/Drive
  locations остаются общими.
- User-scoped Spotify/Yandex playlist vault и отдельные system Qobuz/Yandex +
  infrastructure Drive vaults.
- PWA с Google login/logout и owner-разделом role/disable/revoke;
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

### 3.1 Исторический Google Sign-In / multi-user gate 2026-08-12

- полный Docker pytest с production live-PWA: `293 passed`;
- live-PWA contract на собранном Nginx image: `9 passed`;
- отдельные OIDC/JWKS/state/PKCE/identity/session/CSRF/IDOR тесты: проходят;
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

Этот gate закрыл прежний invitation-only flow. После проверки второй
acceptance-user намеренно оставлен в состоянии `disabled`, его sessions
отозваны; owner остаётся `active`. Acceptance-артефакты добавили один manual
source, playlist, item и job, но не изменили число `File`, SHA-1 или Drive
locations.

### 3.2 Открытая регистрация и RBAC — исторический gate

- новый подтверждённый Google identity атомарно создаётся как `active user`;
- disabled identity не может зарегистрироваться повторно, а email другого
  стабильного `sub` не может быть захвачен;
- административное создание invitations удалено;
- owner может назначать `owner | user`; изменение роли отзывает web sessions;
- bootstrap owner, текущий owner и последний активный owner защищены от
  небезопасного понижения;
- роль `user` получает только собственные sources/playlists/jobs/matching,
  download grants и player credentials; admin/providers/storage/library scan
  остаются owner-only с серверным `403`;
- схема БД и Alembic не меняются: `UserRole(owner|user)` уже существовал.

Production code gate выполнен на VPS из отдельного релизного каталога:

- полный backend/PWA suite: `328 passed`;
- PostgreSQL custom dump прошёл `pg_restore --list` и пробное восстановление;
- GitHub branch и deployed health совпали на полном SHA `8595779f...904e`;
- backend/frontend/PostgreSQL/Redis/sidecars healthy, worker/beat running,
  Celery вернул `pong`, Alembic current=heads=`0010_open_subsonic_players`;
- live PWA содержит role selector и cache `v15`, invitation form отсутствует;
- после deployment runtime error hits и query-secret pattern hits равны `0`.

Остаётся ручной identity gate: подтвердить в Google Console для **Audiofeel
Login** Audience=`External/In production`, затем войти ранее неизвестным реальным
Google-аккаунтом и проверить default role=`user`, owner-only `403`, смену роли с
session revoke, cross-user `404` и disabled relogin `403`.

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
  `docs/qobuz-dl-assessment.md`), `QOBUZ_MIN_FREE_BYTES` для
  автоматической паузы по запасу диска;
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
- fetch-missing дренирует каждую пачку отдельно: import → scan → durable
  replication → verified local eviction → matching. Следующая пачка не
  начинается, пока этот цикл не завершён;
- `POST /api/qobuz/downloads/{job_id}/pause` ставит cooperative pause на
  безопасной межпакетной границе, `resume` продолжает ту же job по
  ledger; `QOBUZ_MIN_FREE_BYTES` использует тот же механизм как disk guard;
- Alembic `0011_qobuz_pause_control` добавляет `pause_requested_at` и
  `paused_at`; PWA показывает «Пауза после пачки» и «Продолжить»;
- `provider_attempts` (`0004_provider_attempts`) хранит терминальный результат
  по `provider + fingerprint`: Qobuz не перепроверяет уже обработанное, а один
  запуск проходит новые MISSING пачками по 25; resume исключает уже
  завершённые fingerprint и берёт только остаток;
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

### 6.5 Персональное радио и умные рекомендации — будущая отдельная итерация

Статус: **отложено**. Эта работа не входит ни в текущий read-only OpenSubsonic
adapter, ни автоматически в Release 2. Начинать её можно только после отдельного
согласования research/design-плана.

Текущая клиентская база уже позволяет использовать в Symfonium:

- `Personal Mix` / `Track Mix`, который учитывает локальную историю
  прослушиваний, оценки и избранное;
- бесконечный `Smart Queue` в artist-based или random режиме;
- локальные режимы `Smart Flow`, не требующие Plex Sonic Analysis.

Это не полный аналог «Моей волны» Яндекс.Музыки. Текущий adapter возвращает
пустые жанры и серверное избранное, не предоставляет граф похожих исполнителей,
а поэтому `Radio Mix` не получает полноценный источник рекомендаций и может
откатываться к ограниченному `Instant Mix`. Режимы звукового сходства
`Transition Maestro`, `Echo Match` и `Steady Vibes` в Symfonium зависят от Plex
Sonic Analysis; их нельзя обещать для нашего OpenSubsonic provider без отдельной
клиентской совместимости.

Предлагаемая последовательность будущей итерации:

1. **R0 — client-only baseline.** На реальной пользовательской библиотеке
   зафиксировать качество `Personal Mix`, `Smart Queue -> Artist-based` и
   доступных `Smart Flow` без изменений backend. Сохранить обезличенные метрики
   повторов исполнителя/альбома и ручную оценку пользователя.
2. **R1 — жанровые metadata.** Спроектировать нормализованное хранение жанров с
   provenance, заполнение из аудиотегов и разрешённых provider metadata,
   дедупликацию названий и user-scoped выдачу только для видимых `READY`-треков.
   Затем заполнить OpenSubsonic genre-поля и методы, реально запрашиваемые
   Symfonium.
3. **R2 — похожие исполнители и Radio Mix.** Сначала снять debug-log вызовов
   актуальной версии Symfonium и подтвердить точный OpenSubsonic/Subsonic API
   contract. После этого выбрать легальный источник или локальный алгоритм
   related-artists и ограничить граф исполнителями, для которых у пользователя
   есть доступные `READY`-треки.
4. **R3 — персональные сигналы.** Отдельно спроектировать user-scoped историю
   play/skip, favorite и rating. Любые новые write-endpoint'ы требуют отдельного
   security review, CSRF/auth boundary и запрета утечки предпочтений между
   пользователями; текущий playlist adapter при этом остаётся read-only.
5. **R4 — опциональное звуковое сходство.** Исследовать локальный анализ аудио
   и embeddings без зависимости от Plex. До подтверждения поддержки со стороны
   Symfonium результат отдавать только как серверное радио или динамический
   read-only playlist, а не заявлять совместимость с Plex-only `Smart Flow`.

Минимальная приёмка будущей версии:

- запуск от трека и от исполнителя создаёт релевантную непрерывную очередь, а не
  простой случайный shuffle;
- жанровый и related-artist режимы подтверждены по debug-log без скрытого
  fallback на пустой `Instant Mix`;
- частые повторы исполнителя/альбома ограничены и измеряются на библиотеке
  достаточного размера;
- play/skip/favorite меняют последующий персональный результат только внутри
  того же аккаунта;
- чужие треки, история и предпочтения не появляются ни в каталоге, ни в
  рекомендациях, ни в artwork/stream/download;
- добавление и удаление `READY`-треков корректно отражается в следующей очереди,
  импортированном read-only playlist и его automatic offline cache;
- сбой внешнего metadata-источника не ломает обычный каталог и воспроизведение:
  остаются безопасные artist-based/random режимы.

## 7. Ключевая история Git

### 7.1 OpenSubsonic iteration — опубликовано и развёрнуто

- additive `/rest/*` adapter и PWA-раздел «Плееры»;
- per-device API keys с one-time display, HMAC storage, revoke и user-disable cascade;
- stable public UUIDs и playlist sync revision на catalog/matching/storage mutations;
- user-scoped browse/search/playlists/artwork/stream/download, `.view`, XML/JSON,
  empty `search3`, `getStarred2`, `getBookmarks`, `getGenres`, Range/HEAD и
  original bytes без transcoding;
- Alembic head `0010_open_subsonic_players`, проверенный upgrade → downgrade 0009
  → upgrade на изолированном PostgreSQL;
- Caddy `/rest/*` direct proxy и отключённый site access log, поскольку API key
  по протоколу находится в query string;
- review defects исправлены: discovery JSON соответствует OpenSubsonic, `/rest/`
  исключён из browser cache, metadata/delivery используют единый playable source,
  public IDs переживают retag, revision выполняется один раз на transaction;
- последняя проверка: полный backend suite `317 passed, 5 skipped`, PostgreSQL
  migration + idempotent rerun, frontend image/nginx, Compose и Caddy validation прошли;
- production code gate `26f1bd5`: custom dump прошёл `pg_restore --list` и
  реальное восстановление, migration `0009 → 0010` и idempotent rerun прошли;
- после migration автоматически создано `0` player credentials, контрольные
  counts/digests для 143 `File` и 143 Drive location совпали до и после;
- live production: backend/frontend healthy, Celery `pong`, публичный HTTPS и
  OpenSubsonic discovery прошли, live-PWA `12 passed`, query-secret log hits `0`;
- real-phone Symfonium acceptance начат: настоящий player credential создан,
  provider добавлен, а первая sync остановилась на отсутствовавших пустых
  `getStarred2`/`getBookmarks`/`getGenres`; compatibility gate `5d3b6b2`
  проверен и развёрнут. Следующая sync завершилась с `0` треков из-за буквального
  `search3 query=""`; gate `a6b885b` нормализует этот wildcard и в production
  возвращает 109 исполнителей, 118 альбомов и 121 песню. Следующая sync дошла до
  49-го альбома и остановилась на `year: null`; gate `041b02a` опускает все
  неизвестные необязательные album/song metadata, а production-каталог проверен:
  109/118/121 объектов и `0` значений `null`.
- после успешной catalog sync подтверждено воспроизведение реального трека;
  Drive-only файлы не давали обложки, поскольку artwork resolver принимал только
  local path. Gate `cec30ac` читает embedded artwork из bounded 5 МиБ Drive Range,
  живой production test вернул JPEG для 3 из 3 альбомов и создал bounded cache.

### 7.2 Spotify OAuth/import gate 2026-08-12

- OAuth scopes сокращены до `playlist-read-private` и
  `playlist-read-collaborative`; `user-library-read` не запрашивается;
- callback проверяет доступ к `current_user_playlists` до сохранения token,
  возвращает результат в PWA и не заменяет прежний credential при `403`;
- PWA после подключения умеет импортировать все доступные плейлисты аккаунта,
  показывает отдельный счётчик `restricted` и сохраняет импорт по одной ссылке;
- full import автоматически запускает matching для созданных или обновлённых
  playlists; чистый access restriction не отправляется на бессмысленные retry;
- provider `403` не включает внешний playlist ID в user-facing diagnostics;
- README, логика и руководство импорта синхронизированы с Development Mode и
  Spotify Web API 2026: до пяти allowlisted users, items только для
  owned/collaborative playlists;
- изолированная VPS-приёмка: targeted `45 passed, 5 skipped`, полный suite с
  live Nginx/PWA `333 passed`; JavaScript/Python syntax и `git diff --check`
  прошли. Схема БД и Alembic не менялись.

Следующая ручная приёмка после deploy: добавить тестовый Spotify-аккаунт в
Dashboard → Users Management, переподключить его в PWA и нажать
«Импортировать мои плейлисты». После проверки `restricted` можно вернуться к
offline-сценарию из `docs/player-sync-symfonium.md` без перевыпуска player key.

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

### 7.3 Provider-neutral acquisition queue и final-only Push

В ветке `codex/qobuz-hardening` реализован новый единый flow:

- любой импорт автоматически ставит затронутые плейлисты в очередь;
- Qobuz и Яндекс проверяются последовательно, сохраняется лучший кандидат;
- обычный режим работает с `MISSING`, «Обновлять качество» включает `READY`
  и скачивает только фактическое улучшение;
- пачка равна 25 позициям плейлиста;
- Drive upload, remote verify и local eviction выполняются в межпакетном окне;
- dispatcher выполняет одну глобальную пачку и справедливо чередует
  пользователей;
- pause/resume сохраняют курсор и provider attempts;
- PWA показывает позиции, файлы, Drive, этап и свободный диск;
- Web Push отправляется только после всех пачек и финального matching.

Миграция `0012_web_push_subscriptions` добавляет `jobs.next_run_at`,
зашифрованные browser subscriptions и partial unique active-job constraint.
Google/Firebase app для Push не требуется: используется VAPID. Сервер может
создать и подключить key без владельца, но разрешение уведомлений и сама
browser subscription требуют пользовательского жеста; на iOS PWA должна быть
добавлена на Home Screen.

Контракт, настройки, отказоустойчивость и production gate описаны в
`docs/acquisition-workflow.md`. Legacy provider endpoints сохранены для
совместимости, но основной пользовательский путь — единая автоматическая
очередь.


## 8. Короткий prompt для нового чата

В новый чат переносится только следующий блок. Актуальные детали нужно взять
из раздела 0 и подтвердить на живом сервере.

> Продолжи Music Service. Работай только на сервере через
> `ssh openclaw-vps`; локальный Windows и локальный Docker не используй.
> Сначала полностью прочитай `docs/music-service-handoff.md`, затем README и
> связанные с текущей задачей документы. Проверь live production, GitHub и
> deployed SHA, Alembic, сервисы, Celery, Drive, диск и paused jobs. Ничего не
> возобновляй автоматически и не выводи секреты или содержимое `.env`. Начни
> с короткого отчёта о расхождениях и выполняй только явно согласованный scope.
