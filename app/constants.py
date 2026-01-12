import glob
import json
import logging
import os
from datetime import datetime

#################################### Instance URLs ####################################
MONOCHROME_API_INSTANCES = [
    "https://tidal-api.binimum.org",
    "https://monochrome-api.samidy.com",
]

STREAMING_INSTANCES = [
    "https://tidal.kinoplus.online",
    "https://triton.squid.wtf",
]

LRCLIB_API = "https://lrclib.net/api/get"

##################################### Configuration ####################################
with open("config.json") as f:
    data = json.load(f)

PLAYLIST_FILE = data["paths"].get("playlistFile", "./playlist.csv")
DOWNLOAD_PATH = data["paths"].get("downloadPath", "./downloads")
LOG_PATH = data["paths"].get("logPath", "./logs")
CACHE_PATH = data["paths"].get("cachePath", "./cache")

RETRY_FAILED = data["downloader"].get("retryFailed", True)
PREFER_TIDAL_NAMING = data["downloader"].get("preferTidalNaming", False)
WINDOWS_SAFE_FILE_NAMES = data["downloader"].get("windowsSafeFileNames", True)
DOWNLOAD_LYRICS = data["downloader"].get("downloadLyrics", True)
DOWNLOAD_UNSYNCED_LYRICS = data["downloader"].get("downloadUnsyncedLyrics", False)
CONCURRENT_DOWNLOADS = data["downloader"].get("concurrentDownloads", 3)
LOG_LIMIT = data["downloader"].get("logLimit", 5)
LOGGING_LEVEL = data["downloader"].get("loggingLevel", "INFO").upper()

SONG_QUALITY = data["songs"].get("quality", "high")  # options: lossless, high, low

if LOGGING_LEVEL not in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
    LOGGING_LEVEL = "INFO"

LOG_LEVEL = logging._nameToLevel.get(LOGGING_LEVEL, logging.INFO)
PLAYLIST_FILE_PATH = os.path.abspath(PLAYLIST_FILE)
CACHE_COMPLETED_DOWNLOADS_PATH = os.path.join(CACHE_PATH, "completed.json")
CACHE_FAILED_DOWNLOADS_PATH = os.path.join(CACHE_PATH, "failed.json")
os.makedirs(CACHE_PATH, exist_ok=True)

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
os.makedirs(LOG_PATH, exist_ok=True)
timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
LOG_FILE = os.path.join(LOG_PATH, f"downloader-{timestamp}.log")

formatter = logging.Formatter(
    "%(asctime)s - %(levelname)s - %(message)s", datefmt="%d/%m/%y %H:%M:%S"
)

file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setFormatter(formatter)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)

logger = logging.getLogger()
logger.setLevel(LOG_LEVEL)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# Cleanup old logs (keep last LOG_LIMIT)
logs = sorted(
    glob.glob(os.path.join(LOG_PATH, "downloader-*.log")),
    key=os.path.getmtime,
)
for old_log in logs[:-LOG_LIMIT]:
    os.remove(old_log)

# Reduce verbosity of HTTP libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").propagate = False
logging.getLogger("httpcore").propagate = False
