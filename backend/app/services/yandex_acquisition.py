"""Acquire missing tracks through Yandex's lossless file-info flow.

The endpoint is undocumented, so this adapter is deliberately fail-closed:
it uses the current signed negotiation contract, accepts only FLAC audio,
validates every response field and host, and never stores an MP3/AAC fallback.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import httpx
from mutagen import File as MutagenFile
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from mutagen.flac import FLAC, FLACNoHeaderError
from mutagen.id3 import COMM, TALB, TIT2, TPE1, TRCK, TSRC, ID3
from mutagen.mp4 import MP4
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Playlist, PlaylistItem, PlaylistSource, ProviderAttempt, ServiceEnum
from app.services.credentials import CredentialError, get_credential_payload
from app.services.delivery import safe_filename
from app.services.matcher import normalize_isrc
from app.services.qobuz import (
    choose_track_candidate,
    missing_provider_items,
    provider_lookup_key,
)
from app.services.yandex import create_yandex_client

# httpx INFO records include complete query strings and temporary signed media
# URLs.  Keep provider traffic out of application logs while retaining warning
# and error diagnostics that do not expose credential-bearing URLs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

YANDEX_LOSSLESS_AVAILABLE = True
_SUPPORTED_CODECS = frozenset(
    {"flac", "flac-mp4", "aac", "he-aac", "mp3", "aac-mp4", "he-aac-mp4"}
)
_CODEC_OUTPUT = {
    "flac": ("flac", ".flac"),
    "flac-mp4": ("flac", ".flac"),
    "aac": ("aac", ".aac"),
    "he-aac": ("he-aac", ".aac"),
    "mp3": ("mp3", ".mp3"),
    "aac-mp4": ("aac", ".m4a"),
    "he-aac-mp4": ("he-aac", ".m4a"),
}
_EXPECTED_OUTPUT_CONTAINERS = {
    ".flac": "FLAC",
    ".aac": "AAC",
    ".mp3": "MP3",
    ".m4a": "MP4",
}
_ALLOWED_DOWNLOAD_SUFFIXES = (".yandex.net", ".yandex.ru")
_YANDEX_FILE_INFO_URL = "https://api.music.yandex.net/get-file-info"
_YANDEX_LOSSLESS_QUALITY = "lossless"
_YANDEX_REQUEST_CODECS = (
    "flac,aac,he-aac,mp3,flac-mp4,aac-mp4,he-aac-mp4"
)
_YANDEX_LOSSLESS_TRANSPORT = "raw"
_YANDEX_WEB_CLIENT = "YandexMusicWebNext/1.0.0"


class YandexAcquisitionError(RuntimeError):
    """Base error for controlled Yandex acquisition failures."""


class YandexAcquisitionConfigurationError(YandexAcquisitionError):
    """The provider is disabled or has no connected playlist source."""


class YandexAcquisitionAuthError(YandexAcquisitionError):
    """The stored Yandex OAuth token was rejected."""


class YandexAcquisitionProviderError(YandexAcquisitionError):
    """Yandex or the unofficial client could not complete an operation."""


@dataclass(frozen=True, slots=True)
class YandexSearchCandidate:
    yandex_id: str
    artist: str
    title: str
    album: str
    duration_ms: int | None
    isrc: str | None
    url: str
    version: str = ""

    @property
    def qobuz_id(self) -> str:
        """Compatibility key for the shared deterministic candidate matcher."""

        return self.yandex_id

    @property
    def quality_rank(self) -> tuple[int, int, int]:
        # Search results do not expose the account-specific download variants.
        return (0, 0, 0)


@dataclass(frozen=True, slots=True)
class YandexLosslessInfo:
    track_id: str
    real_id: str | None
    quality: str
    codec: str
    transport: str
    bitrate: int
    size: int | None
    url: str
    decryption_key: str | None


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _non_negative_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _field(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        result = _value(value, name, None)
        if result is not None:
            return result
    return default


def _candidate(track: Any) -> YandexSearchCandidate | None:
    track_id = str(_value(track, "id") or "").strip()
    title = str(_value(track, "title") or "").strip()
    if not track_id or not title:
        return None
    artists = _value(track, "artists", []) or []
    artist = ", ".join(
        str(_value(item, "name") or "").strip()
        for item in artists
        if str(_value(item, "name") or "").strip()
    )
    albums = _value(track, "albums", []) or []
    album = str(_value(albums[0], "title") or "").strip() if albums else ""
    album_id = str(_value(albums[0], "id") or "").strip() if albums else ""
    url = (
        f"https://music.yandex.ru/album/{album_id}/track/{track_id}"
        if album_id
        else f"https://music.yandex.ru/track/{track_id}"
    )
    return YandexSearchCandidate(
        yandex_id=track_id,
        artist=artist,
        title=title,
        album=album,
        duration_ms=_positive_int(
            _value(track, "duration_ms", _value(track, "durationMs"))
        ),
        isrc=normalize_isrc(str(_value(track, "isrc") or "")),
        url=url,
        version=str(_value(track, "version") or "").strip(),
    )


def _provider_exception(exc: Exception) -> YandexAcquisitionError:
    if type(exc).__name__ in {"UnauthorizedError", "ForbiddenError"}:
        return YandexAcquisitionAuthError("Yandex rejected the stored credential")
    return YandexAcquisitionProviderError("Yandex request failed")


def create_yandex_acquisition_client(db: Session):
    if not settings.yandex_download_enabled:
        raise YandexAcquisitionConfigurationError("Yandex acquisition is disabled")
    if not YANDEX_LOSSLESS_AVAILABLE:
        raise YandexAcquisitionConfigurationError(
            "Yandex lossless acquisition is unavailable in the supported API"
        )
    if len(settings.yandex_internal_token.strip()) < 16:
        raise YandexAcquisitionConfigurationError(
            "Yandex lossless signer is not configured"
        )
    source = db.scalar(
        select(PlaylistSource).where(PlaylistSource.service == ServiceEnum.yandex)
    )
    if source is None:
        raise YandexAcquisitionConfigurationError("Yandex source is not connected")
    try:
        try:
            token = get_credential_payload(db, "yandex").get("token")
        except CredentialError:
            token = source.access_token
        if not str(token or "").strip():
            raise YandexAcquisitionConfigurationError(
                "Yandex source is not connected"
            )
        return create_yandex_client(str(token))
    except Exception as exc:
        if isinstance(exc, YandexAcquisitionConfigurationError):
            raise
        raise _provider_exception(exc) from exc


def search_yandex_tracks(
    client: Any, query: str, limit: int = 10
) -> list[YandexSearchCandidate]:
    try:
        result = client.search(query, nocorrect=True, type_="track", page=0)
    except Exception as exc:
        raise _provider_exception(exc) from exc
    tracks = _value(_value(result, "tracks"), "results", []) or []
    return [
        candidate
        for candidate in (_candidate(track) for track in list(tracks)[:limit])
        if candidate is not None
    ]


def _direct_yandex_candidate(client: Any, external_track_id: str) -> YandexSearchCandidate | None:
    try:
        tracks = client.tracks([external_track_id])
    except Exception as exc:
        raise _provider_exception(exc) from exc
    return _candidate(tracks[0]) if tracks else None


def yandex_download_eligibility(db: Session, playlist: Playlist) -> dict[str, int]:
    all_missing, eligible = missing_provider_items(db, playlist, "yandex")
    return {
        "total_missing": len(all_missing),
        "eligible": len(eligible),
        "already_checked": len(all_missing) - len(eligible),
    }


def _is_allowed_download_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and port in (None, 443)
        and any(host.endswith(suffix) for suffix in _ALLOWED_DOWNLOAD_SUFFIXES)
    )


def _is_audio_download_url(url: str, codec: str) -> bool:
    if not _is_allowed_download_url(url):
        return False
    path = (urlparse(url).path or "").casefold().rstrip("/")
    exact_endings = {
        "flac": ("/flac", ".flac"),
        "flac-mp4": ("/flac-mp4", ".m4a"),
    }
    if any(path.endswith(ending) for ending in exact_endings.get(codec, ())):
        return True
    final_segment = path.rsplit("/", 1)[-1]
    codec_patterns = {
        "aac": r"(?:he-)?aac\d*",
        "he-aac": r"(?:he-)?aac\d*",
        "mp3": r"mp3\d*",
        "aac-mp4": r"(?:he-)?aac\d*-mp4",
        "he-aac-mp4": r"(?:he-)?aac\d*-mp4",
    }
    pattern = codec_patterns.get(codec)
    return bool(pattern and re.fullmatch(pattern, final_segment))


def build_yandex_lossless_request(
    track_id: str | int,
    *,
    timestamp: int | None = None,
    key: str,
) -> dict[str, str | int]:
    """Build a signed request with a test/injected key.

    Production obtains the same fields from the isolated signer sidecar and
    never keeps its signing material in the Python application.
    """

    normalized_track_id = str(track_id).strip()
    if not normalized_track_id:
        raise YandexAcquisitionProviderError("Yandex track id is empty")
    request_timestamp = int(time.time()) if timestamp is None else int(timestamp)
    if not key:
        raise YandexAcquisitionConfigurationError(
            "Yandex lossless signing support is unavailable"
        )
    values = (
        str(request_timestamp),
        normalized_track_id,
        _YANDEX_LOSSLESS_QUALITY,
        _YANDEX_REQUEST_CODECS.replace(",", ""),
        _YANDEX_LOSSLESS_TRANSPORT,
    )
    digest = hmac.new(
        key.encode("utf-8"),
        "".join(values).encode("utf-8"),
        hashlib.sha256,
    ).digest()
    # SHA-256 base64 has one trailing padding character.  The Yandex desktop
    # request contract omits it.
    signature = base64.b64encode(digest).decode("ascii")[:-1]
    return {
        "ts": request_timestamp,
        "trackId": normalized_track_id,
        "quality": _YANDEX_LOSSLESS_QUALITY,
        "codecs": _YANDEX_REQUEST_CODECS,
        "transports": _YANDEX_LOSSLESS_TRANSPORT,
        "sign": signature,
    }


def _signed_request_from_sidecar(
    track_id: str,
    timestamp: int,
) -> dict[str, str | int]:
    token = settings.yandex_internal_token.strip()
    if len(token) < 16:
        raise YandexAcquisitionConfigurationError(
            "Yandex lossless signer is not configured"
        )
    base_url = settings.yandex_signer_url.rstrip("/")
    parsed = urlparse(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"yandex-signer", "127.0.0.1", "localhost"}
        or parsed.port not in {None, 8091}
    ):
        raise YandexAcquisitionConfigurationError(
            "Yandex signer URL is not an allowed internal endpoint"
        )
    try:
        response = httpx.post(
            f"{base_url}/sign",
            headers={"Authorization": f"Bearer {token}"},
            json={"track_id": track_id, "timestamp": timestamp},
            timeout=settings.yandex_connect_timeout_seconds,
            follow_redirects=False,
        )
        response.raise_for_status()
        params = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise YandexAcquisitionProviderError(
            "Yandex lossless signer request failed"
        ) from exc
    if not isinstance(params, dict):
        raise YandexAcquisitionProviderError(
            "Yandex lossless signer returned an invalid response"
        )
    expected = {
        "ts": str(timestamp),
        "trackId": track_id,
        "quality": _YANDEX_LOSSLESS_QUALITY,
        "codecs": _YANDEX_REQUEST_CODECS,
        "transports": _YANDEX_LOSSLESS_TRANSPORT,
    }
    if any(str(params.get(name, "")) != value for name, value in expected.items()):
        raise YandexAcquisitionProviderError(
            "Yandex lossless signer returned a mismatched contract"
        )
    signature = str(params.get("sign") or "")
    if not signature or len(signature) > 128:
        raise YandexAcquisitionProviderError(
            "Yandex lossless signer returned an invalid signature"
        )
    return {**expected, "ts": timestamp, "sign": signature}


def _client_user_id(client: Any) -> int:
    account = _field(_field(client, "me"), "account", default={})
    user_id = _positive_int(_field(account, "uid", "id"))
    if user_id is None:
        raise YandexAcquisitionConfigurationError(
            "Yandex account id is unavailable"
        )
    return user_id


def _request_yandex_file_info(
    client: Any,
    params: dict[str, str | int],
) -> dict[str, Any]:
    token = str(_field(client, "token", default="") or "").strip()
    if not token:
        raise YandexAcquisitionConfigurationError("Yandex OAuth token is unavailable")
    headers = {
        "Authorization": f"OAuth {token}",
        "User-Agent": "YandexMusicAPI/1.0.0",
        "x-yandex-music-client": _YANDEX_WEB_CLIENT,
        "x-yandex-music-without-invocation-info": "1",
        "x-yandex-music-multi-auth-user-id": str(_client_user_id(client)),
        "Referer": "https://music.yandex.ru/",
        "Origin": "https://music.yandex.ru",
    }
    try:
        response = httpx.get(
            _YANDEX_FILE_INFO_URL,
            params=params,
            headers=headers,
            timeout=settings.yandex_connect_timeout_seconds,
            follow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise _provider_exception(exc) from exc
    if not isinstance(payload, dict):
        raise YandexAcquisitionProviderError(
            "Yandex returned an invalid lossless response"
        )
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        raise YandexAcquisitionProviderError(
            "Yandex returned an invalid lossless response"
        )
    return result


def get_yandex_lossless_info(
    client: Any,
    track_id: str | int,
    *,
    timestamp: int | None = None,
    key: str | None = None,
) -> YandexLosslessInfo:
    """Resolve the best trusted Yandex audio variant or reject the response."""

    requested_track_id = str(track_id).strip()
    request_timestamp = int(time.time()) if timestamp is None else int(timestamp)
    params = (
        build_yandex_lossless_request(
            requested_track_id,
            timestamp=request_timestamp,
            key=key,
        )
        if key is not None
        else _signed_request_from_sidecar(requested_track_id, request_timestamp)
    )
    result = _request_yandex_file_info(client, params)

    if not isinstance(result, dict):
        raise YandexAcquisitionProviderError(
            "Yandex returned an invalid lossless response"
        )
    info = _field(result, "download_info", "downloadInfo")
    if not isinstance(info, dict):
        raise YandexAcquisitionProviderError(
            "Yandex returned no lossless download info"
        )

    response_track_id = str(_field(info, "track_id", "trackId", default="")).strip()
    if response_track_id != requested_track_id:
        raise YandexAcquisitionProviderError("Yandex returned a different track")
    quality = str(_field(info, "quality", default="")).casefold()
    if not quality:
        raise YandexAcquisitionProviderError("Yandex returned no quality label")
    codec = str(_field(info, "codec", default="")).casefold()
    if codec not in _SUPPORTED_CODECS:
        raise YandexAcquisitionProviderError("Yandex returned an unsupported codec")
    transport = str(_field(info, "transport", default="")).casefold()
    if transport != _YANDEX_LOSSLESS_TRANSPORT:
        raise YandexAcquisitionProviderError(
            "Yandex returned an unsupported transport"
        )

    raw_urls = _field(info, "urls", default=[]) or []
    if isinstance(raw_urls, (str, bytes)):
        raw_urls = [raw_urls]
    preferred_url = str(_field(info, "url", default="") or "").strip()
    candidates = [preferred_url, *(str(url).strip() for url in raw_urls)]
    download_url = next(
        (url for url in candidates if _is_audio_download_url(url, codec)),
        None,
    )
    if download_url is None:
        raise YandexAcquisitionProviderError(
            "Yandex returned no trusted audio URL"
        )

    size_value = _non_negative_int(_field(info, "size"), default=0)
    size = size_value or None
    if size is not None and size > settings.yandex_max_file_bytes:
        raise YandexAcquisitionProviderError(
            "Yandex file exceeds the configured size limit"
        )
    decryption_key = str(_field(info, "key", default="") or "").strip() or None
    if decryption_key is not None:
        try:
            decoded_key = bytes.fromhex(decryption_key)
        except ValueError as exc:
            raise YandexAcquisitionProviderError(
                "Yandex returned an invalid decryption key"
            ) from exc
        if len(decoded_key) not in {16, 24, 32}:
            raise YandexAcquisitionProviderError(
                "Yandex returned an invalid decryption key"
            )
    return YandexLosslessInfo(
        track_id=response_track_id,
        real_id=(
            str(_field(info, "real_id", "realId")).strip()
            if _field(info, "real_id", "realId") is not None
            else None
        ),
        quality=quality,
        codec=codec,
        transport=transport,
        bitrate=_non_negative_int(_field(info, "bitrate"), default=0),
        size=size,
        url=download_url,
        decryption_key=decryption_key,
    )


def _stream_to_file(
    url: str,
    target: Path,
    *,
    decryption_key: str | None = None,
) -> None:
    current_url = url
    part = target.with_suffix(f"{target.suffix}.part")
    target.parent.mkdir(parents=True, exist_ok=True)
    part.unlink(missing_ok=True)
    try:
        for _redirect in range(4):
            if not _is_allowed_download_url(current_url):
                raise YandexAcquisitionProviderError(
                    "Yandex returned an untrusted download host"
                )
            timeout = httpx.Timeout(
                connect=settings.yandex_connect_timeout_seconds,
                read=settings.yandex_read_timeout_seconds,
                write=30.0,
                pool=5.0,
            )
            with httpx.stream(
                "GET",
                current_url,
                timeout=timeout,
                follow_redirects=False,
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise YandexAcquisitionProviderError(
                            "Yandex returned an invalid download redirect"
                        )
                    current_url = urljoin(current_url, location)
                    continue
                response.raise_for_status()
                content_length = _positive_int(response.headers.get("content-length"))
                if (
                    content_length is not None
                    and content_length > settings.yandex_max_file_bytes
                ):
                    raise YandexAcquisitionProviderError(
                        "Yandex file exceeds the configured size limit"
                    )
                decryptor = None
                if decryption_key is not None:
                    decoded_key = bytes.fromhex(decryption_key)
                    decryptor = Cipher(
                        algorithms.AES(decoded_key),
                        modes.CTR(bytes(16)),
                    ).decryptor()
                written = 0
                with part.open("wb") as output:
                    for chunk in response.iter_bytes(1024 * 1024):
                        written += len(chunk)
                        if written > settings.yandex_max_file_bytes:
                            raise YandexAcquisitionProviderError(
                                "Yandex file exceeds the configured size limit"
                            )
                        output.write(decryptor.update(chunk) if decryptor else chunk)
                    if decryptor is not None:
                        output.write(decryptor.finalize())
                if written <= 0:
                    raise YandexAcquisitionProviderError(
                        "Yandex returned an empty audio file"
                    )
                part.replace(target)
                return
        raise YandexAcquisitionProviderError("Yandex returned too many redirects")
    except httpx.HTTPError as exc:
        raise YandexAcquisitionProviderError("Yandex audio download failed") from exc
    finally:
        part.unlink(missing_ok=True)


def _remux_flac_mp4(source: Path, target: Path) -> None:
    part = target.with_suffix(f"{target.suffix}.remux.part")
    part.unlink(missing_ok=True)
    try:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-map_metadata",
                "0",
                "-c:a",
                "copy",
                "-f",
                "flac",
                "-y",
                str(part),
            ],
            capture_output=True,
            timeout=settings.yandex_read_timeout_seconds,
            check=False,
        )
        if completed.returncode != 0 or not part.is_file() or part.stat().st_size <= 0:
            raise YandexAcquisitionProviderError(
                "Yandex FLAC container remux failed"
            )
        try:
            audio = FLAC(part)
        except FLACNoHeaderError as exc:
            raise YandexAcquisitionProviderError(
                "Yandex remux did not produce native FLAC"
            ) from exc
        if not isinstance(audio.info.length, (int, float)) or audio.info.length <= 0:
            raise YandexAcquisitionProviderError(
                "Yandex remux produced FLAC without duration"
            )
        part.replace(target)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise YandexAcquisitionProviderError(
            "Yandex FLAC container remux failed"
        ) from exc
    finally:
        part.unlink(missing_ok=True)


def _write_yandex_tags(
    target: Path,
    item: PlaylistItem,
    candidate: YandexSearchCandidate,
) -> None:
    artist = item.artist_raw or candidate.artist or "Unknown Artist"
    album = item.album_raw or candidate.album or "Unknown Album"
    title = item.title_raw or candidate.title or target.stem
    track_number = str(item.position + 1)
    try:
        suffix = target.suffix.casefold()
        if suffix == ".flac":
            audio = FLAC(target)
            audio["artist"] = [artist]
            audio["album"] = [album]
            audio["title"] = [title]
            audio["tracknumber"] = [track_number]
            if item.isrc:
                audio["isrc"] = [item.isrc]
            audio["comment"] = [f"Yandex Music track {candidate.yandex_id}"]
            audio.save()
        elif suffix == ".m4a":
            audio = MP4(target)
            audio["\xa9ART"] = [artist]
            audio["\xa9alb"] = [album]
            audio["\xa9nam"] = [title]
            audio["trkn"] = [(item.position + 1, 0)]
            audio["\xa9cmt"] = [f"Yandex Music track {candidate.yandex_id}"]
            if item.isrc:
                audio["----:com.apple.iTunes:ISRC"] = [item.isrc.encode("ascii")]
            audio.save()
        else:
            audio = MutagenFile(target)
            if audio is None:
                raise ValueError("unsupported audio container")
            if audio.tags is None:
                audio.add_tags()
            if not isinstance(audio.tags, ID3):
                raise ValueError("audio container does not support ID3 tags")
            for frame_id in ("TPE1", "TALB", "TIT2", "TRCK", "TSRC", "COMM"):
                audio.tags.delall(frame_id)
            audio.tags.add(TPE1(encoding=3, text=[artist]))
            audio.tags.add(TALB(encoding=3, text=[album]))
            audio.tags.add(TIT2(encoding=3, text=[title]))
            audio.tags.add(TRCK(encoding=3, text=[track_number]))
            if item.isrc:
                audio.tags.add(TSRC(encoding=3, text=[item.isrc]))
            audio.tags.add(
                COMM(
                    encoding=3,
                    lang="eng",
                    desc="",
                    text=[f"Yandex Music track {candidate.yandex_id}"],
                )
            )
            audio.save()
    except Exception as exc:
        raise YandexAcquisitionProviderError(
            "Could not write metadata to Yandex audio"
        ) from exc


def _verify_yandex_staging_file(target: Path) -> int:
    expected_container = _EXPECTED_OUTPUT_CONTAINERS.get(target.suffix.casefold())
    if expected_container is None or not target.is_file() or target.stat().st_size <= 0:
        raise YandexAcquisitionProviderError("Downloaded audio has an invalid file type")
    try:
        audio = MutagenFile(target)
    except Exception as exc:
        raise YandexAcquisitionProviderError(
            "Downloaded audio could not be parsed"
        ) from exc
    if audio is None or type(audio).__name__ != expected_container:
        raise YandexAcquisitionProviderError(
            "Downloaded audio container does not match its extension"
        )
    length = getattr(getattr(audio, "info", None), "length", None)
    if not isinstance(length, (int, float)) or length <= 0:
        raise YandexAcquisitionProviderError("Downloaded audio has no duration")
    bitrate = _non_negative_int(getattr(audio.info, "bitrate", None), default=0)
    return round(bitrate / 1000) if bitrate >= 1000 else bitrate


def download_yandex_track_to_staging(
    client: Any,
    candidate: YandexSearchCandidate,
    item: PlaylistItem,
    *,
    timestamp: int | None = None,
    key: str | None = None,
) -> tuple[list[Path], dict[str, Any]]:
    info = get_yandex_lossless_info(
        client,
        candidate.yandex_id,
        timestamp=timestamp,
        key=key,
    )

    artist_dir = safe_filename(item.artist_raw or candidate.artist, fallback="Unknown Artist")
    album_dir = safe_filename(item.album_raw or candidate.album, fallback="Unknown Album")
    title = safe_filename(item.title_raw or candidate.title, fallback=f"track-{candidate.yandex_id}")
    output_codec, output_extension = _CODEC_OUTPUT[info.codec]
    filename = (
        f"{item.position + 1:03d} - {title} "
        f"[yandex-{candidate.yandex_id}]{output_extension}"
    )
    staging_root = Path(settings.yandex_staging_path).expanduser().resolve()
    target = (staging_root / artist_dir / album_dir / filename).resolve()
    if not target.is_relative_to(staging_root):
        raise YandexAcquisitionProviderError("Unsafe Yandex staging path")
    if not target.exists():
        if info.codec == "flac-mp4":
            source = target.with_suffix(".flac-mp4.m4a")
            try:
                _stream_to_file(
                    info.url,
                    source,
                    decryption_key=info.decryption_key,
                )
                _remux_flac_mp4(source, target)
            finally:
                source.unlink(missing_ok=True)
        else:
            _stream_to_file(
                info.url,
                target,
                decryption_key=info.decryption_key,
            )
        _write_yandex_tags(target, item, candidate)
    try:
        actual_bitrate = _verify_yandex_staging_file(target)
    except YandexAcquisitionProviderError:
        target.unlink(missing_ok=True)
        raise
    return [target], {
        "codec": output_codec,
        "quality": info.quality,
        "transport": info.transport,
        "source_codec": info.codec,
        "remuxed": info.codec == "flac-mp4",
        "lossless": output_codec == "flac",
        "bitrate_kbps": actual_bitrate or info.bitrate,
        "size": info.size,
    }


def _record_yandex_attempt(
    db: Session,
    item: PlaylistItem,
    entry: dict[str, Any],
    job_id: int | None,
) -> None:
    lookup_key = provider_lookup_key(item)
    attempt = db.scalar(
        select(ProviderAttempt).where(
            ProviderAttempt.provider == "yandex",
            ProviderAttempt.lookup_key == lookup_key,
        )
    )
    if attempt is None:
        attempt = ProviderAttempt(provider="yandex", lookup_key=lookup_key)
        db.add(attempt)
    attempt.playlist_item_id = item.id
    attempt.job_id = job_id
    attempt.status = str(entry.get("status") or "failed")
    attempt.provider_item_id = entry.get("yandex_track_id")
    attempt.selection_method = entry.get("selection")
    attempt.error_code = entry.get("error")
    db.flush()


def record_yandex_download_attempts(
    db: Session,
    downloads: dict[str, Any],
    playlist_items: dict[int, PlaylistItem],
    job_id: int | None,
) -> None:
    for entry in downloads.get("items", []):
        item = playlist_items.get(int(entry["item_id"]))
        if item is not None:
            _record_yandex_attempt(db, item, entry, job_id)


def fetch_missing_yandex_tracks(
    db: Session,
    playlist: Playlist,
    client: Any,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    job_id: int | None = None,
) -> tuple[dict[str, Any], list[Path]]:
    all_missing, items = missing_provider_items(db, playlist, "yandex")
    batch_size = settings.yandex_max_tracks_per_run
    batch_count = (len(items) + batch_size - 1) // batch_size
    entries: list[dict[str, Any]] = [
        {
            "item_id": item.id,
            "artist": item.artist_raw,
            "title": item.title_raw,
            "status": "queued",
        }
        for item in items
    ]
    summary: dict[str, Any] = {
        "playlist_id": playlist.id,
        "total_missing": len(all_missing),
        "eligible_total": len(items),
        "skipped_same_source": len(all_missing) - len(items),
        "batch_size": batch_size,
        "batch_count": batch_count,
        "current_batch": 0,
        "current_batch_size": 0,
        "batch_processed": 0,
        "batch_pause_seconds": 0,
        "processed": 0,
        "attempted": 0,
        "downloaded": 0,
        "not_found": 0,
        "ambiguous": 0,
        "failed": 0,
        "items": entries,
    }
    collected: list[Path] = []
    if progress_callback is not None:
        progress_callback(summary)
    is_yandex_playlist = playlist.source is not None and playlist.source.service == ServiceEnum.yandex
    for batch_index, batch_start in enumerate(range(0, len(items), batch_size)):
        batch_items = items[batch_start : batch_start + batch_size]
        batch_entries = entries[batch_start : batch_start + batch_size]
        summary.update(
            current_batch=batch_index + 1,
            current_batch_size=len(batch_items),
            batch_processed=0,
            batch_pause_seconds=0,
            batch_state="running",
        )
        if progress_callback is not None:
            progress_callback(summary)
        for item, entry in zip(batch_items, batch_entries, strict=True):
            entry["status"] = "searching"
            if progress_callback is not None:
                progress_callback(summary)
            query = f"{item.artist_raw or ''} {item.title_raw or ''}".strip()
            try:
                summary["attempted"] += 1
                direct = (
                    _direct_yandex_candidate(client, item.external_track_id)
                    if is_yandex_playlist and item.external_track_id
                    else None
                )
                if direct is not None:
                    best, method = direct, "source_id"
                else:
                    if not query:
                        raise YandexAcquisitionProviderError(
                            "Playlist item has no artist/title query"
                        )
                    candidates = search_yandex_tracks(client, query, limit=10)
                    best, method = choose_track_candidate(
                        artist_raw=item.artist_raw,
                        title_raw=item.title_raw,
                        album_raw=item.album_raw,
                        isrc=item.isrc,
                        duration_ms=item.duration_ms,
                        candidates=candidates,
                    )
                if best is None:
                    status = "ambiguous" if method == "ambiguous" else "not_found"
                    summary[status] += 1
                    entry["status"] = status
                    entry["selection"] = method
                else:
                    entry["yandex_track_id"] = best.yandex_id
                    entry["selection"] = method
                    entry["status"] = "downloading"
                    if progress_callback is not None:
                        progress_callback(summary)
                    files, quality = download_yandex_track_to_staging(
                        client, best, item
                    )
                    collected.extend(files)
                    summary["downloaded"] += 1
                    entry.update(
                        status="downloaded",
                        files=[str(path) for path in files],
                        **quality,
                    )
            except YandexAcquisitionError as exc:
                summary["failed"] += 1
                entry["status"] = "failed"
                entry["error"] = type(exc).__name__
                entry["error_detail"] = str(exc)
            if entry["status"] != "downloaded":
                _record_yandex_attempt(db, item, entry, job_id)
            summary["processed"] += 1
            summary["batch_processed"] += 1
            if progress_callback is not None:
                progress_callback(summary)
            if settings.yandex_request_delay_seconds:
                time.sleep(settings.yandex_request_delay_seconds)
        if batch_index + 1 < batch_count:
            summary["batch_state"] = "paused"
            summary["batch_pause_seconds"] = settings.yandex_batch_delay_seconds
            if progress_callback is not None:
                progress_callback(summary)
            if settings.yandex_batch_delay_seconds:
                time.sleep(settings.yandex_batch_delay_seconds)
    return summary, collected
