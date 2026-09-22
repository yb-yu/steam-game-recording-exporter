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

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) once.
On Windows, run this in PowerShell, then reopen your terminal:

```powershell
winget install --id=astral-sh.uv -e
```

Run the latest published release on Windows, macOS or Linux:

```sh
uvx --isolated --refresh --from https://github.com/yb-yu/steam-game-recording-exporter/releases/latest/download/steam-game-recording-exporter.tar.gz steamexporter
```

uv downloads Python if needed and installs the exporter and its dependencies in
an isolated environment. There is no need to clone the repository or unpack the
archive. `--refresh` checks for a newer release even when the download URL is
cached; `--isolated` prevents an older installed tool from taking precedence.
Append options such as `--list-clips` or `--help` to the command.
This URL becomes available after the first GitHub release is published.

### Install for repeated use

For a shorter everyday command, install from the latest release:

```sh
uv tool install --refresh https://github.com/yb-yu/steam-game-recording-exporter/releases/latest/download/steam-game-recording-exporter.tar.gz
steamexporter
```

Repeat the install command with `--reinstall` to update. The release page also
provides a versioned `.whl` and source `.tar.gz` for manual installation.

### Run from source

Requires Python 3.9 or newer, or uv to manage Python:

```bash
git clone https://github.com/yb-yu/steam-game-recording-exporter.git
cd steam-game-recording-exporter
uv sync
uv run steamexporter
```

With pip, after cloning the repository and entering its directory:

```bash
pip install .
steamexporter
```

## Interactive mode

Run the quick-start command or `steamexporter` with no options. Use the arrow
keys and Enter to choose; press `Esc` to go back one step.

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
? Group exported videos into game folders? No
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

# Export into a separate subfolder for each game
uv run steamexporter --process-all --group-by-game --output ~/Videos

# Preview cleanup without deleting anything
uv run steamexporter --cleanup-only --dry-run

# Show every option
uv run steamexporter --help
```

These examples use a source checkout. When installed with `uv tool install` or
pip, replace `uv run steamexporter` with `steamexporter`. With `uvx`, append the
options to the full quick-start command.
The interactive menu needs a real terminal; under Git Bash/mintty, use the
command-line options instead.

By default, exports go directly into the output directory. `--group-by-game`
saves new videos under a game subfolder instead, for example
`~/Videos/Dota_2/Dota_2_2025-01-02_03-04-05.mp4`. Folder names use the same
character replacements as filenames; names that cannot be used as folders
fall back to `Game_<AppID>`. The menu offers the same choice. Grouping is off
by default for each run and is not saved as a preference.

Duplicate detection and `--cleanup-only` check both the output directory and
the corresponding game subfolder, including numbered filenames, regardless of
the grouping option. Switching layouts leaves existing exports in place and
skips recordings already exported in either layout. Other subfolders are not
searched.

## Conversion and logs

Fragments are copied in filename order into temporary video/audio streams in a
separate `<output>/.temp-*` directory for each recording, then remuxed without
re-encoding. Separate directories prevent one worker's cleanup from interrupting
another recording. Only one source fragment is
open at a time, using 4 MiB copy blocks, so recordings with thousands of fragments
do not exhaust FFmpeg's open-file limit. Single-session recordings need one
FFmpeg pass; multi-session recordings also join their session streams.
All matching fragments are exported; manifest durations are used only for
progress, so an incomplete manifest duration does not trim the recording.

Allow disk space for the temporary streams as well as the final MP4 for each
active worker. Temporary files are removed on success or failure. Exported files
keep the recording time both as their file timestamp and as MP4 `creation_time`
metadata.

Logs are written to `%LOCALAPPDATA%\SteamGameRecordingExporter\logs\` on Windows
or `~/SteamGameRecordingExporter/logs/` on macOS and Linux.
The file log includes runtime and FFmpeg versions, available open-file limits,
per-session fragment counts, FFmpeg commands and exit status. `--verbose` also
prints debug messages to the console; these details are always in the log file.

## Development

```bash
uv sync --extra dev
uv run pytest -q
uv build
```

The test matrix covers Python 3.9 and current Python on Ubuntu, macOS and
Windows. Tests cover 10,000 fragments, bounded source reads, partial-output
cleanup and complete video/audio packet preservation with concurrent exports.
On POSIX, the integration test lowers the child process's open-file limit to 64.
Grouped exports are tested with multiple games and parallel workers, layout
switches, safe folder names, CLI/menu options, and cleanup in both layouts.

Use the Git-ignored `scratchpad/` directory for local investigations and generated
experiments. Keep reusable regression tests in `tests/`.

## Releasing

1. Update `version` in `pyproject.toml` and `__version__` in `steamexporter.py`,
   then run `uv lock` to update the local package version in `uv.lock`.
2. Merge the change into `main` after the Tests and Release checks pass.
3. Tag that commit with its version and push the tag, for example:

   ```sh
   git switch main
   git pull --ff-only
   git tag -a v1.2.0 -m "Release v1.2.0"
   git push origin v1.2.0
   ```

The Release workflow accepts stable `vMAJOR.MINOR.PATCH` tags that match the
package version. It reruns the full test matrix, builds packages, and checks the
installed CLI on Linux, macOS and Windows before publishing. Branch and pull
request runs build and check packages without publishing a release.

Each GitHub release includes generated release notes, a source commit link, a
versioned wheel and source archive, and an identical source archive named
`steam-game-recording-exporter.tar.gz` for the stable `latest/download` URL.
Packages are attached to GitHub Releases; this workflow does not publish to PyPI.
Published tags and assets are not overwritten; ship a new version for corrections.

## License

MIT License
