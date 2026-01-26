import os
from typing import Optional, TypedDict

from app.constants import DOWNLOAD_PATH, PREFER_TIDAL_NAMING, SONG_QUALITY
from app.utils import format_text_for_os


class MatchType:
    EXACT = "exact"
    SUBSTRING = "substring"
    SKIP = "skip"
    NONE = "none"


class TrackFindType:
    TITLE = "title"
    ARTIST = "artist"
    ARTISTS_ALL = "artists_all"
    ALBUM = "album"


class CompletedDownload(TypedDict):
    path: str
    lyrics: bool
    unsynced_exists: Optional[bool]
    tidal_title: str
    tidal_artists: str
    tidal_album: str
    duration: int


def get_artist_names(artists) -> list[str]:
    if isinstance(artists, dict):
        artists = [artists]
    return [artist["name"] for artist in artists]


def get_download_path(artist: str, album: str) -> str:
    return os.path.join(
        DOWNLOAD_PATH,
        format_text_for_os(artist),
        format_text_for_os(album),
    )


class SpotifyTrackData:
    def __init__(self, track_data: dict):
        self.title: str = track_data["title"]
        self.full_title: str = track_data["full_title"]
        self.artist: str = track_data["artist"]
        self.artists: list[str] = track_data["artists_all"]
        self.album: str = track_data["album"]
        self.index: int = track_data["index"]


class TidalTrackData:
    def __init__(self, track_data: dict):
        self.title: str = track_data.get("title", "")
        self.artist: str = track_data.get("artist", {}).get("name", "")
        self.artists: list[str] = get_artist_names(track_data.get("artists", []))
        self.album: str = track_data.get("album", {}).get("title", "")
        self.version: str = track_data.get("version", "")
        self.track_number: int = track_data.get("trackNumber", 0)
        self.number_of_tracks: int = 0
        self.release_date: str = ""

        self.duration: int = track_data.get("duration", 0)
        self.cover: str = track_data.get("album", {}).get("cover", "")
        self.id: int = track_data.get("id", 0)
        self.album_id: int = track_data.get("album", {}).get("id")

        if self.version:
            self.title = f"{self.title} ({self.version})"


class DownloadTrackData:
    def __init__(
        self,
        download_url: str,
        spotify_data: SpotifyTrackData,
        tidal_data: TidalTrackData,
    ):
        title = tidal_data.title if PREFER_TIDAL_NAMING else spotify_data.title
        self.title = format_text_for_os(title)
        self.full_title = spotify_data.full_title
        self.spotify_title = spotify_data.title
        self.tidal_title = tidal_data.title

        self.artist = tidal_data.artist if PREFER_TIDAL_NAMING else spotify_data.artist
        self.spotify_artist = spotify_data.artist
        self.spotify_artists = spotify_data.artists
        tidal_artists = tidal_data.artists
        self.tidal_artists = (
            ", ".join(tidal_artists) if len(tidal_artists) > 1 else tidal_data.artist
        )  # Artist1, Artist2 or Artist1

        self.album = tidal_data.album if PREFER_TIDAL_NAMING else spotify_data.album
        self.tidal_album = tidal_data.album

        self.cover = tidal_data.cover
        self.index = spotify_data.index
        self.track_number = tidal_data.track_number
        self.number_of_tracks = tidal_data.number_of_tracks
        self.release_date = tidal_data.release_date
        self.duration = tidal_data.duration

        self.extension = ".flac" if SONG_QUALITY.upper() == "LOSSLESS" else ".m4a"
        self.url = download_url
        self.download_path = get_download_path(self.artist, self.album)
