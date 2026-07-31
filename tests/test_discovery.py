"""Steam path auto-detection and clip discovery/filtering."""

import os

import pytest

import steamexporter
from .conftest import DEFAULT_STEAM_ID

OTHER_STEAM_ID = "76561198000000002"


def names(paths):
    return sorted(os.path.basename(p) for p in paths)


class TestAutoDetectSteamPaths:
    def test_does_not_crash_on_the_host_platform(self, exporter):
        """Runs the real branch for whichever OS the suite is running on."""
        detected = exporter.auto_detect_steam_paths()
        assert isinstance(detected, list)
        assert all(os.path.isdir(p) for p in detected)

    @pytest.mark.parametrize("system,relative", [
        ("Linux", ".steam/steam"),
        ("Linux", ".local/share/Steam"),
        ("Darwin", "Library/Application Support/Steam"),
    ])
    def test_finds_steam_under_a_fake_home(self, exporter, tmp_path, monkeypatch, system, relative):
        home = tmp_path / "home"
        userdata = home.joinpath(*relative.split("/")) / "userdata" / DEFAULT_STEAM_ID
        userdata.mkdir(parents=True)

        monkeypatch.setattr(steamexporter.platform, "system", lambda: system)
        monkeypatch.setattr(steamexporter.os.path, "expanduser", lambda p: str(home))

        detected = exporter.auto_detect_steam_paths()
        assert [os.path.normpath(p) for p in detected] == [
            os.path.normpath(str(userdata.parent))
        ]

    def test_ignores_userdata_without_numeric_account_dirs(self, exporter, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".steam" / "steam" / "userdata" / "not-an-id").mkdir(parents=True)

        monkeypatch.setattr(steamexporter.platform, "system", lambda: "Linux")
        monkeypatch.setattr(steamexporter.os.path, "expanduser", lambda p: str(home))

        assert exporter.auto_detect_steam_paths() == []

    def test_find_userdata_path_returns_none_when_nothing_detected(self, exporter, monkeypatch):
        monkeypatch.setattr(type(exporter), "auto_detect_steam_paths", lambda self: [])
        assert exporter.find_steam_userdata_path() is None

    def test_find_userdata_path_takes_the_first_hit(self, exporter, monkeypatch):
        monkeypatch.setattr(type(exporter), "auto_detect_steam_paths", lambda self: ["/a", "/b"])
        assert exporter.find_steam_userdata_path() == "/a"


class TestFindSessionMpd:
    def test_finds_nested_session_files(self, exporter, tmp_path):
        clip = tmp_path / "clip_570_20250102_030405"
        (clip / "dash").mkdir(parents=True)
        (clip / "dash" / "session.mpd").write_text("<MPD/>", encoding="utf-8")

        found = exporter.find_session_mpd(str(clip))
        assert len(found) == 1
        assert found[0].endswith("session.mpd")

    def test_finds_multiple_sessions(self, exporter, tmp_path):
        clip = tmp_path / "clip_570_20250102_030405"
        for part in ("a", "b"):
            (clip / part).mkdir(parents=True)
            (clip / part / "session.mpd").write_text("<MPD/>", encoding="utf-8")

        assert len(exporter.find_session_mpd(str(clip))) == 2

    def test_empty_when_absent(self, exporter, tmp_path):
        (tmp_path / "clip").mkdir()
        assert exporter.find_session_mpd(str(tmp_path / "clip")) == []


