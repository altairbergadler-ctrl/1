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

    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    spotify_redirect_uri: str = ""

    yandex_token: str = ""

    musicbrainz_enabled: bool = True
    musicbrainz_base_url: str = "https://musicbrainz.org/ws/2"
    musicbrainz_user_agent: str = ""
    musicbrainz_cache_ttl_seconds: int = 30 * 24 * 60 * 60
    musicbrainz_negative_cache_ttl_seconds: int = 24 * 60 * 60
    musicbrainz_rate_limit_seconds: float = 1.0

    celery_task_always_eager: bool = False
    celery_visibility_timeout_seconds: int = Field(default=24 * 60 * 60, ge=3600)
    scan_job_stale_seconds: int = Field(default=6 * 60 * 60, ge=60)


settings = Settings()
