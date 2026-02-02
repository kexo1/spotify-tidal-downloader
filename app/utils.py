import base64
import json
import os
import time
import unicodedata
from pathlib import Path
from typing import Any, Sequence

import httpx
from mutagen.flac import FLAC

from app.constants import CONFIG_WINDOWS_SAFE_FILE_NAMES, WINDOWS_DISALLOWED_CHARS


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


def is_valid_flac(path: str) -> bool:
    """Check if a file is a valid FLAC file."""

    if not os.path.isfile(path):
        return False
    try:
        FLAC(path)
        return True
    except Exception:
        return False
