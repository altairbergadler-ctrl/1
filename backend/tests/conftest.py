from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ["DATABASE_URL"] = "sqlite+pysqlite://"
os.environ["REDIS_URL"] = "redis://localhost:6379/15"
os.environ["MUSIC_LIBRARY_PATH"] = "./test-music"
os.environ["APP_AUTH_TOKEN"] = "test-auth-token-12345"
os.environ["AUTH_COOKIE_SECURE"] = "false"
os.environ["MUSICBRAINZ_ENABLED"] = "false"
os.environ["CELERY_TASK_ALWAYS_EAGER"] = "false"

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture()
def db(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def api_client(session_factory):
    from fastapi.testclient import TestClient

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(scope="session")
def auth_headers():
    return {"Authorization": "Bearer test-auth-token-12345"}


@pytest.fixture(scope="session")
def ffmpeg_binary() -> str:
    executable = os.environ.get("FFMPEG_BINARY") or shutil.which("ffmpeg")
    if not executable:
        pytest.fail(
            "ffmpeg is required to generate the three FLAC fixtures; "
            "set FFMPEG_BINARY or install ffmpeg"
        )
    return executable


def _generate_flac(
    ffmpeg: str,
    path: Path,
    *,
    frequency: int,
    sample_rate: int,
    metadata: dict[str, str] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={frequency}:duration=0.12",
        "-ar",
        str(sample_rate),
        "-c:a",
        "flac",
    ]
    for key, value in (metadata or {}).items():
        command.extend(["-metadata", f"{key}={value}"])
    command.append(str(path))
    subprocess.run(command, check=True, capture_output=True, text=True)


@pytest.fixture()
def three_flac_library(tmp_path, ffmpeg_binary):
    root = tmp_path / "library"
    _generate_flac(
        ffmpeg_binary,
        root / "Tagged Artist" / "Tagged Album (2024)" / "01 - First Track.flac",
        frequency=440,
        sample_rate=44100,
        metadata={
            "artist": "Tagged Artist",
            "album": "Tagged Album",
            "title": "First Track",
            "date": "2024",
            "track": "1/2",
            "disc": "1/1",
            "isrc": "USAAA2400001",
        },
    )
    _generate_flac(
        ffmpeg_binary,
        root / "Tagged Artist" / "Tagged Album (2024)" / "02 - Second Track.flac",
        frequency=550,
        sample_rate=48000,
        metadata={
            "artist": "Tagged Artist",
            "album": "Tagged Album",
            "title": "Second Track",
            "date": "2024",
            "track": "2/2",
            "disc": "1/1",
        },
    )
    _generate_flac(
        ffmpeg_binary,
        root / "Fallback Artist" / "Fallback Album (2001)" / "03 - Fallback Title.flac",
        frequency=660,
        sample_rate=96000,
    )
    return root