class TestGetClipFolders:
    @pytest.fixture
    def populated(self, steam_tree):
        steam_tree.add_clip("clip_570_20250102_030405", kind="clips")
        steam_tree.add_clip("bg_730_20250103_040506", kind="video")
        steam_tree.add_clip("clip_730_20250101_010101", kind="clips")
        return steam_tree

    def test_finds_manual_and_background_by_default(self, exporter, populated):
        found = exporter.get_clip_folders(str(populated))
        assert names(found) == [
            "bg_730_20250103_040506",
            "clip_570_20250102_030405",
            "clip_730_20250101_010101",
        ]

    def test_media_type_manual(self, exporter, populated):
        found = exporter.get_clip_folders(str(populated), media_type="manual")
        assert names(found) == ["clip_570_20250102_030405", "clip_730_20250101_010101"]

    def test_media_type_background(self, exporter, populated):
        found = exporter.get_clip_folders(str(populated), media_type="background")
        assert names(found) == ["bg_730_20250103_040506"]

    def test_game_id_filter(self, exporter, populated):
        found = exporter.get_clip_folders(str(populated), game_id="730")
        assert names(found) == ["bg_730_20250103_040506", "clip_730_20250101_010101"]

    def test_sorted_newest_first(self, exporter, populated):
        found = exporter.get_clip_folders(str(populated))
        assert [os.path.basename(p) for p in found] == [
            "bg_730_20250103_040506",
            "clip_570_20250102_030405",
            "clip_730_20250101_010101",
        ]

    def test_steam_id_filter(self, exporter, steam_tree):
        steam_tree.add_clip("clip_570_20250102_030405")
        steam_tree.add_clip("clip_730_20250103_040506", steam_id=OTHER_STEAM_ID)

        found = exporter.get_clip_folders(str(steam_tree), steam_id=OTHER_STEAM_ID)
        assert names(found) == ["clip_730_20250103_040506"]

    def test_scans_every_account_when_unfiltered(self, exporter, steam_tree):
        steam_tree.add_clip("clip_570_20250102_030405")
        steam_tree.add_clip("clip_730_20250103_040506", steam_id=OTHER_STEAM_ID)

        assert len(exporter.get_clip_folders(str(steam_tree))) == 2

    def test_skips_folders_without_session_mpd(self, exporter, steam_tree):
        steam_tree.add_clip("clip_570_20250102_030405", with_session=False)
        assert exporter.get_clip_folders(str(steam_tree)) == []

    def test_skips_folder_names_without_underscore(self, exporter, steam_tree):
        steam_tree.add_clip("randomfolder")
        assert exporter.get_clip_folders(str(steam_tree)) == []

    def test_ignores_non_numeric_account_dirs(self, exporter, steam_tree):
        steam_tree.add_clip("clip_570_20250102_030405", steam_id="ac")
        assert exporter.get_clip_folders(str(steam_tree)) == []

    def test_empty_userdata(self, exporter, steam_tree):
        assert exporter.get_clip_folders(str(steam_tree)) == []


class TestCustomRecordPath:
    def test_reads_background_record_path_from_localconfig(self, exporter, steam_tree, tmp_path):
        custom = tmp_path / "custom recordings"
        custom.mkdir()
        steam_tree.set_custom_record_path(str(custom))

        assert exporter.get_custom_record_path(steam_tree.user_dir()) == str(custom)

    def test_result_is_cached(self, exporter, steam_tree, tmp_path):
        custom = tmp_path / "custom"
        custom.mkdir()
        steam_tree.set_custom_record_path(str(custom))
        user_dir = steam_tree.user_dir()

        assert exporter.get_custom_record_path(user_dir) == str(custom)
        (steam_tree.root / DEFAULT_STEAM_ID / "config" / "localconfig.vdf").unlink()
        assert exporter.get_custom_record_path(user_dir) == str(custom)

    def test_missing_localconfig_returns_none(self, exporter, steam_tree):
        steam_tree.add_clip("clip_570_20250102_030405")
        assert exporter.get_custom_record_path(steam_tree.user_dir()) is None

    def test_path_that_no_longer_exists_returns_none(self, exporter, steam_tree, tmp_path):
        steam_tree.set_custom_record_path(str(tmp_path / "gone"))
        assert exporter.get_custom_record_path(steam_tree.user_dir()) is None

    def test_clips_under_custom_path_are_discovered(self, exporter, steam_tree, tmp_path):
        custom = tmp_path / "D_drive_recordings"
        (custom / "clips").mkdir(parents=True)
        (custom / "video").mkdir(parents=True)
        steam_tree.set_custom_record_path(str(custom))
        steam_tree.add_clip("clip_570_20250102_030405", base=custom / "clips")
        steam_tree.add_clip("bg_570_20250103_040506", base=custom / "video")

        found = exporter.get_clip_folders(str(steam_tree))
        assert names(found) == ["bg_570_20250103_040506", "clip_570_20250102_030405"]

    def test_custom_and_default_paths_are_merged(self, exporter, steam_tree, tmp_path):
        custom = tmp_path / "elsewhere"
        (custom / "clips").mkdir(parents=True)
        steam_tree.set_custom_record_path(str(custom))
        steam_tree.add_clip("clip_570_20250102_030405")
        steam_tree.add_clip("clip_570_20250104_050607", base=custom / "clips")

        found = exporter.get_clip_folders(str(steam_tree))
        assert names(found) == ["clip_570_20250102_030405", "clip_570_20250104_050607"]
