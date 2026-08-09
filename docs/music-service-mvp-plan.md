# MVP-план разработки: Hi-Res музыкальный сервис

## Объём MVP (scope freeze)

**Входит:**
1. Импорт плейлистов из Spotify и Яндекс.Музыки
2. Сканер локальной библиотеки (FLAC и др. lossless) с чтением тегов
3. Обогащение метаданных через MusicBrainz (ISRC, MBID)
4. Matching Engine: плейлист ↔ архив (каскад матчинга)
5. PWA: список плейлистов со статусами, скачивание треков/плейлистов bit-perfect + m3u8
6. Авторизация одного пользователя (простая)

**НЕ входит (релиз 2):** скрапер трекеров, авто-загрузка через qBittorrent, спектральный анализ, upgrade-политика, дедуп, Telegram-уведомления, мультипользовательность. На MVP торренты добавляются вручную в qBittorrent (или файлы кладутся в папку напрямую), сканер их подхватывает.

**Критерий готовности MVP:** импортировал плейлист из Spotify → вижу, какие треки есть в архиве → скачал плейлист на смартфон → играю в своём плеере.

---

## 1. Структура репозитория

```
music-service/
├── docker-compose.yml
├── .env.example
├── backend/
│   ├── pyproject.toml            # или requirements.txt
│   ├── alembic/                  # миграции БД
│   ├── app/
│   │   ├── main.py               # FastAPI, подключение роутеров
│   │   ├── config.py             # настройки из .env
│   │   ├── db.py                 # engine, session
│   │   ├── models.py             # SQLAlchemy-модели
│   │   ├── schemas.py            # Pydantic-схемы API
│   │   ├── auth.py               # простая auth (единый токен/логин)
│   │   ├── api/
│   │   │   ├── playlists.py      # роутер плейлистов
│   │   │   ├── library.py        # роутер каталога
│   │   │   ├── matching.py       # роутер матчей
│   │   │   └── download.py       # роутер выдачи файлов
│   │   ├── services/
│   │   │   ├── spotify.py        # OAuth + импорт Spotify
│   │   │   ├── yandex.py         # импорт Яндекс.Музыки
│   │   │   ├── scanner.py        # обход файлов, mutagen
│   │   │   ├── musicbrainz.py    # обогащение, rate limit 1 rps
│   │   │   ├── matcher.py        # каскад матчинга
│   │   │   ├── normalize.py      # нормализация строк
│   │   │   └── delivery.py       # сборка zip, m3u8
│   │   └── workers/
│   │       ├── celery_app.py
│   │       └── tasks.py          # фоновые задачи
├── frontend/
│   ├── index.html                # PWA (манифест, service worker)
│   └── src/                      # React/Vue, 4–5 экранов
└── data/
    └── music/                    # mount point библиотеки (/music/library)
```

---

## 2. Docker Compose (инфраструктура за один файл)

```yaml
services:
  db:        postgres:16          # volume: pgdata
  redis:     redis:7
  backend:   ./backend            # FastAPI + Celery worker + beat
  frontend:  ./frontend           # собранный PWA, раздаёт nginx или сам backend
  # qbittorrent — опционально уже на MVP, для ручного добавления раздач
```

`.env`: `DATABASE_URL`, `REDIS_URL`, `MUSIC_LIBRARY_PATH`, `SPOTIFY_CLIENT_ID/SECRET`, `YANDEX_TOKEN`, `APP_AUTH_TOKEN`, `TZ`.

---

## 3. Схема БД (таблицы MVP)

```
users(id, login, token)
playlist_sources(id, service, access_token, refresh_token, expires_at)
playlists(id, source_id, external_id, name, snapshot_hash, track_count, updated_at)
playlist_items(id, playlist_id, position,
               artist_raw, title_raw, album_raw,
               artist_norm, title_norm, album_norm,
               isrc, duration_ms, external_track_id)
artists(id, name, name_norm, mbid)
albums(id, artist_id, title, title_norm, year, mbid)
tracks(id, album_id, title, title_norm, track_no, disc_no,
       duration_ms, isrc, mbid)
files(id, track_id, path, format, bit_depth, sample_rate,
      size_bytes, sha1, scanned_at)
matches(id, playlist_item_id, track_id, confidence, method,
        status)            # READY | NEEDS_REVIEW | MISSING
jobs(id, type, status, payload, error, created_at, finished_at)
```

