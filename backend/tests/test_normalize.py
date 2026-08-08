import pytest

from app.services.normalize import (
    normalize_album,
    normalize_artist,
    normalize_playlist_item,
    normalize_text,
    normalize_title,
)


def test_normalize_text_applies_nfkc_casefold_and_punctuation_mapping():
    value = "  ＢＥＹＯＮＣÉ　— “Straße”  "

    assert normalize_text(value) == 'beyoncé-"strasse"'


def test_normalize_text_unifies_apostrophes_and_hyphen_spacing():
    assert normalize_text("L’Impératrice – Vanille") == "l'impératrice-vanille"
    assert normalize_text("L'Impératrice-Vanille") == "l'impératrice-vanille"


@pytest.mark.parametrize(
    "value",
    [
        "Track (feat. Guest)",
        "Track [Featuring Guest]",
        "Track (ft Guest)",
        "Track (2011 Remaster)",
        "Track [Remastered 2009]",
        "Track (Live)",
        "Track [Live at Wembley]",
        "Track (1994 Live)",
    ],
)
def test_normalize_text_removes_declared_bracketed_suffix_noise(value):
    assert normalize_text(value) == "track"


@pytest.mark.parametrize(
    "value",
    [
        "Track - Live",
        "Track: Live Version",
        "Track Live at Wembley",
        "Track / 1994 Live",
    ],
)
def test_normalize_text_removes_bare_live_suffix(value):
    assert normalize_text(value) == "track"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Long Live", "long live"),
        ("I Want to Live", "i want to live"),
        ("Track Live", "track live"),
    ],
)
def test_normalize_text_preserves_live_as_part_of_a_title(value, expected):
    assert normalize_text(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Track (Club Remix)", "track (club remix)"),
        ("Track [Radio Version]", "track [radio version]"),
        ("Track (Taylor’s Version)", "track (taylor's version)"),
        ("Track (Live Wire Remix)", "track (live wire remix)"),
        ("Track (Club Remix Remastered)", "track (club remix remastered)"),
        ("Track (Remastered by Bob)", "track (remastered by bob)"),
        ("Album (Deluxe Edition)", "album (deluxe edition)"),
        ("Track (Acoustic Edit)", "track (acoustic edit)"),
    ],
)
def test_normalize_text_preserves_meaningful_editions(value, expected):
    assert normalize_text(value) == expected


def test_normalize_text_handles_multiple_suffix_groups_independently():
    assert (
        normalize_text("Track (Remastered 2011) [Live] (Club Remix)")
        == "track (club remix)"
    )
    assert normalize_text("Track (Club Remix) [Live]") == "track (club remix)"


def test_normalize_text_does_not_remove_non_suffix_or_only_value():
    assert normalize_text("Live (Remaster) Sessions") == "live (remaster) sessions"
    assert normalize_text("(Live)") == "(live)"


@pytest.mark.parametrize("value", [None, "", " \t\n ", "　"])
def test_normalize_text_handles_empty_values(value):
    assert normalize_text(value) == ""


def test_normalize_text_is_idempotent():
    once = normalize_text("  Song — Name (feat. Guest) ")

    assert normalize_text(once) == once


def test_field_specific_helpers_share_normalization_contract():
    assert normalize_artist("Artist (feat. Guest)") == "artist"
    assert normalize_title("Title [Live]") == "title"
    assert normalize_album("Album (2024 Remaster)") == "album"


def test_normalize_playlist_item_returns_all_database_keys():
    assert normalize_playlist_item(
        "  ARTIST  ",
        "Song (feat. Guest)",
        None,
    ) == {
        "artist_norm": "artist",
        "title_norm": "song",
        "album_norm": "",
    }
