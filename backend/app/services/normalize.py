"""Stable normalization keys for playlist imports and library matching."""

from __future__ import annotations

import re
import unicodedata
from typing import TypedDict


class PlaylistItemNormalization(TypedDict):
    """Normalized fields ready to be stored on a ``PlaylistItem``."""

    artist_norm: str
    title_norm: str
    album_norm: str


_PUNCTUATION_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u2032": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u00ab": '"',
        "\u00bb": '"',
        "\u2039": '"',
        "\u203a": '"',
        "\u2033": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
    }
)

_SUFFIX_CHAIN_RE = re.compile(r"(?P<chain>(?:\s*[\(\[][^()\[\]]*[\)\]])+)\s*$")
_SUFFIX_GROUP_RE = re.compile(
    r"\s*(?P<open>[\(\[])\s*(?P<label>[^()\[\]]*?)\s*(?P<close>[\)\]])"
)
_FEATURE_RE = re.compile(
    r"^(?:feat(?:uring)?|ft)\.?(?:$|(?:\s*[:/\-]\s*|\s+).+)$",
    re.IGNORECASE,
)
_REMASTER_RE = re.compile(
    r"^(?:\d{4}\s*[-:/]?\s*)?"
    r"(?:digital\s+)?remaster(?:ed)?"
    r"(?:\s*[-:/]?\s*(?:\d{4}|version))*$",
    re.IGNORECASE,
)
_LIVE_RE = re.compile(
    r"^(?:"
    r"live"
    r"|live\s+(?:version|recording|edit)(?:\s+.*)?"
    r"|live\s+(?:at|from|in|on)\b.+"
    r"|live\s+@\s*.+"
    r"|\d{4}\s+live(?:\s+(?:version|recording|edit))?"
    r")$",
    re.IGNORECASE,
)
_SEPARATED_LIVE_SUFFIX_RE = re.compile(
    r"^(?P<base>.+?)\s*[-:;/]\s*"
    r"(?P<label>(?:"
    r"live"
    r"|live\s+(?:version|recording|edit)(?:\s+.*)?"
    r"|live\s+(?:at|from|in|on)\b.+"
    r"|live\s+@\s*.+"
    r"|\d{4}\s+live(?:\s+(?:version|recording|edit))?"
    r"))\s*$",
    re.IGNORECASE,
)
_CONTEXTUAL_LIVE_SUFFIX_RE = re.compile(
    r"^(?P<base>.+?)\s+"
    r"(?P<label>(?:"
    r"live\s+(?:version|recording|edit)(?:\s+.*)?"
    r"|live\s+(?:at|from|in|on)\b.+"
    r"|live\s+@\s*.+"
    r"|\d{4}\s+live(?:\s+(?:version|recording|edit))?"
    r"))\s*$",
    re.IGNORECASE,
)
_DANGLING_SEPARATOR_RE = re.compile(r"\s*[-:;,/]\s*$")
_WHITESPACE_RE = re.compile(r"\s+")
_HYPHEN_SPACING_RE = re.compile(r"\s*-\s*")


def _is_edition_noise(label: str) -> bool:
    normalized = _WHITESPACE_RE.sub(" ", label).strip().casefold()
    return bool(
        _FEATURE_RE.fullmatch(normalized)
        or _REMASTER_RE.fullmatch(normalized)
        or _LIVE_RE.fullmatch(normalized)
    )


def _strip_noise_suffixes(value: str) -> str:
    """Remove only recognized noise groups from the final bracket chain."""

    match = _SUFFIX_CHAIN_RE.search(value)
    if match is None:
        return value

    base = value[: match.start()].rstrip()
    if not base:
        # Never turn a non-empty source value into an empty matching key.
        return value

    kept_groups: list[str] = []
    removed_noise = False
    for group in _SUFFIX_GROUP_RE.finditer(match.group("chain")):
        if _is_edition_noise(group.group("label")):
            removed_noise = True
            continue
        kept_groups.append(group.group(0).strip())

    if not removed_noise:
        return value

    base = _DANGLING_SEPARATOR_RE.sub("", base).rstrip()
    if kept_groups:
        return f"{base} {' '.join(kept_groups)}"
    return base


def _strip_bare_live_suffix(value: str) -> str:
    """Remove unbracketed live editions only when their context is explicit."""

    match = _SEPARATED_LIVE_SUFFIX_RE.fullmatch(value)
    if match is None:
        match = _CONTEXTUAL_LIVE_SUFFIX_RE.fullmatch(value)
    if match is None or not _is_edition_noise(match.group("label")):
        return value
    base = _DANGLING_SEPARATOR_RE.sub("", match.group("base")).strip()
    return base or value


def normalize_text(value: str | None) -> str:
    """Return a deterministic comparison key for music metadata.

    The operation is intentionally conservative: it standardizes Unicode and
    punctuation, removes bracketed feature/remaster/live suffixes, and removes
    unbracketed live editions only with an explicit separator or context.
    Remix, version, edit and deluxe labels remain part of the key.
    """

    if value is None:
        return ""

    normalized = unicodedata.normalize("NFKC", str(value))
    normalized = normalized.translate(_PUNCTUATION_TRANSLATION)
    normalized = _strip_noise_suffixes(normalized)
    normalized = _strip_bare_live_suffix(normalized)
    normalized = normalized.casefold()
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    return _HYPHEN_SPACING_RE.sub("-", normalized)


def normalize_artist(value: str | None) -> str:
    """Normalize an artist name for matching."""

    return normalize_text(value)


def normalize_title(value: str | None) -> str:
    """Normalize a track title for matching."""

    return normalize_text(value)


def normalize_album(value: str | None) -> str:
    """Normalize an album title for matching."""

    return normalize_text(value)


def normalize_playlist_item(
    artist_raw: str | None,
    title_raw: str | None,
    album_raw: str | None,
) -> PlaylistItemNormalization:
    """Build all normalized fields for an imported playlist item."""

    return {
        "artist_norm": normalize_artist(artist_raw),
        "title_norm": normalize_title(title_raw),
        "album_norm": normalize_album(album_raw),
    }
