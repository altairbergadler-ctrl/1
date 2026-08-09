# Music Service MVP: handoff для новых чатов

Актуально на: 2026-08-09
Статус: MVP acceptance завершён; Release 2 не начат.
Опорная версия: `v0.1.0` (создаётся после итогового коммита handoff).

## 1. Назначение документа

Этот файл — точка входа для нового Codex/разработчика. Перед работой
нужно также прочитать:

1. `README.md`;
2. `docs/music-service-logic.md`;
3. `docs/music-service-mvp-plan.md`.

Никакие секреты в Git не хранятся. Не выводить в логи и чат значения
`.env`, OAuth codes, provider tokens, passwords и session cookies.

## 2. Что реализовано

- Docker Compose: PostgreSQL, Redis, FastAPI, Celery, Nginx/PWA.
- Alembic: `0003_stage4_matching (head)`.
- Read-only монтирование реальной библиотеки через
  `MUSIC_LIBRARY_HOST_PATH`; локально используется `X:/Music`.
- Сканер lossless-файлов с тегами, SHA-1, идемпотентностью,
  обработкой move/retag и удалением устаревших записей каталога.
- MusicBrainz enrichment с Redis cache и rate limit.
- Реальные Spotify OAuth/import и Yandex Music token/import.
- Яндекс «Мне нравится» импортируется как стабильный плейлист.
- Каскад matching: ISRC, exact, fuzzy, manual review, MISSING.
- Bit-perfect delivery: HTTP Range, одиночный файл, ZIP_STORED, M3U8.
- PWA с HttpOnly-cookie; для Tailscale HTTPS локально включен
  `AUTH_COOKIE_SECURE=true`.
- Приватный внешний доступ через Tailscale Serve HTTPS. Funnel не включён.

## 3. Итоговая приёмка MVP

Финальный прогон 2026-08-09:

| Проверка | Результат |
|---|---|
| `docker compose build` | Успешно для backend/worker/migrate/frontend |
| Docker services | backend/frontend/worker running; PostgreSQL/Redis healthy |
| Celery | worker ping: `pong` |
| Alembic | current = heads = `0003_stage4_matching` |
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

Команда полного локального теста с live-PWA:

```powershell
docker compose run --rm `
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
2. В реальных данных не возник естественный `NEEDS_REVIEW`; ручной
   review/resolve покрыт API и Docker-тестами.
3. Tailscale и его Serve-конфигурация установлены на Windows-хосте,
   а не хранятся в репозитории. Точный HTTPS URL получается из
   `tailscale status`; не добавлять его в публичную документацию.
4. Qobuz интегрирован на условиях **RESTRICT** из
   `docs/qobuz-dl-assessment.md`: секреты только через env, скачивание только в
   staging, перенос в библиотеку после верификации и только из worker, один
   активный qobuz-job, лимиты и задержки. Реальный логин не проверялся: для
   проверки нужно заполнить `QOBUZ_ENABLED=true`, `QOBUZ_EMAIL` и
   `QOBUZ_PASSWORD` и вызвать `POST /api/qobuz/connect`.

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
- `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REDIRECT_URI`;
- `YANDEX_TOKEN`;
- `QOBUZ_ENABLED`, `QOBUZ_EMAIL`, `QOBUZ_PASSWORD` (опционально, см. раздел
  «Qobuz» в README и `docs/qobuz-dl-assessment.md`);
- `MUSIC_LIBRARY_HOST_PATH`, `QOBUZ_STAGING_HOST_PATH`;
- `AUTH_COOKIE_SECURE`;
- PostgreSQL/Redis settings.

## 6. Следующие итерации

Ни одна из них не входит в `v0.1.0`.

### 6.1 OpenSubsonic compatibility research

Отдельная research/design-итерация, не Release 2. Результат —
`docs/open-subsonic-integration-plan.md`, матрица Symfonium/Ultrasonic/
Amperfy/desktop и выбранный минимальный API-профиль. Код не менять
до согласования плана.

### 6.2 `qobuz-dl` assessment

Выполнен. Audit — `docs/qobuz-dl-assessment.md`, решение **RESTRICT**. Интеграция
реализована в рамках этих ограничений (2026-08-09):

- `backend/app/services/qobuz.py` — ленивая обёртка над `qobuz-dl==0.9.9.10`
  (клиент с Redis-кэшем bundle, поиск, fuzzy-выбор кандидата, скачивание в
  staging, верификация mutagen, перенос в библиотеку без перезаписи);
- `backend/app/api/qobuz.py` — `status`/`connect`/`search`/`download-url`/
  `fetch-missing`, активен только один qobuz-job;
- `qobuz_download_task` в `backend/app/workers/tasks.py` — pipeline
  staging → verify → library → scan → matching с lease/heartbeat/retry-паттерном
  остальных тасок; ошибки конфигурации/авторизации — без retry;
- `docker-compose.yml`: staging-mount обоим сервисам, `/music/library` rw
  только у `worker` (backend остаётся read-only);
- тесты `backend/tests/test_qobuz.py` — моки на границе сервиса, end-to-end
  fetch-missing на сгенерированных FLAC; реальный логин Qobuz не проверялся
  (нужны `QOBUZ_EMAIL`/`QOBUZ_PASSWORD`, см. п. 4).

### 6.3 Release 2

Начинать только после отдельного явного указания. Scope зафиксирован в
`docs/music-service-mvp-plan.md`: tracker scraper, qBittorrent automation, scheduled rematching,
Telegram notifications, dedup/upgrade policy и dashboard.

## 7. Ключевая история Git

- `2ac6e91` — Stage 4: matching, delivery, PWA.
- `15eedb3` — live PWA manifest media type.
- `8da6f75` — live Yandex import compatibility.
- `f4b47eb` — Spotify API compatibility.
- `7b43368` — Yandex liked tracks.
- `e8f11c2` — prune missing library files.
- `19b1a7e` — OpenSubsonic research plan.
- `833407d`, `aad1384` — `qobuz-dl` assessment gate and scope decision.

## 8. Короткий prompt для нового чата

> Продолжи работу в репозитории Music Service. Сначала прочитай
> `docs/music-service-handoff.md`, `docs/music-service-mvp-plan.md`,
> `docs/music-service-logic.md` и `README.md`; проверь `git status`, тег `v0.1.0`
> и Docker. MVP acceptance завершён. Не начинай Release 2 без моего
> явного указания. Текущий следующий scope бери только из раздела 6
> handoff-документа и после моего выбора.
