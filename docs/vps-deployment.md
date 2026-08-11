# VPS deployment: audiofeel.su

Этот профиль разворачивает Music Service рядом с OpenClaw, не публикуя
контейнерные порты напрямую в интернет.

## Изоляция

- frontend слушает только `127.0.0.1:18080`;
- backend слушает только `127.0.0.1:18000`;
- PostgreSQL, Redis и provider sidecars не публикуют host-порты;
- Qobuz и Yandex control networks остаются `internal`;
- для каждого постоянного контейнера заданы лимиты RAM, CPU и PID;
- backend работает без `--reload`, а код берётся из собранного image;
- Uvicorn access log отключён, поэтому OAuth `code` не попадает в журналы;
- Docker использует отдельный address pool `172.30.0.0/16`, локальный log
  driver с ротацией и не меняет политику IP forwarding, необходимую VPN.

## Подготовка runtime

Из корня checkout на VPS:

```bash
bash deploy/vps/bootstrap-runtime.sh
```

Скрипт идемпотентно создаёт:

- `/etc/audiofeel/music-service.env` с правами `0600`;
- независимые 32-byte credential keys в `/etc/audiofeel/secrets`;
- `/srv/audiofeel/library`, `/srv/audiofeel/staging` и временный
  `/srv/audiofeel/cache` для проверенной сборки ZIP из Drive;
- случайные PostgreSQL, PWA и внутренние sidecar secrets без вывода значений.

Существующий env и существующие ключи скрипт не заменяет.

## Запуск

```bash
docker compose \
  --env-file /etc/audiofeel/music-service.env \
  -f docker-compose.yml \
  -f docker-compose.production.yml \
  up -d --build
```

## Приёмка без раскрытия credentials

```bash
docker compose \
  --env-file /etc/audiofeel/music-service.env \
  -f docker-compose.yml \
  -f docker-compose.production.yml \
  ps

curl --fail --silent http://127.0.0.1:18000/api/health
curl --fail --silent --output /dev/null http://127.0.0.1:18080/

docker compose \
  --env-file /etc/audiofeel/music-service.env \
  -f docker-compose.yml \
  -f docker-compose.production.yml \
  exec -T worker \
  celery -A app.workers.celery_app.celery inspect ping --timeout=10
```

Проверка Alembic:

```bash
docker compose \
  --env-file /etc/audiofeel/music-service.env \
  -f docker-compose.yml \
  -f docker-compose.production.yml \
  run --rm --no-deps migrate sh -lc 'alembic current; alembic heads'
```

## Обновление и откат

Перед обновлением фиксируется Git SHA и снимается backup PostgreSQL. Затем
образы пересобираются той же командой `up -d --build`. Для отката checkout
возвращается на предыдущий проверенный SHA и команда повторяется. Каталог
`/srv/audiofeel/library`, staging, credential keys и volume PostgreSQL не
удаляются.

Команды `down -v`, очистка `/var/lib/docker` и удаление runtime-каталогов в
штатном обновлении запрещены.

## Публичный доступ

TLS reverse proxy подключается к `127.0.0.1:18080`. До завершения DNS/TLS
приёмки контейнеры остаются доступны только с VPS. Provider account tokens
вводятся позднее через PWA и не хранятся в env.

Google OAuth Client secret и refresh tokens также вводятся только через
`PWA -> Хранилище`, проверяются до активации и шифруются внешним ключом.
Callback production-приложения:
`https://audiofeel.su/api/storage/google/callback`.
