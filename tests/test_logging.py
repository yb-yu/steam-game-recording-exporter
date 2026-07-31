"""The real logging setup, which other tests stub out."""

from contextlib import contextmanager
import logging

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
        assert "검은사막" in log_files[0].read_text(encoding="utf-8")
