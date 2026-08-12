from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import (
    Job,
    JobScope,
    JobStatus,
    Match,
    MatchStatus,
    Playlist,
    PlaylistItem,
    PlaylistSource,
    ProviderAttempt,
    ServiceEnum,
    utcnow,
)
from app.services import qobuz as qobuz_service
from app.services.credentials import save_credential
from app.services.normalize import normalize_album, normalize_artist, normalize_title
from app.services.qobuz import (
    QobuzAuthError,
    QobuzConfigurationError,
    QobuzProviderError,
    QobuzSearchCandidate,
    choose_track_candidate,
    fetch_missing_tracks,
    import_files_to_library,
    mark_downloads_stored,
    provider_lookup_key,
    qobuz_download_eligibility,
    select_best_track_candidate,
    search_albums,
    search_tracks,
    verify_staging_files,
)
from app.workers.tasks import qobuz_download_task
from tests.helpers import ensure_user


def _configure_qobuz(monkeypatch, *, enabled=True):
    monkeypatch.setattr("app.config.settings.qobuz_enabled", enabled)
    monkeypatch.setattr(
        "app.config.settings.qobuz_sidecar_url", "http://qobuz-sidecar.invalid"
    )
    monkeypatch.setattr(
        "app.config.settings.qobuz_internal_token", "test-sidecar-token" * 2
    )
    monkeypatch.setattr("app.api.qobuz.has_credential", lambda _db, _provider: True)


class FakeQobuzClient:
    label = "Studio"

    def __init__(self, tracks=None, albums=None):
        self._tracks = list(tracks or [])
        self._albums = list(albums or [])

    def search_tracks(self, query, limit):
        return {"tracks": {"items": self._tracks[:limit]}}

    def search_albums(self, query, limit):
        return {"albums": {"items": self._albums[:limit]}}


def _track_payload(track_id, title, artist="Tagged Artist", duration=187, album="Tagged Album"):
    return {
        "id": track_id,
        "title": title,
        "duration": duration,
        "isrc": "USAAA2400001",
        "hires_streamable": True,
        "performer": {"name": artist},
        "album": {"title": album},
    }


def _write_flac(ffmpeg: str, path: Path, *, frequency: int, metadata: dict[str, str]):
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
        "44100",
        "-c:a",
        "flac",
    ]
    for key, value in metadata.items():
        command.extend(["-metadata", f"{key}={value}"])
    command.append(str(path))
    subprocess.run(command, check=True, capture_output=True, text=True)


# --- status / connect ---------------------------------------------------------


def test_status_reports_disabled_and_unconfigured(api_client, auth_headers):
    response = api_client.get("/api/qobuz/status", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "enabled": False,
        "configured": False,
        "quality": 27,
        "max_tracks_per_run": 25,
        "batch_delay_seconds": 30.0,
    }


