# Yandex Music: lossless acquisition assessment

Дата проверки: 2026-08-10
Решение: **APPROVE WITH RESTRICTIONS для lossless acquisition**
Scope: второй provider текущей библиотеки; не Release 2

## 1. Проверенный upstream

- Импорт плейлистов и поиск остаются на стабильном
  [`yandex-music==3.0.*`](https://pypi.org/project/yandex-music/).
- Вопрос MarshalX/yandex-music-api
  [#656](https://github.com/MarshalX/yandex-music-api/issues/656) описывает
  отдельный подписанный `/get-file-info` flow для FLAC, которого нет в
  стабильном API клиента.
- Для production-подписи выбран
  [`Stmol/yandex-music-downloader`](https://github.com/Stmol/yandex-music-downloader)
  на фиксированном commit
  `ed36c6878207d2d248bafb0c6e55786a04296748`. Docker build проверяет точный SHA,
  выполняет upstream-тесты и собирает только маленький sign-only wrapper.
- Ключ протокола не копируется в Python-код, `.env` или документацию. Готовый
  signer не получает OAuth token и не имеет внешней сети.

Все перечисленные API неофициальны и основаны на недокументированных запросах.
Яндекс может изменить контракт без предупреждения. Это не юридическое
заключение; оператор самостоятельно проверяет условия своей подписки и
применимое право.

## 2. Принятый flow

1. Worker получает OAuth token только из уже подключённого источника Яндекса.
2. Поиск выбирает запись существующим строгим каскадом и вызывает внутренний
   `yandex-signer` только с `track_id` и timestamp.
3. Signer возвращает подписанные поля запроса. OAuth token, URL аудиофайла и
   содержимое трека через signer не проходят.
4. Worker запрашивает `/get-file-info` с текущим web-контрактом, предпочитающим
   lossless, и принимает только совпадающий track id, `transport=raw`,
   известный FLAC/AAC/MP3 codec и доверенный HTTPS-host Яндекса.
5. Native FLAC сохраняется напрямую. FLAC-in-MP4 перепаковывается `ffmpeg
   -c:a copy` в обычный `.flac` без декодирования и повторного кодирования.
6. Если FLAC недоступен, AAC/HE-AAC или MP3 сохраняется в исходном контейнере
   без транскодирования.
7. Mutagen повторно проверяет контейнер и длительность. Только после staging
   verification файл переносится в библиотеку, сканируется и матчится.

Список negotiation-кодеков соответствует текущему подписанному web-контракту.
Ответ Яндекса считается лучшим доступным вариантом для аккаунта. Неизвестный
codec, несовпадающий трек, контейнер, transport или host жёстко отклоняются.

## 3. Изоляция и ограничения

- `yandex-signer` работает non-root, read-only, без capabilities и только во
  внутренней Docker-сети `yandex_control`; наружу порт не публикуется.
- Внутренний API защищён отдельным bearer token минимум 16 символов. Нельзя
  переиспользовать `APP_AUTH_TOKEN`.
- Signer принимает только цифровой track id, timestamp в окне ±300 секунд и
  тело не более 4 KiB; секреты и тела запросов не логируются.
- Download URL разрешён только по HTTPS и Yandex-host allowlist, redirects
  перепроверяются, размер и timeout ограничены, partial-файл удаляется.
- `YANDEX_DOWNLOAD_ENABLED=false` остаётся безопасным default. Локальное
  включение выполняется отдельно после настройки источника и signer token.
- Provider ledger независим от Qobuz: один источник не блокирует последующую
  проверку другим. Повторная проверка тем же provider не выполняется.
- Весь eligible-плейлист проходит пачками по 25 с паузой между пачками.

## 4. Решение

| Возможность | Решение |
|---|---|
| Импорт плейлистов/метаданных | **ALLOW** — стабильный существующий контур |
| Native FLAC и FLAC-in-MP4 | **ALLOW WITH RESTRICTIONS** — только через описанный fail-closed flow |
| AAC/HE-AAC/MP3 fallback | **ALLOW WITH RESTRICTIONS** — только если Яндекс не вернул FLAC, без транскодирования |
| Чтение desktop/mobile offline-cache | **REJECT** — не требуется и не реализовано |
| Автоматизация торрентов/qBittorrent | Вне scope; только Release 2 по отдельному указанию |

Живая и Docker-приёмка зафиксированы в
`docs/yandex-acquisition-acceptance.md`.
