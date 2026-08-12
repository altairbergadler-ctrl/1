# Qobuz hardening acceptance

Дата: 2026-08-10
Ветка: `codex/qobuz-hardening`
База: `b317757` (merge исходного Qobuz-прототипа)

## Результат

Code/Docker acceptance: **PASS**. Live provider acceptance hardened sidecar:
**PASS** на пользовательской библиотеке; значения credentials не записывались
в документ и не выводились.

## Подтверждено

- Полная Docker-сборка backend/frontend/sidecar/egress proxy завершилась.
- `pytest -q` внутри Compose с живой PWA: **203 passed**.
- Sidecar security unit tests: **7 passed**.
- API health: `{"status":"ok"}`; frontend: HTTP 200.
- Alembic current/head: `0004_provider_attempts (head)`.
- Celery inspect: один worker, `pong`.
- `qobuz-dl` отсутствует в backend image.
- Provider Qobuz credentials отсутствуют в backend environment.
- В sidecar отсутствуют `APP_AUTH_TOKEN`, DB/Redis, Spotify/Yandex и
  email/password credentials.
- Разрешённый Qobuz host доступен через proxy; произвольный внешний host
  заблокирован; прямой интернет из sidecar заблокирован.
- `QOBUZ_QUALITY=27` передаётся downloader с `downgrade_quality=True`.
- Реальные connect, catalog search и два `fetch-missing` запуска завершились;
  файлы прошли staging → library → scan → matching без provider-ошибок.
- PWA восстанавливает последний Qobuz job после reload, показывает общий этап,
  счётчики пакета и сохранённые per-track статусы загрузки.
- Qobuz активен постоянно при включённой серверной конфигурации; лишняя кнопка
  ручного подключения удалена из PWA.
- `provider_attempts` восстановлен из истории job: один источник не проверяет
  fingerprint повторно; полный плейлист проходит пакетами 25 + остаток с
  настраиваемой паузой между пакетами.
- Unit/E2E покрывают ISRC/exact/strict-fuzzy, version markers, ambiguity,
  maximum-quality ordering, staging verification, safe library import, scan и
  повторный matching.

## Воспроизводимые команды

Все команды запускаются с заполненными локальными `.env` и без вывода его
содержимого.

```powershell
docker compose up -d --build
docker compose ps
docker compose run --rm migrate sh -lc "alembic current; alembic heads"
docker compose exec -T worker `
  celery -A app.workers.celery_app.celery inspect ping --timeout=10
docker compose run --rm `
  -e PWA_ACCEPTANCE_BASE_URL=http://frontend backend pytest -q
docker compose exec -T qobuz-sidecar python -m unittest -v test_app.py
```

## Live gate

Завершён 2026-08-10 на hardened topology: `/api/qobuz/connect`, реальный
catalog search, CDN-загрузка, перенос в пользовательскую library и повторный
matching подтверждены. Секреты остаются только в runtime sidecar и ignored
локальной конфигурации.

## Emergency disk-safety iteration — 2026-08-12

Причина: большой fetch-missing run достиг 92% использования системного диска.
Worker был остановлен до исчерпания места. Из checkpoint job импортированы,
просканированы и подтверждённо реплицированы 350 файлов (13.48 GB); после
verified remote upload локальные catalog bytes удалены. Использование диска
снизилось до 60%, свободное место выросло с 3.5 до 16 GB. Job сохранена как
pending и не перезапускается до развёртывания безопасного pipeline.

Реализовано:

- cooperative pause/resume одной и той же job на межпакетной границе;
- import → scan → durable replication → verified eviction → matching после
  каждой пачки, до следующего provider download;
- автоматическая пауза до первой и каждой следующей пачки при запасе ниже
  `QOBUZ_MIN_FREE_BYTES` (default 5 GiB);
- PWA controls «Пауза после пачки» / «Продолжить» и persisted paused state;
- Alembic `0011_qobuz_pause_control`, включая PostgreSQL
  upgrade → downgrade to 0010 → upgrade acceptance.

Pre-deploy acceptance:

- focused Qobuz/PWA/migration: **52 passed, 5 skipped**;
- full backend suite: **333 passed, 5 skipped**;
- legacy expand/backfill/contract migration: **3 passed**;
- `git diff --check`, Python compile и JavaScript syntax checks: PASS.
