"""Filename, timestamp, game-name cache and saved preference behavior."""

import json
import os
import platform
from datetime import datetime
from pathlib import Path

import pytest


def test_sanitizes_platform_unsafe_characters(exporter):
    assert exporter.sanitize_filename('<Dota 2>:"|?*\\/검은사막') == "Dota_2_검은사막"
    assert exporter.sanitize_filename("Dota2_2025.mp4") == "Dota2_2025.mp4"


def test_unique_filename_sanitizes_and_increments(exporter, output_dir):
    (output_dir / "Dota_2.mp4").write_bytes(b"")
    (output_dir / "Dota_2_1.mp4").write_bytes(b"")

    result = exporter.get_unique_filename(str(output_dir), "Dota 2.mp4")

    assert os.path.basename(result) == "Dota_2_2.mp4"


def test_parses_valid_and_invalid_recording_dates(exporter):
    assert exporter.extract_datetime_from_folder_name(
        os.path.join("x", "clip_570_20250102_030405")
    ) == datetime(2025, 1, 2, 3, 4, 5)
    assert exporter.extract_datetime_from_folder_name("clip_570_notadate_xxxx") == datetime.min


def test_recorded_time_sets_file_dates(exporter, tmp_path):
    exported = tmp_path / "export.mp4"
    exported.write_bytes(b"mp4")
    recorded_at = datetime(2025, 1, 2, 3, 4, 5)

    exporter.apply_recorded_timestamp(str(exported), recorded_at)

    stat = exported.stat()
    assert stat.st_mtime == recorded_at.timestamp()
    if platform.system() in ("Windows", "Darwin"):
        # Linux has no API for setting a file's creation time. Older Windows
        # Pythons report it as st_ctime.
        assert getattr(stat, "st_birthtime", stat.st_ctime) == recorded_at.timestamp()


def test_expected_filename_matches_converter_naming(exporter, output_dir, monkeypatch):
    monkeypatch.setattr(
        type(exporter), "get_game_name", lambda self, game_id: "Half-Life: Alyx"
    )
    clip = os.path.join("anywhere", "clip_570_20250102_030405")

    expected = exporter.get_expected_output_filename(clip, str(output_dir))
    converted = exporter.get_unique_filename(
        str(output_dir), "Half-Life: Alyx_2025-01-02_03-04-05.mp4"
    )

    assert expected == converted


def test_game_name_cache_is_saved_and_reloaded(exporter, named_games, config_dir):
    assert exporter.get_game_name("570") == "Dota 2"
    assert json.loads((config_dir / "GameIDs.json").read_text(encoding="utf-8")) == {
        "570": "Dota 2"
    }

    fresh = type(exporter)(max_workers=1)
    assert fresh.game_ids == {"570": "Dota 2"}
    assert fresh.group_by_game is False


def test_preferences_keep_only_output_and_workers(exporter, config_dir, tmp_path):
    output = tmp_path / "exports"
    exporter.group_by_game = True

    exporter.save_preferences(str(output), 3)

    assert json.loads((config_dir / "settings.json").read_text(encoding="utf-8")) == {
        "output_dir": str(output), "workers": 3
    }
    fresh = type(exporter)()
    assert fresh.max_workers == 3 and fresh.group_by_game is False
    assert type(exporter)(max_workers=1).max_workers == 1


@pytest.mark.parametrize("game_name, folder", [
    ("Half-Life: Alyx", "Half-Life_Alyx"),
    ("검은사막", "검은사막"),
    ("../Escape", ".._Escape"),
    ("..", "Game_570"),
    ("CON", "Game_570"),
    ("nul.txt", "Game_570"),
    ("Game.", "Game"),
    ("???", "Game_570"),
    ("Game\x01Name", "Game_Name"),
])
def test_game_folder_is_a_safe_component(exporter, output_dir, monkeypatch, game_name, folder):
    exporter.group_by_game = True
    monkeypatch.setattr(exporter, "get_game_name", lambda _: game_name)
    clip = os.path.join("anywhere", "clip_570_20250102_030405")

    expected = Path(exporter.get_expected_output_filename(clip, str(output_dir)))

    assert expected.parent == output_dir / folder
    assert expected.parent.parent == output_dir
    assert str(expected) == exporter.get_unique_filename(
        str(output_dir / folder), f"{game_name}_2025-01-02_03-04-05.mp4"
    )
