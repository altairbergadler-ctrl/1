# Music Service — MVP (Этап 2: библиотека)

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
fallback, SHA-1-дедупликацию, API и MusicBrainz с моками. Реальная проверка
MusicBrainz выполняется отдельно после настройки корректного User-Agent.

## Структура

- `backend/app/api/` — роутеры API (этапы 3–4 пока остаются заглушками)
- `backend/app/models.py` — схема БД из MVP-плана
- `backend/app/services/scanner.py` — локальный lossless-каталог
- `backend/app/services/musicbrainz.py` — внешнее обогащение и Redis-кэш
- `backend/app/workers/` — Celery-задачи
- `data/music/` — музыкальная библиотека (mount `/music/library`)

## Дальше (по docs/music-service-mvp-plan.md)

1. Этап 3: `services/spotify.py`, `services/yandex.py`, `services/normalize.py`
2. Этап 4: `services/matcher.py`, `services/delivery.py`, PWA-фронтенд

Авторизация на MVP: заголовок `Authorization: Bearer <APP_AUTH_TOKEN>`.
