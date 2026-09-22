"""Bounded source reads, ordering and cleanup without requiring video encoding."""

import builtins
from contextlib import contextmanager
import errno
from pathlib import Path
from unittest.mock import Mock

import pytest

import steamexporter


def test_many_fragments_are_copied_in_order_with_one_source_open(
    exporter, named_games, steam_tree, output_dir, monkeypatch, caplog
):
    clip = steam_tree.add_clip("clip_570_20250102_030405")
    dash = clip / "dash"
    expected = []
    for stream in (0, 1):
        init = f"init-{stream}".encode()
        (dash / f"init-stream{stream}.m4s").write_bytes(init)
        chunks = []
        # Create in reverse order so enumeration order cannot substitute for sorting.
        for index in range(5000, 0, -1):
            data = f"{stream}:{index:05d}".encode()
            if index == 2500:
                data += b"x" * (4 * 1024 * 1024 + 17)
            (dash / f"chunk-stream{stream}-{index:05d}.m4s").write_bytes(data)
            chunks.append(data)
        expected.append(init + b"".join(reversed(chunks)))
    (dash / "chunk-stream0-99999.m4s.tmp").write_bytes(b"unfinished")
    # An old pending write must not cause this fixture to be skipped as active.
    steamexporter.os.utime(dash / "chunk-stream0-99999.m4s.tmp", (0, 0))
    (dash / "unrelated.m4s").write_bytes(b"not a recording fragment")

    open_count = peak_open = source_reads = 0

    @contextmanager
    def tracked_open(path, mode="r", *args, **kwargs):
        nonlocal open_count, peak_open, source_reads
        with builtins.open(path, mode, *args, **kwargs) as file:
            if mode != "rb" or Path(path).parent != dash:
                yield file
                return
            open_count += 1
            source_reads += 1
            peak_open = max(peak_open, open_count)

            class Reader:
                def read(self, size=-1):
                    assert 0 < size <= 4 * 1024 * 1024
                    return file.read(size)

            try:
                yield Reader()
            finally:
                open_count -= 1

    def mux(command, *args):
        inputs = [command[index + 1] for index, arg in enumerate(command) if arg == "-i"]
        assert [Path(path).read_bytes() for path in inputs] == expected
        assert open_count == 0
        Path(command[-1]).write_bytes(b"muxed")

    monkeypatch.setattr(steamexporter, "open", tracked_open, raising=False)
    monkeypatch.setattr(exporter, "_run_ffmpeg", Mock(side_effect=mux))
    progress = []
    with caplog.at_level("DEBUG"):
        success, message = exporter.process_single_clip(
            str(clip), str(output_dir), progress_cb=lambda fraction, stage: progress.append(fraction)
        )

    assert success, message
    exporter._run_ffmpeg.assert_called_once()
    assert peak_open == 1 and source_reads == 10002 and open_count == 0
    assert progress == sorted(progress) and progress[-1] == 1.0
    assert "video_chunks=5000 audio_chunks=5000 input_files=10002 mode=sequential" in caplog.text
    assert clip.exists() and not (output_dir / ".temp").exists()


@pytest.mark.parametrize("error,expected", [(FileNotFoundError, None), (PermissionError, False)])
def test_source_read_failure_keeps_source_and_removes_partial_streams(
    exporter, named_games, steam_tree, output_dir, monkeypatch, error, expected
):
    clip = steam_tree.add_clip("clip_570_20250102_030405")
    failing = clip / "dash" / "chunk-stream1-00001.m4s"
    failing.write_bytes(b"chunk")

    def failing_open(path, mode="r", *args, **kwargs):
        if Path(path) == failing and mode == "rb":
            raise error("source unavailable")
        return builtins.open(path, mode, *args, **kwargs)

    monkeypatch.setattr(steamexporter, "open", failing_open, raising=False)
    monkeypatch.setattr(exporter, "_run_ffmpeg", Mock())
    success, message = exporter.process_single_clip(str(clip), str(output_dir), delete_source=True)

    assert success is expected, message
    exporter._run_ffmpeg.assert_not_called()
    assert clip.exists()
    assert list(output_dir.iterdir()) == []


def test_full_output_disk_removes_incomplete_stream_and_keeps_source(
    exporter, named_games, steam_tree, output_dir, monkeypatch
):
    clip = steam_tree.add_clip("clip_570_20250102_030405")
    real_temporary_file = steamexporter.tempfile.NamedTemporaryFile

    def full_disk(*args, **kwargs):
        file = real_temporary_file(*args, **kwargs)
        write = file.write

        def fail_write(data):
            write(data[:1])
            raise OSError(errno.ENOSPC, "No space left on device")

        file.write = fail_write
        return file

    monkeypatch.setattr(steamexporter.tempfile, "NamedTemporaryFile", full_disk)
    monkeypatch.setattr(exporter, "_run_ffmpeg", Mock())
    success, message = exporter.process_single_clip(str(clip), str(output_dir), delete_source=True)

    assert success is False and "No space left on device" in message
    exporter._run_ffmpeg.assert_not_called()
    assert clip.exists()
    assert list(output_dir.iterdir()) == []
