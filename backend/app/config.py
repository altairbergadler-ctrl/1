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
    release_sha: str = Field(
        default="dev", pattern="^(?:[0-9a-f]{40}|dev|bootstrap)$"
    )
    timezone: str = Field(default="UTC", validation_alias="TZ")
    public_origin: str = "http://127.0.0.1:8080"
    auth_key_file: str = "/run/secrets/auth_key"
    auth_cookie_max_age_seconds: int = Field(default=30 * 24 * 60 * 60, ge=300)
    auth_session_idle_seconds: int = Field(default=7 * 24 * 60 * 60, ge=300)
    auth_session_touch_interval_seconds: int = Field(default=5 * 60, ge=30)
    auth_recovery_max_age_seconds: int = Field(default=15 * 60, ge=60, le=3600)
    auth_recovery_idle_seconds: int = Field(default=5 * 60, ge=60, le=1800)
    auth_cookie_secure: bool = False
    opensubsonic_auth_touch_interval_seconds: int = Field(default=5 * 60, ge=30)
    opensubsonic_failed_attempts: int = Field(default=10, ge=1, le=1000)
    opensubsonic_failed_window_seconds: int = Field(default=5 * 60, ge=30)
    opensubsonic_success_requests_per_minute: int = Field(default=1200, ge=60)
    opensubsonic_artwork_cache_path: str = "/music/cache/artwork"
    opensubsonic_artwork_cache_max_bytes: int = Field(
        default=256 * 1024 * 1024, ge=16 * 1024 * 1024
    )
    opensubsonic_artwork_cache_ttl_seconds: int = Field(default=7 * 24 * 60 * 60, ge=3600)
    opensubsonic_artwork_max_input_bytes: int = Field(default=20 * 1024 * 1024, ge=1024)
    opensubsonic_artwork_remote_prefix_bytes: int = Field(
        default=5 * 1024 * 1024, ge=64 * 1024, le=32 * 1024 * 1024
    )
    opensubsonic_artwork_max_pixels: int = Field(default=40_000_000, ge=1_000_000)
    opensubsonic_artwork_max_output_pixels: int = Field(default=2048, ge=64, le=4096)

    google_login_client_id: str = ""
    google_login_client_secret_file: str = "/run/secrets/google_login_client_secret"
    google_login_redirect_uri: str = "https://audiofeel.su/api/auth/google/callback"
    google_login_state_ttl_seconds: int = Field(default=10 * 60, ge=60, le=3600)
    google_login_clock_skew_seconds: int = Field(default=60, ge=0, le=300)
    google_login_max_token_age_seconds: int = Field(default=10 * 60, ge=60, le=3600)

    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    spotify_redirect_uri: str = ""
    spotify_oauth_state_ttl_seconds: int = Field(default=10 * 60, ge=60, le=3600)

    yandex_token: str = ""
    provider_credential_key_file: str = "/run/secrets/provider_credential_key"
    qobuz_credential_key_file: str = "/run/secrets/qobuz_credential_key"
    provider_health_interval_seconds: int = Field(default=30 * 60, ge=60)
    provider_health_stale_seconds: int = Field(default=90 * 60, ge=120)
    provider_health_manual_cooldown_seconds: int = Field(default=60, ge=10)

    # Google Drive is the primary durable object store. Local library files are
    # temporary upload sources only and are evicted after the verified remote
    # location has been committed.
    storage_primary_backend: str = Field(
        default="google_drive", pattern="^(google_drive|local)$"
    )
    storage_cache_path: str = "/music/cache"
    storage_cache_max_bytes: int = Field(
        default=20 * 1024 * 1024 * 1024, ge=64 * 1024 * 1024
    )
    storage_cache_ttl_seconds: int = Field(default=24 * 60 * 60, ge=15 * 60)
    storage_reconcile_interval_seconds: int = Field(default=15 * 60, ge=60)
    google_drive_redirect_uri: str = (
        "https://audiofeel.su/api/storage/google/callback"
    )
    google_drive_oauth_state_ttl_seconds: int = Field(default=10 * 60, ge=60, le=3600)
    google_drive_connect_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    google_drive_read_timeout_seconds: float = Field(default=5 * 60, ge=30, le=3600)
    google_drive_upload_chunk_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=256 * 1024,
        le=256 * 1024 * 1024,
        multiple_of=256 * 1024,
    )
    google_drive_upload_retry_attempts: int = Field(default=5, ge=1, le=10)
    google_drive_min_free_bytes: int = Field(
        default=1024 * 1024 * 1024, ge=0
    )
    # Lossless file-info signing is isolated in an internal sidecar. The
    # Python app keeps the OAuth token, validates FLAC and never accepts lossy
    # fallback responses.
    yandex_download_enabled: bool = False
    yandex_signer_url: str = "http://yandex-signer:8091"
    yandex_internal_token: str = ""
    yandex_staging_path: str = "/music/staging/yandex"
    yandex_max_tracks_per_run: int = Field(default=25, ge=1, le=500)
    yandex_request_delay_seconds: float = Field(default=1.0, ge=0)
    yandex_batch_delay_seconds: float = Field(default=30.0, ge=0, le=3600)
    yandex_download_job_stale_seconds: int = Field(default=6 * 60 * 60, ge=60)
    yandex_connect_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    yandex_read_timeout_seconds: float = Field(default=5 * 60, ge=30, le=3600)
    yandex_max_file_bytes: int = Field(default=512 * 1024 * 1024, ge=1024 * 1024)

    # Qobuz downloads run under the RESTRICT rules of
    # docs/qobuz-dl-assessment.md. The main application keeps no plaintext
    # Qobuz credential in its environment and sends only an encrypted envelope
    # to the private sidecar API.
    qobuz_enabled: bool = False
    qobuz_sidecar_url: str = "http://qobuz-sidecar:8090"
    qobuz_internal_token: str = ""
    # 5=MP3, 6=16/44.1, 7=24/<96kHz, 27=24/>96kHz (falls back to availability)
    #
    # Запрошенное качество скачивания. 27 (24 бит / >96 кГц) по умолчанию —
    # максимум Hi-Res; при недоступности релиза в этом качестве downloader
    # работает с downgrade_quality=True, но worker импортирует только FLAC.
    qobuz_quality: int = 27
    # Каталог staging внутри контейнера (bind-mount QOBUZ_STAGING_HOST_PATH).
    # Скачанное пишется ТОЛЬКО сюда; в библиотеку — после верификации
    # (assessment sections 2 and 6).
    qobuz_staging_path: str = "/music/staging"
    # Operational limits (assessment section 7): весь плейлист проходит одним
    # sweep, разбитым на пакеты по N треков. Между запросами и пакетами есть
    # отдельные паузы, чтобы не выглядеть как скрапер.
    qobuz_max_tracks_per_run: int = Field(default=25, ge=1, le=500)
    qobuz_request_delay_seconds: float = Field(default=1.0, ge=0)
    qobuz_batch_delay_seconds: float = Field(default=30.0, ge=0, le=3600)
    # Never start another Qobuz batch when the filesystem has less headroom.
    # Every completed batch is drained to durable storage before rechecking it.
    qobuz_min_free_bytes: int = Field(
        default=5 * 1024 * 1024 * 1024, ge=512 * 1024 * 1024
    )
    # Stale timeout remains a second line of defence after the sidecar's
    # mandatory connect/read timeouts.
    qobuz_download_job_stale_seconds: int = Field(default=6 * 60 * 60, ge=60)
    # Встраивать обложку альбома в теги FLAC/MP3 (mutagen, внутри файла —
    # никаких посторонних запросов кроме static.qobuz.com; assessment section 4).
    qobuz_embed_art: bool = True
    qobuz_sidecar_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    qobuz_sidecar_read_timeout_seconds: float = Field(default=15 * 60, ge=30, le=3600)

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
