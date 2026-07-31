"""Shared fixtures for the Steam Game Recording Exporter test suite.

No binary fixtures are committed: fake Steam recording trees are built inside
``tmp_path``, and the one end-to-end test generates real ``.m4s`` segments with
the bundled ffmpeg (see ``test_convert.py``).
"""

import logging
import subprocess
from pathlib import Path

import pytest

import steamexporter
from steamexporter import SteamGameRecordingExporter

DEFAULT_STEAM_ID = "76561198000000001"


class SteamTree:
    """Builder for a fake ``userdata`` directory that mimics Steam's layout."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def add_clip(self, folder_name, steam_id=DEFAULT_STEAM_ID, kind="clips",
                 base=None, with_session=True, nested="dash"):
        """Create a clip folder and return its path.

        Args:
            folder_name: e.g. ``clip_570_20250102_030405``
            kind: ``clips`` (manual) or ``video`` (background)
            base: override the parent directory (used for custom record paths)
            with_session: whether to drop a ``session.mpd`` inside
            nested: sub-directory holding the media, or None to put it flat
        """
        parent = Path(base) if base else self.root / steam_id / "gamerecordings" / kind
        clip = parent / folder_name
        data_dir = clip / nested if nested else clip
        data_dir.mkdir(parents=True, exist_ok=True)
        if with_session:
            (data_dir / "session.mpd").write_text("<MPD></MPD>", encoding="utf-8")
            (data_dir / "init-stream0.m4s").write_bytes(b"\x00")
            (data_dir / "init-stream1.m4s").write_bytes(b"\x00")
        return clip

    def set_custom_record_path(self, path, steam_id=DEFAULT_STEAM_ID):
        """Write a localconfig.vdf declaring a custom BackgroundRecordPath."""
        config_dir = self.root / steam_id / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "localconfig.vdf").write_text(
            '\t\t\t\t"BackgroundRecordPath"\t\t"%s"\n' % path,
            encoding="utf-8",
        )

    def user_dir(self, steam_id=DEFAULT_STEAM_ID):
        return str(self.root / steam_id)

    def __str__(self):
        return str(self.root)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if a test reaches for the Steam store API."""

    def _blocked(*args, **kwargs):
        raise AssertionError("test attempted a network request: %r" % (args,))

    monkeypatch.setattr(steamexporter.requests, "get", _blocked)


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """Redirect the exporter's config/GameIDs location into tmp_path."""
    cfg = tmp_path / "appconfig"
    monkeypatch.setattr(SteamGameRecordingExporter, "CONFIG_DIR", str(cfg))
    monkeypatch.setattr(SteamGameRecordingExporter, "GAME_IDS_FILE", str(cfg / "GameIDs.json"))
    return cfg


@pytest.fixture
def exporter(config_dir, monkeypatch):
    """An exporter with logging stubbed out and game-name lookups offline.

    ``setup_logging`` is replaced because the real one attaches a file handler
    to the root logger that would leak across tests (and keep a handle open on
    tmp_path under Windows). It gets its own test in ``test_logging.py``.
    """
    monkeypatch.setattr(
        SteamGameRecordingExporter,
        "setup_logging",
        lambda self: setattr(self, "logger", logging.getLogger("steamexporter.test")),
    )
    monkeypatch.setattr(
        SteamGameRecordingExporter,
        "fetch_game_name_from_steam",
        lambda self, game_id: None,
    )
    return SteamGameRecordingExporter(max_workers=2)


@pytest.fixture
def named_games(monkeypatch):
    """Give the exporter a canned game-id -> name table (no network)."""
    names = {"570": "Dota 2", "730": "Counter-Strike 2"}

    def _fetch(self, game_id):
        return names.get(game_id)

    monkeypatch.setattr(SteamGameRecordingExporter, "fetch_game_name_from_steam", _fetch)
    return names


@pytest.fixture
def steam_tree(tmp_path):
    return SteamTree(tmp_path / "userdata")


@pytest.fixture
def output_dir(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    return out


@pytest.fixture(scope="session")
def ffmpeg_exe():
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def has_encoder(ffmpeg, name):
    """Whether this ffmpeg build ships the given encoder."""
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"],
        capture_output=True, text=True,
    )
    return name in result.stdout
