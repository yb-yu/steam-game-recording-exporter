"""Filename, timestamp and game-name cache behavior."""

import json
import os
from datetime import datetime


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