Индексы: `files(sha1)`, `tracks(isrc)`, `tracks(title_norm)`, `playlist_items(isrc)`, `matches(playlist_item_id)`.

---

## 4. API-эндпоинты

### Auth
| Метод | Путь | Описание |
|---|---|---|
| POST | `/api/auth/login` | Вход (единый пользователь, выдача JWT) |

### Подключение источников
| Метод | Путь | Описание |
|---|---|---|
| GET | `/api/sources/spotify/connect` | Редирект на Spotify OAuth |
| GET | `/api/sources/spotify/callback` | Callback, сохранение токенов |
| POST | `/api/sources/yandex/connect` | Сохранение токена Яндекса |
| GET | `/api/sources` | Список подключённых источников |

### Плейлисты
| Метод | Путь | Описание |
|---|---|---|
| POST | `/api/playlists/import` | Запуск импорта `{source_id}` → job |
| GET | `/api/playlists` | Список плейлистов + сводка статусов (ready/missing/review, % собранности) |
| GET | `/api/playlists/{id}` | Детали плейлиста |
| GET | `/api/playlists/{id}/items?status=` | Треки плейлиста со статусами матчинга |
| POST | `/api/playlists/{id}/refresh` | Повторный импорт (инкремент по snapshot_hash) |

### Библиотека
| Метод | Путь | Описание |
|---|---|---|
| POST | `/api/library/scan` | Запуск сканера → job |
| GET | `/api/library/stats` | Сводка: файлов, треков, альбомов, объём, форматы |
| GET | `/api/library/albums?q=` | Поиск по каталогу |

### Матчинг
| Метод | Путь | Описание |
|---|---|---|
| POST | `/api/matching/run` | Запуск матчинга (все или `{playlist_id}`) → job |
| GET | `/api/matching/review` | Элементы NEEDS_REVIEW с кандидатами |
| POST | `/api/matching/{match_id}/resolve` | Ручной выбор кандидата / «нет в архиве» |

### Выдача (delivery)
| Метод | Путь | Описание |
|---|---|---|
| GET | `/api/download/track/{item_id}` | Отдать файл (Range-запросы), имя = Artist - Title.flac |
| GET | `/api/download/album/{album_id}` | Zip альбома (store, без сжатия) |
| GET | `/api/download/playlist/{id}?mode=matched` | Zip всех READY-треков + `playlist.m3u8` внутри |
| GET | `/api/download/playlist/{id}/m3u8` | Отдельно m3u8 |

### Система
| Метод | Путь | Описание |
|---|---|---|
| GET | `/api/jobs/{id}` | Статус фоновой задачи (для прогресс-баров в UI) |
| GET | `/api/health` | Проверка живости |

---

## 5. Пошаговый план (4 этапа, ~2 недели)

### Этап 1. Скелет (дни 1–2)
- [ ] Репозиторий, docker-compose: db + redis + backend + frontend
- [ ] SQLAlchemy-модели + первая миграция Alembic
- [ ] FastAPI: `/api/health`, auth, роутеры-заглушки
- [ ] Celery worker + endpoint `/api/jobs/{id}`
- [ ] Монтирование `MUSIC_LIBRARY_PATH` в контейнер
**Готово, когда:** `docker compose up` поднимает всё, `/api/health` отвечает.

