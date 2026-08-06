# Music Service — MVP (Этап 1: скелет)

Hi-Res музыкальный архив: импорт плейлистов Spotify/Яндекс.Музыки,
матчинг с локальной lossless-библиотекой, bit-perfect выдача на смартфон.

ТЗ: `docs/music-service-logic.md`, `docs/music-service-mvp-plan.md`.

## Запуск

```bash
cp .env.example .env        # заполнить APP_AUTH_TOKEN и ключи
docker compose up --build
```

Проверка: `curl http://localhost:8000/api/health` → `{"status":"ok"}`

## Миграции

```bash
docker compose exec backend alembic revision --autogenerate -m "init"
docker compose exec backend alembic upgrade head
```

## Структура

- `backend/app/api/` — роутеры (заглушки Этапов 2–4, помечены TODO)
- `backend/app/models.py` — схема БД из MVP-плана
- `backend/app/workers/` — Celery-задачи (заглушки)
- `data/music/` — музыкальная библиотека (mount /music)

## Дальше (по docs/music-service-mvp-plan.md)

1. Этап 2: `services/scanner.py` (mutagen) + `services/musicbrainz.py`
2. Этап 3: `services/spotify.py`, `services/yandex.py`, `services/normalize.py`
3. Этап 4: `services/matcher.py`, `services/delivery.py`, PWA-фронтенд

Авторизация на MVP: заголовок `Authorization: Bearer <APP_AUTH_TOKEN>`.
