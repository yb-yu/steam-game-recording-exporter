# Steam Game Recording Exporter

Export Steam game recordings to standard MP4 format. Converts Steam's fragmented recording format (.m4s + .mpd) into universally playable MP4 files.

## Features

- Cross-platform support (Windows, macOS, Linux)
- Automatic Steam path detection
- Lossless conversion with multiprocessing
- Filter by game, user, or clip type
- Batch processing with cleanup options

## Installation

### Using uv (recommended)

```bash
git clone https://github.com/yb-yu/steam-game-recording-exporter.git
cd steam-game-recording-exporter
uv sync
```

### Using pip

```bash
git clone https://github.com/yb-yu/steam-game-recording-exporter.git
cd steam-game-recording-exporter
pip install .
```

**Requirements:** Python 3.9+

## Usage

### With uv

```bash
# List all recordings
uv run steamexporter --list-clips

# Export all recordings
uv run steamexporter --process-all

# Export specific game
uv run steamexporter --game-id 570 --process-all

# Custom output directory
uv run steamexporter --output ~/Videos --process-all
```

### With pip

```bash
# List all recordings
python steamexporter.py --list-clips

# Export all recordings
python steamexporter.py --process-all

# Export specific game
python steamexporter.py --game-id 570 --process-all
```

## Options

| Argument | Description |
|----------|-------------|
| `--list-clips` | List available recordings |
| `--process-all` | Export all recordings |
| `--game-id ID` | Filter by game ID |
| `--steam-id ID` | Filter by Steam user ID |
| `--media-type TYPE` | Filter by type (all/manual/background) |
| `--output DIR` | Output directory |
| `--workers N` | Number of worker processes |
| `--delete-source` | Delete original files after export |
| `--cleanup-only` | Delete sources for already-exported clips |

## License

MIT License