### Этап 2. Библиотека (дни 3–5)
- [ ] `scanner.py`: обход директории, фильтр по расширениям (flac, alac, wav, dsf, dff, ape)
- [ ] Чтение тегов mutagen → artists/albums/tracks/files (sha1, формат, bit_depth, sample_rate)
- [ ] `musicbrainz.py`: поиск релиза по artist+album, запись ISRC/MBID, кэш ответов в Redis, rate limit 1 rps
- [ ] Эндпоинты `/api/library/scan`, `/api/library/stats`, `/api/library/albums`
- [ ] Идемпотентность: повторный скан не создаёт дублей (по sha1)
**Готово, когда:** скан реальной папки заполняет каталог, stats показывает верные цифры.

### Этап 3. Импорт плейлистов (дни 6–8)
- [ ] Spotify: OAuth flow, `GET /v1/me/playlists`, `GET /v1/playlists/{id}/tracks` с пагинацией, сохранение ISRC/duration_ms
- [ ] Яндекс.Музыка: `yandex-music-api`, список плейлистов и треков
- [ ] `normalize.py`: NFKC, lower, удаление «(feat. …)», «(remaster)», кавычки/дефисы → норм-ключи
- [ ] Инкрементальный импорт по snapshot_hash (Spotify snapshot_id)
- [ ] Эндпоинты раздела «Плейлисты»
**Готово, когда:** оба сервиса импортируют плейлисты в единую таблицу `playlist_items`.

### Этап 4. Матчинг + выдача + PWA (дни 9–14)
- [ ] `matcher.py`, каскад:
  1. ISRC совпадение (confidence 1.0)
  2. exact: artist_norm + title_norm + album_norm, длительность ±2 с (0.9+)
  3. fuzzy: rapidfuzz token_set_ratio ≥ 85, длительность ±5 с (0.7–0.9) → NEEDS_REVIEW при неоднозначности
  4. иначе MISSING
- [ ] `/api/matching/run`, `/api/matching/review`, `/resolve`
- [ ] `delivery.py`: отдача файла с поддержкой Range; zip (ZIP_STORED) альбома/плейлиста; генерация m3u8 с относительными путями
- [ ] PWA (экраны):
  1. Логин
  2. Список плейлистов (имя, источник, прогресс-бар собранности)
  3. Треки плейлиста (статус-значки READY / REVIEW / MISSING, кнопка скачивания)
  4. Экран review (выбор кандидата из списка)
  5. Кнопки «Скачать плейлист» / «Скачать m3u8»
- [ ] Тест на смартфоне: Tailscale → PWA → скачать zip → открыть в плеере
**Готово, когда:** сквозной сценарий критерия MVP работает от импорта до воспроизведения.

---

## 6. Порядок обработки данных (end-to-end на MVP)

```
Spotify/Yandex OAuth ──▶ playlist_items (raw + norm + isrc)
                                   │
Локальная папка ─▶ scanner ─▶ files/tracks/albums (+ ISRC из MusicBrainz)
                                   │
                        matching/run (каскад)
                                   │
              matches: READY / NEEDS_REVIEW / MISSING
                                   │
            PWA ◀── статусы ──▶ download (file | zip+m3u8)
                                   │
                            Смартфон → плеер
```

---

## 7. Риски и решения на MVP

| Риск | Решение |
|---|---|
| Теги в файлах битые/пустые | Fallback: парсинг имени папки `Artist/Album (Year)` и имени файла `01 - Title.flac` |
| MusicBrainz не находит релиз | Матчинг работает и без ISRC (ступени 2–3 каскада) |
| Яндекс не отдаёт ISRC | Ожидаемо — полагаемся на нормализованный exact/fuzzy |
| Fuzzy даёт ложные совпадения (live/remix) | Порог 85 + штраф за ключевые слова (live, remix, cover) + обязательный review ниже 0.9 |
| Большие zip на смартфоне | Стриминг zip на лету (zipstream), без временных файлов; Range для одиночных треков |
| Долгий первый скан | Всё в фоновых job'ах с прогрессом в `/api/jobs/{id}` |

---

## 8. Исследование OpenSubsonic-интеграции (после MVP acceptance)

