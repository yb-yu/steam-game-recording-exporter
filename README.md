# Steam Game Recording Exporter

Export Steam's fragmented game recordings (`.m4s` + `.mpd`) as standard MP4
files without re-encoding. Choose recordings from an interactive terminal menu
or use command-line options for batch exports.

- Windows, Linux and macOS with automatic Steam path detection
- Background recordings and saved clips, filtered by game or Steam account
- Parallel exports with per-worker and overall progress
- Original timestamps preserved, with optional folders per game

Original recordings are kept unless you explicitly choose source deletion or
cleanup mode.

## Quick start

Run the latest release with [uv](https://docs.astral.sh/uv/getting-started/installation/)
on Windows, Linux or macOS:

```sh
uvx --isolated --refresh --from https://github.com/yb-yu/steam-game-recording-exporter/releases/latest/download/steam-game-recording-exporter.tar.gz steamexporter
```

uv manages Python and dependencies. The download URL becomes available after
the first GitHub release.

## Interactive mode

Run the quick-start command with no extra options in an interactive terminal.
Use arrow keys and Enter to select, `Esc` to go back, and `Ctrl+C` to cancel.

```text
? What do you want to do?
❯ Export recordings to MP4
  List recordings
  Clean up sources of already-exported recordings
  Show detected Steam paths

? Which Steam recordings?
❯ Background recordings (default)
  Everything Steam has stored
  Saved clips (manually created)

? Which game?
❯ All games (21 clips)
  Counter-Strike 2 (14 clips)
  Factorio (7 clips)

? Which of the 21 clips? All of them
? Output directory /home/you/Videos
? Group exported videos into game folders? No
? How many parallel workers? (HDD source: 1-2, SSD source: 2-4) 2
? Delete the original Steam folders after a successful export? No
? Export 21 clips to /home/you/Videos with 2 workers? Yes
```

The menu remembers the output folder and worker count. Game grouping and source
deletion are off by default each run.

## Command line mode

Pass recording filters and export settings directly by appending options to the
quick-start command:

- `--list-clips` — list available recordings.
- `--process-all --group-by-game --output ~/Videos` — export into game folders.
- `--help` — show all options.

Both modes detect existing exports in flat and game-folder layouts without
moving them. Active background recordings are skipped; stop the game and wait
up to a minute before retrying. Saved clips are unaffected.

## Logs and disk space

Allow space for temporary streams as well as the final MP4 for each worker.
Temporary files are removed on success or failure.

Logs include runtime versions, fragment counts and FFmpeg diagnostics:

- Windows: `%LOCALAPPDATA%\SteamGameRecordingExporter\logs\`
- macOS/Linux: `~/SteamGameRecordingExporter/logs/`

`--verbose` also prints debug logs to the console.

## Development

Requires Python 3.9+; uv can install it automatically.

```sh
git clone https://github.com/yb-yu/steam-game-recording-exporter.git
cd steam-game-recording-exporter
uv sync --extra dev
uv run steamexporter
uv run pytest -q
uv build
```

CI tests Python 3.9 and 3.14 on Linux, and 3.14 on macOS and Windows, including
large recordings, concurrent exports and cleanup. Package checks run the built
CLI without a source checkout.

Use the Git-ignored `scratchpad/` for local experiments and `tests/` for
regression tests.

## License

MIT License
