# Логика работы сервиса: Hi-Res музыкальный архив с импортом плейлистов

## 1. Концепция

Сервис — self-hosted платформа, которая:
1. Регистрирует любой подтверждённый Google-аккаунт с базовой ролью `user`.
2. Парсит торрент-трекеры по тематике «музыкальные альбомы в максимальном качестве» (FLAC 16/44.1, Hi-Res 24/96–24/192, DSD, vinyl rips).
3. Скачивает раздачи на сетевое хранилище (NAS).
4. Строит общий дедуплицированный каталог-библиотеку всей музыки.
5. Импортирует личные плейлисты пользователя из Spotify, Яндекс.Музыки или файла.
6. Сопоставляет треки из плейлистов с архивом.
7. Отдаёт найденные файлы на смартфон **без сжатия и конвертации** (bit-perfect), для воспроизведения в любом локальном плеере.

Многопользовательский принцип: **shared bytes, private rights**. Физические
музыкальные файлы общие, а sources, playlists, jobs, matching review и право
скачивания проверяются по `user_id` на каждом API-входе.

---

## 2. Высокоуровневая архитектура

```
┌────────────────────────────────────────────────────────────────┐
│                        CORE SERVER (Docker)                     │
│                                                                │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────────────┐  │
│  │ Tracker     │  │ Download     │  │ Library / Catalog     │  │
│  │ Scraper     │→ │ Manager      │→ │ Service               │  │
│  │ (парсер)    │  │ (qBittorrent │  │ (сканер + теги + БД)  │  │
│  └─────────────┘  │  /Deluge)    │  └───────────┬───────────┘  │
│                   └──────┬───────┘              │              │
│  ┌─────────────┐         │         ┌────────────▼───────────┐  │
│  │ Playlist    │─────────┼────────▶│ Matching Engine        │  │
│  │ Importer    │         │         │ (плейлист ↔ архив)     │  │
│  │ Spotify/Ya  │         │         └────────────┬───────────┘  │
│  └─────────────┘         │                      │              │
│  ┌─────────────┐         │         ┌────────────▼───────────┐  │
│  │ API + Web UI│◀────────┴─────────│ Delivery Service       │  │
│  │ (mobile)    │                   │ (bit-perfect download) │  │
│  └─────────────┘                   └────────────────────────┘  │
│                                                                │
│  PostgreSQL (метаданные, каталог, матчинг, задачи)             │
│  Redis (очереди задач, кэш)                                    │
│  Message Queue: Celery/Dramatiq или NATS                       │
└────────────────────────────────────────────────────────────────┘
            │                                    │
      ┌─────▼─────┐                       ┌──────▼──────┐
      │    NAS    │                       │  Смартфон   │
      │ (SMB/NFS, │                       │  (Web UI /  │
      │  FLAC/DSF)│                       │   app +     │
      └───────────┘                       │  плеер)     │
                                          └─────────────┘
```

### Слои
- **Data layer** — PostgreSQL + файловая структура NAS.
- **Worker layer** — асинхронные воркеры: скрапинг, загрузка, сканирование, матчинг.
- **API layer** — REST/GraphQL для Web UI и мобильного клиента.
- **Client layer** — адаптивный Web UI (PWA) для смартфона; воспроизведение — во внешнем плеере пользователя.

### Identity и access layer

- Google Sign-In — публичная регистрация через OIDC Authorization Code + PKCE, state,
  nonce, проверка Google JWKS/aud/azp/exp/iat/email_verified.
- После callback выдаётся собственная hashed server session в HttpOnly cookie;
  Google tokens не сохраняются.
- Unsafe API требует session-bound CSRF header и exact Origin/Referer.
- Роль `owner` управляет ролями/состояниями пользователей, system providers,
  каталогом и Google Drive; роль `user` работает только со своими объектами.
- `APP_AUTH_TOKEN` изолирован в краткоживущем bootstrap/recovery контуре.
- Google Login и Google Drive используют разные OAuth clients/callbacks.

Подтверждённый Google identity создаёт активного `user`, а последующие входы
разрешаются по стабильному `sub`. Чужие ID отвечают `404`, две личные READY
записи могут ссылаться на один физический `File`, а disable или изменение роли
немедленно отзывает все web sessions пользователя. Исторический production gate
изоляции 2026-08-12 и новый gate открытой регистрации описаны в
`docs/music-service-handoff.md`.

---

