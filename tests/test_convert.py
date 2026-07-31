"""A few end-to-end checks using real DASH segments and bundled FFmpeg."""

import os
import shutil
import subprocess
from datetime import datetime

import pytest

from .conftest import has_encoder

pytestmark = pytest.mark.ffmpeg


@pytest.fixture(scope="session")
def dash_source(ffmpeg_exe, tmp_path_factory):
    missing = [name for name in ("libx264", "aac") if not has_encoder(ffmpeg_exe, name)]
    if missing:
        pytest.skip("ffmpeg build lacks encoder(s): %s" % ", ".join(missing))

    source = tmp_path_factory.mktemp("dash_source")
    subprocess.run(
        [
            ffmpeg_exe, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-f", "dash", "-seg_duration", "1", "session.mpd",
        ],
        check=True, capture_output=True, cwd=str(source),
    )
    return source


@pytest.fixture
def make_clip(dash_source, steam_tree):
    def make(folder_name="clip_570_20250102_030405"):
        clip = steam_tree.add_clip(folder_name, with_session=False)
        shutil.copytree(str(dash_source), str(clip / "dash"), dirs_exist_ok=True)
        return clip

    return make


def test_exports_a_playable_timestamped_mp4(
    exporter, named_games, make_clip, output_dir, ffmpeg_exe
):
    clip = make_clip()
    success, message = exporter.process_single_clip(str(clip), str(output_dir))

    assert success, message
    produced = output_dir / "Dota_2_2025-01-02_03-04-05.mp4"
    probe = subprocess.run(
        [ffmpeg_exe, "-hide_banner", "-i", str(produced)], capture_output=True, text=True
    )
    assert "Video:" in probe.stderr and "Audio:" in probe.stderr
    assert datetime.fromtimestamp(produced.stat().st_mtime).replace(microsecond=0) == datetime(
        2025, 1, 2, 3, 4, 5
    )
    assert clip.exists()
    assert not (output_dir / ".temp").exists()


def test_second_export_is_skipped(exporter, named_games, make_clip, output_dir):
    clip = make_clip()
    assert exporter.process_single_clip(str(clip), str(output_dir))[0]
    first_pass = sorted(os.listdir(str(output_dir)))

    success, message = exporter.process_single_clip(str(clip), str(output_dir))

    assert success and "Already converted (skipped)" in message
    assert sorted(os.listdir(str(output_dir))) == first_pass


def test_delete_source_only_after_an_export_exists(exporter, named_games, make_clip, output_dir):
    clip = make_clip()
    assert exporter.process_single_clip(str(clip), str(output_dir))[0]

    success, message = exporter.process_single_clip(
        str(clip), str(output_dir), delete_source=True
    )

    assert success and "deleted source" in message
    assert not clip.exists()


def test_invalid_recording_layout_is_reported(
    exporter, named_games, make_clip, steam_tree, output_dir
):
    missing_init = make_clip()
    (missing_init / "dash" / "init-stream0.m4s").unlink()
    no_manifest = steam_tree.add_clip("clip_730_20250103_040506", with_session=False)

    assert exporter.process_single_clip(str(missing_init), str(output_dir))[0] is False
    assert exporter.process_single_clip(str(no_manifest), str(output_dir))[0] is False


def test_batch_converts_good_clips_and_collects_failures(
    exporter, named_games, make_clip, steam_tree, tmp_path
):
    good = str(make_clip())
    bad = str(steam_tree.add_clip("clip_730_20250103_040506", with_session=False))
    output = tmp_path / "nested" / "exports"

    results = exporter.process_clips_batch([good, bad], str(output))

    assert results["successful"] == [good]
    assert [path for path, _ in results["failed"]] == [bad]
    assert (output / "Dota_2_2025-01-02_03-04-05.mp4").exists()
