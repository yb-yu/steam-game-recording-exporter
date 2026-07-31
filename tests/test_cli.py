"""Small subprocess smoke tests for the public CLI."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = str(Path(__file__).resolve().parents[1] / "steamexporter.py")


@pytest.fixture
def run_cli(tmp_path):
    config = tmp_path / "clicfg" / "SteamGameRecordingExporter"
    config.mkdir(parents=True)
    (config / "GameIDs.json").write_text(
        json.dumps({"570": "Dota 2", "730": "Counter-Strike 2"}), encoding="utf-8"
    )

    def run(*args, **kwargs):
        env = dict(os.environ)
        env.update({
            "LOCALAPPDATA": str(tmp_path / "clicfg"),
            "HOME": str(tmp_path / "clicfg"),
            "USERPROFILE": str(tmp_path / "clicfg"),
        })
        env.update(kwargs.pop("env", {}))
        return subprocess.run(
            [sys.executable, SCRIPT, *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env, **kwargs
        )

    return run


def test_help_exits_cleanly(run_cli):
    result = run_cli("--help")
    assert result.returncode == 0
    assert "--list-clips" in result.stdout


def test_list_clips_prints_both_recording_types(run_cli, steam_tree):
    steam_tree.add_clip("clip_570_20250102_030405")
    steam_tree.add_clip("bg_730_20250103_040506", kind="video")

    result = run_cli("--userdata-path", str(steam_tree), "--list-clips")

    assert result.returncode == 0, result.stderr
    assert "Dota 2" in result.stdout
    assert "Counter-Strike 2" in result.stdout


def test_no_clips_is_safe_with_a_legacy_console_encoding(run_cli, tmp_path):
    empty = tmp_path / "empty_userdata"
    empty.mkdir()

    result = run_cli(
        "--userdata-path", str(empty), "--list-clips",
        env={"PYTHONIOENCODING": "cp949"},
    )

    assert result.returncode == 1
    assert "No clips found" in result.stdout
    assert "UnicodeEncodeError" not in result.stderr


def test_cleanup_dry_run_deletes_nothing(run_cli, steam_tree, output_dir):
    clip = steam_tree.add_clip("clip_570_20250102_030405")
    (output_dir / "Dota_2_2025-01-02_03-04-05.mp4").write_bytes(b"x")

    result = run_cli(
        "--userdata-path", str(steam_tree), "--output", str(output_dir),
        "--cleanup-only", "--dry-run",
    )

    assert result.returncode == 0, result.stderr
    assert "WOULD BE deleted" in result.stdout
    assert clip.exists()
