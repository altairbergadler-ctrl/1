# Yandex best-available acquisition: acceptance

Дата: 2026-08-10
Ветка: `codex/qobuz-hardening`
База ветки: `b317757`

## Результат

Политика: **FLAC preferred, AAC/MP3 fallback / LIVE VERIFIED**.

Стабильный `yandex-music==3.0.*` отвечает за импорт и поиск. Подписанный
file-info запрос выполняется через внутренний `yandex-signer`. Worker сначала
просит лучший вариант и сохраняет ответ без транскодирования: native FLAC,
FLAC-in-MP4 → native FLAC через stream copy, либо исходный AAC/HE-AAC/MP3.

## Живая проверка

Проверены два staging-only сценария на реальных треках плейлиста 13:

- FLAC: source codec `flac-mp4` → native `.flac` без перекодирования,
  44 100 Hz, 16 bit, 187.096 s;
- fallback: source codec `aac-mp4` → исходный `.m4a`, MP4/AAC, 275 kbps,
  44 100 Hz, 390.56 s;
- MP3-кандидат с несовпадающим track id был отклонён до скачивания;
- оба принятых файла прошли Mutagen/container/duration verification;
- тестовые файлы удалены из временного staging и не импортировались;
- OAuth token, подпись и полные download URL не выводились.

## Docker-приёмка

- `yandex-signer`: healthy;
- backend использует `yandex-music 3.0.0`;
- API status: `enabled=true`, `configured=true`,
  `supported_codecs=[flac,aac,mp3]`;
- Celery: `pong`;
- Alembic: `0004_provider_attempts (head)`;
- полный Docker/PWA pytest: `203 passed`;
- целевые scanner/Yandex/PWA проверки: `42 passed, 5 skipped`.

## Проверенные инварианты

- FLAC всегда предпочтителен в подписанном negotiation-контракте;
- AAC/HE-AAC/MP3 сохраняются только как fallback и без перекодирования;
- неизвестный codec, неподдерживаемый transport, другой track id и недоверенный
  URL отклоняются до записи;
- расширение и фактический контейнер `.flac`/`.m4a`/`.aac`/`.mp3` должны
  совпадать, Mutagen обязан определить положительную длительность;
- scanner каталогизирует новые fallback-контейнеры, общий pipeline
  library → scan → matching не меняется;
- прогресс содержит фактический codec, bitrate и безопасную причину ошибки;
- httpx INFO-логи отключены, чтобы подписанные query strings и временные media
  URL не попадали в worker log;
- signer не получает OAuth token и не имеет внешней сети.

## Повторная попытка после смены политики

21 отказ задания №50 сохранён в ledger под архивной policy-меткой
`yandex-lossless-v1`. Активный provider `yandex` получил ровно одну новую
попытку для этих записей: API сообщает `eligible=21`, остальные терминальные
результаты не сбрасывались.

Repository default остаётся `YANDEX_DOWNLOAD_ENABLED=false`. В локальном
ignored `.env` тестового стека флаг включён. Изменения не закоммичены и не
опубликованы.
