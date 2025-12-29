import asyncio
import csv
import os
import unicodedata

from music_metadata_filter.functions import (
    remove_remastered,
    remove_feature,
    remove_version,
    remove_reissue,
    album_artist_from_artist,
)
from playwright.async_api import (
    async_playwright,
    Page,
    TimeoutError,
)


class MatchType:
    EXACT = "exact"
    SUBSTRING = "substring"
    SKIP = "skip"
    NONE = "none"


# ===================== CONFIG =====================

SITE_URL = "https://tidal.qqdl.site/"
# https://music.binimum.org/
# https://tidal.qqdl.site/
# https://tidal.squid.wtf/"
CSV_FILE = "playlist.csv"
DOWNLOAD_DIR = os.path.abspath("D:/Music/Lossless")

MAX_PARALLEL_DOWNLOADS = 12
PREFER_TIDAL_NAMING = False

SEARCH_INPUT_SELECTOR = "input[type='text']"
SEARCH_BUTTON_SELECTOR = "button.search-button"
DOWNLOAD_BUTTON_SELECTOR = 'button[title="Download track"]'
TRACK_TITLE_SELECTOR = "h3.truncate.font-semibold.text-white"
ARTIST_NAME_SELECTOR = 'a[href^="/artist/"]'
ALBUM_NAME_SELECTOR = 'a[href^="/album/"]'

FAILED_CSV = "failed.csv"
FAILED_CSV_HEADERS = ["index", "song", "reason"]

WINDOWS_DISALLOWED_CHARS = '<>:"/\\|?*\0'
LINUX_DISALLOWED_CHARS = "/\0"

# =================================================


def format_text_for_os(text: str) -> str:
    # Replace characters invalid on Windows/Linux with underscore
    for char in WINDOWS_DISALLOWED_CHARS:
        text = text.replace(char, "")
    # Strip spaces and dots (Windows doesn't like trailing dots in folder names)
    return text.strip(" .")


def normalize(s: str) -> str:
    s = remove_accents(s.lower())
    return s.strip()


def rename_track(old_track_name: str, new_track_name: str = None) -> str:
    if PREFER_TIDAL_NAMING:
        new_track_name = old_track_name.split(" - ", 1)[-1].strip()
    else:
        new_track_name += ".flac"

    return new_track_name


def already_downloaded(download_dir: str, track_name: str) -> bool:
    if not os.path.exists(download_dir):
        return False

    # Check if skip file exists or track already downloaded
    for filename in os.listdir(download_dir):
        if filename.startswith(track_name):
            return True

    return False


def log_failed(index: int, song: str, reason: str):
    reason = reason.strip().replace("\n", " ").replace("\r", " ")
    with open(FAILED_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        if f.tell() == 0:
            writer.writerow(FAILED_CSV_HEADERS)

        writer.writerow([index, song, reason])


def create_skip_file(track_name: str, download_dir: str):
    skip_filepath = os.path.join(download_dir, f"{track_name}.skip")
    f = open(skip_filepath, "w", newline="", encoding="utf-8")
    f.close()


def remove_accents(text: str) -> str:
    # Tidal doesn't use accents in artist names
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )


def tokens(s: str) -> set[str]:
    return set(normalize(s).split())


def load_csv() -> dict[int, dict[str, str, str]]:
    """
    Supports:
    1) Spotify CSV:
        - Track Name
        - Album Name
        - Artist Name(s)

    2) Retry CSV (failed.csv):
        - index
        - song
        - reason
    """
    tracks = {}

    with open(CSV_FILE, newline="", encoding="utf-8-sig") as f:
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
            track = row[track_col]

            artist = fix_spotify_to_tidal_namings(row[artist_col], "artist")
            artists_all = artist
            artist = artist.split(";")[0]

            album = fix_spotify_to_tidal_namings(row[album_col], "album")

            if artist and track:
                tracks[index] = {
                    "artist": artist,
                    "artist_normalized": format_text_for_os(artist),
                    "artists_all": artists_all if ";" in artists_all else None,
                    "title": track,
                    "title_normalized": format_text_for_os(track),
                    "album_normalized": format_text_for_os(album),
                }
                index += 1

    return tracks


def fix_spotify_to_tidal_namings(text: str, field: str) -> str:
    if field == "artist" and "¥$;" in text:
        text = text.replace("¥$;", "")

    if field == "artist" and "JAY-Z" in text:
        text = text.replace("JAY-Z", "JAY Z")

    if field == "album" and "Original Me" == text:
        text = text.replace("Original Me", "Everytime We Touch")

    return text


async def get_text_from_selector_clean(page: Page, selector: str) -> str:
    try:
        element = await page.wait_for_selector(selector, timeout=5000)
        text = await element.inner_text()
        return text
    except TimeoutError:
        return ""


