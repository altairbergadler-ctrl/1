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
только для чтения, worker — с записью (нужно для provider-импорта, см. разделы
«Qobuz» и «Яндекс.Музыка: докачка»). Общий staging
`data/qobuz-staging/` доступен только изолированному Qobuz-sidecar и worker;
backend его не видит. Яндекс-загрузки используют вложенный каталог
`/music/staging/yandex`.

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

Сканер поддерживает `.flac`, `.alac`, `.wav`, `.dsf`, `.dff`, `.ape`, а также
Yandex fallback-контейнеры `.m4a`, `.aac`, `.mp3`; читает теги Mutagen и использует fallback
`Artist/Album (Year)/NN - Title.ext`. Дедупликация файлов выполняется по SHA-1.

## Источники и плейлисты

- `GET /api/sources/spotify/connect` — начало Spotify Authorization Code OAuth;
- `GET /api/sources/spotify/callback` — callback с одноразовым Redis state;
- `PUT /api/providers/yandex/credentials` — проверка и атомарная ротация токена;
- `PUT /api/providers/qobuz/credentials` — проверка и атомарная ротация token/user_id;
- `GET /api/providers/health` — отдельное здоровье account/API/sidecar/worker;
- `POST /api/providers/{provider}/health-check` — ручная фоновая проверка;
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
Yandex/Qobuz credentials вводятся на странице PWA «Провайдеры», проверяются
до активации и хранятся в AES-256-GCM vault. Старое значение остаётся активным,
если проверка нового завершилась ошибкой. Токены никогда не возвращаются API.

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

Докачка отсутствующих (`MISSING`) треков из каталога Qobuz. Неофициальная
библиотека `qobuz-dl==0.9.9.10` запускается только в изолированном sidecar:
без базы, Redis, Docker socket и локальной библиотеки, с hash-locked
зависимостями и точным egress allowlist. Условия **RESTRICT**, остаточные риски
и границы доверия описаны в `docs/qobuz-dl-assessment.md`.

Переменные окружения (`.env`):

Qobuz включается серверным флагом, а account credential добавляется и
ротируется на странице PWA «Провайдеры» без перезапуска Docker. Endpoint
`POST /api/qobuz/connect` сохранён как диагностическая проверка активной версии.

- `QOBUZ_ENABLED=true` — включает интеграцию (по умолчанию выключена);
- `QOBUZ_CREDENTIAL_KEY_HOST_FILE` — ignored файл с отдельным 32-byte ключом
  шифрования Qobuz vault; sidecar получает этот файл через Docker Secret;
- `PROVIDER_CREDENTIAL_KEY_HOST_FILE` — отдельный ключ vault для
  Yandex/Spotify, недоступный Qobuz sidecar;
- `QOBUZ_INTERNAL_TOKEN` — отдельный случайный длинный токен приватного API
  sidecar; он не должен совпадать с `APP_AUTH_TOKEN`;
- `QOBUZ_QUALITY` — `6` (16/44.1), `7` (24/<96 kHz),
  `27` (24/>96 kHz, по умолчанию) с downgrade только между lossless-tier;
  MP3 tier `5` не допускается к импорту;
- `QOBUZ_STAGING_PATH` (в контейнере `/music/staging`) и
  `QOBUZ_STAGING_HOST_PATH` (на хосте `./data/qobuz-staging`);
- `QOBUZ_MAX_TRACKS_PER_RUN` (по умолчанию 25) — размер внутренней пачки;
- `QOBUZ_REQUEST_DELAY_SECONDS` (по умолчанию 1.0) — пауза между скачиваниями;
- `QOBUZ_BATCH_DELAY_SECONDS` (по умолчанию 30) — пауза между пачками;
- `QOBUZ_DOWNLOAD_JOB_STALE_SECONDS` — stale-таймаут задания (по умолчанию
  21600);
- `QOBUZ_EMBED_ART` — встраивать обложку в теги файла;
- `QOBUZ_CONNECT_TIMEOUT_SECONDS`, `QOBUZ_READ_TIMEOUT_SECONDS` и
  `QOBUZ_MAX_FILE_BYTES` — сетевые и размерные границы sidecar.

Endpoints (все требуют авторизации):

- `GET /api/qobuz/status` — `enabled`/`configured`/лимиты, без секретов;
- `POST /api/qobuz/connect` — проверка токена; при успехе возвращает `label`
  тарифа (`400` — токен отклонён, `502` — Qobuz недоступен, `503` — не
  настроен);
- `PUT /api/providers/qobuz/credentials` — проверяет новый token/user_id в
  sidecar и только после успеха атомарно активирует новую encrypted-версию;
