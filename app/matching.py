import logging
import re

from music_metadata_filter.functions import (
    remove_feature,
    remove_reissue,
    remove_remastered,
    remove_version,
)

from app.constants import (
    KEYWORDS_SONG_COLLECTIONS,
    KEYWORDS_SONG_EDITS,
    SPOTIFY_TO_TIDAL_NAMING,
)
from app.types import MatchType, SpotifyTrackData, TidalTrackData, TrackFindType
from app.utils import normalize


def custom_clean_title(text: str) -> str:
    """
    Normalize a track title for matching.
    Removes:
        - feat/ft/featuring (with or without brackets)
        - remastered tags
        - radio edit / single version / album version
        - any extra whitespace
    """

    t = text

    # Remove bracketed "with / feat / featuring"
    t = re.sub(
        r"[\(\[\{]\s*(?:with|feat\.?|featuring)\s+.*?[\)\]\}]",
        "",
        t,
        flags=re.IGNORECASE,
    )
    # Remove inline feat/ft/featuring outside brackets
    t = re.sub(r"\b(?:feat\.?|ft\.?|featuring)\s+[^-()]+", "", t, flags=re.IGNORECASE)

    # Remove remaster / version / radio / single / album
    t = re.sub(r"\(.*remaster(ed)?\)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\bremaster(ed)?\b", "", t, flags=re.IGNORECASE)
    t = re.sub(
        r"radio edit|single version|album version|version", "", t, flags=re.IGNORECASE
    )

    # Remove "from ..." patterns
    t = re.sub(r"\s*[-–]\s*from\s+.*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\(from\s+.*?\)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\[from\s+.*?\]", "", t, flags=re.IGNORECASE)

    # Normalize separators: replace dash between title and mix/edition with space
    t = re.sub(r"\s*[-–]\s*", " ", t)

    # Remove leftover parentheses/brackets around trailing info
    t = re.sub(r"[\(\[\{]+(.*?)[\)\]\}]+", r"\1", t)

    # Collapse multiple spaces and remove trailing punctuation
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[\s\-–:]+$", "", t)

    return t.strip()


def cleanse_track(text: str, field: str) -> str:
    """Clean text based on field type for better matching.
    Removes: features, versions, remasters, reissues as appropriate.
    """

    if field == TrackFindType.ARTIST or field == TrackFindType.ARTISTS_ALL:
        return remove_feature(text)
    elif field == TrackFindType.TITLE:
        return custom_clean_title(
            remove_version(remove_remastered(remove_feature(remove_version(text))))
        )
    elif field == TrackFindType.ALBUM:
        return remove_reissue(remove_version(remove_remastered(text)))
    return text


def is_song_edit(title: str) -> bool:
    """Check if the title indicates an edited version of a song, such as 'Radio Edit' or 'Club Mix'."""

    title_casefold = title.casefold()
    return any(keyword in title_casefold for keyword in KEYWORDS_SONG_EDITS)


def is_collection(title: str) -> bool:
    """Check if the title indicates a collection, such as 'Greatest Hits' or 'Anthology'."""

    title_casefold = title.casefold()
    return any(keyword in title_casefold for keyword in KEYWORDS_SONG_COLLECTIONS)


def compare_results(search: str, found: str, field: str) -> MatchType:
    """Compare two strings and determine the match type: exact, substring, skip, or none."""

    search_clean = cleanse_track(search, field)
    found_clean = cleanse_track(found, field)
    search_clean_normalized = normalize(search_clean)
    found_clean_normalized = normalize(found_clean)

    # Debug
    logging.debug(
        f"Comparing '{search_clean_normalized}' with '{found_clean_normalized}' for field '{field}'"
    )

    if search_clean_normalized == found_clean_normalized:
        logging.debug(f"Found exact match for '{search_clean_normalized}'")
        return MatchType.EXACT

    if field == TrackFindType.ALBUM and is_collection(found_clean):
        logging.debug(f"Found collection match for '{search_clean_normalized}'")
        return MatchType.SKIP

    if len(search_clean_normalized) < 3 or len(found_clean_normalized) < 3:
        logging.debug(f"Found short match for '{search_clean_normalized}'")
        return MatchType.SKIP

    if field == TrackFindType.ARTISTS_ALL:
        search_parts = set(search_clean_normalized.split(";"))
        found_parts = set(found_clean_normalized.split(";"))
    else:
        search_parts = set(search_clean_normalized.split())
        found_parts = set(found_clean_normalized.split())

    for search_part in search_parts:
        for found_part in found_parts:
            if (
                len(search_part) >= 3
                and len(found_part) >= 3
                and (search_part in found_part or found_part in search_part)
            ):
                logging.debug(
                    f"Found substring match for '{search_clean_normalized}' in '{found_clean_normalized}'"
                )
                return MatchType.SUBSTRING

    return MatchType.NONE


def generate_no_match_error(
    spotify_track: SpotifyTrackData, tidal_track: TidalTrackData
) -> dict:
    """Generate a detailed error message for no match found between Spotify and Tidal tracks."""

    return {
        "reason": "No suitable match found",
        "comparing_title": f"'{spotify_track.title}' vs '{tidal_track.title}'",
        "comparing_artists": f"'{spotify_track.artist}' vs '{tidal_track.artist}'",
        "comparing_album": f"'{spotify_track.album}' vs '{tidal_track.album}'",
    }


def fix_spotify_to_tidal_namings(text: str, field: str) -> str:
    """Replace Spotify-specific naming conventions with Tidal equivalents in the given text.
    Examples: YMCA on Spotify to Y.M.C.A. on Tidal
    """

    for key, value in SPOTIFY_TO_TIDAL_NAMING.items():
        if value["type"] == field and key in text:
            text = text.replace(key, value["replacement"])
    return text