def is_remix(title: str) -> bool:
    title_lower = title.lower()
    return "remix" in title_lower or "edit" in title_lower or "slowed" in title_lower


def clean_text(text: str, field: str) -> str:
    """Clean text based on field type for better matching."""
    if field == "artist" or field == "artists_all":
        return album_artist_from_artist(remove_feature(text))
    elif field == "title":
        return remove_version(remove_remastered(text))
    elif field == "album":
        return remove_reissue(remove_version(remove_remastered(text)))
    return text


def is_collection(title: str) -> bool:
    title_lower = title.lower()
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
    return any(keyword in title_lower for keyword in collection_keywords)


async def delete_temp_files(download):
    try:
        await download.delete()
    except Exception:
        pass


def is_match(search: str, found: str, field: str) -> MatchType:
    """Check if two strings match using classic substring matching."""
    # Clean and normalize
    s_clean = clean_text(search, field)
    f_clean = clean_text(found, field)
    s_norm = normalize(s_clean)
    f_norm = normalize(f_clean)

    # Exact match
    if s_norm == f_norm:
        return MatchType.EXACT

    # Check for ";" vs "&" in artist names
    if field == "artists_all":
        s_norm_temp = s_norm.replace(";", " & ")
        if s_norm_temp == f_norm:
            return MatchType.EXACT

    # Skip collection albums for album matching
    if field == "album":
        if is_collection(f_clean):
            return MatchType.SKIP

    # If query has multiple artists, require all to be present
    if field == "artists_all":
        search_words = set(s_norm.split(";"))
        for s in search_words:
            if s in f_norm:
                return MatchType.SUBSTRING
    else:
        search_words = set(s_norm.split())
        found_words = set(f_norm.split())
        for s in search_words:
            for f in found_words:
                if len(s) > 3 and len(f) > 3 and (s in f or f in s):
                    return MatchType.SUBSTRING

    return MatchType.NONE


async def get_track_elements(page: Page) -> list[str]:
    track_names = []
    try:
        await page.locator(TRACK_TITLE_SELECTOR).first.wait_for(timeout=10000)
        track_names = await page.locator(TRACK_TITLE_SELECTOR).all_inner_texts()
    except TimeoutError:
        pass
    return track_names


async def get_artist_elements(page: Page) -> list[str]:
    artist_names = []
    try:
        await page.locator(ARTIST_NAME_SELECTOR).first.wait_for(timeout=10000)
        artist_names = await page.locator(ARTIST_NAME_SELECTOR).all_inner_texts()
    except TimeoutError:
        pass
    return artist_names


async def get_album_elements(page: Page) -> list[str]:
    album_names = []
    try:
        await page.locator(ALBUM_NAME_SELECTOR).first.wait_for(timeout=10000)
        album_names = await page.locator(ALBUM_NAME_SELECTOR).all_inner_texts()
    except TimeoutError:
        pass
    return album_names


async def get_download_button_elements(page: Page) -> list[str]:
    download_buttons = []
    try:
        await page.locator(DOWNLOAD_BUTTON_SELECTOR).first.wait_for(timeout=10000)
        download_buttons = page.locator(DOWNLOAD_BUTTON_SELECTOR)
    except TimeoutError:
        pass
    return download_buttons


