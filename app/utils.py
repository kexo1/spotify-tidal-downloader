import base64
import json
import logging
import os
import time
import unicodedata
from pathlib import Path
from typing import Any, Sequence

import httpx
from mutagen.flac import FLAC

from app.constants import (
    CACHE_INSTANCES_PATH,
    CONFIG_WINDOWS_SAFE_FILE_NAMES,
    INSTANCES_API,
    INSTANCES_STREAMING,
    REFRESH_INSTANCES_DAYS,
    WINDOWS_DISALLOWED_CHARS,
)


def format_text_for_os(text: str) -> str:
    """Format text to be safe for OS file names."""

    if not CONFIG_WINDOWS_SAFE_FILE_NAMES:
        return text

    for char in WINDOWS_DISALLOWED_CHARS:
        text = text.replace(char, "")
    return text.strip(" .")


def remove_accents(text: str) -> str:
    """Remove accents from a string."""

    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )


def normalize(s: str) -> str:
    """Normalize a string for comparison."""

    s = remove_accents(s.lower())
    return s.strip()


def tokens(s: str) -> set[str]:
    """Tokenize a string into a set of words for comparison."""

    return set(normalize(s).split())


def base64_decode(text: str) -> str:
    """Decode a base64 encoded string."""

    decoded_bytes = base64.b64decode(text)
    return decoded_bytes.decode("utf-8")


def get_fastest_instance(urls: Sequence[str], timeout: float = 5) -> str | None:
    """Return the fastest reachable URL from the provided list."""

    fastest_url = None
    fastest_time = float("inf")

    for url in urls:
        start_time = time.perf_counter()
        try:
            response = httpx.get(url, timeout=timeout)
            response.raise_for_status()
        except (httpx.RequestError, httpx.HTTPStatusError):
            continue

        elapsed_time = time.perf_counter() - start_time
        if elapsed_time < fastest_time:
            fastest_time = elapsed_time
            fastest_url = url

    return fastest_url


def load_json_file(file_path: str) -> dict[str, Any]:
    """Load JSON data from a file, returning an empty dict on absence."""

    if not Path(file_path).exists():
        return {}

    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json_file(file_path: str, data: dict) -> None:
    """Save JSON data to a file."""

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def is_file_older_than_days(file_path: str, days: int) -> bool:
    """Return True when file does not exist or is older than the provided days."""

    path = Path(file_path)
    if not path.exists():
        return True

    try:
        file_age_seconds = time.time() - path.stat().st_mtime
    except OSError:
        return True

    return file_age_seconds > days * 24 * 60 * 60


def load_instance_cache(file_path: str) -> tuple[str | None, str | None]:
    """Load cached API and streaming instance URLs."""

    data = load_json_file(file_path)
    if not isinstance(data, dict):
        return None, None

    api_instance = data.get("apiInstance")
    streaming_instance = data.get("streamingInstance")

    if not isinstance(api_instance, str) or not api_instance.strip():
        api_instance = None
    if not isinstance(streaming_instance, str) or not streaming_instance.strip():
        streaming_instance = None

    return api_instance, streaming_instance


def save_instance_cache(
    file_path: str, api_instance: str, streaming_instance: str
) -> None:
    """Persist resolved API and streaming instances to disk."""

    save_json_file(
        file_path,
        {
            "apiInstance": api_instance,
            "streamingInstance": streaming_instance,
        },
    )


def resolve_instances() -> tuple[str, str]:
    """Resolve API and streaming instances using cache when possible."""

    cached_api_instance, cached_streaming_instance = load_instance_cache(
        CACHE_INSTANCES_PATH
    )
    is_cache_stale = is_file_older_than_days(
        CACHE_INSTANCES_PATH, REFRESH_INSTANCES_DAYS
    )

    if (
        not is_cache_stale
        and cached_api_instance is not None
        and cached_streaming_instance is not None
    ):
        logging.info("Using cached API instances.")
        logging.info(f"API Instance: {cached_api_instance}")
        logging.info(f"Streaming Instance: {cached_streaming_instance}")
        return cached_api_instance, cached_streaming_instance

    logging.info("Refreshing fastest API instances...")
    api_instance = get_fastest_instance(INSTANCES_API)
    streaming_instance = get_fastest_instance(INSTANCES_STREAMING)

    if api_instance is None:
        api_instance = cached_api_instance or INSTANCES_API[0]
    if streaming_instance is None:
        streaming_instance = cached_streaming_instance or INSTANCES_STREAMING[0]

    save_instance_cache(CACHE_INSTANCES_PATH, api_instance, streaming_instance)
    logging.info(f"API Instance: {api_instance}")
    logging.info(f"Streaming Instance: {streaming_instance}")
    return api_instance, streaming_instance


def is_valid_flac(path: str) -> bool:
    """Check if a file is a valid FLAC file."""

    if not os.path.isfile(path):
        return False
    try:
        FLAC(path)
        return True
    except Exception:
        return False
