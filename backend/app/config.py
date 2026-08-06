from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://music:music@db:5432/music"
    redis_url: str = "redis://redis:6379/0"
    music_library_path: str = "/music/library"
    app_auth_token: str = "change-me"

    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    spotify_redirect_uri: str = ""

    yandex_token: str = ""


settings = Settings()
