# `qobuz-dl` assessment: hardened RESTRICT profile

Дата актуализации: 2026-08-09. Это gate из
`docs/music-service-mvp-plan.md`. Решение: **RESTRICT** — интеграция разрешена
только в описанной здесь изолированной конфигурации.

## 1. Объект и границы

| Параметр | Значение |
| --- | --- |
| Апстрим | <https://github.com/vitiko98/qobuz-dl> |
| Артефакт | `qobuz-dl==0.9.9.10` |
| Тип | сторонний неофициальный CLI/Python-модуль |
| Лицензия | GPL-3.0 |
| Использование | только как библиотека внутри `qobuz-sidecar` |

На дату проверки репозиторий не архивирован, но последний найденный code merge
датирован 2023 годом, а изменения 2025 года затрагивали README. Открытые issues
подтверждают актуальные для нас риски: поломку password-login
([#330](https://github.com/vitiko98/qobuz-dl/issues/330)), запросы без timeout
([#317](https://github.com/vitiko98/qobuz-dl/issues/317)) и возможные ограничения
аккаунта ([#305](https://github.com/vitiko98/qobuz-dl/issues/305)). Поэтому
безопасность не полагается на активное сопровождение апстрима.

Пакет отсутствует в `backend/requirements.txt` и недоступен API/Celery-процессам.
Wheel `qobuz-dl==0.9.9.10` зафиксирован SHA-256
`48e6b09cc03b4f1621867667afd91f550cbe9823729ffa8e9400b90631ebcd4d`.
Все транзитивные зависимости sidecar также зафиксированы с SHA-256 в
`qobuz_sidecar/requirements.lock`; установка выполняется с `pip
--require-hashes`. CLI, интерактивные режимы и пользовательский `config.ini` не
используются.

## 2. Доверенные границы

```text
backend/worker --Bearer internal token--> qobuz-sidecar
                                         | staging RW
                                         v
                                  allowlist CONNECT proxy --> Qobuz HTTPS

worker <-- verified relative paths ------+
  | Mutagen/containment/no-overwrite verification
  v
local music library RW
```

- `qobuz-sidecar` получает только `QOBUZ_AUTH_TOKEN`, необязательный
  `QOBUZ_USER_ID`, внутренний control-токен и staging mount.
- У sidecar нет `APP_AUTH_TOKEN`, Spotify/Yandex-секретов, URL базы/Redis,
  Docker socket и mount локальной библиотеки.
- `backend` не видит staging и монтирует библиотеку read-only.
- Только `worker` видит одновременно staging и библиотеку и может перенести
  проверенный файл. Существующие файлы не перезаписываются.
- Контейнеры sidecar/proxy работают без capabilities, с
  `no-new-privileges`, read-only root filesystem и непривилегированным user.

## 3. Авторизация и секреты

Разрешён только токен браузерной сессии (`QOBUZ_AUTH_TOKEN`). Основной пароль
Qobuz стороннему пакету не передаётся; email/password fallback удалён. Токен:

- хранится только в ignored `.env` и передаётся только sidecar;
- не записывается в БД, ответы API или логи;
- не возвращается `/status` и `/connect`;
- при ротации/истечении заменяется владельцем в `.env` с последующим restart
  sidecar.

Внутренний `QOBUZ_INTERNAL_TOKEN` — отдельный случайный секрет для приватного
HTTP API sidecar. Он не является credential аккаунта Qobuz.

## 4. Сеть

Sidecar подключён только к двум internal Docker-сетям и не имеет прямого
интернет-маршрута. HTTPS принудительно проходит через отдельный CONNECT-proxy.
Proxy разрешает только TCP/443 к точным именам:

- `play.qobuz.com`;
- `open.qobuz.com`;
- `www.qobuz.com`;
- `static.qobuz.com`;
- `streaming-qobuz-std.akamaized.net`;
- `streaming-qobuz-sec.akamaized.net`.

Поддомены по маске, IP-адреса, HTTP, нестандартные порты и redirect за пределы
allowlist отклоняются. Для всех запросов обязательны connect/read timeouts.
Размер одного файла ограничен `QOBUZ_MAX_FILE_BYTES`; незавершённый или
превысивший лимит временный файл удаляется.

Извлечение `app_id`/signing secrets веб-плеера остаётся хрупкой частью
апстрима. Sidecar держит их только в памяти с TTL и повторно извлекает после
ошибки подписи; Redis для этого не используется.

## 5. Выбор правильной записи и качества

`fetch-missing` обрабатывает только элементы со статусом `MISSING`. Кандидат
скачивается автоматически лишь при однозначном результате каскада:

1. совпавший нормализованный ISRC;
2. точные artist/title, совместимые маркеры версии и длительность в пределах
   двух секунд;
3. строгий fuzzy-порог 95, отдельные artist/title не ниже 92, длительность в
   пределах трёх секунд и отрыв не менее пяти пунктов.

Маркеры `live`, `remix`, `cover`, `acoustic`, `instrumental`, `radio edit` и
`remaster` должны совпадать. Разные ISRC/каталожные записи при равной оценке
считаются неоднозначными и **не скачиваются**. Повторные издания одной записи
с одинаковым ISRC сортируются по максимальной доступной bit depth/sample rate.

`QOBUZ_QUALITY=27` запрашивает максимальный Hi-Res tier; downloader с
`downgrade_quality=true` выбирает лучшее качество, реально доступное для этой
записи и подписки. Это не означает искусственный upsample.

## 6. Запись на диск

Поток: `download to staging → sidecar Mutagen check → worker Mutagen check →
safe move → library scan → playlist rematch`.

До импорта обе границы проверяют:

- относительный путь и containment внутри staging;
- отсутствие symlink/path traversal;
- allowlist расширений только `.flac`; MP3 downgrade не импортируется;
- ненулевой размер и читаемую Mutagen audio info с длительностью;
- containment целевого пути внутри `MUSIC_LIBRARY_PATH`.

Конфликтующее имя остаётся в staging, а имеющийся библиотечный файл не
перезаписывается. Обложки, booklet и прочие не-аудио артефакты не импортируются.

## 7. Операционные ограничения

- Интеграция выключена по умолчанию (`QOBUZ_ENABLED=false`).
- Одновременно допускается только один qobuz-job; другой запрос получает 409,
  кроме идемпотентного повтора того же задания.
- `QOBUZ_MAX_TRACKS_PER_RUN` ограничивает пакет (по умолчанию 25), а
  `QOBUZ_REQUEST_DELAY_SECONDS` задаёт паузу между треками.
- Конфигурационные/auth ошибки не retry; временные provider/network ошибки
  имеют не более двух retry.
- `stale` heartbeat остаётся аварийной защитой поверх сетевых timeout.

## 8. Остаточные риски и решение

- Используется неофициальный и недокументированный API веб-плеера; Qobuz может
  изменить его без обратной совместимости или ограничить аккаунт.
- Браузерный токен обладает правами пользовательской сессии. Изоляция снижает
  blast radius, но не делает компрометацию безвредной.
- Точность metadata зависит от Spotify/Yandex/Qobuz. Поэтому неоднозначность
  всегда оставляется человеку, а не разрешается выбором «первого результата».
- Использование и хранение загруженного контента должно соответствовать
  применимым условиям подписки и закону; сервис не публикует файлы сам.

**RESTRICT** действует, пока сохранены token-only auth, sidecar/proxy isolation,
точный egress allowlist, hash-locked зависимости, лимиты, строгий matcher и
двойная проверка staging. Снятие любого из этих ограничений требует нового
assessment. Результаты воспроизводимой проверки записываются в
`docs/qobuz-hardening-acceptance.md`.