- `GET /api/qobuz/search?q=&type=track|album&limit=` — поиск по каталогу Qobuz;
- `GET /api/qobuz/download-status/{playlist_id}` — последний сохранённый
  прогресс Qobuz-загрузки плейлиста, включая этап job и статусы отдельных
  треков;
- `POST /api/qobuz/download-url` с `{ "url": "https://play.qobuz.com/..." }` —
  скачивание по ссылке; в MVP поддерживаются только URL типа `album` и `track`
  (playlist/artist/label отклоняются с понятной ошибкой);
- `GET /api/qobuz/download-eligibility/{playlist_id}` — сколько MISSING-треков
  ещё не проверялись в Qobuz;
- `POST /api/qobuz/fetch-missing` с `{ "playlist_id": N }` — один полный проход
  всех ещё не проверенных MISSING-треков плейлиста пачками по
  `QOBUZ_MAX_TRACKS_PER_RUN`.

Оба download-endpoint возвращают `202` и задание (`GET /api/jobs/{id}`).
Одновременно активно только одно qobuz-задание: повторный запрос вернёт уже
идущее, только если это точный идемпотентный повтор; конфликтующий запрос
получит `409` и id активного задания.

Прогресс хранится в `jobs.payload` и не теряется при обновлении страницы.
PWA показывает этапы `downloading → importing → scanning → matching`, счётчики
текущего пакета и статусы треков: `queued`, `searching`, `downloading`,
`stored`, `not_found`, `ambiguous`, `conflict` или `failed`. При кратком обрыве
API интерфейс переподключается и продолжает следить за той же job.

Терминальный результат проверки записывается в `provider_attempts` по паре
`provider + SHA-256 fingerprint` трека. Поэтому один источник не получает
повторный запрос для уже проверенного трека, одинаковые треки в разных
плейлистах дедуплицируются, а будущий другой provider сможет проверить тот же
трек независимо. Результат `stored`/`conflict` блокирует повторное скачивание
из любого источника, даже если автоматический matching ещё не связал файл.

Pipeline задания: `staging → верификация → библиотека → scan → matching`.
Скачанное никогда не пишется в `MUSIC_LIBRARY_PATH` напрямую: файлы сначала
попадают в staging, проверяются (только `.flac`, mutagen парсит и
видит ненулевую длительность, размер > 0), затем worker переносит их в
библиотеку с сохранением структуры папок и containment-проверкой путей.
Существующие файлы не перезаписываются — конфликт остаётся в staging;
обложки/буклеты в библиотеку не переносятся. После переноса задание инлайн
запускает скан библиотеки (если другой скан уже идёт — scan помечается
`deferred`, а matching пропускается) и, для fetch-missing, повторный матчинг
плейлиста. Из-за записи в библиотеку **только** контейнер `worker` получил
read-write mount `/music/library`; backend остаётся read-only
(обоснование — разделы 2 и 6 assessment-документа).

Автовыбор консервативен: ISRC имеет приоритет, затем идут точные metadata и
строгий fuzzy с контролем длительности/версии/отрыва. `live`, `remix`, `cover`,
`acoustic`, `instrumental`, `radio edit` и `remaster` не смешиваются. Если две
разные записи остаются равновероятными, трек помечается `ambiguous` и не
скачивается. Для одной и той же записи выбирается лучшее опубликованное
качество, а запрос tier `27` автоматически понижается лишь до реально
доступного.

Ошибки конфигурации/токена завершают задание без retry; временные provider/
network ошибки имеют не более двух retry. `app_id`/signing secrets веб-плеера
кэшируются только в памяти sidecar и перевытягиваются после ошибки подписи.

## Яндекс.Музыка: lossless-докачка