Это отдельная research/design-итерация после тега `v0.1.0` и до начала
Release 2. Она не включает реализацию API, миграции БД или замену
текущей PWA.

- [ ] Изучить актуальную спецификацию OpenSubsonic и выделить минимальный
  read-only профиль для каталога, плейлистов, streaming и download.
- [ ] Составить матрицу совместимости клиентов: Symfonium (Android),
  Ultrasonic (Android), Amperfy (iOS/iPadOS) и один desktop-клиент.
- [ ] Проверить методы авторизации: Subsonic token+salt и OpenSubsonic API key;
  не переиспользовать `APP_AUTH_TOKEN` как пароль плеера.
- [ ] Сопоставить OpenSubsonic endpoints с текущей БД и API: `ping`,
  `getMusicFolders`, `getArtists`, `getArtist`, `getAlbum`, `getSong`, `search3`,
  `getPlaylists`, `getPlaylist`, `stream`, `download`, `getCoverArt`.
- [ ] Отдельно изучить поведение импортированных Spotify/Яндекс
  плейлистов: только `READY`-треки, `readonly`, порядок треков и
  обновление «Мне нравится».
- [ ] На реальных устройствах проверить FLAC/Hi-Res, HTTP Range, gapless,
  Unicode-метаданные, большие плейлисты, ручной и автоматический
  offline-cache, повторную синхронизацию и работу через Tailscale HTTPS.
- [ ] Описать модель угроз: учётные данные плеера, отзыв доступа,
  rate limit, пределы Tailscale-сети и запрет публичного Funnel по умолчанию.
- [ ] Создать `docs/open-subsonic-integration-plan.md` с выбранным профилем
  совместимости, картой endpoints, этапами реализации, тестами,
  рисками, оценкой объёма и явными границами scope.

### Security/legal gate: `qobuz-dl`

Репозиторий-кандидат: <https://github.com/vitiko98/qobuz-dl>. Это сторонний
CLI/Python-модуль, а не официальный Qobuz-плагин. До окончания аудита
пакет не устанавливается, не запускается и не получает учётные данные.

- [ ] Зафиксировать конкретный commit/package hash, лицензию, активность
  сопровождения, открытые security-issues и полное дерево зависимостей.
- [ ] Провести code review авторизации, хранения email/password,
  логирования, извлечения app secrets из web bundle, сетевых адресов,
  записи на диск и вызова внешних процессов.
- [ ] Провести отдельный terms/rights review. Допускать лишь файлы,
  которые Qobuz явно разрешает выгружать (например, купленные DRM-free
  релизы). Если такой режим нельзя технически ограничить или нет
  явного разрешения, интеграция отклоняется.
- [ ] Только при успешном gate собрать изолированный прототип:
  без доступа к `.env`, базе, Docker socket и `X:\Music`; с отдельной staging-
  папкой, allowlist сетевых адресов, лимитами объёма/частоты и
  проверкой контейнера, хеша и метаданных до импорта.
- [ ] Не передавать основной Qobuz-пароль стороннему коду. До
  прототипа описать минимально привилегированный способ доступа,
  хранение, ротацию и отзыв секрета; при отсутствии такого способа
  отклонить интеграцию.
- [ ] Создать `docs/qobuz-dl-assessment.md`: таблица рисков, SBOM/аудит
  зависимостей, карта data flow, результаты sandbox-теста и явное
  решение `allow / restrict / reject`.

**Готово, когда:** матрица совместимости подтверждена на реальных
клиентах, выбран минимальный безопасный API-профиль, а план реализации
письменно согласован до изменения кода.

---

## 9. Что подключается в релизе 2 (без переработки MVP)
- Tracker Scraper → пишет в wantlist из `matches(status=MISSING)`
- qBittorrent API: авто-добавление раздач, вебхук завершения → триггер `/api/library/scan`
- Ре-матчинг по расписанию (Celery beat): MISSING → READY
- Telegram-уведомления, дедуп, upgrade-политика, дашборд
