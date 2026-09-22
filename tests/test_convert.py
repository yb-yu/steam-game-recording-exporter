"""A few end-to-end checks using real DASH segments and bundled FFmpeg."""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

import steamexporter

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
        clip = steam_tree.add_clip(
            folder_name, with_session=False,
            base=steam_tree.root.parent / "Steam Library With Spaces" / "clips",
        )
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


def test_failed_export_removes_partial_output(
    exporter, named_games, make_clip, output_dir, monkeypatch
):
    clip = make_clip()

    def fail_after_writing(command, *args):
        with open(command[-1], "wb") as partial:
            partial.write(b"partial mp4")
        raise subprocess.CalledProcessError(1, command, stderr="mux failed")

    monkeypatch.setattr(exporter, "_run_ffmpeg", fail_after_writing)
    success, _ = exporter.process_single_clip(str(clip), str(output_dir))

    assert success is False
    assert exporter.check_converted_exists(str(clip), str(output_dir)) is None
    assert clip.exists()
    assert not (output_dir / ".temp").exists()


def test_batch_converts_good_clips_and_collects_failures(
    exporter, named_games, make_clip, steam_tree, tmp_path
):
    good_path = make_clip()
    bad_path = steam_tree.add_clip("clip_730_20250103_040506", with_session=False)
    good, bad = str(good_path), str(bad_path)
    output = tmp_path / "nested" / "exports"

    results = exporter.process_clips_batch(
        [good, bad], str(output), delete_source=True
    )

    assert results["successful"] == [good]
    assert [path for path, _ in results["failed"]] == [bad]
    assert (output / "Dota_2_2025-01-02_03-04-05.mp4").exists()
    assert not good_path.exists()
    assert bad_path.exists()


@pytest.fixture(scope="session")
def dense_dash_source(ffmpeg_exe, dash_source, tmp_path_factory):
    # dash_source checks the required encoders. Each stream here has 660 chunks.
    source = tmp_path_factory.mktemp("dense_dash_source")
    subprocess.run([
        ffmpeg_exe, "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=64x48:rate=20:duration=33",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=33",
        "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-g", "1", "-sc_threshold", "0", "-c:a", "aac",
        "-f", "dash", "-seg_duration", "0.05", "session.mpd",
    ], check=True, capture_output=True, cwd=source)

    # Independent reference: all source bytes, with no manifest duration/period
    # interpretation. This is the complete output of the original concatf path
    # when its file limit is sufficiently high.
    inputs = []
    for stream in (0, 1):
        full_stream = source / f"reference-stream{stream}.mp4"
        with full_stream.open("wb") as output:
            fragments = [source / f"init-stream{stream}.m4s"]
            fragments.extend(sorted(source.glob(f"chunk-stream{stream}-*.m4s")))
            for path in fragments:
                with path.open("rb") as fragment:
                    shutil.copyfileobj(fragment, output)
        inputs += ["-i", str(full_stream)]
    subprocess.run([
        ffmpeg_exe, "-v", "error", "-y", *inputs,
        "-c", "copy", "-shortest", str(source / "reference.mp4"),
    ], check=True, capture_output=True)
    return source


def test_many_real_fragments_export_completely_under_low_file_limit(
    dense_dash_source, steam_tree, output_dir, tmp_path, ffmpeg_exe
):
    clips = []
    for index in (1, 2):
        clip = steam_tree.add_clip(f"clip_570_2025010{index}_030405", with_session=False)
        dash = clip / "dash"
        for path in dense_dash_source.iterdir():
            if path.suffix in (".m4s", ".mpd"):
                shutil.copyfile(path, dash / path.name)
        assert len(list(dash.glob("*.m4s"))) > 1000
        clips.append(clip)

    # A rolling/multi-period manifest must not trim the enumerated fragments.
    (clips[1] / "dash" / "session.mpd").write_text(
        '<MPD mediaPresentationDuration="PT0.1S"><Period/><Period/></MPD>', encoding="utf-8"
    )
    script = """
import json, logging, sys, tempfile
from pathlib import Path
from steamexporter import SteamGameRecordingExporter as Exporter
root = Path(sys.argv[1])
tempfile.tempdir = str(root)
Exporter.CONFIG_DIR = str(root / "config")
Exporter.GAME_IDS_FILE = str(root / "config" / "GameIDs.json")
Exporter.SETTINGS_FILE = str(root / "config" / "settings.json")
Exporter.fetch_game_name_from_steam = lambda self, game_id: "Synthetic"
try:
    import resource
except ImportError:
    limit = None
else:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    limit = min(64, hard) if hard != resource.RLIM_INFINITY else 64
    resource.setrlimit(resource.RLIMIT_NOFILE, (limit, hard))
exporter = Exporter(max_workers=2)
exporter.set_console_log_level(logging.CRITICAL)
result = exporter.process_clips_batch(json.loads(sys.argv[3]), sys.argv[2], show_progress=False)
print(json.dumps({"limit": limit, "result": result}))
"""
    # Run in a subprocess: changing pytest's descriptor limit would also affect
    # its plugins and any parallel tests. Use the same exporter as the parent.
    result = subprocess.run([
        sys.executable, "-c", script, str(tmp_path), str(output_dir),
        json.dumps([str(clip) for clip in clips]),
    ], capture_output=True, text=True, cwd=Path(steamexporter.__file__).parent, timeout=60)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["result"]["failed"] == [], report
    assert report["result"]["skipped"] == [], report
    assert len(report["result"]["successful"]) == 2
    if os.name == "posix":
        assert report["limit"] <= 64

    def frame_hashes(path):
        return subprocess.run([
            ffmpeg_exe, "-v", "error", "-i", str(path), "-map", "0", "-c", "copy",
            "-f", "framehash", "-hash", "sha256", "-",
        ], check=True, capture_output=True, text=True).stdout

    expected = frame_hashes(dense_dash_source / "reference.mp4")
    outputs = sorted(output_dir.glob("*.mp4"))
    assert len(outputs) == 2
    for output in outputs:
        # Includes every encoded packet's timestamp, duration, size and hash.
        assert frame_hashes(output) == expected
        decoded = subprocess.run([
            ffmpeg_exe, "-v", "error", "-i", str(output), "-f", "null", "-",
        ], capture_output=True, text=True, timeout=30)
        assert decoded.returncode == 0 and decoded.stderr == "", decoded.stderr
    assert all(clip.exists() for clip in clips)
    assert not (output_dir / ".temp").exists()


def test_multiple_sessions_preserve_all_frames(exporter, named_games, make_clip, output_dir, ffmpeg_exe):
    clip = make_clip()
    shutil.copytree(clip / "dash", clip / "second-session")
    success, message = exporter.process_single_clip(str(clip), str(output_dir))
    assert success, message
    output = next(output_dir.glob("*.mp4"))
    decoded = subprocess.run([
        ffmpeg_exe, "-v", "error", "-i", str(output), "-map", "0:v:0", "-f", "framemd5", "-",
    ], check=True, capture_output=True, text=True)
    assert len([line for line in decoded.stdout.splitlines() if not line.startswith("#")]) == 20
    assert not (output_dir / ".temp").exists()