async def download_song(
    page_pool: asyncio.Queue, index: int, track_info: dict[str, str, str]
):
    page: Page = await page_pool.get()
    try:
        query_track_name = f"{track_info['artist']} - {track_info['title']}"
        print(f"▶ [{index:02d}] Searching: {query_track_name}")

        track_download_dir = os.path.join(
            DOWNLOAD_DIR,
            track_info["artist_normalized"],
            track_info["album_normalized"],
        )

        # Check if already downloaded
        if already_downloaded(track_download_dir, track_info["title_normalized"]):
            print(f"⏭ [{index:02d}] Skipped (already downloaded)")
            return

        # Load site
        try:
            if page.url.rstrip("/") != SITE_URL.rstrip("/"):
                await page.goto(
                    SITE_URL,
                    timeout=100000,
                    wait_until="domcontentloaded",
                )
        except TimeoutError:
            print(f"✖ [{index:02d}] Timeout loading site")
            log_failed(index, query_track_name, "timeout loading site")
            return

        # Build search queries
        search_queries = [query_track_name]

        if track_info["artists_all"]:
            search_queries.append(
                f"{track_info['artists_all']} - {track_info['title']}"
            )

        parts = query_track_name.split(" - ")
        search_queries.append(" - ".join(parts[1:]))

        if len(parts) > 2:
            search_queries.append(" - ".join(parts[:-1]))

        cleaned_title = remove_version(remove_remastered(track_info["title"]))
        if cleaned_title != track_info["title"]:
            cleaned_artist = album_artist_from_artist(
                remove_feature(track_info["artist"])
            )
            search_queries.append(f"{cleaned_artist} - {cleaned_title}")
            search_queries.append(f"{track_info['artist']} - {cleaned_title}")

        if "(" in track_info["title"] and ")" in track_info["title"]:
            cleaned_title = track_info["title"].split(" (")[0].strip()
            search_queries.append(f"{track_info['artist']} - {cleaned_title}")

        found = False
        error_reason = ""

        for q in search_queries:
            try:
                await page.fill(SEARCH_INPUT_SELECTOR, q)
                await page.keyboard.press("Enter")
                await page.wait_for_selector(DOWNLOAD_BUTTON_SELECTOR, timeout=20000)
            except TimeoutError:
                error_reason = "not found"
                continue
            except Exception as e:
                error_reason = str(e)
                continue

            found_track_names = await get_track_elements(page)
            found_artist_names = await get_artist_elements(page)
            found_album_names = await get_album_elements(page)

            if not (found_track_names and found_artist_names and found_album_names):
                error_reason = "results incomplete"
                continue

            matched = False

            for result_index in range(len(found_track_names)):
                remix = not is_remix(track_info["title"]) and is_remix(
                    found_track_names[result_index]
                )
                if remix:
                    error_reason = "found remix"
                    continue

                if track_info["artists_all"]:
                    artists_all_match = is_match(
                        track_info["artists_all"],
                        found_artist_names[result_index],
                        "artists_all",
                    )
                else:
                    artists_all_match = MatchType.NONE

                artist_match = is_match(
                    track_info["artist"],
                    found_artist_names[result_index],
                    "artist",
                )

                if (
                    artist_match == MatchType.NONE
                    and artists_all_match == MatchType.NONE
                ):
                    error_reason = "no match: artist"
                    continue

                if (
                    artist_match == MatchType.EXACT
                    or artists_all_match == MatchType.EXACT
                ):
                    matched = True
                    break

                album_match = is_match(
                    track_info["album_normalized"],
                    found_album_names[result_index],
                    "album",
                )

                if album_match == MatchType.NONE:
                    error_reason = "no match: album"
                    continue

                matched = True
                break

            if matched:
                print(
                    f"✔ [{index:02d}] Found: "
                    f"{found_track_names[result_index].strip()} by "
                    f"{found_artist_names[result_index]}"
                )
                query_track_name = q
                found = True
                break

        if not found:
            log_failed(index, query_track_name, error_reason)
            print(f"✖ [{index:02d}] {error_reason}: {query_track_name}")
            return

        os.makedirs(track_download_dir, exist_ok=True)

        try:
            download_button = page.locator(DOWNLOAD_BUTTON_SELECTOR).nth(result_index)

            async with page.expect_download(timeout=120000) as d:
                await download_button.click()

            download = await d.value
        except Exception as e:
            log_failed(index, query_track_name, f"download failed: {e}")
            print(f"✖ [{index:02d}] Download failed")
            return

        filename = format_text_for_os(download.suggested_filename)
        if filename.lower().endswith("flac") and not filename.lower().endswith(".flac"):
            filename = filename[:-4] + ".flac"

        filename = rename_track(filename, format_text_for_os(track_info["title"]))

        save_path = os.path.join(track_download_dir, filename)

        try:
            await download.save_as(save_path)
        except Exception as e:
            log_failed(index, query_track_name, f"save failed: {e}")
            await delete_temp_files(download)
            return

        await delete_temp_files(download)

        print(f"✔ [{index:02d}] Downloaded: {filename}")

        # Reset page cheaply
        await page.goto(SITE_URL, wait_until="domcontentloaded")

    finally:
        await page_pool.put(page)


async def main():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    tracks = load_csv()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-software-rasterizer",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "--no-first-run",
                "--disable-features=VizDisplayCompositor",
            ],
        )

        try:
            NUM_CONTEXTS = 2
            PAGES_PER_CONTEXT = MAX_PARALLEL_DOWNLOADS // NUM_CONTEXTS
            page_pool = asyncio.Queue()
            contexts = []

            for _ in range(NUM_CONTEXTS):
                context = await browser.new_context(
                    accept_downloads=True,
                    viewport=None,
                )

                async def block_resources(route, request):
                    if request.resource_type == "image":
                        await route.abort()
                    else:
                        await route.continue_()

                await context.route("**/*", block_resources)
                contexts.append(context)

                for _ in range(PAGES_PER_CONTEXT):
                    page = await context.new_page()
                    await page_pool.put(page)

            tasks = [
                download_song(page_pool, index, track_info)
                for index, track_info in tracks.items()
            ]
            await asyncio.gather(*tasks)

        except asyncio.CancelledError:
            print("\n🛑 Execution interrupted by user.")
            raise

        finally:
            for context in contexts:
                await context.close()
            await browser.close()

    print("\n✅ RUN COMPLETE")


if __name__ == "__main__":
    asyncio.run(main())
