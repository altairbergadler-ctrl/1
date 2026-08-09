# Music Service — MVP (Этап 4: матчинг, выдача и PWA)

Hi-Res музыкальный архив: импорт плейлистов Spotify/Яндекс.Музыки,
матчинг с локальной lossless-библиотекой, bit-perfect выдача на смартфон.

ТЗ: `docs/music-service-logic.md`, `docs/music-service-mvp-plan.md`.
Итоги реальной MVP-приёмки и точка входа для продолжения разработки:
`docs/music-service-handoff.md`.

## Запуск

```bash
cp .env.example .env        # заполнить APP_AUTH_TOKEN и настройки
docker compose up --build
```

Проверка API: `curl http://localhost:8000/api/health` → `{"status":"ok"}`.
PWA открывается на `http://localhost:8080`.

Миграция выполняется отдельным сервисом `migrate` до запуска API и Celery.
Локальная библиотека `data/music/` монтируется в `/music/library`: backend —
только для чтения, worker — с записью (нужно для Qobuz-импорта, см. раздел
«Qobuz»). Staging Qobuz-загрузок `data/qobuz-staging/` монтируется в
`/music/staging` обоим сервисам.

## Миграции

Начальная миграция уже находится в `backend/alembic/versions/`. Для ручного
применения: `docker compose run --rm migrate`.

## API библиотеки

Все запросы, кроме health, требуют заголовок
`Authorization: Bearer <APP_AUTH_TOKEN>`.

PWA выполняет `POST /api/auth/login` с тем же токеном и получает HttpOnly
SameSite-cookie. Bearer-аутентификация для API остаётся доступной. Для HTTPS
установите `AUTH_COOKIE_SECURE=true`.

- `POST /api/library/scan` — создаёт фоновое задание сканирования;
- `GET /api/jobs/{id}` — возвращает статус и результат задания;
- `GET /api/library/stats` — файлы, треки, альбомы, объём и форматы;
- `GET /api/library/albums?q=` — поиск альбомов и исполнителей.

Сканер поддерживает `.flac`, `.alac`, `.wav`, `.dsf`, `.dff`, `.ape`, читает
теги Mutagen и использует fallback
`Artist/Album (Year)/NN - Title.ext`. Дедупликация файлов выполняется по SHA-1.

## Источники и плейлисты

- `GET /api/sources/spotify/connect` — начало Spotify Authorization Code OAuth;
- `GET /api/sources/spotify/callback` — callback с одноразовым Redis state;
- `POST /api/sources/yandex/connect` — проверка и сохранение `YANDEX_TOKEN`;
- `GET /api/sources` — подключённые источники без выдачи токенов;
- `POST /api/playlists/import` — фоновый импорт по `source_id`;
- `GET /api/playlists` — список и сводка READY/MISSING/REVIEW/UNMATCHED;
- `GET /api/playlists/{id}` и `/items?status=` — детали и треки;
- `POST /api/playlists/{id}/refresh` — инкрементальное обновление.

Spotify требует `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` и точного
`SPOTIFY_REDIRECT_URI`, зарегистрированного в приложении Spotify. Для
локального запуска используйте
`http://127.0.0.1:8000/api/sources/spotify/callback`: Spotify не принимает
`localhost` как OAuth redirect URI. В текущем Development Mode содержимое
плейлиста доступно только владельцу и соавторам; подписанные чужие плейлисты
могут быть перечислены, но будут пропущены провайдером с `403`.
Для Яндекс.Музыки используется `YANDEX_TOKEN` из `.env`. Токены провайдеров
никогда не возвращаются API.

Spotify пропускает неизменившиеся плейлисты по `snapshot_id`. Для
Яндекс.Музыки вычисляется детерминированный SHA-256 по версии и упорядоченному
содержимому плейлиста. Сырые и нормализованные значения сохраняются вместе.
Для одного источника одновременно выполняется только один импорт. Совпадающий
запрос возвращает уже активную задачу, а несовместимый refresh/full-import —
`409` с ID задачи, завершения которой нужно дождаться.

После обновления с этапа 2 один раз повторно запустите `/api/library/scan`:
скан идемпотентно переведёт существующий каталог на те же norm-ключи, которые
используются импортерами плейлистов.

## Матчинг

