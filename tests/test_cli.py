"""CLI smoke tests.

These run the script in a subprocess so they exercise argument parsing, exit
codes and console output encoding the way a user actually hits them - which is
where Windows and POSIX differ most.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = str(Path(__file__).resolve().parents[1] / "steamexporter.py")


@pytest.fixture
def run_cli(tmp_path):
    """Run steamexporter.py with an isolated config dir and no network access.

    ``CONFIG_DIR`` is derived from ``LOCALAPPDATA`` with a fallback to the home
    directory, so setting that variable redirects it on every platform.
    """
    app_config = tmp_path / "clicfg" / "SteamGameRecordingExporter"
    app_config.mkdir(parents=True)
    # Pre-seed the game-name cache so no Steam API call is made.
    (app_config / "GameIDs.json").write_text(
        json.dumps({"570": "Dota 2", "730": "Counter-Strike 2"}), encoding="utf-8"
    )

    def _run(*args, **kwargs):
        env = dict(os.environ)
        env["LOCALAPPDATA"] = str(tmp_path / "clicfg")
        env["HOME"] = str(tmp_path / "clicfg")
        env["USERPROFILE"] = str(tmp_path / "clicfg")
        env.update(kwargs.pop("env", {}))
        return subprocess.run(
            [sys.executable, SCRIPT] + list(args),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, **kwargs
        )

    return _run


def test_help_exits_cleanly(run_cli):
    result = run_cli("--help")
    assert result.returncode == 0
    assert "--list-clips" in result.stdout


def test_detect_paths_never_crashes(run_cli):
    result = run_cli("--detect-paths")
    assert result.returncode == 0, result.stderr
    assert "Steam userdata path" in result.stdout or "No Steam userdata paths" in result.stdout


def test_list_clips_prints_game_names_and_dates(run_cli, steam_tree):
    steam_tree.add_clip("clip_570_20250102_030405")
    steam_tree.add_clip("bg_730_20250103_040506", kind="video")

    result = run_cli("--userdata-path", str(steam_tree), "--list-clips")

    assert result.returncode == 0, result.stderr
    assert "Dota 2" in result.stdout
    assert "Counter-Strike 2" in result.stdout
    assert "2025-01-02 03:04:05" in result.stdout


def test_game_id_filter_narrows_the_listing(run_cli, steam_tree):
    steam_tree.add_clip("clip_570_20250102_030405")
    steam_tree.add_clip("clip_730_20250103_040506")

    result = run_cli("--userdata-path", str(steam_tree), "--game-id", "570", "--list-clips")

    assert result.returncode == 0, result.stderr
    assert "Dota 2" in result.stdout
    assert "Counter-Strike 2" not in result.stdout


def test_no_clips_found_exits_nonzero(run_cli, tmp_path):
    empty = tmp_path / "empty_userdata"
    empty.mkdir()

    result = run_cli("--userdata-path", str(empty), "--list-clips")

    assert result.returncode == 1
    assert "No clips found" in result.stdout


@pytest.mark.parametrize("io_encoding", ["cp1252", "cp949", "ascii"])
def test_emoji_output_survives_a_legacy_code_page(run_cli, tmp_path, io_encoding):
    """Console output is full of emoji; a non-UTF-8 stdout must not abort the run.

    Reproduces what a Windows console (cp949/cp1252) or a redirected stdout
    does to ``print("🔍 ...")``.
    """
    empty = tmp_path / "empty_userdata"
    empty.mkdir()

    result = run_cli(
        "--userdata-path", str(empty), "--list-clips",
        env={"PYTHONIOENCODING": io_encoding},
    )

    assert "UnicodeEncodeError" not in result.stderr
    assert result.returncode == 1
    assert "No clips found" in result.stdout


def test_cleanup_only_dry_run_deletes_nothing(run_cli, steam_tree, output_dir):
    clip = steam_tree.add_clip("clip_570_20250102_030405")
    (output_dir / "Dota_2_2025-01-02_03-04-05.mp4").write_bytes(b"x")

    result = run_cli(
        "--userdata-path", str(steam_tree),
        "--output", str(output_dir),
        "--cleanup-only", "--dry-run",
    )

    assert result.returncode == 0, result.stderr
    assert "WOULD BE deleted" in result.stdout
    assert clip.exists()


def test_cleanup_only_removes_converted_sources(run_cli, steam_tree, output_dir):
    clip = steam_tree.add_clip("clip_570_20250102_030405")
    (output_dir / "Dota_2_2025-01-02_03-04-05.mp4").write_bytes(b"x")

    result = run_cli(
        "--userdata-path", str(steam_tree),
        "--output", str(output_dir),
        "--cleanup-only",
    )

    assert result.returncode == 0, result.stderr
    assert not clip.exists()
