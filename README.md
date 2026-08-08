# Music Service — MVP (Этап 3: импорт плейлистов)

Hi-Res музыкальный архив: импорт плейлистов Spotify/Яндекс.Музыки,
матчинг с локальной lossless-библиотекой, bit-perfect выдача на смартфон.

ТЗ: `docs/music-service-logic.md`, `docs/music-service-mvp-plan.md`.

## Запуск

```bash
cp .env.example .env        # заполнить APP_AUTH_TOKEN и настройки
docker compose up --build
```

Проверка: `curl http://localhost:8000/api/health` → `{"status":"ok"}`

Миграция выполняется отдельным сервисом `migrate` до запуска API и Celery.
Локальная библиотека `data/music/` монтируется только для чтения в
`/music/library`.

## Миграции

Начальная миграция уже находится в `backend/alembic/versions/`. Для ручного
применения: `docker compose run --rm migrate`.

## API библиотеки

Все запросы, кроме health, требуют заголовок
`Authorization: Bearer <APP_AUTH_TOKEN>`.

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
моками. Реальная проверка внешних музыкальных API выполняется отдельно после
настройки ключей и токенов.

## Структура

- `backend/app/api/` — роутеры API (matching/download остаются этапом 4)
- `backend/app/models.py` — схема БД из MVP-плана
- `backend/app/services/scanner.py` — локальный lossless-каталог
- `backend/app/services/musicbrainz.py` — внешнее обогащение и Redis-кэш
- `backend/app/services/spotify.py` — OAuth, refresh токенов и импорт Spotify
- `backend/app/services/yandex.py` — импорт Яндекс.Музыки
- `backend/app/services/normalize.py` — единые нормализованные ключи
- `backend/app/workers/` — Celery-задачи
- `data/music/` — музыкальная библиотека (mount `/music/library`)

## Дальше (по docs/music-service-mvp-plan.md)

1. Этап 4: `services/matcher.py`, `services/delivery.py`, PWA-фронтенд

Авторизация на MVP: заголовок `Authorization: Bearer <APP_AUTH_TOKEN>`.
