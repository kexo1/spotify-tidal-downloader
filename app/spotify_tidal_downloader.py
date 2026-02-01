import asyncio
import csv
import json
import logging
import os
import tempfile
from pathlib import Path

import httpx
from music_metadata_filter.functions import remove_feature
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover

from app.constants import (
    API_LRCLIB,
    CONFIG_CONCURRENT_DOWNLOADS,
    CONFIG_DOWNLOAD_LYRICS,
    CONFIG_DOWNLOAD_UNSYNCED_LYRICS,
    CONFIG_LOG_SKIPPED,
    CONFIG_PLAYLIST_FILE,
    CONFIG_RETRY_FAILED,
    CONFIG_SONG_QUALITY,
    CONFIG_SYNC,
    ERROR_RATE_LIMITED,
    LYRICS_DOWNLOAD_COUNT,
    PATH_CACHE_COMPLETED_DOWNLOADS,
    PATH_CACHE_FAILED_DOWNLOADS,
    RETRY_COUNT_API,
    RETRY_COUNT_DOWNLOAD,
)
from app.matching import (
    cleanse_track,
    compare_results,
    fix_spotify_to_tidal_namings,
    generate_no_match_error,
    is_song_edit,
)
from app.types import (
    CompletedDownload,
    DownloadTrackData,
    MatchType,
    SpotifyTrackData,
    TidalTrackData,
    TrackFindType,
)
from app.utils import base64_decode, is_valid_flac, load_json_file, save_json_file