## 3. Модуль 1: Парсер торрент-ссылок (Tracker Scraper)

### 3.1 Источники
- Музыкальные трекеры с разделами lossless/Hi-Res (по настраиваемому списку).
- Подписки на RSS-ленты релизов, мониторинг новых тем форумов.

### 3.2 Пайплайн парсинга
```
RSS/раздел трекера → список тем → фильтр тематики → карточка релиза
    → нормализация → dedup → запись в БД (status: NEW)
```

Каждая тема нормализуется в структуру **Release**:
```json
{
  "artist": "...", "album": "...", "year": 2007,
  "format": "FLAC", "bit_depth": 24, "sample_rate": 96000,
  "source": "WEB | Vinyl | SACD | CD",
  "label": "...", "catalog_no": "...",
  "tracker": "...", "topic_url": "...", "magnet": "magnet:?xt=...",
  "size_bytes": 0, "seeders": 0
}
```

### 3.3 Фильтр тематики
- Обязательно: формат ∈ {FLAC, ALAC, WAV, DSF/DFF (DSD), APE→конверсия в FLAC опционально}.
- Приоритет: Hi-Res (24 bit) > CD-rip (16/44.1) > прочее.
- Отсев: MP3/AAC/lossy — по ключевым словам и по метаданным карточки.
- Парсинг заголовка трекера регулярками + словарём паттернов (Artist – Album (Year) [Format, Source]).

### 3.4 Dedup и версии релизов
- Ключ дедупликации: нормализованные `artist + album + year + format + source`.
- Хранение **нескольких вариантов** одного альбома (разные мастеринги/издания) с рейтингом предпочтения.

### 3.5 Режимы отбора к скачиванию
- **Auto-wantlist**: релиз скачивается, если его треки есть в импортированных плейлистах пользователя (матчинг на уровне альбома через MusicBrainz/Discogs).
- **Watchlist**: пользовательские фильтры (любимые исполнители, жанры, лейблы).
- **Manual**: очередь «найдено → ждёт одобрения».

---

## 4. Модуль 2: Download Manager (загрузка на NAS)

- Торрент-клиент: **qBittorrent** (Web API) или **Deluge** в контейнере.
- NAS монтируется в контейнер (NFS/SMB) как `/music/incoming` (неполные) и `/music/library` (готовое).

### Логика
1. Scraper ставит Release в очередь → Download Manager добавляет magnet/torrent в клиент с меткой `auto` и категорией `music`.
2. Ограничения: лимит одновременных загрузок, приоритет по seeders/размеру, пауза при заполнении NAS > 90%.
3. По завершении (webhook/poll `completed`): статус `DOWNLOADED` → триггер на Catalog Service.
4. Раздача продолжает сидироваться (настраиваемый ratio/время).
5. Обработка ошибок: stalled > N часов → рестарт/замена magnet из альтернативной темы (другая раздача того же релиза).

---

## 5. Модуль 3: Catalog / Library Service

### 5.1 Сканер
- Финализированные раздачи перемещаются/линкуются в `/music/library/<Artist>/<Album (Year) [Format]>/`.
- Сканер обходит файлы, читает теги через **mutagen** (FLAC/Vorbis, ID3, DSF).
- Анализ аудио: `ffprobe` — реальный битрейт, частота, битность; **spek-подобная проверка спектра** (опционально) для отлова апскейлов «фальшивого Hi-Res».

### 5.2 Обогащение метаданных
- Lookup в **MusicBrainz** (основной) + **Discogs** (fallback): MBID релиза, трек-лист, ISRC, обложки (Cover Art Archive).
- Нормализация: приведение исполнителей/названий к каноническому виду (правила транслитерации, удаление фичерингов в отдельное поле, unicode-нормализация NFKC, lower-case ключ для матчинга).

