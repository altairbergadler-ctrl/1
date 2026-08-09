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
    qobuz_enabled: bool = False
    qobuz_email: str = ""
    qobuz_password: str = ""
    # Browser session token workaround: Qobuz moved login to OAuth, so the
    # classic email+password user/login flow of qobuz-dl can return 401 even
    # with valid credentials. A user_auth_token extracted from play.qobuz.com
    # localStorage works instead (docs/qobuz-dl-assessment.md, addendum).
    qobuz_auth_token: str = ""
    qobuz_user_id: str = ""
    # 5=MP3, 6=16/44.1, 7=24/<96kHz, 27=24/>96kHz (falls back to availability)
    qobuz_quality: int = 27
    qobuz_staging_path: str = "/music/staging"
    qobuz_max_tracks_per_run: int = Field(default=25, ge=1, le=500)
    qobuz_request_delay_seconds: float = Field(default=1.0, ge=0)
    qobuz_download_job_stale_seconds: int = Field(default=6 * 60 * 60, ge=60)
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