def test_status_reports_configured_without_leaking_secrets(
    api_client, auth_headers, monkeypatch
):
    _configure_qobuz(monkeypatch)
    monkeypatch.setattr("app.api.qobuz._sidecar_status", lambda: {"configured": True})

    response = api_client.get("/api/qobuz/status", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["configured"] is True
    assert "password" not in json.dumps(body).lower()
    assert "secret" not in body.values()


def test_connect_success_returns_label(api_client, auth_headers, monkeypatch):
    _configure_qobuz(monkeypatch)
    monkeypatch.setattr(
        "app.api.qobuz.create_qobuz_client", lambda _db: FakeQobuzClient()
    )

    response = api_client.post("/api/qobuz/connect", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"connected": True, "label": "Studio"}


def test_connect_rejected_credentials_returns_400(
    api_client, auth_headers, monkeypatch
):
    _configure_qobuz(monkeypatch)

    def raise_auth(_db):
        raise QobuzAuthError("rejected")

    monkeypatch.setattr("app.api.qobuz.create_qobuz_client", raise_auth)
    response = api_client.post("/api/qobuz/connect", headers=auth_headers)

    assert response.status_code == 400


def test_connect_unconfigured_returns_503(api_client, auth_headers, monkeypatch):
    _configure_qobuz(monkeypatch, enabled=False)
    monkeypatch.setattr(
        "app.api.qobuz.create_qobuz_client",
        lambda: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    response = api_client.post("/api/qobuz/connect", headers=auth_headers)

    assert response.status_code == 503


def test_qobuz_endpoints_require_auth(api_client):
    assert api_client.get("/api/qobuz/status").status_code == 401
    assert api_client.post("/api/qobuz/connect").status_code == 401
    assert api_client.get("/api/qobuz/search?q=x").status_code == 401
    assert api_client.get("/api/qobuz/download-status/1").status_code == 401
    assert api_client.get("/api/qobuz/download-eligibility/1").status_code == 401
    assert api_client.post("/api/qobuz/download-url", json={"url": "u"}).status_code == 401
    assert (
        api_client.post("/api/qobuz/fetch-missing", json={"playlist_id": 1}).status_code
        == 401
    )


# --- search service mapping -----------------------------------------------------


def test_search_tracks_maps_qobuz_response():
    client = FakeQobuzClient(tracks=[_track_payload(5966783, "First Track")])

    candidates = search_tracks(client, "tagged first", limit=5)

    assert candidates == [
        QobuzSearchCandidate(
            qobuz_id="5966783",
            artist="Tagged Artist",
            title="First Track",
            album="Tagged Album",
            duration_ms=187_000,
            isrc="USAAA2400001",
            hires=True,
            url="https://play.qobuz.com/track/5966783",
        )
    ]


def test_search_albums_maps_qobuz_response():
    client = FakeQobuzClient(
        albums=[
            {
                "id": "abc123",
                "title": "Tagged Album",
                "duration": 3600,
                "hires_streamable": False,
                "artist": {"name": "Tagged Artist"},
            }
        ]
    )

    candidates = search_albums(client, "tagged", limit=5)

    assert len(candidates) == 1
    album = candidates[0]
    assert album.qobuz_id == "abc123"
    assert album.title == "Tagged Album"
    assert album.artist == "Tagged Artist"
    assert album.url == "https://play.qobuz.com/album/abc123"
    assert album.hires is False
    assert album.duration_ms == 3_600_000


def test_search_endpoint_maps_items(api_client, auth_headers, monkeypatch):
    _configure_qobuz(monkeypatch)
    client = FakeQobuzClient(tracks=[_track_payload(42, "First Track")])
    monkeypatch.setattr("app.api.qobuz.create_qobuz_client", lambda _db: client)

    response = api_client.get(
        "/api/qobuz/search?q=tagged&type=track&limit=5", headers=auth_headers
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "track"
    assert items[0]["qobuz_id"] == "42"
    assert items[0]["hires"] is True


def test_search_endpoint_provider_error_returns_502(
    api_client, auth_headers, monkeypatch
):
    _configure_qobuz(monkeypatch)

    class BrokenClient(FakeQobuzClient):
        def search_tracks(self, query, limit):
            raise RuntimeError("network down")

    monkeypatch.setattr("app.api.qobuz.create_qobuz_client", lambda _db: BrokenClient())
    response = api_client.get("/api/qobuz/search?q=x", headers=auth_headers)

    assert response.status_code == 502


# --- candidate selection --------------------------------------------------------


def _candidate(title, artist="Tagged Artist", duration_ms=187_000, **overrides):
    values = {
        "qobuz_id": "1",
        "artist": artist,
        "title": title,
        "album": "Tagged Album",
        "duration_ms": duration_ms,
        "isrc": None,
        "hires": True,
        "url": "https://play.qobuz.com/track/1",
    }
    values.update(overrides)
    return QobuzSearchCandidate(
        **values,
    )


def test_select_best_track_candidate_picks_best_fuzzy_match():
    best = select_best_track_candidate(
        artist_raw="Tagged Artist",
        title_raw="First Track",
        album_raw="Different Album",
        duration_ms=187_000,
        candidates=[_candidate("Totally Different Song"), _candidate("First Track")],
    )

    assert best is not None
    assert best.title == "First Track"


def test_select_best_track_candidate_enforces_threshold():
    best = select_best_track_candidate(
        artist_raw="Unknown Artist",
        title_raw="Unknown Title",
        duration_ms=187_000,
        candidates=[_candidate("First Track")],
    )

    assert best is None


def test_select_best_track_candidate_enforces_duration_tolerance():
    within = _candidate("First Track", duration_ms=190_000)
    outside = _candidate("First Track", duration_ms=200_000)

    best = select_best_track_candidate(
        artist_raw="Tagged Artist",
        title_raw="First Track",
        duration_ms=187_000,
        candidates=[outside, within],
    )

    assert best is within

    none_found = select_best_track_candidate(
        artist_raw="Tagged Artist",
        title_raw="First Track",
        duration_ms=187_000,
        candidates=[outside],
    )
    assert none_found is None


def test_select_best_track_candidate_rejects_wrong_version_markers():
    candidate = _candidate("First Track (Live)")

    best = select_best_track_candidate(
        artist_raw="Tagged Artist",
        title_raw="First Track",
        album_raw="Tagged Album",
        duration_ms=187_000,
        candidates=[candidate],
    )

    assert best is None


def test_select_best_track_candidate_prefers_highest_quality_for_same_isrc():
    low = _candidate(
        "First Track (Live)",
        qobuz_id="low",
        isrc="USBBB2400002",
        maximum_bit_depth=16,
        maximum_sampling_rate=44,
    )
    high = replace(
        low,
        qobuz_id="high",
        maximum_bit_depth=24,
        maximum_sampling_rate=192,
    )

    selected, method = choose_track_candidate(
        artist_raw="Tagged Artist",
        title_raw="First Track",
        album_raw="Tagged Album",
        isrc="US-BBB-24-00002",
        duration_ms=187_000,
        candidates=[low, high],
    )

    assert selected is high
    assert method == "isrc"


def test_select_best_track_candidate_rejects_distinct_exact_recordings():
    first = _candidate("First Track", qobuz_id="1", isrc="USBBB2400001")
    second = replace(first, qobuz_id="2", isrc="USBBB2400002")

    selected, method = choose_track_candidate(
        artist_raw="Tagged Artist",
        title_raw="First Track",
        album_raw="Tagged Album",
        isrc=None,
        duration_ms=187_000,
        candidates=[first, second],
    )

    assert selected is None
    assert method == "ambiguous"


def test_select_best_track_candidate_rejects_ambiguous_fuzzy_results():
    first = _candidate("First Trak", artist="Tagged Artist")
    second = replace(first, qobuz_id="2", isrc="USBBB2400002")

    best = select_best_track_candidate(
        artist_raw="Tagged Artist",
        title_raw="First Track",
        album_raw="Tagged Album",
        duration_ms=187_000,
        candidates=[first, second],
    )

    assert best is None


def test_fetch_missing_persists_queued_searching_and_downloading_states(
    db, monkeypatch, tmp_path
):
    user = ensure_user(db)
    source = PlaylistSource(user_id=user.id, service=ServiceEnum.spotify)
    playlist = Playlist(
        source=source, user_id=user.id, external_id="progress", name="Progress"
    )
    item = PlaylistItem(
        playlist=playlist,
        position=0,
        artist_raw="Tagged Artist",
        title_raw="First Track",
        album_raw="Tagged Album",
        artist_norm=normalize_artist("Tagged Artist"),
        title_norm=normalize_title("First Track"),
        album_norm=normalize_album("Tagged Album"),
        duration_ms=187_000,
    )
    db.add_all([source, playlist, item])
    db.flush()
    db.add(
        Match(
            playlist_item_id=item.id,
            status=MatchStatus.missing,
            confidence=0.0,
            method="none",
        )
    )
    db.commit()
    monkeypatch.setattr("app.config.settings.qobuz_request_delay_seconds", 0)
    monkeypatch.setattr("app.config.settings.qobuz_max_tracks_per_run", 25)
    target = tmp_path / "First Track.flac"
    target.write_bytes(b"audio")
    monkeypatch.setattr(
        "app.services.qobuz.download_track_to_staging",
        lambda *args, **kwargs: [target],
    )
    snapshots: list[dict] = []
    drained: list[list[Path]] = []

    summary, _ = fetch_missing_tracks(
        db,
        playlist,
        FakeQobuzClient(tracks=[_track_payload(101, "First Track")]),
        progress_callback=lambda progress: snapshots.append(
            json.loads(json.dumps(progress))
        ),
        batch_complete_callback=lambda _summary, files: drained.append(files),
    )

    states = [snapshot["items"][0]["status"] for snapshot in snapshots]
    assert states[0] == "queued"
    assert states.index("searching") < states.index("downloading") < states.index("downloaded")
    assert summary["total_missing"] == 1
    assert summary["eligible_total"] == 1
    assert summary["batch_count"] == 1
    assert summary["processed"] == 1
    assert drained == [[target]]
    assert any(snapshot.get("batch_state") == "draining" for snapshot in snapshots)


def test_fetch_missing_sweeps_every_unattempted_track_in_batches(
    db, monkeypatch
):
    user = ensure_user(db)
    source = PlaylistSource(user_id=user.id, service=ServiceEnum.spotify)
    playlist = Playlist(
        source=source, user_id=user.id, external_id="full-sweep", name="Full sweep"
    )
    db.add_all([source, playlist])
    db.flush()
    items = []
    for position in range(53):
        title = f"Track {position:02d}"
        item = PlaylistItem(
            playlist=playlist,
            position=position,
            artist_raw="Artist",
            title_raw=title,
            artist_norm=normalize_artist("Artist"),
            title_norm=normalize_title(title),
            album_norm="",
        )
        items.append(item)
        db.add(item)
    db.flush()
    db.add_all(
        [Match(playlist_item_id=item.id, status=MatchStatus.missing) for item in items]
    )
    db.ç¾ø¶‰žËkºwµç[ÝÚÙ[ˆ‹ˆŠBˆ\ÜÙ\[Ø^—ÜÙ\šXÙKš\×Ü[Ø^—ØÛÛ™šYÝ\™Y

H\È˜[ÙBˆ[ÛšÙ^\]ÚœÙ]]Š˜\˜ÛÛ™šYËœÙ][™ÜËœ[Ø^—Ú[\›˜[ÝÚÙ[ˆ‹ÛË\ÚÜŠBˆ\ÜÙ\[Ø^—ÜÙ\šXÙKš\×Ü[Ø^—ØÛÛ™šYÝ\™Y

H\È˜[ÙB‚‚™Yˆ\ÝØÜ™X]WØÛY[ØÛÛ›™XÝ×Ý×ÜÚYXØ\—ÝÚ]Ý]Ü›ÝšY\—ÜÙXÜ™]Ê‹[ÛšÙ^\]Ú
N‚ˆØÛÛ™šYÝ\™WÜ[Ø^Š[ÛšÙ^\]Ú
BˆØ]™WØÜ™Y[X[
‹œ[Ø^ˆ‹ÈÚÙ[ˆŽˆœHˆ
ˆÌ‹\Ù\—ÚYŽˆˆŸJBˆ‹˜ÛÛ[Z]

BˆØ[Îˆ\ÝÜÝ—HH×B‚ˆYˆÛÛ›™XÝ
Ù[ŠN‚ˆØ[Ë˜\[™
Ù[‹š[\›˜[ÝÚÙ[ŠBˆÙ[‹›X™[H”ÝY[È‚ˆ™]\›ˆÈ˜ÛÛ›™XÝYŽˆYK›X™[Žˆ”ÝY[ÈŸB‚ˆ[ÛšÙ^\]ÚœÙ]]Š[Ø^—ÜÙ\šXÙK”[Ø^”ÚYXØ\ÛY[˜ÛÛ›™XÝ‹ÛÛ›™XÝ
BˆÛY[H[Ø^—ÜÙ\šXÙK˜Ü™X]WÜ[Ø^—ØÛY[
ŠB‚ˆ\ÜÙ\ÛY[›X™[OH”ÝY[È‚ˆ\ÜÙ\Ø[ÈOHÈ\Ý\ÚYXØ\‹]ÚÙ[ˆˆ
ˆ—Bˆ\ÜÙ\›Ý\Ø]Š[Ø^—ÜÙ\šXÙKœÙ][™ÜËœ[Ø^—Ø]]ÝÚÙ[ˆŠB‚‚™Yˆ\ÝÜÚYXØ\—ÚÙ\œ›Ü—ÙÙ\×Û›ÝÚ[˜ÛYWÜ™\ÜÛœÙWØ›ÙJ[ÛšÙ^\]Ú
N‚ˆØÛÛ™šYÝ\™WÜ[Ø^Š[ÛšÙ^\]Ú
B‚ˆÛ\ÜÈ™\ÜÛœÙN‚ˆÝ]\×ØÛÙHHL‚ˆ^Hœ›ÝšY\‹]ÚÙ[‹\ÚÝ[[™]™\‹Y\ØØ\H‚‚ˆYˆœÛÛŠÙ[ŠN‚ˆ™]\›ˆÈ™\œ›ÜˆŽˆÙ[‹^B‚ˆ[ÛšÙ^\]ÚœÙ]]Š[Ø^—ÜÙ\šXÙKšœ™\]Y\Ý‹[X™H
˜\™ÜË
ŠšÝØ\™ÜÎˆ™\ÜÛœÙJ
JBˆÛY[H[Ø^—ÜÙ\šXÙK”[Ø^”ÚYXØ\ÛY[
ˆš‹ËÜ[Ø^‹\ÚYXØ\‹š[˜[Y‹ˆ\Ý\ÚYXØ\‹]ÚÙ[ˆ‹ˆÜ™Y[X[^Èœ›ÝšY\ˆŽˆœ[Ø^ˆŸKˆ
B‚ˆÚ]]\Ýœ˜Z\Ù\Ê[Ø^”›ÝšY\‘\œ›ÜŠH\ÈØ\\™Y‚ˆÛY[˜ÛÛ›™XÝ

Bˆ\ÜÙ\œ›ÝšY\‹]ÚÙ[ˆˆ›Ý[ˆÝŠØ\\™Y˜[YJB‚‚ˆÈKKHÛÜšÙ\ˆ\ÚÈKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKB‚‚™Yˆ\ÝÜ[Ø^—Ý\Ú×Ü]\Ù\×Ø™Y›Ü™WÙš\œÝØ˜]ÚÝÚ[—Ù\Ú×ÙÝX\™Ú\×ÛÝÊˆÙ\ÜÚ[Û—Ù˜XÝÜžK[ÛšÙ^\]Ú\Ü]ŠN‚ˆÝYÚ[™ÈH\Ü]ÈœÝYÚ[™È‚ˆÝYÚ[™Ë›ZÙ\Š
Bˆ[ÛšÙ^\]ÚœÙ]]Š˜\˜ÛÛ™šYËœÙ][™ÜËœ[Ø^—ÜÝYÚ[™×Ü]‹ÝŠÝYÚ[™ÊJBˆ[ÛšÙ^\]ÚœÙ]]Šˆ˜\˜ÛÛ™šYËœÙ][™ÜËœ[Ø^—ÛZ[—Ùœ™YWØž]\È‹ˆL
ˆL
ˆL
ˆLˆ
Bˆ[ÛšÙ^\]ÚœÙ]]Š˜\ÛÜšÙ\œË\ÚÜË”Ù\ÜÚ[Û“ØØ[‹Ù\ÜÚ[Û—Ù˜XÝÜžJB‚ˆÙ\ÜÚ[ÛˆHÙ\ÜÚ[Û—Ù˜XÝÜžJ
Bˆ\Ù\ˆH[œÝ\™WÝ\Ù\ŠÙ\ÜÚ[ÛŠBˆÛÝ\˜ÙHH^[\ÝÛÝ\˜ÙJ\Ù\—ÚY]\Ù\‹šYÙ\šXÙOTÙ\šXÙQ[[KœÜÝYžJBˆ^[\ÝH^[\Ý
ˆÛÝ\˜ÙO\ÛÝ\˜ÙKˆ\Ù\—ÚY]\Ù\‹šYˆ^\›˜[ÚYH™\ÚËYÝX\™‹ˆ˜[YOH‘\ÚÈÝX\™‹ˆ
BˆÙ\ÜÚ[Û‹˜YØ[
ÜÛÝ\˜ÙK^[\ÝJBˆÙ\ÜÚ[Û‹™›\Ú

Bˆ›ØˆH›ØŠˆ\OHœ[Ø^—ÙÝÛ›ØY‹ˆ^[\ÝÚY\^[\ÝšYˆ\Ù\—ÚY]\Ù\‹šYˆØÛÜOR›Ø”ØÛÜK\Ù\‹ˆÝ]\ÏR›Ø”Ý]\Ëœ[™[™Ëˆ^[ØYZœÛÛ‹™[\ÊˆÈ›[ÙHŽˆ™™]ÚÛZ\ÜÚ[™È‹œ^[\ÝÚYŽˆ^[\ÝšYBˆ
Kˆ
BˆÙ\ÜÚ[Û‹˜Y
›ØŠBˆÙ\ÜÚ[Û‹˜ÛÛ[Z]

Bˆ›Ø—ÚY^[\ÝÚYH›Ø‹šY^[\ÝšYˆÙ\ÜÚ[Û‹˜ÛÜÙJ
B‚ˆYˆ[™^XÝYØÛY[
ÙŠN‚ˆ˜Z\ÙH\ÜÙ\[Û‘\œ›ÜŠœ›ÝšY\ˆ]\Ý›ÝÝ\Ú[H\ÚÈÝX\™\ÈXÝ]™HŠB‚ˆ[ÛšÙ^\]ÚœÙ]]Šˆ˜\ÛÜšÙ\œË\ÚÜË˜Ü™X]WÜ[Ø^—ØÛY[‹ˆ[™^XÝYØÛY[ˆ
B‚ˆ™\Ý[H[Ø^—ÙÝÛ›ØYÝ\ÚËœ[Šˆ›Ø—ÚY™™]ÚÛZ\ÜÚ[™È‹^[\ÝÚY\^[\ÝÚYˆ
B‚ˆÚXÚÈHÙ\ÜÚ[Û—Ù˜XÝÜžJ
Bˆ]\ÙYÚ›ØˆHÚXÚË™Ù]
›Ø‹›Ø—ÚY
Bˆ\ÜÙ\™\Ý[OHÂˆœÝ]\ÈŽˆœ]\ÙY‹ˆš›Ø—ÚYŽˆ›Ø—ÚYˆœ™X\ÛÛˆŽˆ™\Ú×ÙÝX\™‹ˆBˆ\ÜÙ\]\ÙYÚ›Ø‹œÝ]\ÈOH›Ø”Ý]\Ëœ[™[™Âˆ\ÜÙ\]\ÙYÚ›Ø‹œ]\ÙYØ]\È›Ý›Û™Bˆ\ÜÙ\]\ÙYÚ›Ø‹›ØÚ×ÛÝÛ™\ˆ\È›Û™BˆÚXÚË˜ÛÜÙJ
B‚‚™Yˆ\ÝÜ[Ø^—Ü™\Ý[YWÙ˜Z[œ×Ú[\œ\YØ˜]ÚØ™Y›Ü™WÜ›ÝšY\ŠˆÙ\ÜÚ[Û—Ù˜XÝÜžK[ÛšÙ^\]Ú\Ü]ŠN‚ˆÝYÚ[™ÈH\Ü]ÈœÝYÚ[™È‚ˆXœ˜\žHH\Ü]È›Xœ˜\žH‚ˆÝYÚ[™Ë›ZÙ\Š
BˆXœ˜\žK›ZÙ\Š
Bˆ[ÛšÙ^\]ÚœÙ]]Š˜\˜ÛÛ™šYËœÙ][™ÜËœ[Ø^—ÜÝYÚ[™×Ü]‹ÝŠÝYÚ[™ÊJBˆ[ÛšÙ^\]ÚœÙ]]Š˜\˜ÛÛ™šYËœÙ][™ÜË›]\ÚX×ÛXœ˜\žWÜ]‹ÝŠXœ˜\žJJBˆ[ÛšÙ^\]ÚœÙ]]Š˜\˜ÛÛ™šYËœÙ][™ÜËœ[Ø^—ÛZ[—Ùœ™YWØž]\È‹
Bˆ[ÛšÙ^\]ÚœÙ]]Š˜\ÛÜšÙ\œË\ÚÜË”Ù\ÜÚ[Û“ØØ[‹Ù\ÜÚ[Û—Ù˜XÝÜžJB‚ˆÙ\ÜÚ[ÛˆHÙ\ÜÚ[Û—Ù˜XÝÜžJ
Bˆ\Ù\ˆH[œÝ\™WÝ\Ù\ŠÙ\ÜÚ[ÛŠBˆÛÝ\˜ÙHH^[\ÝÛÝ\˜ÙJ\Ù\—ÚY]\Ù\‹šYÙ\šXÙOTÙ\šXÙQ[[KœÜÝYžJBˆ^[\ÝH^[\Ý
ˆÛÝ\˜ÙO\ÛÝ\˜ÙKˆ\Ù\—ÚY]\Ù\‹šYˆ^\›˜[ÚYHœ™XÛÝ™\‹Y˜Z[ˆ‹ˆ˜[YOH”™XÛÝ™\ˆ˜Z[ˆ‹ˆ
BˆÙ\ÜÚ[Û‹˜YØ[
ÜÛÝ\˜ÙK^[\ÝJBˆÙ\ÜÚ[Û‹™›\Ú

Bˆ›ØˆH›ØŠˆ\OHœ[Ø^—ÙÝÛ›ØY‹ˆ^[\ÝÚY\^[\ÝšYˆ\Ù\—ÚY]\Ù\‹šYˆØÛÜOR›Ø”ØÛÜK\Ù\‹ˆÝ]\ÏR›Ø”Ý]\Ëœ[™[™Ëˆ^[ØYZœÛÛ‹™[\ÊˆÂˆœ\ÙHŽˆœ]\ÙY‹ˆœ]\ÙWÜ™X\ÛÛˆŽˆœÝÜ˜YÙWÙYÜ˜YY‹ˆ›[ÙHŽˆ™™]ÚÛZ\ÜÚ[™È‹ˆœ^[\ÝÚYŽˆ^[\ÝšYˆ™ÝÛ›ØYÈŽˆÈš][\ÈŽˆ×_Kˆš[\ÜŽˆÂˆš[\ÜYŽˆÜÝŠXœ˜\žHÈœ[™[™Ë™›XÈŠWKˆ˜ÛÛ™›XÝÈŽˆ×Kˆœ™Z™XÝYŽˆ×KˆKˆBˆ
Kˆ
BˆÙ\ÜÚ[Û‹˜Y
›ØŠBˆÙ\ÜÚ[Û‹˜ÛÛ[Z]

Bˆ›Ø—ÚY^[\ÝÚYH›Ø‹šY^[\ÝšYˆÙ\ÜÚ[Û‹˜ÛÜÙJ
B‚ˆÜ™\Žˆ\ÝÜÝ—HH×Bˆ[ÛšÙ^\]ÚœÙ]]Šˆ˜\ÛÜšÙ\œË\ÚÜËœØØ[—ÛXœ˜\žH‹ˆ[X™H
—Ø\™ÜË
Š—ÚÝØ\™ÜÎˆ
ˆÜ™\‹˜\[™
œØØ[ˆŠBˆÜˆÚ[\S˜[Y\ÜXÙJ×ÙXÝ[[X™NˆÈ™\ØÛÝ™\™YŽˆ_JBˆ
Kˆ
Bˆ[ÛšÙ^\]ÚœÙ]]Šˆ˜\ÛÜšÙ\œË\ÚÜËœ™\XØ]WÚ[\ÜYÙš[\È‹ˆ[X™H
—Ø\™ÜÎˆ
ˆÜ™\‹˜\[™
œ™\XØ]HŠBˆÜˆÈœÝ]\ÈŽˆ˜ÛÛ\]Y‹\ØYYŽˆK™]šXÝYŽˆ_Bˆ
Kˆ
Bˆ[ÛšÙ^\]ÚœÙ]]Šˆ˜\ÛÜšÙ\œË\ÚÜËœ[—ÛX]Ú[™È‹ˆ[X™H
—Ø\™ÜË
Š—ÚÝØ\™ÜÎˆ
ˆÜ™\‹˜\[™
›X]Ú[™ÈŠBˆÜˆÚ[\S˜[Y\ÜXÙJ×ÙXÝ[[X™NˆÈœ™XYHŽˆK›Z\ÜÚ[™ÈŽˆJBˆ
Kˆ
Bˆ[ÛšÙ^\]ÚœÙ]]Šˆ˜\ÛÜšÙ\œË\ÚÜË˜Ü™X]WÜ[Ø^—ØÛY[‹ˆ[X™HÙŽˆÜ™\‹˜\[™
œ›ÝšY\ˆŠHÜˆ˜ZÙT[Ø^ÛY[

Kˆ
Bˆ[ÛšÙ^\]ÚœÙ]]Šˆ˜\ÛÜšÙ\œË\ÚÜË™™]ÚÛZ\ÜÚ[™×Ý˜XÚÜÈ‹ˆ[X™H
—Ø\™ÜË
Š—ÚÝØ\™ÜÎˆ
Èš][\ÈŽˆ×Kœ›ØÙ\ÜÙYŽˆK×JKˆ
B‚ˆ™\Ý[H[Ø^—ÙÝÛ›ØYÝ\ÚËœ[Šˆ›Ø—ÚY™™]ÚÛZ\ÜÚ[™È‹^[\ÝÚY\^[\ÝÚYˆ
B‚ˆ\ÜÙ\Ü™\ˆOHÈœØØ[ˆ‹œ™\XØ]H‹›X]Ú[™È‹œ›ÝšY\ˆ—Bˆ\ÜÙ\™\Ý[Èœ\ÙH—HOH˜ÛÛ\]Y‚ˆ\ÜÙ\™\Ý[ÈœØØ[ˆ—VÈœÝ]\È—HOH˜ÛÛ\]Y‚ˆ\ÜÙ\™\Ý[È›X]Ú[™È—VÈœ™XYH—HOHB‚‚™YˆØÜ™X]WÜ[Ø^—Ú›ØŠÙ\ÜÚ[Û—Ù˜XÝÜžK^[\ÝS›Û™K[ÙOH™™]ÚÛZ\ÜÚ[™ÈŠN‚ˆÙ\ÜÚ[ÛˆHÙ\ÜÚ[Û—Ù˜XÝÜžJ
Bˆ\Ù\ˆH[œÝ\™WÝ\Ù\ŠÙ\ÜÚ[ÛŠBˆ^[\ÝÚYH›Û™BˆYˆ^[\Ý\È›Ý›Û™N‚ˆÙ\ÜÚ[Û‹˜Y
^[\ÝœÛÝ\˜ÙJBˆÙ\ÜÚ[Û‹˜Y
^[\Ý
BˆÙ\ÜÚ[Û‹™›\Ú

Bˆ^[\ÝÚYH^[\ÝšYˆ›ØˆH›ØŠˆ\OHœ[Ø^—ÙÝÛ›ØY‹ˆ^[\ÝÚY\^[\ÝÚYˆ\Ù\—ÚY]\Ù\‹šYˆØÛÜOR›Ø”ØÛÜK\Ù\‹ˆÝ]\ÏR›Ø”Ý]\Ëœ[™[™Ëˆ^[ØYZœÛÛ‹™[\ÊÈ›[ÙHŽˆ[ÙKœ^[\ÝÚYŽˆ^[\ÝÚYJKˆ
BˆÙ\ÜÚ[Û‹˜Y
›ØŠBˆÙ\ÜÚ[Û‹˜ÛÛ[Z]

Bˆ™\Ý[H›Ø‹šY^[\ÝÚYˆÙ\ÜÚ[Û‹˜ÛÜÙJ
Bˆ™]\›ˆ™\Ý[‚‚™Yˆ\ÝÜ[Ø^—Ý\Ú×Ù˜Z[×ÝÚ]Ý]Ü™]žWÛÛ—ØÛÛ™šYÝ\˜][Û—Ù\œ›ÜŠˆÙ\ÜÚ[Û—Ù˜XÝÜžK[ÛšÙ^\]ÚŠN‚ˆ›Ø—ÚYÈHØÜ™X]WÜ[Ø^—Ú›ØŠÙ\ÜÚ[Û—Ù˜XÝÜžK[ÙOH\›ŠBˆ[ÛšÙ^\]ÚœÙ]]Š˜\ÛÜšÙ\œË\ÚÜË”Ù\ÜÚ[Û“ØØ[‹Ù\ÜÚ[Û—Ù˜XÝÜžJB‚ˆYˆ˜Z\ÙWØÛÛ™šYÊÙŠN‚ˆ˜Z\ÙH[Ø^ÛÛ™šYÝ\˜][Û‘\œ›ÜŠ››ÝÛÛ™šYÝ\™YŠB‚ˆ[ÛšÙ^\]ÚœÙ]]Š˜\ÛÜšÙ\œË\ÚÜË˜Ü™X]WÜ[Ø^—ØÛY[‹˜Z\ÙWØÛÛ™šYÊB‚ˆ™\Ý[H[Ø^—ÙÝÛ›ØYÝ\ÚËœ[Šˆ›Ø—ÚY\›‹\›HšÎ‹ËÜ^Kœ[Ø^‹˜ÛÛKÝ˜XÚËÌH‚ˆ
B‚ˆÚXÚÈHÙ\ÜÚ[Û—Ù˜XÝÜžJ
Bˆ›ØˆHÚXÚË™Ù]
›Ø‹›Ø—ÚY
Bˆ\ÜÙ\™\Ý[ÈœÝ]\È—HOH™˜Z[Y‚ˆ\ÜÙ\›Ø‹œÝ]\ÈOH›Ø”Ý]\Ë™˜Z[Yˆ\ÜÙ\›Ø‹™\œ›ÜˆOH”[Ø^ˆÝÛ›ØY˜Z[Y
[Ø^ÛÛ™šYÝ\˜][Û‘\œ›ÜŠH‚ˆ\ÜÙ\›Ø‹™š[š\ÚYØ]\È›Ý›Û™BˆÚXÚË˜ÛÜÙJ
B‚‚™Yˆ\ÝÜ[Ø^—Ù™]ÚÛZ\ÜÚ[™×Ù[™Ý×Ù[™
ˆÙ\ÜÚ[Û—Ù˜XÝÜžK[ÛšÙ^\]Ú\Ü]™›\Y×Øš[˜\žBŠN‚ˆÝYÚ[™ÈH\Ü]ÈœÝYÚ[™È‚ˆXœ˜\žHH\Ü]È›Xœ˜\žH‚ˆXœ˜\žK›ZÙ\Š
Bˆ[ÛšÙ^\]ÚœÙ]]Š˜\˜ÛÛ™šYËœÙ][™ÜËœ[Ø^—ÜÝYÚ[™×Ü]‹ÝŠÝYÚ[™ÊJBˆ[ÛšÙ^\]ÚœÙ]]Š˜\˜ÛÛ™šYËœÙ][™ÜË›]\ÚX×ÛXœ˜\žWÜ]‹ÝŠXœ˜\žJJBˆ[ÛšÙ^\]ÚœÙ]]Š˜\˜ÛÛ™šYËœÙ][™ÜËœ[Ø^—Ü™\]Y\ÝÙ[^WÜÙXÛÛ™È‹
Bˆ[ÛšÙ^\]ÚœÙ]]Š˜\˜ÛÛ™šYËœÙ][™ÜË›]\ÚXØœ˜Z[ž—Ù[˜X›Y‹˜[ÙJBˆ[ÛšÙ^\]ÚœÙ]]Š˜\˜ÛÛ™šYËœÙ][™ÜËœÝÜ˜YÙWÜš[X\žWØ˜XÚÙ[™‹›ØØ[ŠB‚ˆÙ\ÜÚ[ÛˆHÙ\ÜÚ[Û—Ù˜XÝÜžJ
Bˆ\Ù\ˆH[œÝ\™WÝ\Ù\ŠÙ\ÜÚ[ÛŠBˆÛÝ\˜ÙHH^[\ÝÛÝ\˜ÙJ\Ù\—ÚY]\Ù\‹šYÙ\šXÙOTÙ\šXÙQ[[KœÜÝYžJBˆ^[\ÝH^[\Ý
ˆÛÝ\˜ÙO\ÛÝ\˜ÙKˆ\Ù\—ÚY]\Ù\‹šYˆ^\›˜[ÚYHœ[Ø^‹\‹ˆ˜[YOH”[Ø^ˆ^[\Ý‹ˆ
Bˆ˜XÚÜÈHÂˆ
•YÙÙY\\Ý‹‘š\œÝ˜XÚÈ‹•YÙÙY[[HŠKˆ
•YÙÙY\\Ý‹”ÙXÛÛ™˜XÚÈ‹•YÙÙY[[HŠKˆBˆ][\ÈH×Bˆ›ÜˆÜÚ][Û‹
\\Ý]K[[JH[ˆ[[Y\˜]J˜XÚÜÊN‚ˆ][HH^[\Ý][Jˆ^[\Ý\^[\ÝˆÜÚ][Û\ÜÚ][Û‹ˆ\\ÝÜ˜]ÏX\\Ýˆ]WÜ˜]Ï]]Kˆ[[WÜ˜]ÏX[[Kˆ\\ÝÛ›Ü›O[›Ü›X[^™WØ\\Ý
\\Ý
Kˆ]WÛ›Ü›O[›Ü›X[^™WÝ]J]JKˆ[[WÛ›Ü›O[›Ü›X[^™WØ[[J[[JKˆ
Bˆ][\Ë˜\[™
][JBˆÈS“PUÒQ][HÚ]Ý]HX]Ú›ÝÈ]\Ý™HYÛ›Ü™YžH™]Ú[Z\ÜÚ[™Ë‚ˆ[›X]ÚYH^[\Ý][Jˆ^[\Ý\^[\ÝˆÜÚ][ÛL‹ˆ\\ÝÜ˜]ÏH•YÙÙY\\Ý‹ˆ]WÜ˜]ÏH•\™˜XÚÈ‹ˆ[[WÜ˜]ÏH•YÙÙY[[H‹ˆ\\ÝÛ›Ü›O[›Ü›X[^™WØ\\Ý
•YÙÙY\\ÝŠKˆ]WÛ›Ü›O[›Ü›X[^™WÝ]J•\™˜XÚÈŠKˆ[[WÛ›Ü›O[›Ü›X[^™WØ[[J•YÙÙY[[HŠKˆ
BˆÙ\ÜÚ[Û‹˜YØ[
ÜÛÝ\˜ÙK^[\Ý
š][\Ë[›X]ÚYJBˆÙ\ÜÚ[Û‹™›\Ú

Bˆ›Üˆ][H[ˆ][\Î‚ˆÙ\ÜÚ[Û‹˜Y
ˆX]Ú
ˆ^[\ÝÚ][WÚYZ][KšYˆÝ]\ÏSX]ÚÝ]\Ë›Z\ÜÚ[™ËˆÛÛ™šY[˜ÙOLŒˆY]ÙH››Û™H‹ˆ
Bˆ
Bˆ›ØˆH›ØŠˆ\OHœ[Ø^—ÙÝÛ›ØY‹ˆ^[\ÝÚY\^[\ÝšYˆ\Ù\—ÚY]\Ù\‹šYˆØÛÜOR›Ø”ØÛÜK\Ù\‹ˆÝ]\ÏR›Ø”Ý]\Ëœ[™[™Ëˆ^[ØYZœÛÛ‹™[\ÊÈ›[ÙHŽˆ™™]ÚÛZ\ÜÚ[™È‹œ^[\ÝÚYŽˆ^[\ÝšYJKˆ
BˆÙ\ÜÚ[Û‹˜Y
›ØŠBˆÙ\ÜÚ[Û‹˜ÛÛ[Z]

Bˆ›Ø—ÚY^[\ÝÚYH›Ø‹šY^[\ÝšYˆÙ\ÜÚ[Û‹˜ÛÜÙJ
B‚ˆ˜ZÙWØÛY[H˜ZÙT[Ø^ÛY[
ˆ˜XÚÜÏVÂˆÝ˜XÚ×Ü^[ØY
LK‘š\œÝ˜XÚÈŠKˆÝ˜XÚ×Ü^[ØY
L‹”ÙXÛÛ™˜XÚÈŠKˆÝ˜XÚ×Ü^[ØY
LË•\™˜XÚÈŠKˆBˆ
Bˆ[ÛšÙ^\]ÚœÙ]]Š˜\ÛÜšÙ\œË\ÚÜË”Ù\ÜÚ[Û“ØØ[‹Ù\ÜÚ[Û—Ù˜XÝÜžJBˆ[ÛšÙ^\]ÚœÙ]]Š˜\ÛÜšÙ\œË\ÚÜË˜Ü™X]WÜ[Ø^—ØÛY[‹[X™HÙŽˆ˜ZÙWØÛY[
B‚ˆYˆ˜ZÙWÙÝÛ›ØY
ÛY[˜XÚ×ÚYÝYÚ[™×Ù\‹]X[]K[X™YØ\
N‚ˆ]\ÈHÈŒLHŽˆ‘š\œÝ˜XÚÈ‹ŒLˆŽˆ”ÙXÛÛ™˜XÚÈ‹ŒLÈŽˆ•\™˜XÚÈŸBˆ]HH]\ÖÜÝŠ˜XÚ×ÚY
WBˆ\™Ù]H
ˆ]
ÝYÚ[™×Ù\ŠBˆÈ•YÙÙY\\ÝHYÙÙY[[H
Œ
HÌ‹NMšÒ—H‚ˆÈˆžÝ]_K™›XÈ‚ˆ
BˆÝÜš]WÙ›XÊˆ™›\Y×Øš[˜\žKˆ\™Ù]ˆœ™\]Y[˜ÞOMYˆ]HOH‘š\œÝ˜XÚÈˆ[ÙHMLˆY]Y]O^Âˆ˜\\ÝŽˆ•YÙÙY\\Ý‹ˆ˜[[HŽˆ•YÙÙY[[H‹ˆ]HŽˆ]Kˆ™]HŽˆŒŒ‹ˆKˆ
Bˆ™]\›ˆÝ\™Ù]B‚ˆ[ÛšÙ^\]ÚœÙ]]Šˆ˜\œÙ\šXÙ\Ëœ[Ø^‹™ÝÛ›ØYÝ˜XÚ×Ý×ÜÝYÚ[™È‹˜ZÙWÙÝÛ›ØYˆ
B‚ˆ™\Ý[H[Ø^—ÙÝÛ›ØYÝ\ÚËœ[Šˆ›Ø—ÚY™™]ÚÛZ\ÜÚ[™È‹^[\ÝÚY\^[\ÝÚYˆ
B‚ˆ\ÜÙ\™\Ý[Èœ\ÙH—HOH˜ÛÛ\]Y‚ˆ\ÜÙ\™\Ý[È™ÝÛ›ØYÈ—VÈÝ[ÛZ\ÜÚ[™È—HOH‚ˆ\ÜÙ\™\Ý[È™ÝÛ›ØYÈ—VÈ™ÝÛ›ØYY—HOH‚ˆ\ÜÙ\™\Ý[È™ÝÛ›ØYÈ—VÈœ›ØÙ\ÜÙY—HOH‚ˆ\ÜÙ\™\Ý[È™ÝÛ›ØYÈ—VÈœÝÜ™Y—HOH‚ˆ\ÜÙ\Ú][VÈœÝ]\È—H›Üˆ][H[ˆ™\Ý[È™ÝÛ›ØYÈ—VÈš][\È—_HOHÈœÝÜ™YŸBˆ\ÜÙ\™\Ý[È™ÝÛ›ØYÈ—VÈ™˜Z[Y—HOHˆ\ÜÙ\[Š™\Ý[Èš[\Ü—VÈš[\ÜY—JHOH‚ˆ\ÜÙ\™\Ý[ÈœØØ[ˆ—VÈœÝ]\È—HOH˜ÛÛ\]Y‚ˆ\ÜÙ\™\Ý[ÈœØØ[ˆ—VÈ˜YY—HOH‚ˆ\ÜÙ\™\Ý[È›X]Ú[™È—VÈœ™XYH—HOH‚ˆ\ÜÙ\™\Ý[È›X]Ú[™È—VÈ›Z\ÜÚ[™È—HOHHÈH[ÝXÚYS“PUÒQ][B‚ˆ[\ÜYHÔ]
]
H›Üˆ][ˆ™\Ý[Èš[\Ü—VÈš[\ÜY—WBˆ\ÜÙ\[
]š\×Ùš[J
H›Üˆ][ˆ[\ÜY
Bˆ\ÜÙ\[
Xœ˜\žKœ™\ÛÛ™J
H[ˆ]œ™\ÛÛ™J
Kœ\™[È›Üˆ][ˆ[\ÜY
Bˆ\ÜÙ\›Ý\Ý
ÝYÚ[™Ëœ™ÛØŠŠ‹™›XÈŠJB‚ˆÚXÚÈHÙ\ÜÚ[Û—Ù˜XÝÜžJ
Bˆ›ØˆHÚXÚË™Ù]
›Ø‹›Ø—ÚY
Bˆ\ÜÙ\›Ø‹œÝ]\ÈOH›Ø”Ý]\Ë™Û™Bˆ\ÜÙ\›Ø‹›ØÚ×ÛÝÛ™\ˆ\È›Û™BˆX]Ú\ÈHÚXÚËœØØ[\œÊˆÙ[XÝ
X]Ú
Bˆš›Ú[Š^[\Ý][K^[\Ý][KšYOHX]Úœ^[\ÝÚ][WÚY
BˆÚ\™J^[\Ý][Kœ^[\ÝÚYOH^[\ÝÚY
Bˆ
K˜[

BˆÝ]\Ù\ÈHÛX]ÚœÝ]\È›ÜˆX]Ú[ˆX]Ú\×Bˆ\ÜÙ\Ý]\Ù\Ë˜ÛÝ[
X]ÚÝ]\Ëœ™XYJHOH‚ˆ\ÜÙ\Ý]\Ù\Ë˜ÛÝ[
X]ÚÝ]\Ë›Z\ÜÚ[™ÊHOHBˆ™XYWÛX]Ú\ÈHÛH›ÜˆH[ˆX]Ú\ÈYˆKœÝ]\ÈOHX]ÚÝ]\Ëœ™XYWBˆ\ÜÙ\[
X]Ú˜XÚ×ÚY\È›Ý›Û™H›ÜˆX]Ú[ˆ™XYWÛX]Ú\ÊBˆ][\ÈHÚXÚËœØØ[\œÊˆÙ[XÝ
›ÝšY\][\
KÚ\™J›ÝšY\][\œ›ÝšY\ˆOHœ[Ø^ˆŠBˆ
K˜[

Bˆ\ÜÙ\[Š][\ÊHOH‚ˆ\ÜÙ\Ø][\œÝ]\È›Üˆ][\[ˆ][\ßHOHÈœÝÜ™YŸBˆÚXÚË˜ÛÜÙJ
B‚‚™Yˆ\ÝÜ[Ø^—Ý\›Ý\Ú×ÙÝÛ›ØY×Ú[\Ü×Ø[™ÜØØ[œÊˆÙ\ÜÚ[Û—Ù˜XÝÜžK[ÛšÙ^\]Ú\Ü]™›\Y×Øš[˜\žBŠN‚ˆÝYÚ[™ÈH\Ü]ÈœÝYÚ[™È‚ˆXœ˜\žHH\Ü]È›Xœ˜\žH‚ˆXœ˜\žK›ZÙ\Š
Bˆ[ÛšÙ^\]ÚœÙ]]Š˜\˜ÛÛ™šYËœÙ][™ÜËœ[Ø^—ÜÝYÚ[™×Ü]‹ÝŠÝYÚ[™ÊJBˆ[ÛšÙ^\]ÚœÙ]]Š˜\˜ÛÛ™šYËœÙ][™ÜË›]\ÚX×ÛXœ˜\žWÜ]‹ÝŠXœ˜\žJJB‚ˆ›Ø—ÚYÈHØÜ™X]WÜ[Ø^—Ú›ØŠÙ\ÜÚ[Û—Ù˜XÝÜžK[ÙOH\›ŠBˆ[ÛšÙ^\]ÚœÙ]]Š˜\ÛÜšÙ\œË\ÚÜË”Ù\ÜÚ[Û“ØØ[‹Ù\ÜÚ[Û—Ù˜XÝÜžJBˆ[ÛšÙ^\]ÚœÙ]]Šˆ˜\ÛÜšÙ\œË\ÚÜË˜Ü™X]WÜ[Ø^—ØÛY[‹[X™HÙŽˆ˜ZÙT[Ø^ÛY[

Bˆ
B‚ˆYˆ˜ZÙWÝ\›ÙÝÛ›ØY
ÛY[\›ÝYÚ[™×Ù\‹]X[]K[X™YØ\
N‚ˆ\™Ù]H]
ÝYÚ[™×Ù\ŠHÈ\\ÝH[[H
Œ
HÌ‹NMšÒ—HˆÈ”ÛÛ™Ë™›XÈ‚ˆÝÜš]WÙ›XÊˆ™›\Y×Øš[˜\žKˆ\™Ù]ˆœ™\]Y[˜ÞOMˆY]Y]O^È˜\\ÝŽˆ\\Ý‹˜[[HŽˆ[[H‹]HŽˆ”ÛÛ™ÈŸKˆ
Bˆ™]\›ˆÝ\™Ù]B‚ˆ[ÛšÙ^\]ÚœÙ]]Šˆ˜\ÛÜšÙ\œË\ÚÜË™ÝÛ›ØYÝ\›Ý×ÜÝYÚ[™È‹˜ZÙWÝ\›ÙÝÛ›ØYˆ
B‚ˆ™\Ý[H[Ø^—ÙÝÛ›ØYÝ\ÚËœ[Šˆ›Ø—ÚY\›‹\›HšÎ‹ËÜ^Kœ[Ø^‹˜ÛÛKØ[[KØX˜ÌLŒÈ‚ˆ
B‚ˆ\ÜÙ\™\Ý[Èœ\ÙH—HOH˜ÛÛ\]Y‚ˆ\ÜÙ\™\Ý[È›[ÙH—HOH\›‚ˆ\ÜÙ\™\Ý[È™ÝÛ›ØYÈ—VÈ™ÝÛ›ØYY—HOHBˆ\ÜÙ\[Š™\Ý[Èš[\Ü—VÈš[\ÜY—JHOHBˆ\ÜÙ\™\Ý[ÈœØØ[ˆ—VÈ˜YY—HOHBˆ\ÜÙ\™\Ý[È›X]Ú[™È—H\È›Û™B‚ˆÚXÚÈHÙ\ÜÚ[Û—Ù˜XÝÜžJ
Bˆ›ØˆHÚXÚË™Ù]
›Ø‹›Ø—ÚY
Bˆ\ÜÙ\›Ø‹œÝ]\ÈOH›Ø”Ý]\Ë™Û™BˆÚXÚË˜ÛÜÙJ
B