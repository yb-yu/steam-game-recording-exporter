# Steam Game Recording Exporter

Export Steam's fragmented game recordings (`.m4s` + `.mpd`) as standard MP4
files without re-encoding.

- Interactive arrow-key menu with per-worker and overall progress
- Windows, macOS and Linux support with automatic Steam path detection
- Filters for recording type, game, Steam user and individual clips
- Original recording timestamps preserved on exported files

Original Steam folders are kept by default. They are deleted only when you
explicitly choose source deletion or run cleanup mode.

## Quick start

Requires Python 3.9 or newer. With [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/yb-yu/steam-game-recording-exporter.git
cd steam-game-recording-exporter
uv sync
uv run steamexporter
```

With pip:

```bash
pip install .
steamexporter
```

## Interactive mode

Run `uv run steamexporter` with no options. Use the arrow keys and Enter to
choose; press `Esc` to go back one step.

```text
Esc: back one step · Ctrl+C: cancel

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

? Which of the 21 clips?
❯ All of them
  Pick individually

? Output directory  C:\Users\you\Desktop
? How many parallel workers? (HDD source: 1-2, SSD source: 2-4)
❯ 2 (current default)
  4

? Delete the original Steam folders after a successful export? No
? Export 21 clips with 2 workers? Yes
```

Background recordings are Steam's temporary rolling history and may be
overwritten. Saved clips are created manually with `Clip > Save in Steam` and
are kept permanently. `Everything Steam has stored` includes both.

The menu remembers the output directory and worker count. Environment variables
`STEAM_EXPORTER_OUTPUT_DIR` and `STEAM_EXPORTER_WORKERS` can provide defaults;
command-line options override them, and the menu can override either before a
run starts. Source deletion is never remembered.

Choose workers for the source drive: start with 1-2 for an HDD or 2-4 for an
SSD/NVMe drive. More workers can make an HDD slower by forcing it to seek between
several recordings.

If Steam is actively writing background footage, that background recording
store is skipped so rotating chunks cannot be exported or deleted. Saved clips
are unaffected. Stop the game, wait up to one minute, then run the exporter
again.

## Command-line usage

```bash
# List recordings
uv run steamexporter --list-clips

# Export everything, using two workers and a custom output folder
uv run steamexporter --process-all --workers 2 --output ~/Videos

# Export one game
uv run steamexporter --process-all --game-id 570

# Preview cleanup without deleting anything
uv run steamexporter --cleanup-only --dry-run

# Show every option
uv run steamexporter --help
```

When installed with pip, replace `uv run steamexporter` with `steamexporter`.
The interactive menu needs a real terminal; under Git Bash/mintty, use the
command-line options instead.

## Conversion and logs

Single-session recordings stream directly from Steam's fragments into the final
MP4. Multi-session recordings use temporary files under `<output>/.temp`, which
are removed after the run. Exported files keep the recording time both as their
file timestamp and as MP4 `creation_time` metadata.

Logs are written to `%LOCALAPPDATA%\SteamGameRecordingExporter\logs\` on Windows
or `~/SteamGameRecordingExporter/logs/` on macOS and Linux.

## Development

```bash
uv sync --extra dev
uv run pytest -q
uv build
```

The test matrix covers Python 3.9 and current Python on Ubuntu, macOS and
Windows.

## License

MIT License
