import base64
import json
import os
import time
import unicodedata
from pathlib import Path

import httpx
from mutagen.flac import FLAC

from app.constants import WINDOWS_DISALLOWED_CHARS, WINDOWS_SAFE_FILE_NAMES


def format_text_for_os(text: str) -> str:
    if not WINDOWS_SAFE_FILE_NAMES:
        return text

    for char in WINDOWS_DISALLOWED_CHARS:
        text = text.replace(char, "")
    return text.strip(" .")


def normalize(s: str) -> str:
    s = remove_accents(s.lower())
    return s.strip()


def tokens(s: str) -> set[str]:
    return set(normalize(s).split())


def base64_decode(text: str) -> str:
    decoded_bytes = base64.b64decode(text)
    return decoded_bytes.decode("utf-8")


def remove_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )


def get_fastest_instance(urls: list, timeout: int = 5) -> str:
    fastest_url = None
    fastest_time = float("inf")

    for url in urls:
        try:
            start_time = time.time()
            response = httpx.get(url, timeout=timeout)
            response.raise_for_status()
            elapsed_time = time.time() - start_time

            if elapsed_time < fastest_time:
                fastest_time = elapsed_time
                fastest_url = url
        except (httpx.RequestError, httpx.HTTPStatusError):
            continue

    return fastest_url


def load_json_file(file_path: str) -> dict:
    data = {}
    if Path(file_path).exists():
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    return data


def save_json_file(file_path: str, data: dict) -> None:
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)


def is_valid_flac(path: str) -> bool:
    if not os.path.isfile(path):
        return False
    try:
        FLAC(path)
        return True
    except Exception:
        return False
