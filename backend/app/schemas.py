import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator


class HealthOut(BaseModel):
    status: str = "ok"


class LoginIn(BaseModel):
    token: str


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    type: str
    status: str
    payload: dict | None = None
    error: str | None = None
    created_at: datetime
    heartbeat_at: datetime
    finished_at: datetime | None = None

    @field_validator("payload", mode="before")
    @classmethod
    def parse_payload(cls, value):
        if value is None or isinstance(value, dict):
            return value
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {"raw": str(value)}
        return parsed if isinstance(parsed, dict) else {"value": parsed}


class FormatStatsOut(BaseModel):
    files: int
    bytes: int


class LibraryStatsOut(BaseModel):
    files: int
    tracks: int
    albums: int
    bytes: int
    formats: dict[str, FormatStatsOut]


class AlbumArtistOut(BaseModel):
    id: int
    name: str
    mbid: str | None = None


class LibraryAlbumOut(BaseModel):
    id: int
    title: str
    year: int | None = None
    mbid: str | None = None
    artist: AlbumArtistOut
    tracks: int
    files: int
    bytes: int


class LibraryAlbumsOut(BaseModel):
    items: list[LibraryAlbumOut]
    total: int


class PlaylistOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    track_count: int
