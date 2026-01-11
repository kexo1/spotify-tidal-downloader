import asyncio
import logging
import httpx
import ua_generator

from app.spotify_tidal_downloader import SpotifyTidalDownloader
from app.constants import (
    MONOCHROME_API_INSTANCES,
    STREAMING_INSTANCES,
    SONG_QUALITY,
    CONCURRENT_DOWNLOADS,
)
from app.utils import get_fastest_instance


async def main() -> None:
    session = httpx.AsyncClient()
    session.headers = httpx.Headers({"User-Agent": ua_generator.generate().text})

    logging.info("#####################################")
    logging.info("Starting Spotify-Tidal Downloader...")
    logging.info(f"Quality: {SONG_QUALITY.capitalize()}")
    logging.info(f"Concurrent Downloads: {CONCURRENT_DOWNLOADS}")

    api_instance = get_fastest_instance(MONOCHROME_API_INSTANCES)
    logging.info(f"API Instance: {api_instance}")

    streaming_instance = get_fastest_instance(STREAMING_INSTANCES)
    logging.info(f"Streaming Instance: {streaming_instance}")

    downloader = SpotifyTidalDownloader(session, api_instance, streaming_instance)
    await downloader.run()


if __name__ == "__main__":
    asyncio.run(main())
