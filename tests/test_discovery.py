"""Steam path and recording discovery."""

import os

import steamexporter
from .conftest import DEFAULT_STEAM_ID

OTHER_STEAM_ID = "76561198000000002"


def names(paths):
    return [os.path.basename(path) for path in paths]


def test_auto_detection_runs_on_the_host_platform(exporter):
    detected = exporter.auto_detect_steam_paths()
    assert isinstance(detected, list)
    assert all(os.path.isdir(path) for path in detected)


def test_auto_detection_finds_a_linux_steam_home(exporter, tmp_path, monkeypatch):
    home = tmp_path / "home"
    userdata = home / ".steam" / "steam" / "userdata" / DEFAULT_STEAM_ID
    userdata.mkdir(parents=True)
    monkeypatch.setattr(steamexporter.platform, "system", lambda: "Linux")
    monkeypatch.setattr(steamexporter.os.path, "expanduser", lambda path: str(home))

    assert [os.path.normpath(path) for path in exporter.auto_detect_steam_paths()] == [
        os.path.normpath(str(userdata.parent))
    ]


def test_finds_multiple_nested_session_manifests(exporter, tmp_path):
    clip = tmp_path / "clip_570_20250102_030405"
    for part in ("a", "b"):
        folder = clip / part
        folder.mkdir(parents=True)
        (folder / "session.mpd").write_text("<MPD/>", encoding="utf-8")

    assert len(exporter.find_session_mpd(str(clip))) == 2


def test_discovers_and_filters_recordings(exporter, steam_tree):
    steam_tree.add_clip("clip_570_20250102_030405")
    steam_tree.add_clip("bg_730_20250103_040506", kind="video")
    steam_tree.add_clip("clip_730_20250101_010101", steam_id=OTHER_STEAM_ID)

    assert names(exporter.get_clip_folders(str(steam_tree))) == [
        "bg_730_20250103_040506",
        "clip_570_20250102_030405",
        "clip_730_20250101_010101",
    ]
    assert names(exporter.get_clip_folders(str(steam_tree), media_type="background")) == [
        "bg_730_20250103_040506"
    ]
    assert names(exporter.get_clip_folders(str(steam_tree), game_id="570")) == [
        "clip_570_20250102_030405"
    ]
    assert names(exporter.get_clip_folders(str(steam_tree), steam_id=OTHER_STEAM_ID)) == [
        "clip_730_20250101_010101"
    ]


def test_ignores_invalid_accounts_and_recording_folders(exporter, steam_tree):
    steam_tree.add_clip("clip_570_20250102_030405", with_session=False)
    steam_tree.add_clip("randomfolder")
    steam_tree.add_clip("clip_730_20250103_040506", steam_id="not-an-id")

    assert exporter.get_clip_folders(str(steam_tree)) == []


def test_merges_default_and_custom_recording_paths(exporter, steam_tree, tmp_path):
    custom = tmp_path / "custom recordings"
    (custom / "video").mkdir(parents=True)
    steam_tree.set_custom_record_path(str(custom))
    steam_tree.add_clip("clip_570_20250102_030405")
    steam_tree.add_clip("bg_570_20250104_050607", base=custom / "video")

    assert exporter.get_custom_record_path(steam_tree.user_dir()) == str(custom)
    assert sorted(names(exporter.get_clip_folders(str(steam_tree)))) == [
        "bg_570_20250104_050607",
        "clip_570_20250102_030405",
    ]