- `POST /api/matching/run` с `{ "playlist_id": 1 }` или
  `{ "playlist_id": null }` — фоновый каскадный матчинг одного плейлиста или
  всей коллекции;
- `GET /api/matching/review` — неоднозначные fuzzy-совпадения и кандидаты;
- `POST /api/matching/{match_id}/resolve` — подтвердить `track_id` либо
  отправить `{ "track_id": null }`, если трека нет в архиве.

Каскад сначала проверяет ISRC, затем точные norm-ключи с допуском ±2 секунды,
после чего fuzzy `token_set_ratio` с порогом 85 и допуском ±5 секунд. Маркеры
`live`, `remix`, `cover` не позволяют студийной версии автоматически заменить
другую редакцию. Неоднозначные и недостаточно уверенные варианты уходят в
review; ручные решения повторный запуск не перезаписывает.

## Bit-perfect выдача

- `GET /api/download/track/{item_id}` — исходный файл с HTTP Range;
- `GET /api/download/album/{album_id}` — потоковый ZIP_STORED;
- `GET /api/download/playlist/{id}` — все READY-треки и `playlist.m3u8`;
- `GET /api/download/playlist/{id}/m3u8` — отдельный UTF-8-плейлист с
  относительными путями.

Файлы не транскодируются и не рекомпрессируются. Перед выдачей путь повторно
проверяется: он должен существовать и оставаться внутри `MUSIC_LIBRARY_PATH`.
Если у трека несколько копий, выбирается лучшая по bit depth, sample rate и
размеру; это не вводит release-2 upgrade-политику.

## PWA

Мобильный интерфейс включает вход, список плейлистов с прогрессом, статусы
READY/REVIEW/MISSING, ручной review и скачивание трека, ZIP или M3U8. Service
Worker кэширует только оболочку приложения; API и музыкальные файлы в кэш не
попадают. Nginx проксирует `/api/` к backend, поэтому cookie и скачивания
работают в одном origin.

## Qobuz

Докачка отсутствующих (MISSING) треков из каталога Qobuz через библиотеку
`qobuz-dl==0.9.9.10`. Интеграция выполнена на условиях **RESTRICT** — обязательно
прочитайте `docs/qobuz-dl-assessment.md` (угрозы, правовой аспект, ограничения
из раздела 7) до включения. Требуется активная платная подписка Qobuz Studio;
риск блокировки аккаунта несёт владелец.

Переменные окружения (`.env`):

- `QOBUZ_ENABLED=true` — включает интеграцию (по умолчанию выключена);
- `QOBUZ_EMAIL`, `QOBUZ_PASSWORD` — учётные данные Qobuz. Живут только в
  `.env`: в БД не сохраняются, API их не возвращает, наружу уходит лишь
  MD5-хеш пароля по HTTPS;
- `QOBUZ_QUALITY` — `5` (MP3), `6` (16/44.1), `7` (24/<96 kHz),
  `27` (24/>96 kHz, по умолчанию) с автоматическим downgrade до доступного;
- `QOBUZ_STAGING_PATH` (в контейнере `/music/staging`) и
  `QOBUZ_STAGING_HOST_PATH` (на хосте `./data/qobuz-staging`);
- `QOBUZ_MAX_TRACKS_PER_RUN` (по умолчанию 25) — лимит треков за один запуск;
- `QOBUZ_REQUEST_DELAY_SECONDS` (по умолчанию 1.0) — пауза между скачиваниями;
- `QOBUZ_DOWNLOAD_JOB_STALE_SECONDS` — stale-таймаут задания (по умолчанию
  21600);
- `QOBUZ_EMBED_ART` — встраивать обложку в теги файла.

Endpoints (все требуют авторизации):

- `GET /api/qobuz/status` — `enabled`/`configured`/лимиты, без секретов;
- `POST /api/qobuz/connect` — проверка логина; при успехе возвращает `label`
  тарифа (`400` — креденшелы отклонены, `502` — Qobuz недоступен, `503` — не
  настроен);
- `GET /api/qobuz/search?q=&type=track|album&limit=` — поиск по каталогу Qobuz;
- `POST /api/qobuz/download-url` с `{ "url": "https://play.qobuz.com/..." }` —
  скачивание по ссылке; в MVP поддерживаются только URL типа `album` и `track`
  (playlist/artist/label отклоняются с понятной ошибкой);
