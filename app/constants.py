import glob
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

#################################### Instance URLs ####################################
INSTANCES_MONOCHROME = [
    "https://tidal-api.binimum.org",
    "https://monochrome-api.samidy.com",
]

INSTANCES_STREAMING = [
    "https://tidal.kinoplus.online",
    "https://triton.squid.wtf",
]

API_LRCLIB = "https://lrclib.net/api/get"

##################################### Configuration ####################################
DEFAULT_CONFIG = {
    "paths": {
        "playlistFile": "./playlist.csv",
        "downloadPath": "./downloads",
        "cachePath": "./cache",
        "logPath": "./logs",
    },
    "downloader": {
        "sync": False,
        "retryFailed": True,
        "preferTidalNaming": False,
        "windowsSafeFileNames": True,
        "concurrentDownloads": 10,
    },
    "songs": {
        "quality": "high",
        "lyrics": False,
        "unsyncedLyrics": False,
    },
    "logging": {
        "fileLimit": 5,
        "level": "INFO",
        "logSkipped": True,
    },
}


def load_config() -> dict[str, Any]:
    env_dir = os.environ.get("DOWNLOAD_CONFIG_DIR")
    config_dir = Path(env_dir) if env_dir else Path.cwd()
    config_path = config_dir / "config.json"

    if not config_path.exists():
        config_dir.mkdir(parents=True, exist_ok=True)
        with config_path.open("w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
        print(
            f"Config file was missing. A new one has been created at '{config_path}'."
        )
        print("Please check config.json and restart the program.")
        os._exit(1)

    with config_path.open(encoding="utf-8") as f:
        return json.load(f)


data = load_config()


def get_cfg(
    section: str,
    key: str,
    default: Any,
    expected_type: type,
    min_val: Any | None = None,
    options: list[str] | None = None,
):
    """Safely retrieve and validate configuration values."""

    val = data.get(section, {}).get(key, default)

    if not isinstance(val, expected_type):
        return default

    if (min_val is not None) and val < min_val:
        return default

    if options is not None and isinstance(val, str) and val.lower() not in options:
        return default

    return val


# Path Configurations
CONFIG_PLAYLIST_FILE = get_cfg("paths", "playlistFile", "./playlist.csv", str)
CONFIG_DOWNLOAD_PATH = get_cfg("paths", "downloadPath", "./downloads", str)
CONFIG_CACHE_PATH = get_cfg("paths", "cachePath", "./cache", str)
CONFIG_LOG_PATH = get_cfg("paths", "logPath", "./logs", str)

# Downloader Configurations
CONFIG_SYNC = get_cfg("downloader", "sync", False, bool)
CONFIG_RETRY_FAILED = get_cfg("downloader", "retryFailed", True, bool)
CONFIG_PREFER_TIDAL_NAMING = get_cfg("downloader", "preferTidalNaming", False, bool)
CONFIG_WINDOWS_SAFE_FILE_NAMES = get_cfg(
    "downloader", "windowsSafeFileNames", True, bool
)
CONFIG_CONCURRENT_DOWNLOADS = get_cfg(
    "downloader", "concurrentDownloads", 10, int, min_val=1
)

# Song Configurations
CONFIG_SONG_QUALITY = get_cfg(
    "songs", "quality", "high", str, options=["lossless", "high", "low"]
)
CONFIG_SONG_QUALITY = CONFIG_SONG_QUALITY.lower()
CONFIG_DOWNLOAD_LYRICS = get_cfg("songs", "lyrics", False, bool)
CONFIG_DOWNLOAD_UNSYNCED_LYRICS = get_cfg("songs", "unsyncedLyrics", False, bool)

# Logging Configurations
CONFIG_LOG_LEVEL = get_cfg(
    "logging",
    "level",
    "info",
    str,
    options=[
        "debug",
        "info",
        "warning",
        "error",
        "critical",
    ],
)
CONFIG_LOG_LEVEL = CONFIG_LOG_LEVEL.upper()
CONFIG_LOG_LIMIT = get_cfg("logging", "fileLimit", 5, int, min_val=0)
CONFIG_LOG_SKIPPED = get_cfg("logging", "logSkipped", True, bool)

LOG_LEVEL = logging._nameToLevel.get(CONFIG_LOG_LEVEL, logging.INFO)
PATH_PLAYLIST_FILE = os.path.abspath(CONFIG_PLAYLIST_FILE)
PATH_CACHE_COMPLETED_DOWNLOADS = os.path.join(CONFIG_CACHE_PATH, "completed.json")
PATH_CACHE_FAILED_DOWNLOADS = os.path.join(CONFIG_CACHE_PATH, "failed.json")
os.makedirs(CONFIG_CACHE_PATH, exist_ok=True)


##################################### File Safety #####################################
WINDOWS_DISALLOWED_CHARS = '<>:"/\\|?*\0'

##################################### Naming Fixes ####################################
SPOTIFY_TO_TIDAL_NAMING = {
    "¥$;": {"type": "artist", "replacement": ""},
    "JAY-Z": {"type": "artist", "replacement": "JAY Z"},
    "Original Me": {"type": "album", "replacement": "Everytime We Touch"},
    "YMCA - Original Version 1978": {"type": "title", "replacement": "Y.M.C.A."},
    "Bad Meets Evil": {"type": "artist", "replacement": "Eminem;Royce da 5'9\""},
}


###################################### Logging Setup ####################################
formatter = logging.Formatter(
    "%(asctime)s - %(levelname)s - %(message)s", datefmt="%d/%m/%y %H:%M:%S"
)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)

logger = logging.getLogger()
logger.setLevel(LOG_LEVEL)
logger.addHandler(console_handler)

if CONFIG_LOG_LIMIT > 0:
    os.makedirs(CONFIG_LOG_PATH, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    LOG_FILE = os.path.join(CONFIG_LOG_PATH, f"downloader-{timestamp}.log")

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Cleanup old logs (keep last LOG_LIMIT)
    logs = sorted(
        glob.glob(os.path.join(CONFIG_LOG_PATH, "downloader-*.log")),
        key=os.path.getmtime,
    )

    if len(logs) > CONFIG_LOG_LIMIT:
        num_files_to_delete = len(logs) - CONFIG_LOG_LIMIT
        files_to_delete = logs[:num_files_to_delete]

        for old_log in files_to_delete:
            os.remove(old_log)

# Reduce verbosity of HTTP libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").propagate = False
logging.getLogger("httpcore").propagate = False

##################################### Keywords ####################################
KEYWORDS_SONG_COLLECTIONS = [
    "greatest hits",
    "best of",
    "anthology",
    "compilation",
    "collection",
    "box set",
    "hits",
    "classics",
]

KEYWORDS_SONG_EDITS = ["remix", "edit", "slowed", "instrumental", "live"]

##################################### Limits ####################################
RETRY_COUNT_DOWNLOAD = 3
RETRY_COUNT_API = 5
LYRICS_DOWNLOAD_COUNT = 5

##################################### Errors ####################################
ERROR_RATE_LIMITED = (
    "Failed to fetch album metadata after matching, try again next run."
)
