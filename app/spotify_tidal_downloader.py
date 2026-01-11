import json
import re
import httpx
import logging
import os
import csv
import asyncio
import tempfile

from datetime import datetime
from mutagen.mp4 import MP4, MP4Cover
from mutagen.flac import FLAC, Picture

from app.utils import (
    normalize,
    format_text_for_os,
    load_json_file,
    save_json_file,
    base64_decode,
)
from app.constants import (
    PLAYLIST_FILE,
    RETRY_FAILED,
    SONG_QUALITY,
    DOWNLOAD_PATH,
    CONCURRENT_DOWNLOADS,
    PREFER_TIDAL_NAMING,
    SPOTIFY_TO_TIDAL_NAMING,
    CACHE_COMPLETED_DOWNLOADS_PATH,
    CACHE_FAILED_DOWNLOADS_PATH,
)
from music_metadata_filter.functions import (
    remove_feature,
    remove_remastered,
    remove_version,
    remove_reissue,
)


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


class SpotifyTidalDownloader:
    def __init__(
        self, session: httpx.AsyncClient, api_instance: str, streaming_instance: str
    ):
        self.session = session
        self.api_instance = api_instance
        self.streaming_instance = streaming_instance
        self.spotify_tracks = None
        self.failed_downloads = {}
        self.completed_downloads = {}
        self.download_queue = asyncio.Queue()

    async def run(self) -> None:
        self.spotify_tracks = load_spotify_playlist()
        if not self.spotify_tracks:
            return

        self.failed_downloads = load_json_file(CACHE_FAILED_DOWNLOADS_PATH)
        self.completed_downloads = load_json_file(CACHE_COMPLETED_DOWNLOADS_PATH)

        logging.info("🔄 Starting download workers...")
        workers = [
            asyncio.create_task(self._download_worker())
            for _ in range(CONCURRENT_DOWNLOADS)
        ]

        index = 1
        for spotify_track in self.spotify_tracks.values():
            full_title = f"{spotify_track['artist']} - {spotify_track['title']}"

            if self._is_downloaded(full_title):
                logging.info(f"⏭️ [{index:02d}] Skipping downloaded track: {full_title}")
                index += 1
                continue

            if self._is_failed(full_title):
                logging.info(f"⏭️ [{index:02d}] Skipping failed track: {full_title}")
                index += 1
                continue

            queries = await self._get_queries(spotify_track, index)
            track_data = await self._search_track(queries, spotify_track, index)
            if track_data:
                await self.download_queue.put(track_data)
            index += 1

        await self.download_queue.join()
        for w in workers:
            w.cancel()

        if self.failed_downloads:
            logging.info(f"😞 Failed downloads: {len(self.failed_downloads)}.")

        logging.info(
            f"✅ Completed {len(self.completed_downloads)}/{len(self.spotify_tracks)} ({round(len(self.completed_downloads) / len(self.spotify_tracks) * 100, 2)})% downloads."
        )

    async def _get_queries(
        self, spotify_track_data: dict[str, str], index: int
    ) -> set[str]:
        query_track_name = (
            f"{spotify_track_data['artist']} - {spotify_track_data['title']}"
        )
        logging.info(f"🔍 [{index:02d}] Searching: {query_track_name}")
        search_queries = [query_track_name]

        # Query with all artists if available
        if spotify_track_data["artists_all"]:
            search_queries.append(
                f"{';'.join(spotify_track_data['artists_all'])} - {spotify_track_data['title']}"
            )

        # Remove artist, example: "[Artist1; Artist2 - ]Track Title - Special Edition"
        parts = query_track_name.split(" - ")
        search_queries.append(" - ".join(parts[1:]))

        # Remove last part, example: "Track Title[ - Special Edition]"
        if len(parts) > 2:
            search_queries.append(" - ".join(parts[:-1]))
            # Remove last part and first part, example: "[Artist -]Track Title[ - Special Edition]"
            search_queries.append(" - ".join(parts[1:-1]))

        # Clean title from feature (feat. Artist)
        cleaned_title = remove_feature(spotify_track_data["title"])
        search_queries.append(f"{spotify_track_data['artist']} - {cleaned_title}")
        search_queries.append(cleaned_title)

        # Search only artist
        search_queries.append(spotify_track_data["artist"])

        return set(search_queries)

    async def _search_track(
        self, queries: list[str], spotify_track_data: dict[str, str], index: int
    ) -> dict:
        failed_track = {"reason": "No results found."}
        full_title = f"{spotify_track_data['artist']} - {spotify_track_data['title']}"
        logging.debug(f"Searching Monochrome for queries: {queries}")

        for query in queries:
            logging.debug("----- Match Debugging -----")
            logging.debug(f"Searching Monochrome for query: '{query}'")
            try:
                result = await self.session.get(
                    f"{self.api_instance}/search/", params={"s": query}
                )
                found_tracks = result.json()["data"]["items"]
                if not found_tracks:
                    continue

                tidal_track, failed_track = await self._match_track(
                    found_tracks, spotify_track_data, index
                )
                if tidal_track:
                    if self.failed_downloads.get(full_title):
                        del self.failed_downloads[full_title]
                        save_json_file(
                            CACHE_FAILED_DOWNLOADS_PATH, self.failed_downloads
                        )

                    return tidal_track

            except (httpx.HTTPError, json.JSONDecodeError, TypeError, KeyError) as e:
                logging.info(f"❌ No results for query '{query}': {e}")
                continue

        logging.info(f"❌ {failed_track['reason']}")
        if not self.failed_downloads.get(full_title):
            self.failed_downloads[full_title] = failed_track
            save_json_file(CACHE_FAILED_DOWNLOADS_PATH, self.failed_downloads)
        return {}

    async def _match_track(
        self, found_tracks: list[dict], spotify_track_data: dict[str, str], index: int
    ) -> tuple:
        download_url = ""
        for tidal_track in found_tracks:
            matched = False

            s_track_title = spotify_track_data["title"]
            s_track_artist = spotify_track_data["artist"]
            s_track_artists = spotify_track_data["artists_all"]
            s_track_album = spotify_track_data["album"]

            t_track_title = tidal_track["title"]
            t_track_artists = get_artist_names(tidal_track["artist"])
            t_track_artist = t_track_artists[0]
            t_track_album = tidal_track["album"]["title"]
            t_track_version = tidal_track.get("version", "")

            if t_track_version:
                t_track_title = f"{t_track_title} ({t_track_version})"

            error_message = ""
            is_edit = not is_song_edit(s_track_title) and is_song_edit(t_track_title)

            logging.debug(
                f"Edit check: {s_track_title} vs {t_track_title} => {is_edit}"
            )
            if is_edit:
                error_message = f"Edit detected: {s_track_title} [Spotify] vs {t_track_title} [Tidal]"
                continue

            title_match = is_match(s_track_title, t_track_title, TrackFindType.TITLE)
            logging.debug("---------------------------")
            if title_match == MatchType.NONE:
                error_message = f"Title mismatch: {s_track_title} [Spotify] vs {t_track_title} [Tidal]"
                continue

            artists_all_match = MatchType.NONE
            if s_track_artists and len(t_track_artists) > 1:
                artists_all_match = is_match(
                    ";".join(s_track_artists),
                    ";".join(t_track_artists),
                    TrackFindType.ARTISTS_ALL,
                )
                logging.debug("---------------------------")

            artist_match = is_match(
                s_track_artist, t_track_artist, TrackFindType.ARTIST
            )
            logging.debug("---------------------------")
            if artist_match == MatchType.NONE and artists_all_match == MatchType.NONE:
                error_message = f"Artist mismatch: {s_track_artist} [Spotify] vs {t_track_artist} [Tidal]"
                continue

            if (
                artist_match == MatchType.EXACT or artists_all_match == MatchType.EXACT
            ) and title_match == MatchType.EXACT:
                matched = True
                break

            # Check if track is a single (title matches album title) in either service
            is_single = (
                cleanse_track(s_track_title, TrackFindType.TITLE).casefold()
                == cleanse_track(s_track_album, TrackFindType.TITLE).casefold()
                or cleanse_track(t_track_title, TrackFindType.TITLE).casefold()
                == cleanse_track(t_track_album, TrackFindType.TITLE).casefold()
            )
            logging.debug(f"Single check: {is_single}")

            album_match = is_match(s_track_album, t_track_album, TrackFindType.ALBUM)
            logging.debug("---------------------------")
            if album_match == MatchType.NONE and not is_single:
                error_message = f"Album mismatch: {s_track_album} [Spotify] vs {t_track_album} [Tidal]"
                continue

            if not error_message:
                matched = True
                break

        if matched:
            download_url = await self._get_download_url(tidal_track["id"])
            if not download_url:
                error_message = (
                    f"Failed to get download URL for track '{t_track_title}'"
                )
                matched = False

        if matched:
            track_data = {
                "url": download_url,
                "title": t_track_title if PREFER_TIDAL_NAMING else s_track_title,
                "artist": t_track_artist if PREFER_TIDAL_NAMING else s_track_artist,
                "album": t_track_album if PREFER_TIDAL_NAMING else s_track_album,
                "cover": tidal_track["album"].get("cover"),
                "trackNumber": tidal_track.get("trackNumber", 0),
                "releaseDate": tidal_track.get("streamStartDate", ""),
                "audioQuality": SONG_QUALITY.upper(),
                "spotify_title": s_track_title,
                "spotify_artist": s_track_artist,
            }

            logging.info(
                f"👀 [{index:02d}] Found: {t_track_title.strip()} by {t_track_artist.strip()}"
            )
            return track_data, {}

        error_message = {
            "comparing_title": f"'{s_track_title}' vs '{t_track_title}'",
            "comparing_artists": f"'{s_track_artist}' vs '{t_track_artist}'",
            "comparing_album": f"'{s_track_album}' vs '{t_track_album}'",
            "reason": error_message,
        }
        return {}, error_message

    async def _get_download_url(self, track_id: str) -> str:
        try:
            response = await self.session.get(
                f"{self.streaming_instance}/track/",
                params={"id": track_id, "quality": SONG_QUALITY.upper()},
            )
            data = response.json().get("data")
            if not data:
                return ""

            manifest = json.loads(base64_decode(data["manifest"]))
            download_url = manifest["urls"][0]
            return download_url

        except json.JSONDecodeError as e:
            logging.error(f"❌ Failed to decode JSON response: {e}")
        except httpx.HTTPError as e:
            logging.error(f"❌ HTTP error while fetching download URL: {e}")

        return ""

    async def _download_worker(self):
        async with httpx.AsyncClient() as client:
            while True:
                track: dict = await self.download_queue.get()
                full_title = f"{track['spotify_artist']} - {track['spotify_title']}"

                try:
                    url = track["url"]

                    track_download_dir = get_download_path(track)
                    os.makedirs(track_download_dir, exist_ok=True)

                    track_title = format_text_for_os(track["title"])
                    ext = ".m4a" if track["audioQuality"].upper() == "HIGH" else ".flac"
                    save_path = os.path.join(track_download_dir, f"{track_title}{ext}")

                    async with client.stream("GET", url) as response:
                        response.raise_for_status()
                        with open(save_path, "wb") as f:
                            async for chunk in response.aiter_bytes(8192):
                                f.write(chunk)

                    await self._add_metadata(track, ext, save_path)
                    logging.info(f"💾 Downloaded: {track['title']}")

                    self.completed_downloads[full_title] = {
                        "path": os.path.normpath(save_path),
                        "date": datetime.now().isoformat(),
                    }
                    save_json_file(
                        CACHE_COMPLETED_DOWNLOADS_PATH, self.completed_downloads
                    )

                except Exception as e:
                    logging.error(f"❌ Failed to download {track['title']}: {e}")
                    self.failed_downloads[full_title] = {"reason": str(e)}
                    save_json_file(CACHE_FAILED_DOWNLOADS_PATH, self.failed_downloads)

                finally:
                    self.download_queue.task_done()

    async def _add_metadata(self, track: dict, ext: str, save_path: str) -> None:
        cover_path = None
        cover_id = track.get("cover")

        if cover_id:
            cover_url = (
                "https://resources.tidal.com/images/"
                + "/".join(cover_id.split("-"))
                + "/1280x1280.jpg"
            )
            with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp_cover:
                cover_path = tmp_cover.name

            async with self.session.stream("GET", cover_url) as resp:
                resp.raise_for_status()
                with open(cover_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(8192):
                        f.write(chunk)

        if ext == ".m4a":
            audio = MP4(save_path)

            primary_artist = track["artist"]
            all_artists = track.get("artists_all") or []

            audio["\xa9nam"] = track["title"]
            audio["\xa9alb"] = track["album"]

            audio["aART"] = primary_artist

            if all_artists:
                audio["\xa9ART"] = all_artists
            else:
                audio["\xa9ART"] = primary_artist

            audio["trkn"] = [(track.get("trackNumber", 0), 0)]
            audio["\xa9day"] = track.get("releaseDate")

            if cover_path and os.path.exists(cover_path):
                with open(cover_path, "rb") as img:
                    audio["covr"] = [
                        MP4Cover(img.read(), imageformat=MP4Cover.FORMAT_JPEG)
                    ]

            audio.save()

        else:
            audio = FLAC(save_path)

            audio["title"] = track["title"]
            audio["artist"] = track["artist"]
            audio["album"] = track["album"]
            audio["tracknumber"] = str(track.get("trackNumber", 0))
            audio["date"] = track.get("releaseDate")

            if cover_path and os.path.exists(cover_path):
                pic = Picture()
                with open(cover_path, "rb") as f:
                    pic.data = f.read()
                pic.type = 3
                pic.mime = "image/jpeg"
                audio.add_picture(pic)

            audio.save()

        if cover_path and os.path.exists(cover_path):
            os.remove(cover_path)

    def _is_downloaded(self, full_title: str) -> bool:
        """Check if the track is already downloaded based on its info."""
        return self.completed_downloads.get(full_title) is not None

    def _is_failed(self, full_title: str) -> bool:
        """Check if the track download has previously failed."""
        if RETRY_FAILED:
            return False
        return self.failed_downloads.get(full_title) is not None


def is_song_edit(title: str) -> bool:
    title_casefold = title.casefold()
    return (
        "remix" in title_casefold
        or "edit" in title_casefold
        or "slowed" in title_casefold
        or "instrumental" in title_casefold
    )


def get_download_path(track: dict) -> str:
    return os.path.join(
        DOWNLOAD_PATH,
        format_text_for_os(track["artist"]),
        format_text_for_os(track["album"]),
    )


def is_collection(title: str) -> bool:
    title_casefold = title.casefold()
    collection_keywords = [
        "greatest hits",
        "best of",
        "anthology",
        "compilation",
        "collection",
        "box set",
        "hits",
        "classics",
    ]
    return any(keyword in title_casefold for keyword in collection_keywords)


def is_match(search: str, found: str, field: str) -> MatchType:
    s_clean = cleanse_track(search, field)
    f_clean = cleanse_track(found, field)
    s_norm = normalize(s_clean)
    f_norm = normalize(f_clean)

    # Debug
    logging.debug(f"Comparing '{s_norm}' with '{f_norm}' for field '{field}'")

    if s_norm == f_norm:
        logging.debug(f"Found exact match for '{s_norm}'")
        return MatchType.EXACT

    if field == TrackFindType.ALBUM and is_collection(f_clean):
        logging.debug(f"Found collection match for '{s_norm}'")
        return MatchType.SKIP

    if len(s_norm) < 3 or len(f_norm) < 3:
        logging.debug(f"Found short match for '{s_norm}'")
        return MatchType.SKIP

    if field == TrackFindType.ARTISTS_ALL:
        for s in set(s_norm):
            if s in f_norm:
                logging.debug(f"Found substring match for '{s_norm}' in '{f_norm}'")
                return MatchType.SUBSTRING
    else:
        for s in set(s_norm.split()):
            for f in set(f_norm.split()):
                if len(s) >= 3 and len(f) >= 3 and (s in f or f in s):
                    logging.debug(f"Found substring match for '{s_norm}' in '{f_norm}'")
                    return MatchType.SUBSTRING

    return MatchType.NONE


def get_artist_names(artists):
    if isinstance(artists, dict):
        artists = [artists]
    return [artist["name"] for artist in artists]


def load_spotify_playlist() -> dict[int, dict[str, str]]:
    tracks = {}
    if not os.path.exists(PLAYLIST_FILE):
        logging.error(f"❌ File not found: {PLAYLIST_FILE}")
        return tracks

    with open(PLAYLIST_FILE, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = {h.lower().strip(): h for h in reader.fieldnames}

        track_col = headers.get("track name")
        artist_col = headers.get("artist name(s)")
        album_col = headers.get("album name")

        if not track_col or not artist_col:
            raise RuntimeError(
                f"Unsupported CSV format. Found columns: {reader.fieldnames}"
            )

        index = 1
        for row in reader:
            track = fix_spotify_to_tidal_namings(row[track_col], "title")
            artist = fix_spotify_to_tidal_namings(row[artist_col], "artist")
            artists_all = artist.split(";") if ";" in artist else []
            artist = artists_all[0] if artists_all else artist
            album = fix_spotify_to_tidal_namings(row.get(album_col, ""), "album")

            if artist and track:
                tracks[index] = {
                    "title": track,
                    "artist": artist,
                    "artists_all": artists_all,
                    "album": album,
                }
                index += 1
    return tracks


def fix_spotify_to_tidal_namings(text: str, field: str) -> str:
    for key, value in SPOTIFY_TO_TIDAL_NAMING.items():
        if value["type"] == field and key in text:
            text = text.replace(key, value["replacement"])
    return text


def cleanse_track(text: str, field: str) -> str:
    """Clean text based on field type for better matching."""
    if field == TrackFindType.ARTIST or field == TrackFindType.ARTISTS_ALL:
        return remove_feature(text)
    elif field == TrackFindType.TITLE:
        return custom_clean_title(
            remove_version(remove_remastered(remove_feature(remove_version(text))))
        )
    elif field == TrackFindType.ALBUM:
        return remove_reissue(remove_version(remove_remastered(text)))
    return text


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