class SpotifyTidalDownloader:
    def __init__(
        self,
        api_client: httpx.AsyncClient,
        download_client: httpx.AsyncClient,
        api_instance: str,
        streaming_instance: str,
    ):
        self.session = api_client
        self.download_client = download_client
        self.api_instance = api_instance
        self.streaming_instance = streaming_instance
        self.spotify_tracks = None
        self.failed_downloads = {}
        self.completed_downloads = {}
        self.download_queue = asyncio.Queue()
        self.semaphore = asyncio.Semaphore(LYRICS_DOWNLOAD_COUNT)
        self.workers = []

    async def run(self) -> None:
        if not self._initialize_resources():
            return

        logging.info("#####################################")

        self._sync_tracks()

        workers = self._start_workers()
        lyrics_tasks = []

        for spotify_track in self.spotify_tracks.values():
            await self._process_track(spotify_track, lyrics_tasks)

        await self._finish_tasks(lyrics_tasks, workers)
        self._log_completion_stats()

    def _sync_tracks(self) -> None:
        """Sync previously downloaded tracks with the current Spotify playlist.
        Remove previously downloaded tracks that are no longer in the playlist from the directory and cache.
        """

        if not self.completed_downloads or not CONFIG_SYNC:
            return

        current_titles = {track["full_title"] for track in self.spotify_tracks.values()}

        completed_cached_titles = self.completed_downloads.keys()
        completed_tracks_to_remove = completed_cached_titles - current_titles

        if self.failed_downloads:
            failed_cached_titles = self.failed_downloads.keys()
            failed_tracks_to_remove = failed_cached_titles - current_titles

            for title in failed_tracks_to_remove:
                del self.failed_downloads[title]
                logging.info(
                    f"Removed failed track from cache no longer in playlist: {title}"
                )

        for title in completed_tracks_to_remove:
            path = Path(self.completed_downloads[title]["path"])
            lyrics_path = path.with_suffix(".lrc")

            self._delete_track(path, title)
            self._delete_track(lyrics_path, f"{title} (Lyrics)")

            del self.completed_downloads[title]

        if completed_tracks_to_remove:
            save_json_file(PATH_CACHE_COMPLETED_DOWNLOADS, self.completed_downloads)

    def _delete_track(self, path: Path, title: str) -> None:
        """Delete a track file from the filesystem."""
        if not os.path.exists(path):
            return

        try:
            os.remove(path)
            logging.info(f"Deleted file no longer in playlist: {title}")
        except Exception as e:
            logging.error(f"Failed to delete file {title}: {e}")

        # Also remove empty parent directories up to the download path
        parent_dir = os.path.dirname(path)
        try:
            while (
                parent_dir and os.path.isdir(parent_dir) and not os.listdir(parent_dir)
            ):
                folder_name = os.path.basename(parent_dir)
                os.rmdir(parent_dir)
                parent_dir = os.path.dirname(parent_dir)
        except Exception as e:
            logging.error(f"Failed to remove empty directory {folder_name}: {e}")

    def _initialize_resources(self) -> bool:
        """Initialize resources: load Spotify playlist and caches."""

        self.spotify_tracks = load_spotify_playlist()
        if not self.spotify_tracks:
            return False

        self.failed_downloads = load_json_file(PATH_CACHE_FAILED_DOWNLOADS)
        self.completed_downloads = load_json_file(PATH_CACHE_COMPLETED_DOWNLOADS)
        return True

    def _start_workers(self) -> list[asyncio.Task]:
        """Preload download workers."""

        self.workers = [
            asyncio.create_task(self._download_worker())
            for _ in range(CONFIG_CONCURRENT_DOWNLOADS)
        ]
        return self.workers

    async def _process_track(self, spotify_track: dict, lyrics_tasks: list) -> None:
        """Process a single Spotify track.
        Check if already downloaded or failed, else queue for download.
        If already downloaded, check for missing lyrics and queue lyrics download if needed.
        """

        full_title = spotify_track["full_title"]
        index = spotify_track["index"]

        if self._is_downloaded(full_title):
            if task := self._check_missing_lyrics(spotify_track):
                lyrics_tasks.append(task)
            elif CONFIG_LOG_SKIPPED:
                logging.info(f"[{index:02d}] Skipping downloaded track: {full_title}")
            return

        # If it's rate limited previously, always retry
        if (
            not CONFIG_RETRY_FAILED
            and self._is_failed(full_title)
            and self._fail_reason(full_title) != ERROR_RATE_LIMITED
        ):
            if CONFIG_LOG_SKIPPED:
                logging.info(f"[{index:02d}] Skipping failed track: {full_title}")
            return

        await self._queue_track_for_download(spotify_track)

    def _check_missing_lyrics(self, spotify_track: dict):
        """Check if lyrics are missing for a downloaded track and queue lyrics download if needed."""

        if not CONFIG_DOWNLOAD_LYRICS:
            return None

        full_title = spotify_track["full_title"]
        index = spotify_track["index"]
        completed_download = self.completed_downloads.get(full_title, {})

        lyrics_missing = completed_download.get("lyrics") is False
        unsynced_exists = completed_download.get("unsynced_exists")

        find_lyrics = (
            lyrics_missing
            and unsynced_exists is not True
            and unsynced_exists is not False
        )

        find_unsynced = (
            lyrics_missing is True
            and unsynced_exists is not False
            and CONFIG_DOWNLOAD_UNSYNCED_LYRICS
        )

        if find_lyrics or find_unsynced:
            logging.info(
                f"[{index:02d}] Fetching lyrics for previously downloaded track: {full_title}"
            )
            track_data_copy = completed_download.copy()
            track_data_copy["full_title"] = full_title
            track_data_copy["index"] = index
            track_data_copy = self._prepare_track_data_lyrics(track_data_copy)
            return self._download_lyrics_for_cached_tracks(track_data_copy)
        return None

    async def _queue_track_for_download(self, spotify_track: dict) -> None:
        """Queue a Spotify track for download after searching and matching on Monochrome."""

        queries = await self._get_queries(spotify_track)
        if track_data := await self._search_track(queries, spotify_track):
            await self.download_queue.put(track_data)

    async def _finish_tasks(self, lyrics_tasks: list, workers: list) -> None:
        """Finish all download and lyrics tasks."""

        if lyrics_tasks:
            await asyncio.gather(*lyrics_tasks)

        await self.download_queue.join()
        for w in workers:
            w.cancel()

    def _log_completion_stats(self) -> None:
        """Log statistics about completed and failed downloads."""

        if self.failed_downloads:
            logging.info(f"Failed downloads: {len(self.failed_downloads)}.")

        total = len(self.spotify_tracks)
        completed = len(self.completed_downloads)
        percentage = round(completed / total * 100, 2) if total > 0 else 0
        logging.info(f"Completed {completed}/{total} ({percentage}%) downloads.")

    async def _search_track(
        self, queries: list[str], spotify_track_data: dict[str, str]
    ) -> DownloadTrackData:
        """Search for a track on Monochrome using provided queries and match with Spotify track data.
        We use different queries to maximize the chances of finding the correct track.
        If a match is found, return DownloadTrackData, else return an error dict.
        """

        full_title = spotify_track_data["full_title"]
        index = spotify_track_data["index"]
        error = {"reason": "No results found."}

        logging.debug(f"Searching Monochrome for queries: {queries}")
        for query in queries:
            logging.debug("----- Match Debugging -----")
            logging.debug(f"Searching Monochrome for query: '{query}'")

            for _ in range(RETRY_COUNT_API):
                try:
                    response = await self.session.get(
                        f"{self.api_instance}/search/", params={"s": query}
                    )
                    found_tracks = response.json()["data"]["items"]
                    break
                except Exception:
                    await asyncio.sleep(1)
            else:
                continue

            # No results found for this query
            if not found_tracks:
                continue

            download_track_data = await self._match_track(
                found_tracks, spotify_track_data
            )
            # If error dict is returned, it's a error
            if type(download_track_data) is dict:
                error = download_track_data
                if error["reason"] == "No suitable match found":
                    continue

                break  # Break on fetching error, no reason to continue searching

            # If previously failed, remove from failed downloads cache
            if self.failed_downloads.get(full_title):
                del self.failed_downloads[full_title]
                save_json_file(PATH_CACHE_FAILED_DOWNLOADS, self.failed_downloads)

            return download_track_data

        self._cache_failed_song(full_title, error)
        logging.info(f"[{index:02d}] {error['reason']} for track: {full_title}.")
        return {}

    async def _get_queries(self, spotify_track_data: dict[str, str]) -> list[str]:
        """Generate a list of search queries for a given Spotify track to maximize matching chances."""
        query_track_name = (
            f"{spotify_track_data['artist']} - {spotify_track_data['title']}"
        )
        logging.info(
            f"[{spotify_track_data['index']:02d}] Searching: {query_track_name}"
        )
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

        return list(
            dict.fromkeys(search_queries)
        )  # Remove duplicates while preserving order

    async def _match_track(
        self, found_tracks: list[dict], spotify_track_data: dict[str, str]
    ) -> DownloadTrackData | dict:
        """Match found Monochrome tracks with Spotify track data.
        If a exact match is found, break and return DownloadTrackData.
        If a suitable match is found, return DownloadTrackData, else return an error dict.
        """

        spotify_track = SpotifyTrackData(spotify_track_data)
        for tidal_track_source in found_tracks:
            matched = False

            tidal_track = TidalTrackData(tidal_track_source)

            is_edit = not is_song_edit(spotify_track.title) and is_song_edit(
                tidal_track.title
            )

            logging.debug(
                f"Edit check: {spotify_track.title} vs {tidal_track.title} => {is_edit}"
            )
            if is_edit:
                continue

            title_match = compare_results(
                spotify_track.title, tidal_track.title, TrackFindType.TITLE
            )
            if title_match == MatchType.NONE:
                continue

            artists_all_match = MatchType.NONE
            if len(spotify_track.artists) > 1 and len(tidal_track.artists) > 1:
                artists_all_match = compare_results(
                    ";".join(spotify_track.artists),
                    ";".join(tidal_track.artists),
                    TrackFindType.ARTISTS_ALL,
                )

            artist_match = compare_results(
                spotify_track.artist, tidal_track.artist, TrackFindType.ARTIST
            )
            if artist_match == MatchType.NONE and artists_all_match == MatchType.NONE:
                continue

            if (title_match == MatchType.EXACT) and (
                artist_match == MatchType.EXACT or artists_all_match == MatchType.EXACT
            ):
                matched = True
                break

            # Check if track is a single (title matches album title) in either service
            is_single = (
                cleanse_track(spotify_track.title, TrackFindType.TITLE).casefold()
                == cleanse_track(spotify_track.album, TrackFindType.TITLE).casefold()
                or cleanse_track(tidal_track.title, TrackFindType.TITLE).casefold()
                == cleanse_track(tidal_track.album, TrackFindType.TITLE).casefold()
            )
            logging.debug(f"Single check: {is_single}")

            album_match = compare_results(
                spotify_track.album, tidal_track.album, TrackFindType.ALBUM
            )
            if album_match == MatchType.NONE and not is_single:
                continue

            matched = True
            break

        if not matched:
            return generate_no_match_error(spotify_track, tidal_track)

        download_url = await self._get_download_url(tidal_track.id, spotify_track)
        if not download_url:
            return {
                "reason": "Failed to get download URL from streaming instance",
            }

        fetch_album_data_success = await self._get_additional_track_data(
            tidal_track, spotify_track
        )
        if not fetch_album_data_success:
            return {"reason": ERROR_RATE_LIMITED}

        track_data = DownloadTrackData(download_url, spotify_track, tidal_track)
        logging.info(
            f"[{spotify_track.index:02d}] Found: {tidal_track.title} by {tidal_track.artist}"
        )
        return track_data

    def _cache_failed_song(self, full_title: str, reason: dict) -> None:
        """Cache a failed download attempt for a track."""

        if not self.failed_downloads.get(full_title):
            self.failed_downloads[full_title] = reason
            save_json_file(PATH_CACHE_FAILED_DOWNLOADS, self.failed_downloads)

    def _cache_completed_download(
        self,
        track_data: DownloadTrackData,
        save_path: str,
        found_lyrics: bool,
        unsynced_exists: bool | None,
    ) -> None:
        """Cache a completed download for a track."""

        self.completed_downloads[track_data.full_title] = CompletedDownload(
            path=os.path.normpath(save_path),
            lyrics=found_lyrics,
            unsynced_exists=unsynced_exists,
            tidal_title=track_data.tidal_title,
            tidal_artists=track_data.tidal_artists,
            tidal_album=track_data.tidal_album,
            duration=track_data.duration,
        )
        save_json_file(PATH_CACHE_COMPLETED_DOWNLOADS, self.completed_downloads)

    async def _get_download_url(
        self, track_id: str, spotify_track: SpotifyTrackData
    ) -> str:
        """Get the download URL for a given Tidal track ID from the streaming instance."""

        error = "No response"
        for _ in range(RETRY_COUNT_API):
            try:
                response = await self.session.get(
                    f"{self.streaming_instance}/track/",
                    params={"id": track_id, "quality": CONFIG_SONG_QUALITY.upper()},
                )
                data = response.json().get("data")
                if not data:
                    return ""

                manifest = json.loads(base64_decode(data["manifest"]))
                download_url = manifest["urls"][0]
                return download_url

            except Exception as e:
                error = e
                await asyncio.sleep(1)

        logging.error(
            f"[{spotify_track.index:02d}] Failed to fetch download URL for track {spotify_track.title}: {error}"
        )
        return ""

    async def _get_additional_track_data(
        self, tidal_track: TidalTrackData, spotify_track: SpotifyTrackData
    ) -> bool:
        """Fetch additional track data such as number of tracks in album and release date after a successful match."""
        album_data = await self.fetch_album_data(tidal_track.album_id, spotify_track)

        if not album_data:
            return False

        tidal_track.number_of_tracks = album_data.get("numberOfTracks", 0)
        release_date = album_data.get("releaseDate")
        if release_date:
            tidal_track.release_date = release_date.split("-")[0]

        return True

    async def fetch_album_data(
        self, album_id: int, spotify_track: SpotifyTrackData
    ) -> dict:
        """Fetch album data to get the number of tracks and release date."""

        error = "No response"
        for _ in range(RETRY_COUNT_API):
            try:
                response = await self.session.get(
                    f"{self.api_instance}/album/",
                    params={"id": album_id},
                )
                data = response.json()
                if data.get("detail") == "Upstream rate limited":
                    error = "Rate limited, please try again later."
                    continue

                data = data.get("data", {})
                return data
            except Exception as e:
                error = e
                await asyncio.sleep(1)

        logging.error(
            f"[{spotify_track.index:02d}] Error fetching album data for track {spotify_track.full_title}: {error}"
        )
        return {}

    async def _download_worker(self):
        """Worker to download tracks from the download queue.
        Downloads the track, adds metadata, fetches lyrics, and caches the completed download.
        """
        client = self.download_client

        while True:
            track_data = None
            track_save_path = None
            completed_successfully = False

            try:
                track_data: DownloadTrackData = await self.download_queue.get()

                os.makedirs(track_data.download_path, exist_ok=True)

                track_save_path = os.path.join(
                    track_data.download_path,
                    f"{track_data.title}{track_data.extension}",
                )

                downloaded = False
                error = None
                for _ in range(RETRY_COUNT_DOWNLOAD):
                    try:
                        async with client.stream("GET", track_data.url) as response:
                            response.raise_for_status()
                            with open(track_save_path, "wb") as f:
                                async for chunk in response.aiter_bytes(8192):
                                    f.write(chunk)
                        downloaded = True
                        break
                    except Exception as e:
                        error = e
                        await asyncio.sleep(1)

                if downloaded:
                    try:
                        await self._add_metadata(track_data, track_save_path, client)
                        found_lyrics, unsynced_exists = await self._fetch_lyrics(
                            track_data
                        )
                        logging.info(
                            f"[{track_data.index:02d}] Downloaded: {track_data.full_title}"
                        )
                        self._cache_completed_download(
                            track_data, track_save_path, found_lyrics, unsynced_exists
                        )
                        completed_successfully = True
                    except Exception as e:
                        logging.error(
                            f"[{track_data.index:02d}] Post-download error for {track_data.full_title}: {e}"
                        )
                        self.failed_downloads[track_data.full_title] = {
                            "reason": f"Post-download error: {e}"
                        }
                        save_json_file(
                            PATH_CACHE_FAILED_DOWNLOADS, self.failed_downloads
                        )
                else:
                    self.failed_downloads[track_data.full_title] = {
                        "reason": f"Failed to download: {error}"
                    }
                    save_json_file(PATH_CACHE_FAILED_DOWNLOADS, self.failed_downloads)

                self.download_queue.task_done()

            except asyncio.CancelledError:
                if (
                    track_save_path
                    and os.path.exists(track_save_path)
                    and not completed_successfully
                ):
                    try:
                        os.remove(track_save_path)
                        logging.info(
                            f"Removed partial/incomplete download: {track_save_path}"
                        )
                    except Exception:
                        pass
                raise

    async def _add_metadata(
        self, track_data: DownloadTrackData, save_path: str, client: httpx.AsyncClient
    ) -> None:
        """Add metadata to the downloaded track file. Downloads cover art if available.
        Metadata added: Title, Artist, Album, Album Artist(s), Track Number, Release Date, Cover Art
        """

        cover_path = await self._download_cover_art(client, track_data.cover)

        if track_data.extension == ".m4a":
            self._tag_m4a(save_path, track_data, cover_path)
        else:
            self._tag_flac(save_path, track_data, cover_path)

        if cover_path and os.path.exists(cover_path):
            os.remove(cover_path)

    async def _download_cover_art(
        self, client: httpx.AsyncClient, cover_uuid: str
    ) -> str | None:
        """Download cover art from Tidal and return the local file path."""
        if not cover_uuid:
            return None

        cover_url = (
            "https://resources.tidal.com/images/"
            + "/".join(cover_uuid.split("-"))
            + "/1280x1280.jpg"
        )
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp_cover:
            cover_path = tmp_cover.name

        try:
            async with client.stream("GET", cover_url) as resp:
                resp.raise_for_status()
                with open(cover_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(8192):
                        f.write(chunk)
            return cover_path
        except Exception:
            if os.path.exists(cover_path):
                os.remove(cover_path)
            raise  # Re-raise to let the worker handle the error

    def _tag_m4a(
        self, save_path: str, track_data: DownloadTrackData, cover_path: str | None
    ) -> None:
        """Add metadata to M4A file."""

        audio = MP4(save_path)
        audio["\xa9nam"] = track_data.title
        audio["\xa9alb"] = track_data.album
        audio["aART"] = track_data.artist
        audio["\xa9ART"] = track_data.spotify_artists
        audio["trkn"] = [(track_data.track_number, track_data.number_of_tracks)]
        audio["\xa9day"] = track_data.release_date

        if cover_path and os.path.exists(cover_path):
            with open(cover_path, "rb") as img:
                audio["covr"] = [MP4Cover(img.read(), imageformat=MP4Cover.FORMAT_JPEG)]

        audio.save()

    def _tag_flac(
        self, save_path: str, track_data: DownloadTrackData, cover_path: str | None
    ) -> None:
        """Add metadata to FLAC file."""
        if not is_valid_flac(save_path):
            logging.warning(
                f"Ignoring metadata for invalid FLAC file: {track_data.title}.flac"
            )
            return

        audio = FLAC(save_path)
        audio["TITLE"] = [track_data.title]
        audio["ARTIST"] = track_data.spotify_artists
        audio["ALBUM"] = [track_data.album]
        audio["ALBUMARTIST"] = [track_data.artist]
        audio["TRACKNUMBER"] = [str(track_data.track_number)]
        audio["DATE"] = [track_data.release_date]

        if cover_path and os.path.exists(cover_path):
            pic = Picture()
            with open(cover_path, "rb") as f:
                pic.data = f.read()
            pic.type = 3
            pic.mime = "image/jpeg"
            audio.add_picture(pic)

        audio.save()

    async def _fetch_lyrics(self, track: DownloadTrackData) -> bool:
        """Fetch lyrics for a given track. If found, save them to a .lrc file.
        If user opts to download synced lyrics only, unsynced_exists will be None, because we don't know if unsynced lyrics exist.
        If user opts to download unsynced lyrics as well, unsynced_exists will be True/False based on availability.
        """

        found_lyrics, unsynced_exists = False, None
        if not CONFIG_DOWNLOAD_LYRICS:
            return found_lyrics, unsynced_exists

        params = {
            "track_name": track.tidal_title,
            "artist_name": track.tidal_artists,
            "album_name": track.tidal_album,
            "duration": track.duration,
        }
        data = {}
        error = "No response"

        for _ in range(RETRY_COUNT_API):
            try:
                response = await self.session.get(
                    API_LRCLIB,
                    params=params,
                )
                data = response.json()
                break
            except Exception as e:
                error = e
                await asyncio.sleep(1)
        else:
            logging.error(
                f"[{track.index:02d}] Failed to fetch lyrics for track {track.full_title}: {error}"
            )
            return found_lyrics, unsynced_exists

        lyrics_type = "synced"
        lyrics = data.get("syncedLyrics", "")

        # If synced lyrics are not found and user only wants synced lyrics, return
        if not lyrics and not CONFIG_DOWNLOAD_UNSYNCED_LYRICS:
            logging.info(
                f"[{track.index:02d}] No synced lyrics found for: {track.full_title}"
            )
            # Check if unsynced lyrics exist for future reference
            unsynced_exists = data.get("plainLyrics") is not None
            return found_lyrics, unsynced_exists

        # If synced lyrics are not found, try to get unsynced lyrics if user opted in
        if not lyrics and CONFIG_DOWNLOAD_UNSYNCED_LYRICS:
            lyrics = data.get("plainLyrics", "")
            lyrics_type = "unsynced"

        # If no lyrics found at all
        if not lyrics:
            logging.info(f"[{track.index:02d}] No lyrics found for: {track.full_title}")
            unsynced_exists = False
            return found_lyrics, unsynced_exists

        lyrics_path = os.path.join(track.download_path, f"{track.title}.lrc")
        with open(lyrics_path, "w", encoding="utf-8") as f:
            f.write(lyrics)

        # If synced exists, unsynced_exists will exist too
        found_lyrics, unsynced_exists = True, True
        logging.info(
            f"[{track.index:02d}] Lyrics ({lyrics_type}) downloaded for: {track.full_title}"
        )

        return found_lyrics, unsynced_exists

    async def _download_lyrics_for_cached_tracks(self, track: dict) -> None:
        """Download lyrics for previously downloaded tracks stored in cache.
        This is only used if users downloaded tracks previously without lyrics and now want to fetch lyrics.
        """

        async with self.semaphore:
            try:
                # Create a simple object to mimic DownloadTrackData for _fetch_lyrics
                track_obj = type("CachedTrack", (), track)()
                found_lyrics, unsynced_exists = await self._fetch_lyrics(track_obj)

                self.completed_downloads[track["full_title"]].update(
                    lyrics=found_lyrics,
                    unsynced_exists=unsynced_exists,
                )
                save_json_file(PATH_CACHE_COMPLETED_DOWNLOADS, self.completed_downloads)
            except Exception as e:
                logging.error(
                    f"[{track['index']:02d}] Failed to fetch lyrics for {track['full_title']}: {e}"
                )

    def _prepare_track_data_lyrics(self, track_data: dict) -> dict:
        """Prepare track data for lyrics fetching.
        Extract path, title, and download path from cached data.
        """

        file_path = Path(track_data["path"])
        track_data["title"] = file_path.stem
        track_data["download_path"] = str(file_path.parent)
        return track_data

    def _is_downloaded(self, full_title: str) -> bool:
        """Check if the track is already downloaded based on its info."""
        return self.completed_downloads.get(full_title) is not None

    def _is_failed(self, full_title: str) -> bool:
        """Check if the track download has previously failed."""
        return self.failed_downloads.get(full_title) is not None

    def _fail_reason(self, full_title: str) -> str | None:
        """Get the reason for a failed download if it exists."""

        failed_entry = self.failed_downloads.get(full_title)
        if failed_entry:
            return failed_entry.get("reason")
        return None

    async def shutdown(self) -> None:
        """Stop all workers and cleanup."""
        logging.info("Shutting down workers...")
        for worker in self.workers:
            worker.cancel()

        await asyncio.gather(*self.workers, return_exceptions=True)


def load_spotify_playlist() -> dict[int, dict[str, str]]:
    """Load Spotify playlist from CSV file exported from Exportify.
    Expected columns: Track Name, Artist Name(s), Album Name (optional)
    """

    tracks = {}
    if not os.path.exists(CONFIG_PLAYLIST_FILE):
        logging.error(f"Playlist file not found: {CONFIG_PLAYLIST_FILE}")
        return tracks

    with open(CONFIG_PLAYLIST_FILE, newline="", encoding="utf-8-sig") as f:
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
            artists_all = artist.split(";") if ";" in artist else [artist]
            artist = artists_all[0]
            album = fix_spotify_to_tidal_namings(row.get(album_col, ""), "album")

            if artist and track:
                tracks[index] = {
                    "title": track,
                    "full_title": f"{artist} - {track}",
                    "artist": artist,
                    "artists_all": artists_all,
                    "album": album,
                    "index": index,
                }
                index += 1
    return tracks
