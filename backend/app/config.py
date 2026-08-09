from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str
    redis_url: str
    music_library_path: str
    app_auth_token: str = Field(min_length=16)
    timezone: str = Field(default="UTC", validation_alias="TZ")
    auth_cookie_max_age_seconds: int = Field(default=30 * 24 * 60 * 60, ge=300)
    auth_cookie_secure: bool = False

    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    spotify_redirect_uri: str = ""
    spotify_oauth_state_ttl_seconds: int = Field(default=10 * 60, ge=60, le=3600)

    yandex_token: str = ""

    # Qobuz downloads run under the RESTRICT rules of docs/qobuz-dl-assessment.md.
    #
    # Все поля настраиваются через env (секреты только здесь — assessment §5,
    # §7: в БД ничего не пишем, API секреты не возвращает).

    # Главный выключатель интеграции. По умолчанию выключена: пакет и
    # креденшелы активируются только осознанным действием владельца.
    qobuz_enabled: bool = False
    # Fallback-авторизация (email + пароль). Пароль наружу уходит только как
    # MD5-хеш по HTTPS — ровно как делает CLI апстрима (assessment §3.1).
    # Сейчас обычно пустые: классический user/login у Qobuz сломан (см. ниже).
    qobuz_email: str = ""
    qobuz_password: str = ""
    # Browser session token workaround: Qobuz moved login to OAuth, so the
    # classic email+password user/login flow of qobuz-dl can return 401 even
    # with valid credentials. A user_auth_token extracted from play.qobuz.com
    # localStorage works instead (docs/qobuz-dl-assessment.md, addendum).
    #
    # Основной способ авторизации (addendum, раздел 8 assessment): токен
    # браузерной сессии и id пользователя из Local Storage play.qobuz.com
    # (ключ localuser). Токен — секрет того же класса, что пароль: только
    # .env, не логируется, не возвращается API. Протухает со временем —
    # признак: 400/401 от connect/search, лечение: повторное извлечение.
    qobuz_auth_token: str = ""
    qobuz_user_id: str = ""
    # 5=MP3, 6=16/44.1, 7=24/<96kHz, 27=24/>96kHz (falls back to availability)
    #
    # Запрошенное качество скачивания. 27 (24 бит / >96 кГц) по умолчанию —
    # максимум Hi-Res; при недоступности релиза в этом качестве downloader
    # работает с downgrade_quality=True и берёт лучшее доступное.
    qobuz_quality: int = 27
    # Каталог staging внутри контейнера (bind-mount QOBUZ_STAGING_HOST_PATH).
    # Скачанное пишется ТОЛЬКО сюда; в библиотеку — после верификации
    # (assessment §3.5, §7 п. 3).
    qobuz_staging_path: str = "/music/staging"
    # Анти-бан лимиты (assessment §5, §7 п. 4): не более N треков за один
    # запуск fetch-missing и пауза между скачиваниями, чтобы не выглядеть
    # как скрапер. 25 треков и 1 секунда — консервативные дефолты.
    qobuz_max_tracks_per_run: int = Field(default=25, ge=1, le=500)
    qobuz_request_delay_seconds: float = Field(default=1.0, ge=0)
    # Stale-таймаут задания докачки: если heartbeat молчит дольше, API
    # считает job зависшим (у qobuz-dl есть requests-вызовы без timeout —
    # assessment §3.3, дефект 1) и помечает failed.
    qobuz_download_job_stale_seconds: int = Field(default=6 * 60 * 60, ge=60)
    # Встраивать обложку альбома в теги FLAC/MP3 (mutagen, внутри файла —
    # никаких посторонних запросов кроме static.qobuz.com, assessment §3.4).
    qobuz_embed_art: bool = True

    musicbrainz_enabled: bool = True
    musicbrainz_base_url: str = "https://musicbrainz.org/ws/2"
    musicbrainz_user_agent: str = ""
    musicbrainz_cache_ttl_seconds: int = 30 * 24 * 60 * 60
    musicbrainz_negative_cache_ttl_seconds: int = 24 * 60 * 60
    musicbrainz_rate_limit_seconds: float = 1.0

    celery_task_always_eager: bool = False
    celery_visibility_timeout_seconds: int = Field(default=24 * 60 * 60, ge=3600)
    scan_job_stale_seconds: int = Field(default=6 * 60 * 60, ge=60)
    playlist_import_job_stale_seconds: int = Field(default=6 * 60 * 60, ge=60)
    matching_job_stale_seconds: int = Field(default=6 * 60 * 60, ge=60)


settings = Settings()
