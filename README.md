# Music Service — MVP (Этап 4: матчинг, выдача и PWA)

Hi-Res музыкальный архив: импорт плейлистов Spotify/Яндекс.Музыки,
матчинг с локальной lossless-библиотекой, bit-perfect выдача на смартфон.

ТЗ: `docs/music-service-logic.md`, `docs/music-service-mvp-plan.md`.

## Запуск

```bash
cp .env.example .env        # заполнить APP_AUTH_TOKEN и настройки
docker compose up --build
```

Проверка API: `curl http://localhost:8000/api/health` → `{"status":"ok"}`.
PWA открывается на `http://localhost:8080`.

Миграция выполняется отдельным сервисом `migrate` до запуска API и Celery.
Локальная библиотека `data/music/` монтируется только для чтения в
`/music/library`.

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
Яндекс.Музыки используется `YANDEX_TOKEN` из `.env`. Токены провайдеров никогда
не возвращаются API.

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
моками, каскад matcher, ручной review, Range и содержимое ZIP/M3U8. Реальная
проверка внешних музыкальных API выполняется отдельно после настройки ключей и
токенов.

## Структура

- `backend/app/api/` — роутеры API, включая matching/download/auth
- `backend/app/models.py` — схема БД из MVP-плана
- `backend/app/services/scanner.py` — локальный lossless-каталог
- `backend/app/services/musicbrainz.py` — внешнее обогащение и Redis-кэш
- `backend/app/services/spotify.py` — OAuth, refresh токенов и импорт Spotify
- `backend/app/services/yandex.py` — импорт Яндекс.Музыки
- `backend/app/services/normalize.py` — единые нормализованные ключи
- `backend/app/services/matcher.py` — каскад сопоставления и review-кандидаты
- `backend/app/services/delivery.py` — безопасная bit-perfect выдача
- `backend/app/workers/` — Celery-задачи
- `frontend/` — адаптивная PWA и Nginx reverse proxy
- `data/music/` — музыкальная библиотека (mount `/music/library`)

Этап 4 завершает функциональный scope MVP из
`docs/music-service-mvp-plan.md`. Проверка на конкретном смартфоне и реальные
Spotify/Яндекс API требуют пользовательского окружения, Tailscale и credentials.
