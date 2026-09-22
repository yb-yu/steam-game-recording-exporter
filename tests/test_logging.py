"""The real logging setup, which other tests stub out."""

from contextlib import contextmanager
import logging
import subprocess
import sys

import pytest

from steamexporter import SteamGameRecordingExporter


@contextmanager
def isolated_root_logger():
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    root.handlers = []
    try:
        yield root
    finally:
        for handler in root.handlers:
            if isinstance(handler, logging.FileHandler):
                handler.close()
        root.handlers = saved_handlers


def test_writes_a_utf8_log_file(config_dir):
    with isolated_root_logger() as root:
        exporter = SteamGameRecordingExporter(max_workers=1)
        exporter.logger.info("변환 완료: 검은사막")
        for handler in root.handlers:
            handler.flush()

        log_files = list((config_dir / "logs").glob("*.log"))
        assert len(log_files) == 1
        text = log_files[0].read_text(encoding="utf-8")
        assert "검은사막" in text
        assert "Runtime: exporter=" in text and "python=" in text and "os=" in text
        assert "Open-file limit" in text
        assert "FFmpeg executable=" in text and "version=" in text


def test_failed_subprocess_logs_actual_command_and_exit_status(config_dir, tmp_path):
    with isolated_root_logger() as root:
        exporter = SteamGameRecordingExporter(max_workers=1)
        output = tmp_path / "unfinished.mp4"
        command = [sys.executable, "-c", "import sys; sys.stderr.write('read failed'); sys.exit(7)", str(output)]
        with pytest.raises(subprocess.CalledProcessError) as error:
            exporter._run_ffmpeg(command, 0)
        assert error.value.returncode == 7
        assert error.value.stderr == "read failed"
        for handler in root.handlers:
            handler.flush()

        text = next((config_dir / "logs").glob("*.log")).read_text(encoding="utf-8")
        assert "FFmpeg command:" in text and "'-progress', 'pipe:1'" in text
        assert "FFmpeg exit=7" in text and str(output) in text
        assert "stderr=read failed" in text
