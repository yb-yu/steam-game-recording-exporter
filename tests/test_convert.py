"""End-to-end conversion against real DASH segments.

ffmpeg's DASH muxer produces exactly the layout Steam writes
(``session.mpd`` + ``init-stream{0,1}.m4s`` + ``chunk-stream{0,1}-NNNNN.m4s``),
so the test data is generated on the fly instead of being committed.
"""

import os
import shutil
import subprocess

import pytest

from .conftest import has_encoder

pytestmark = pytest.mark.ffmpeg


@pytest.fixture(scope="session")
def encoders_available(ffmpeg_exe):
    missing = [n for n in ("libx264", "aac") if not has_encoder(ffmpeg_exe, n)]
    if missing:
        pytest.skip("ffmpeg build lacks encoder(s): %s" % ", ".join(missing))
    return True


@pytest.fixture(scope="session")
def dash_source(ffmpeg_exe, encoders_available, tmp_path_factory):
    """A one-second audio+video DASH recording, built once per session."""
    source = tmp_path_factory.mktemp("dash_source")
    # Run from inside the target directory: the DASH muxer resolves segment
    # names relative to the working directory, not to the .mpd path.
    subprocess.run(
        [
            ffmpeg_exe, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-f", "dash", "-seg_duration", "1",
            "session.mpd",
        ],
        check=True, capture_output=True, cwd=str(source),
    )
    assert (source / "init-stream0.m4s").exists()
    return source


@pytest.fixture
def make_clip(dash_source, steam_tree):
    """Materialize a Steam clip folder holding real DASH segments."""

    def _make(folder_name="clip_570_20250102_030405", **kwargs):
        clip = steam_tree.add_clip(folder_name, with_session=False, **kwargs)
        data_dir = clip / "dash"
        shutil.copytree(str(dash_source), str(data_dir), dirs_exist_ok=True)
        return clip

    return _make


def probe(ffmpeg_exe, path):
    """Return ffmpeg's stream description of a file."""
    result = subprocess.run(
        [ffmpeg_exe, "-hide_banner", "-i", str(path)],
        capture_output=True, text=True,
    )
    return result.stderr


class TestProcessSingleClip:
    def test_produces_a_playable_mp4(self, exporter, named_games, make_clip, output_dir, ffmpeg_exe):
        clip = make_clip()

        success, message = exporter.process_single_clip(str(clip), str(output_dir))

        assert success, message
        produced = output_dir / "Dota_2_2025-01-02_03-04-05.mp4"
        assert produced.exists(), "expected %s, got %s" % (produced, os.listdir(str(output_dir)))
        assert produced.stat().st_size > 0

        streams = probe(ffmpeg_exe, produced)
        assert "Video:" in streams
        assert "Audio:" in streams

    def test_cleans_up_its_temp_directory(self, exporter, named_games, make_clip, output_dir):
        clip = make_clip()

        exporter.process_single_clip(str(clip), str(output_dir))

        assert not (output_dir / ".temp").exists()

    def test_keeps_the_source_by_default(self, exporter, named_games, make_clip, output_dir):
        clip = make_clip()

        exporter.process_single_clip(str(clip), str(output_dir))

        assert clip.exists()

    def test_second_run_skips_instead_of_reconverting(self, exporter, named_games, make_clip, output_dir):
        clip = make_clip()
        assert exporter.process_single_clip(str(clip), str(output_dir))[0]
        first_pass = sorted(os.listdir(str(output_dir)))

        success, message = exporter.process_single_clip(str(clip), str(output_dir))

        assert success
        assert "Already converted (skipped)" in message
        assert sorted(os.listdir(str(output_dir))) == first_pass

    def test_delete_source_removes_an_already_converted_clip(self, exporter, named_games,
                                                             make_clip, output_dir):
        clip = make_clip()
        assert exporter.process_single_clip(str(clip), str(output_dir))[0]

        success, message = exporter.process_single_clip(
            str(clip), str(output_dir), delete_source=True
        )

        assert success
        assert "deleted source" in message
        assert not clip.exists()

    def test_unknown_game_id_falls_back_to_the_id(self, exporter, make_clip, output_dir):
        clip = make_clip("clip_999_20250102_030405")

        success, message = exporter.process_single_clip(str(clip), str(output_dir))

        assert success, message
        assert (output_dir / "Game_999_2025-01-02_03-04-05.mp4").exists()

    def test_missing_init_files_is_reported(self, exporter, named_games, make_clip, output_dir):
        clip = make_clip()
        (clip / "dash" / "init-stream0.m4s").unlink()

        success, message = exporter.process_single_clip(str(clip), str(output_dir))

        assert success is False
        assert "Missing initialization files" in message

    def test_folder_without_session_mpd_is_reported(self, exporter, named_games,
                                                    steam_tree, output_dir):
        clip = steam_tree.add_clip("clip_570_20250102_030405", with_session=False)

        success, message = exporter.process_single_clip(str(clip), str(output_dir))

        assert success is False
        assert "No session.mpd" in message


class TestProcessClipsBatch:
    def test_converts_every_clip(self, exporter, named_games, make_clip, output_dir):
        clips = [
            str(make_clip("clip_570_20250102_030405")),
            str(make_clip("clip_730_20250103_040506")),
        ]

        results = exporter.process_clips_batch(clips, str(output_dir))

        assert results["total"] == 2
        assert sorted(results["successful"]) == sorted(clips), results["failed"]
        assert results["failed"] == []
        assert sorted(os.listdir(str(output_dir))) == [
            "Counter-Strike_2_2025-01-03_04-05-06.mp4",
            "Dota_2_2025-01-02_03-04-05.mp4",
        ]

    def test_creates_the_output_directory(self, exporter, named_games, make_clip, tmp_path):
        target = tmp_path / "nested" / "exports"
        clips = [str(make_clip())]

        results = exporter.process_clips_batch(clips, str(target))

        assert target.is_dir()
        assert results["successful"] == clips

    def test_delete_source_runs_after_all_conversions(self, exporter, named_games,
                                                      make_clip, output_dir):
        clip = make_clip()

        results = exporter.process_clips_batch([str(clip)], str(output_dir), delete_source=True)

        assert results["successful"] == [str(clip)]
        assert not clip.exists()
        assert (output_dir / "Dota_2_2025-01-02_03-04-05.mp4").exists()

    def test_failures_are_collected_not_raised(self, exporter, named_games,
                                               make_clip, steam_tree, output_dir):
        good = str(make_clip("clip_570_20250102_030405"))
        bad = str(steam_tree.add_clip("clip_730_20250103_040506", with_session=False))

        results = exporter.process_clips_batch([good, bad], str(output_dir))

        assert results["successful"] == [good]
        assert [path for path, _ in results["failed"]] == [bad]

    def test_empty_batch(self, exporter, output_dir):
        results = exporter.process_clips_batch([], str(output_dir))
        assert results == {"successful": [], "failed": [], "total": 0}