- `POST /api/qobuz/fetch-missing` с `{ "playlist_id": N }` — докачка всех
  MISSING-треков плейлиста (не более `QOBUZ_MAX_TRACKS_PER_RUN` за запуск).

Оба download-endpoint возвращают `202` и задание (`GET /api/jobs/{id}`).
Одновременно активно только одно qobuz-задание: повторный запрос вернёт уже
идущее.

Pipeline задания: `staging → верификация → библиотека → scan → matching`.
Скачанное никогда не пишется в `MUSIC_LIBRARY_PATH` напрямую: файлы сначала
попадают в staging, проверяются (расширение `.flac`/`.mp3`, mutagen парсит и
видит ненулевую длительность, размер > 0), затем worker переносит их в
библиотеку с сохранением структуры папок и containment-проверкой путей.
Существующие файлы не перезаписываются — конфликт остаётся в staging;
обложки/буклеты в библиотеку не переносятся. После переноса задание инлайн
запускает скан библиотеки (если другой скан уже идёт — scan помечается
`deferred`, а matching пропускается) и, для fetch-missing, повторный матчинг
плейлиста. Из-за записи в библиотеку **только** контейнер `worker` получил
read-write mount `/music/library`; backend остаётся read-only
(обоснование — раздел 5 assessment-документа).

Ошибки учётных данных (`QobuzConfigurationError`/`QobuzAuthError`) завершают
задание без retry; прочие ошибки — с retry по тому же паттерну, что и остальные
Celery-задачи. `app_id`/secrets веб-плеера кэшируются в Redis
(`qobuz:bundle:v1`, TTL 7 дней) и перевытягиваются только при промахе или
`InvalidAppSecretError`; при недоступном Redis работа продолжается без кэша.

## MusicBrainz

Обогащение включается через `MUSICBRAINZ_ENABLED=true`. Перед включением
обязательно замените `MUSICBRAINZ_USER_AGENT` на строку с реальным контактом
сопровождающего. Ответы кэшируются в Redis, общий лимит — не более одного
внешнего запроса в секунду.

## Тесты

```bash
docker compose run --rm backend pytest -q
```

Тесты генерируют три коротких FLAC через ffmpeg, проверяют повторный скан,
fallback, SHA-1-дедупликацию, API, MusicBrainz, Spotify и Яндекс.Музыку с
моками, каскад matcher, ручной review, Range и содержимое ZIP/M3U8. Qobuz
тестируется с моками клиента/скачивания на границе сервиса (Redis-кэш bundle
подменяется FakeRedis), включая end-to-end сценарий fetch-missing → staging →
верификация → библиотека → scan → matching на сгенерированных FLAC. Реальная
проверка внешних музыкальных API выполняется отдельно после настройки ключей и
токенов.

## Структура

- `backend/app/api/` — роутеры API, включая matching/download/auth/qobuz
- `backend/app/models.py` — схема БД из MVP-плана
- `backend/app/services/scanner.py` — локальный lossless-каталог
- `backend/app/services/musicbrainz.py` — внешнее обогащение и Redis-кэш
- `backend/app/services/spotify.py` — OAuth, refresh токенов и импорт Spotify
- `backend/app/services/yandex.py` — импорт Яндекс.Музыки
- `backend/app/services/qobuz.py` — обёртка qobuz-dl: клиент, поиск, staging,
  верификация и импорт в библиотеку
- `backend/app/services/normalize.py` — единые нормализованные ключи
- `backend/app/services/matcher.py` — каскад сопоставления и review-кандидаты
- `backend/app/services/delivery.py` — безопасная bit-perfect выдача
- `backend/app/workers/` — Celery-задачи (scan/import/matching/qobuz_download)
- `frontend/` — адаптивная PWA и Nginx reverse proxy
- `data/music/` — музыкальная библиотека (mount `/music/library`)
- `data/qobuz-staging/` — staging Qobuz-загрузок (mount `/music/staging`)
- `docs/music-service-handoff.md` — проверенное состояние MVP и следующие итерации
- `docs/qobuz-dl-assessment.md` — security/terms/code audit интеграции Qobuz

Этап 4 завершает функциональный scope MVP из
`docs/music-service-mvp-plan.md`. Реальная MVP-приёмка Spotify/Яндекс API,
локальной библиотеки и смартфона завершена 2026-08-09; детали и известные
ограничения зафиксированы в handoff-документе.
