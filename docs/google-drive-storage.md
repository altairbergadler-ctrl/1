# Google Drive: основное хранилище Audiofeel

## Границы итерации

Google Drive хранит единственную постоянную копию файлов каталога. Локальный
диск используется только как временный staging/cache и не является постоянной
библиотекой. Release 2 и torrent/qBittorrent automation не входят в итерацию.

Один OAuth-проект обслуживает несколько Google-аккаунтов. Каждый аккаунт
предоставляет отдельную квоту и папку `Audiofeel Library`. Для нового файла
worker выбирает включённый здоровый аккаунт с достаточным свободным местом;
при временной ошибке переходит к следующему. Это пул ёмкости, а не скрытая
RAID-репликация.

## Подготовка OAuth-приложения

1. Создать отдельный проект в [Google Cloud Console](https://console.cloud.google.com/).
2. Включить [Google Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com).
3. Настроить OAuth consent screen. Если приложение находится в режиме Testing,
   добавить Google-адреса владельца в Test users.
4. Создать OAuth Client ID типа **Web application**.
5. В `Authorized redirect URIs` точно добавить:

   `https://audiofeel.su/api/storage/google/callback`

6. Войти в Audiofeel, открыть `Хранилище`, ввести Client ID и Client secret и
   нажать `Проверить через Google`.
7. После возврата в Audiofeel добавлять остальные аккаунты кнопкой
   `Добавить аккаунт`.

Официальные справочники Google:

- [OAuth 2.0 for web-server applications](https://developers.google.com/identity/protocols/oauth2/web-server);
- [Drive API scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth);
- [resumable uploads](https://developers.google.com/workspace/drive/api/guides/manage-uploads);
- [downloads and HTTP Range](https://developers.google.com/workspace/drive/api/guides/manage-downloads);
- [user and storage quota](https://developers.google.com/workspace/drive/api/guides/user-info).

## Безопасность

- запрашивается ограниченный scope `drive.file`;
- OAuth `state` хранится только как SHA-256 и одноразово потребляется;
- PKCE verifier, Client secret и refresh tokens хранятся в AES-256-GCM
  envelopes;
- ключ шифрования находится вне PostgreSQL и вне Git в Docker secret;
- новый OAuth config становится активным только после успешного входа,
  проверки аккаунта и создания/поиска корневой папки;
- при отказе Google старый config и старые account credentials не меняются;
- access token живёт только в памяти процесса;
- ответы `/api/storage*` имеют `Cache-Control: no-store`;
- Uvicorn access log отключён, чтобы callback query с authorization code не
  оказался в журнале;
- PWA не записывает credentials в localStorage/sessionStorage и никогда не
  заполняет секретные поля сохранёнными значениями.

## Целостность и выдача

Загрузка выполняется resumable-сессией с повторным запросом её состояния после
обрыва. Объект активируется только после совпадения размера и Google
`sha1Checksum`. SHA-1 здесь является проверкой побитовой идентичности уже
доверенного файла, а не механизмом аутентификации.

Удаление локального источника выполняется только после отдельного commit
проверенного remote location в PostgreSQL. Перед удалением локальный файл ещё
раз сверяется по размеру и SHA-1. Ошибка загрузки, проверки или фиксации в БД
сохраняет локальный источник для следующей плановой попытки.

Одиночный remote-only трек передаётся потоково с поддержкой HTTP Range. Для ZIP
remote-only файлы сначала скачиваются во временный ограниченный кэш, повторно
проверяются по размеру и SHA-1, после чего архивируются без перекодирования и
сжатия (`ZIP_STORED`). Временный каталог удаляется после ответа; оставшиеся
после аварий каталоги старше `STORAGE_CACHE_TTL_SECONDS` удаляет плановая
сверка.

## Миграция существующей библиотеки

Кнопка `Перенести временные файлы` создаёт фоновое задание. Оно загружает ещё
не перенесённые файлы, фиксирует проверенный remote location и удаляет
локальный источник. Старые локальные копии уже подтверждённых объектов также
очищаются. Ошибки остаются в безопасных счётчиках без credentials, а файл при
ошибке сохраняется для повторной попытки. Плановая задача повторяет сверку
каждые `STORAGE_RECONCILE_INTERVAL_SECONDS`. Повторный запуск идемпотентен
благодаря Drive `appProperties` с SHA-1 и уникальному location.

## Критерии приёмки

- Alembic head `0006_google_drive_storage` на PostgreSQL;
- OAuth success, denial, expired/replayed state и сохранение старого config при
  неудачной проверке;
- отсутствие Client secret/refresh/access token в API, логах и PWA storage;
- два аккаунта, выбор по свободной квоте и failover при ошибке первого;
- resumable retry после оборванного chunk и проверка size/SHA-1;
- remote-only Range возвращает `206`, а Drive ZIP побитово верен;
- плановая и ручная проверка health;
- локальный оригинал удаляется только после remote verification и DB commit;
- при ошибке до DB commit локальный оригинал сохраняется;
- плановая сверка повторяет незавершённые переносы и очищает просроченный кэш;
- полный pytest, Docker migration, Celery pong и PWA runtime acceptance.
