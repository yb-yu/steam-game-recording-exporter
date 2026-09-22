# Steam Game Recording Exporter

Export Steam's fragmented game recordings to MP4 without re-encoding, preserving
recording timestamps. Works on Windows, macOS and Linux with automatic Steam
path detection and an interactive menu.

Original recordings are kept unless you explicitly choose source deletion or
cleanup mode.

## Quick start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) once.
On Windows, run this in PowerShell, then reopen your terminal:

```powershell
winget install --id=astral-sh.uv -e
```

Run the latest release:

```sh
uvx --isolated --refresh --from https://github.com/yb-yu/steam-game-recording-exporter/releases/latest/download/steam-game-recording-exporter.tar.gz steamexporter
```

uv manages Python and dependencies; no cloning or unpacking is needed.
`--isolated --refresh` checks for the latest release even if an older version is
installed or cached. This URL becomes available after the first release.

For a shorter command on repeated use:

```sh
uv tool install --refresh https://github.com/yb-yu/steam-game-recording-exporter/releases/latest/download/steam-game-recording-exporter.tar.gz
steamexporter
```

Repeat the install command with `--reinstall` to update.

## Usage

Run with no options to choose recordings, games and an output folder from the
menu. Use arrow keys and Enter to select, `Esc` to go back, and `Ctrl+C` to cancel.
The menu needs a real terminal; use PowerShell or Windows Terminal on Windows.

- Choose background recordings (Steam's rolling history), saved clips, or both.
- Enable game folders to group exports by game. This is off by default each run.
  Existing exports are detected in either layout and are not moved.
- The output folder and worker count are remembered; source deletion is not.
  Start with 1–2 workers for an HDD or 2–4 for an SSD.
- Active background recordings are skipped. Stop the game and wait up to a
  minute before trying again. Saved clips are unaffected.

For scripts or non-interactive use, append options to the quick-start command.
After installing, for example:

```sh
steamexporter --process-all --group-by-game --output ~/Videos
```

See `steamexporter --help` for all options.

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

## Releasing

1. Update the version in `pyproject.toml` and `steamexporter.py`, then run `uv lock`.
2. Merge into `main` after CI passes.
3. Push a matching stable version tag, for example:

   ```sh
   git switch main
   git pull --ff-only
   git tag -a v1.2.0 -m "Release v1.2.0"
   git push origin v1.2.0
   ```

Tag pushes rerun tests and package checks before publishing a GitHub release
with generated notes, a commit link, and wheel/source archives. A fixed-name
source archive supports the `latest` download URL. Branch and PR runs only
validate packages. For corrections, publish a new version.

## License

MIT License
