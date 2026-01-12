<div align="center">

# Spotify Tidal Downloader

**spotify-tidal-downloader** downloads songs from your Spotify playlists using Monochrome instance (Tidal), including lyrics and metadata.

</div>

## 🚀 Features

- Downloads tracks from Spotify playlists
- Downloads synced (.lrc) and unsynced lyrics
- Embeds correct metadata (Artist, Album, Title, Cover Art)
- Concurrent downloads for speed
- Automatic cache and log management
- Highly configurable

## 📥 Installation

1. **Prerequisites**: Ensure you have [Python 3.9+](https://www.python.org/downloads/) installed.
2. **Download**: Download the project and unzip it or clone the repository.
3. **Install Dependencies**:
   Open a terminal/command prompt in the project folder and run:
   ```sh
   pip install -r requirements.txt
   ```

## 🛠️ Usage

1. **Export Playlist**: Use [Exportify](https://exportify.net/) to export your Spotify playlist as a CSV file.
2. **Configure**: Create a `config.json` file in the project directory (next to the `app` folder). You can use the example below.
3. **Run**:
   Run the application using Python:
   ```sh
   python -m app.main
   ```
   *Note: Ensure you are in the root directory of the project when running this command.*

4. **Deduplication**: It is **highly recommended** to deduplicate your playlists using [Spotify Dedup](https://spotify-dedup.com/) before exporting to avoid redownloading duplicates.

## ⚙️ Configuration

Create a `config.json` file in the root directory.

### Example Configuration
```json
{
    "paths": {
        "playlistFile": "./playlist.csv",
        "downloadPath": "./downloads",
        "cachePath": "./cache",
        "logPath": "./logs"
    },
    "downloader": {
        "retryFailed": true,
        "preferTidalNaming": false,
        "windowsSafeFileNames": true,
        "downloadLyrics": true,
        "downloadUnsyncedLyrics": false,
        "concurrentDownloads": 10,
        "logLimit": 5,
        "loggingLevel": "INFO"
    },
    "songs": {
        "quality": "high"
    }
}
```

### Settings Explanation

| Category | Setting | Description | Default |
| :--- | :--- | :--- | :--- |
| **Paths** | `playlistFile` | Path to your Spotify playlist CSV file (exported from Exportify). | `./playlist.csv` |
| | `downloadPath` | Directory where songs will be downloaded. | `./downloads` |
| | `cachePath` | Directory for storing download cache (completed/failed logs). | `./cache` |
| | `logPath` | Directory for application logs. | `./logs` |
| **Downloader** | `retryFailed` | If `true`, the downloader will attempt to redownload songs that failed in previous runs. | `true` |
| | `preferTidalNaming` | If `true`, uses Tidal's naming (Title/Artist) instead of Spotify's. | `false` |
| | `windowsSafeFileNames` | Removes invalid characters (`<>:"/\|?*`) from filenames. Essential for Windows users. | `true` |
| | `downloadLyrics` | Enables downloading of synced lyrics (`.lrc`). | `true` |
| | `downloadUnsyncedLyrics`| Enables downloading of unsynced lyrics if synced ones are missing. | `false` |
| | `concurrentDownloads` | Number of songs to download simultaneously. Higher values use more bandwidth/CPU. | `10` |
| | `logLimit` | Number of recent log files to keep. Older logs are cycled trough. | `5` |
| | `loggingLevel` | Detail level of logs. Options: `DEBUG`, `INFO`, `WARNING`, `ERROR`. | `INFO` |
| **Songs** | `quality` | Audio quality. Options: `low` (96kbps), `high` (320kbps), `lossless` (FLAC). | `high` |

## ⚠️ Known Issues & Notes

- **Accuracy**: Matching Spotify songs to Tidal tracks is not 100% perfect. Titles, albums, or artist names may differ. The tool attempts to match best efforts (~98% accuracy observed).
- **Region Locks**: Some songs might be available on Spotify but not on the Tidal.

## 📄 License

This project is licensed under the [MIT](/LICENSE) License.