[`yandex-music`](https://github.com/MarshalX/yandex-music-api) `3.0.*` отвечает
за импорт плейлистов, поиск и метаданные. Lossless file-info подписывает
отдельный `yandex-signer`, собранный из фиксированного upstream commit. Signer
не получает OAuth token, не имеет внешней сети и не публикует порт на хост.

Worker всегда запрашивает лучший вариант. Native FLAC сохраняется напрямую,
FLAC-in-MP4 перепаковывается в `.flac` через `ffmpeg -c:a copy`, то есть без
повторного кодирования. Если FLAC для трека недоступен, исходный AAC/HE-AAC или
MP3 сохраняется без транскодирования. Другой track id, неизвестный codec,
transport или host по-прежнему отклоняются.

Безопасный default — `YANDEX_DOWNLOAD_ENABLED=false`. Для включения нужны
подключённый источник Яндекса и `YANDEX_INTERNAL_TOKEN` минимум 16 символов,
отдельный от `APP_AUTH_TOKEN`. `YANDEX_SIGNER_URL` внутри Compose уже указывает
на `http://yandex-signer:8091`.

Один запуск проверяет весь список eligible MISSING-треков пачками по
`YANDEX_MAX_TRACKS_PER_RUN` (по умолчанию 25), выдерживает
`YANDEX_BATCH_DELAY_SECONDS` между пачками и сохраняет per-track прогресс в
job. Терминальная попытка Яндекса не повторяется тем же provider, но не мешает
Qobuz или будущему отдельному источнику.

Недокументированный API может измениться без предупреждения. Audit,
ограничения и живая проверка: `docs/yandex-music-api-assessment.md` и
`docs/yandex-acquisition-acceptance.md`.

## MusicBrainz

Обогащение включается через `MUSICBRAINZ_ENABLED=true`. Перед включением
обязательно замените `MUSICBRAINZ_USER_AGENT` на строку с реальным контактом
сопровождающего. Ответы кэшируются в Redis, общий лимит — не более одного
внешнего запроса в секунду.

## Google Drive

Google Drive используется как основное долговременное хранилище. Несколько
Google-аккаунтов образуют общий пул объёма: новый файл размещается на здоровом
аккаунте с достаточным свободным местом, а при временной ошибке используется
следующий. OAuth-приложение и аккаунты подключаются через `PWA -> Хранилище`;
секреты в API не возвращаются и в `.env` не записываются.

Подробная памятка, модель безопасности и приёмка:
[`docs/google-drive-storage.md`](docs/google-drive-storage.md).

## Тесты

```bash
docker compose run --rm backend pytest -q
```

Тесты генерируют короткие аудиофайлы через ffmpeg, проверяют повторный скан,
fallback, SHA-1-дедупликацию, API, MusicBrainz, Spotify и Яндекс.Музыку с
моками, каскад matcher, ручной review, Range и содержимое ZIP/M3U8. Qobuz
тестируется на границе приватного sidecar, включая строгий выбор записи и
end-to-end сценарий fetch-missing → staging → верификация → библиотека → scan
→ matching на сгенерированных FLAC. Яндекс-тесты проверяют signed contract,
подмену track id, native FLAC, stream-copy перепаковку FLAC-in-MP4, AAC/MP3
fallback без транскодирования, полный batching и общий import/scan/matching pipeline.
Sidecar отдельно проверяет allowlist,
лимит размера, очистку partial-файла и отсутствие секретов в status.

## Структура

- `backend/app/api/` — роутеры API, включая matching/download/auth/qobuz
- `backend/app/models.py` — схема БД из MVP-плана
- `backend/app/services/scanner.py` — локальный lossless-каталог
- `backend/app/services/musicbrainz.py` — внешнее обогащение и Redis-кэш
- `backend/app/services/spotify.py` — OAuth, refresh токенов и импорт Spotify
- `backend/app/services/yandex.py` — импорт Яндекс.Музыки
- `backend/app/services/yandex_acquisition.py` — Yandex provider: signed
  file-info, FLAC preference, AAC/MP3 fallback, staging и batching
- `backend/app/services/qobuz.py` — клиент приватного sidecar, строгий выбор
  записи, повторная верификация staging и безопасный импорт
- `backend/app/services/normalize.py` — единые нормализованные ключи
- `backend/app/services/matcher.py` — каскад сопоставления и review-кандидаты
- `backend/app/services/delivery.py` — безопасная bit-perfect выдача
- `backend/app/services/google_drive.py` — OAuth, resumable upload и Range
- `backend/app/services/storage.py` — multi-account placement и миграция
- `backend/app/workers/` — Celery-задачи
  (scan/import/matching/qobuz_download/yandex_download)
- `frontend/` — адаптивная PWA и Nginx reverse proxy
- `qobuz_sidecar/` — изолированный адаптер `qobuz-dl` и egress allowlist proxy
- `yandex_signer/` — изолированный sign-only wrapper без OAuth token и egress
- `data/music/` — музыкальная библиотека (mount `/music/library`)
- `data/qobuz-staging/` — staging Qobuz-загрузок (mount `/music/staging`)
- `docs/music-service-handoff.md` — проверенное состояние MVP и следующие итерации
- `docs/qobuz-dl-assessment.md` — security/terms/code audit интеграции Qobuz
- `docs/yandex-music-api-assessment.md` — audit и ограничения lossless flow
- `docs/yandex-acquisition-acceptance.md` — Docker/live-приёмка Яндекса

Этап 4 завершает функциональный scope MVP из
`docs/music-service-mvp-plan.md`. Реальная MVP-приёмка Spotify/Яндекс API,
локальной библиотеки и смартфона завершена 2026-08-09; детали и известные
ограничения зафиксированы в handoff-документе.
