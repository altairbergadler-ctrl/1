import json

import pytest
from sqlalchemy import select

from app.models import Job, Playlist, PlaylistItem, PlaylistSource, ServiceEnum
from app.services.playlist_converter import (
    PlaylistConversionError,
    convert_playlist_content,
)


def test_converter_reads_spotify_style_csv_metadata():
    content = """Track URI,Track Name,Artist Name(s),Album Name,ISRC,Track Duration (ms)
spotify:track:4uLU6hMCjMI75M1A2tKUQC,Never Gonna Give You Up,Rick Astley,Whenever You Need Somebody,GBARL9300135,213573
"""

    converted = convert_playlist_content(content)

    assert converted.format == "csv"
    assert converted.skipped == 0
    assert len(converted.tracks) == 1
    track = converted.tracks[0]
    assert track.artist == "Rick Astley"
    assert track.title == "Never Gonna Give You Up"
    assert track.album == "Whenever You Need Somebody"
    assert track.isrc == "GBARL9300135"
    assert track.duration_ms == 213573
    assert track.external_track_id == "4uLU6hMCjMI75M1A2tKUQC"


def test_converter_reads_extended_m3u_and_plain_text():
    m3u = convert_playlist_content(
        "#EXTM3U\n#EXTINF:201,Artist One - First Song\n/music/first.flac\n"
    )
    text = convert_playlist_content(
        "Artist Two — Second Song\nArtist Three\tThird Song\tThird Album\nbroken row"
    )

    assert m3u.format == "m3u"
    assert [(track.artist, track.title, track.duration_ms) for track in m3u.tracks] == [
        ("Artist One", "First Song", 201000)
    ]
    assert text.format == "text"
    assert text.skipped == 1
    assert [(track.artist, track.title, track.album) for track in text.tracks] == [
        ("Artist Two", "Second Song", ""),
        ("Artist Three", "Third Song", "Third Album"),
    ]


@pytest.mark.parametrize(
    "content",
    [
        "not enough metadata",
        "Title,Album\nSong,Album",
    ],
)
def test_converter_rejects_content_without_artist_and_title(content):
    with pytest.raises(PlaylistConversionError):
        convert_playlist_content(content)


def test_content_import_creates_manual_playlist_and_queues_matching(
    api_client, auth_headers, db, monkeypatch
):

    response = api_client.post(
        "/api/playlists/import-content",
        json={
            "name": "Shared list",
            "format": "auto",
            "content": "Artist One — First Song\nArtist Two — Second Song",
        },
        headers=auth_headers,
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["imported"] == 2
    assert payload["skipped"] == 0
    assert payload["format"] == "text"
    playlist = db.get(Playlist, payload["playlist_id"])
    source = db.get(PlaylistSource, playlist.source_id)
    items = list(
        db.scalars(
            select(PlaylistItem)
            .where(PlaylistItem.playlist_id == playlist.id)
            .order_by(PlaylistItem.position)
        )
    )
    job = db.get(Job, payload["matching_job"]["id"])
    assert payload["acquisition_job"]["id"] == payload["matching_job"]["id"]
    assert source.service == ServiceEnum.manual
    assert playlist.track_count == 2
    assert playlist.external_id.startswith("manual:")
    assert [(item.artist_raw, item.title_raw) for item in items] == [
        ("Artist One", "First Song"),
        ("Artist Two", "Second Song"),
    ]
    assert job.type == "acquisition_workflow"
    assert json.loads(job.payload)["update_quality"] is False


def test_manual_source_is_connected_but_cannot_run_provider_import(
    api_client, auth_headers, db, monkeypatch
):
    queued = []
    monkeypatch.setattr(
        "app.api.matching.run_matching_task.delay",
        lambda *args: queued.append(args),
    )
    imported = api_client.post(
        "/api/playlists/import-content",
        json={"name": "List", "content": "Artist — Song", "format": "text"},
        headers=auth_headers,
    )
    source = db.scalar(
        select(PlaylistSource).where(PlaylistSource.service == ServiceEnum.manual)
    )

    listing = api_client.get("/api/sources", headers=auth_headers)
    provider_import = api_client.post(
        "/api/playlists/import",
        json={"source_id": source.id},
        headers=auth_headers,
    )
    refresh = api_client.post(
        f"/api/playlists/{imported.json()['playlist_id']}/refresh",
        headers=auth_headers,
    )

    manual_source = next(
        item for item in listing.json()["items"] if item["service"] == "manual"
    )
    assert manual_source["connected"] is True
    assert provider_import.status_code == 422
    assert refresh.status_code == 422