### 5.3 Схема БД (упрощённо)
```
artists(id, name, name_normalized, mbid)
albums(id, artist_id, title, title_normalized, year, mbid, edition)
tracks(id, album_id, title, title_normalized, track_no, disc_no,
       duration_ms, isrc, mbid_recording)
files(id, track_id, path_on_nas, format, bit_depth, sample_rate,
      bitrate, size_bytes, sha1, spectrum_verified bool)
releases(id, tracker, topic_url, magnet, status, quality_score)
users(id, email, email_key, google_sub, display_name, role, state,
      is_bootstrap_owner, created_at, activated_at, last_login_at)
user_sessions(id, user_id, kind, token_hash, csrf_hash, created_at,
              last_seen_at, expires_at, revoked_at)
google_login_attempts(id, state_hash, browser_binding_hash, nonce_hash,
                      encrypted_pkce, expires_at, consumed_at)
playlist_sources(id, user_id, service[spotify|yandex|manual])
user_provider_credentials(id, user_id, provider[spotify|yandex], ciphertext,
                          nonce, key_id, version, validated_at, updated_at)
provider_credentials(id, provider[qobuz|yandex], ciphertext, nonce, key_id,
                     version, validated_at, updated_at)
provider_health(id, provider, component[account|provider_api|sidecar|worker],
                state, checked_at, retry_at)
playlists(id, user_id, source_id, external_id, name, snapshot_hash, updated_at)
playlist_items(id, playlist_id, position, artist_raw, title_raw,
               album_raw, isrc, external_track_id)
matches(id, playlist_item_id, track_id, confidence, method, status)
jobs(id, user_id|null, scope[user|system], source_id, playlist_id,
     type, payload, status, retries)
```

`playlist_sources` и `playlists` имеют composite constraints, не позволяющие
сослаться на source другого пользователя. `jobs` применяет ту же проверку для
source/playlist. `File.sha1` остаётся глобально уникальным.

---

## 6. Модуль 4: Playlist Importer (Spotify / Яндекс.Музыка / файлы)

### 6.1 Spotify
- OAuth 2.0 (Authorization Code Flow), scopes: `playlist-read-private`, `playlist-read-collaborative`, `user-library-read`.
- API: `GET /v1/me/playlists`, `GET /v1/playlists/{id}/tracks` (пагинация), поля: track name, artists, album, **ISRC** (критично для матчинга), duration_ms.
- Webhook/polling по расписанию (например, каждые 6 часов) + инкремент через `snapshot_id`.

### 6.2 Яндекс.Музыка
- Токен пользователя (OAuth Яндекс ID) или cookie-токен.
- Неофициальный API (`yandex-music-api` / `yandex_music` Python lib): список плейлистов, треки с artists/title/albums, `trackId`.
- Особенность: ISRC часто отсутствует → матчинг по artist+title+album+duration.

### 6.3 Независимый импорт файла или текста
- CSV с обязательными artist/title и необязательными album/ISRC/duration/URI.
- Extended M3U/M3U8 с `#EXTINF`.
- Текст `Исполнитель — Трек` или tab-separated artist/title/album.
- Источник `manual`, сохранение порядка и дубликатов, немедленная matching-job.
- Не требует provider OAuth и не извлекает содержимое Spotify-ссылки.

### 6.4 Нормализация на входе
Единая структура `playlist_items` независимо от источника; сырые строки сохраняются для аудита; строятся нормализованные ключи.

---

## 7. Модуль 5: Matching Engine (сопоставление с архивом)

### 7.1 Каскад матчинга (от точного к нечёткому)
1. **ISRC match** — точное совпадение ISRC трека плейлиста с ISRC файла (из тегов/MusicBrainz). Confidence = 1.0.
2. **MBID match** — если у элемента плейлиста есть recording MBID (Spotify не даёт, но можно дообогатить через MusicBrainz search).
3. **Exact normalized** — `artist_norm + title_norm + album_norm` + допуск по длительности ±2 c. Confidence ≈ 0.9–0.98.
4. **Fuzzy match** — `rapidfuzz` (token_set_ratio) по artist+title, штраф за фичеринги/ремиксы/live-версии, сравнение длительности (±5 c), порог ~85. Confidence 0.7–0.9.
5. **Album-level fallback** — трек не найден, но альбом есть → статус `ALBUM_AVAILABLE` (можно отдать весь альбом).
6. **Not found** → трек/альбом попадает в **wantlist** для Tracker Scraper (замыкаем петлю: чего нет — то и ищем на трекерах).

### 7.2 Выбор лучшего файла
Если несколько копий трека: сортировка по quality_score = f(bit_depth, sample_rate, source: Vinyl/SACD/WEB/CD, spectrum_verified). Пользовательский пресет: «всегда максимальное качество» / «предпочитать CD-мастер».

### 7.3 Статусы элемента плейлиста
`MATCHED` → `READY` (файл на NAS) → `DELIVERED` (скачан на устройство) | `MISSING` → `WANTED` (скрапер ищет) | `NEEDS_REVIEW` (неоднозначный fuzzy, ручное подтверждение в UI).

