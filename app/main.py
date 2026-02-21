import asyncio
import logging

import httpx
import ua_generator

from app.constants import CONFIG_CONCURRENT_DOWNLOADS, CONFIG_SONG_QUALITY
from app.spotify_tidal_downloader import SpotifyTidalDownloader
from app.utils import resolve_instances


async def main() -> None:
    api_client = httpx.AsyncClient(
        limits=httpx.Limits(
            max_connections=8,
            max_keepalive_connections=4,
        ),
        timeout=httpx.Timeout(10.0),
    )
    api_client.headers = httpx.Headers({"User-Agent": ua_generator.generate().text})

    download_client = httpx.AsyncClient(
        limits=httpx.Limits(
            max_connections=CONFIG_CONCURRENT_DOWNLOADS
            * 2,  # Extra headroom for cover art downloads
            max_keepalive_connections=CONFIG_CONCURRENT_DOWNLOADS,
        ),
        timeout=None,  # streaming
    )
    download_client.headers = httpx.Headers(
        {"User-Agent": ua_generator.generate().text}
    )

    logging.info("#####################################")
    logging.info("Starting Spotify-Tidal Downloader...")
    logging.info(f"Quality: {CONFIG_SONG_QUALITY.capitalize()}")
    logging.info(f"Concurrent Downloads: {CONFIG_CONCURRENT_DOWNLOADS}")

    api_instance, streaming_instance = resolve_instances()

    downloader = SpotifyTidalDownloader(
        api_client, download_client, api_instance, streaming_instance
    )

    try:
        await downloader.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logging.info("\nCancellation requested, shutting down...")
        await downloader.shutdown()
    finally:
        logging.info("Closing sessions...")
        await api_client.aclose()
        await download_client.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
