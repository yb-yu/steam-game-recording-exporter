"""Filename derivation: sanitizing, uniqueness and the date/game-name scheme.

These are the parts most likely to drift between Windows and POSIX, since the
invalid-character set and the path separator differ.
"""

import json
import os
from datetime import datetime

import pytest


class TestSanitizeFilename:
    @pytest.mark.parametrize("char", list('<>:"|?*\\/ '))
    def test_replaces_every_invalid_character(self, exporter, char):
        assert exporter.sanitize_filename("a%sb" % char) == "a_b"

    def test_collapses_repeated_underscores(self, exporter):
        assert exporter.sanitize_filename("Half-Life: Alyx") == "Half-Life_Alyx"

    def test_strips_leading_and_trailing_underscores(self, exporter):
        assert exporter.sanitize_filename(" Portal 2 ") == "Portal_2"

    def test_keeps_non_ascii_characters(self, exporter):
        assert exporter.sanitize_filename("검은사막 온라인") == "검은사막_온라인"

    def test_leaves_safe_names_untouched(self, exporter):
        assert exporter.sanitize_filename("Dota2_2025-01-02_03-04-05.mp4") == \
            "Dota2_2025-01-02_03-04-05.mp4"


class TestUniqueFilename:
    def test_returns_plain_path_when_free(self, exporter, output_dir):
        result = exporter.get_unique_filename(str(output_dir), "Dota_2.mp4")
        assert result == os.path.join(str(output_dir), "Dota_2.mp4")

    def test_appends_counter_on_collision(self, exporter, output_dir):
        (output_dir / "Dota_2.mp4").write_bytes(b"")
        result = exporter.get_unique_filename(str(output_dir), "Dota_2.mp4")
        assert os.path.basename(result) == "Dota_2_1.mp4"

    def test_counter_keeps_climbing(self, exporter, output_dir):
        (output_dir / "Dota_2.mp4").write_bytes(b"")
        (output_dir / "Dota_2_1.mp4").write_bytes(b"")
        result = exporter.get_unique_filename(str(output_dir), "Dota_2.mp4")
        assert os.path.basename(result) == "Dota_2_2.mp4"

    def test_sanitizes_before_checking(self, exporter, output_dir):
        result = exporter.get_unique_filename(str(output_dir), "Dota 2.mp4")
        assert os.path.basename(result) == "Dota_2.mp4"


class TestExtractDatetime:
    def test_parses_steam_folder_name(self, exporter):
        assert exporter.extract_datetime_from_folder_name(
            os.path.join("x", "clip_570_20250102_030405")
        ) == datetime(2025, 1, 2, 3, 4, 5)

    def test_unparseable_date_falls_back_to_min(self, exporter):
        assert exporter.extract_datetime_from_folder_name("clip_570_notadate_xxxx") == datetime.min

    def test_too_few_parts_falls_back_to_min(self, exporter):
        assert exporter.extract_datetime_from_folder_name("clip_570") == datetime.min


class TestExpectedOutputFilename:
    def test_uses_game_name_and_formatted_date(self, exporter, named_games, output_dir):
        expected = exporter.get_expected_output_filename(
            os.path.join("anywhere", "clip_570_20250102_030405"), str(output_dir)
        )
        assert expected == os.path.join(str(output_dir), "Dota_2_2025-01-02_03-04-05.mp4")

    def test_unknown_date_marker(self, exporter, named_games, output_dir):
        expected = exporter.get_expected_output_filename(
            os.path.join("anywhere", "clip_570_bogus_stamp"), str(output_dir)
        )
        assert os.path.basename(expected) == "Dota_2_UnknownDate.mp4"

    def test_falls_back_to_game_id_when_lookup_fails(self, exporter, output_dir):
        # `exporter` fixture makes the Steam lookup return None.
        expected = exporter.get_expected_output_filename(
            os.path.join("anywhere", "clip_999_20250102_030405"), str(output_dir)
        )
        assert os.path.basename(expected) == "Game_999_2025-01-02_03-04-05.mp4"

    @pytest.mark.parametrize("game_name", [
        "Dota 2",
        "Half-Life: Alyx",
        "Tom Clancy's Rainbow Six | Siege",
        "검은사막",
    ])
    def test_matches_what_the_converter_would_write(self, exporter, output_dir, monkeypatch, game_name):
        """The skip-if-exists check and the real conversion must agree.

        ``process_single_clip`` sanitizes ``"{name}_{date}"`` as a whole, while
        ``get_expected_output_filename`` sanitizes the name and then appends the
        date. If those ever diverge, clips get re-converted forever.
        """
        monkeypatch.setattr(type(exporter), "get_game_name", lambda self, gid: game_name)
        clip = os.path.join("anywhere", "clip_570_20250102_030405")

        expected = exporter.get_expected_output_filename(clip, str(output_dir))
        as_converted = exporter.get_unique_filename(
            str(output_dir), "%s_2025-01-02_03-04-05.mp4" % game_name
        )
        assert expected == as_converted


class TestGameIdCache:
    def test_unknown_game_is_cached_to_disk(self, exporter, config_dir):
        assert exporter.get_game_name("999") == "Game_999"
        saved = json.loads((config_dir / "GameIDs.json").read_text(encoding="utf-8"))
        assert saved == {"999": "Game_999"}

    def test_resolved_name_is_cached_to_disk(self, exporter, named_games, config_dir):
        assert exporter.get_game_name("570") == "Dota 2"
        saved = json.loads((config_dir / "GameIDs.json").read_text(encoding="utf-8"))
        assert saved["570"] == "Dota 2"

    def test_cache_is_reloaded_on_next_run(self, exporter, config_dir):
        exporter.game_ids = {"570": "Dota 2"}
        exporter.save_game_ids()

        fresh = type(exporter)(max_workers=1)
        assert fresh.game_ids == {"570": "Dota 2"}

    def test_corrupt_cache_does_not_crash(self, exporter, config_dir):
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "GameIDs.json").write_text("{not json", encoding="utf-8")

        fresh = type(exporter)(max_workers=1)
        assert fresh.game_ids == {}