---

## 8. Модуль 6: Delivery Service (выдача на смартфон, bit-perfect)

### Принцип
**Никакого транскодинга.** Файл отдаётся как есть (HTTP Range / прямая ссылка), даже FLAC 24/192 и DSD.

Перед выдачей требуется private grant: собственный READY `playlist_item` с
match на общий `Track/File`. Чужой item, playlist или album возвращает `404`,
не раскрывая существование объекта.

### Варианты доставки
1. **PWA / Web UI (основной)**: пользователь на смартфоне открывает сервис → плейлист со статусами → кнопка «Скачать» по треку/альбомом/весь плейлист (zip без рекомпрессии аудио — просто упаковка, либо пачка файлов).
2. **WebDAV-эндпоинт**: сервис экспонирует папку `Matched/<Playlist>/...` по WebDAV → подключается как сетевой диск из файлового менеджера смартфона.
3. **Мобильное приложение** (позднее, опционально): фоновая загрузка, авто-синк новых матчей, экспорт в медиатеку.

### Интеграция с «любимым плеером»
- После скачивания файлы лежат в `Music/<Service>/` → любой локальный плеер (Poweramp, USB Audio Player PRO, Neutron, AIMP, foobar2000 mobile) подхватывает их сканированием папки. Для iOS — экспорт в «Файлы» / передача через Share Sheet.
- Опционально: генерация `.m3u8` плейлиста с относительными путями, чтобы плеер сразу увидел структуру плейлиста.
- Теги и обложки в файлах полные (модуль 5.2) — плееру ничего дозагружать не нужно.

---

## 9. Сквозной сценарий (end-to-end flow)

### OpenSubsonic delivery flow

1. Активный пользователь создаёт отдельный device key через Google-session + CSRF.
2. Symfonium передаёт только `apiKey`; web/provider/recovery credentials не подходят.
3. Каждый `/rest/*` catalog query начинается с собственных Playlist → PlaylistItem
   → READY Match → Track → реально доступный File.
4. Публичные artist/album/song/playlist IDs стабильны и не содержат user ID или email.
5. Изменение видимого порядка, metadata, READY или storage availability увеличивает
   playlist revision; no-op транзакция revision не меняет.
6. Stream/download повторно проверяет grant и отдаёт local или Drive object с Range
   и исходными байтами. Transcoding отсутствует.
7. Disable user атомарно отзывает web sessions и все player keys; уже сохранённые на
   телефоне offline bytes сервер удалить не может.

```
1. Пользователь подключает Spotify/Яндекс либо импортирует CSV/M3U/текст.
2. Matching Engine сопоставляет треки с каталогом:
     - найдено → статус READY
     - не найдено → wantlist для Scraper
3. Scraper находит раздачи wantlist-альбомов → Download Manager качает на NAS.
4. Catalog Service сканирует, тегирует, верифицирует качество → новые файлы в БД.
5. Re-match по расписанию: MISSING → READY.
6. Пользователь в Web UI видит прогресс плейлиста, жмёт «Скачать всё».
7. Delivery отдаёт файлы bit-perfect → смартфон → любимый плеер.
```

## 10. Состояния задач и надёжность
- Все долгие операции — фоновые job'ы с retry и экспоненциальной задержкой.
- Идемпотентность: повторный скан/матчинг не плодит дубликаты (sha1, snapshot_hash).
- Логирование: audit-трейл «кто/когда/почему файл выбран».
- Мониторинг: метрики (Prometheus): объём NAS, длина очередей, hit-rate матчинга, failed downloads.

## 11. Технологический стек (рекомендация)
- Backend: Python (FastAPI) или Node.js (NestJS); воркеры Celery/Dramatiq.
- Торрент: qBittorrent-nox (Web API).
- Метаданные: mutagen, ffprobe, MusicBrainz NGS API, Discogs API, rapidfuzz.
- БД: PostgreSQL + Redis.
- Хранилище: NAS по NFS/SMB, структура `/music/library/<Artist>/<Album>/`.
- Frontend: PWA (React/Vue), адаптирован под смартфон.
- Развёртывание: Docker Compose (app, db, redis, qbittorrent) на домашнем сервере рядом с NAS.

## 12. Юридическая оговорка
Скачивание контента через торренты должно быть легальным в юрисдикции пользователя (собственные рипы, свободные раздачи, легальные источники). Сервис проектируется как органайзер легально полученной библиотеки.
