"""The real ``setup_logging`` (stubbed out everywhere else)."""

from contextlib import contextmanager
import logging

from steamexporter import SteamGameRecordingExporter


@contextmanager
def isolated_root_logger():
    """Let ``basicConfig`` install its handlers, then take them back out.

    Only file handlers are closed: the stream handler writes to the captured
    ``sys.stdout``, which must stay open for the rest of the session.
    """
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


def flush(root):
    for handler in root.handlers:
        handler.flush()


def test_creates_a_timestamped_log_file(config_dir):
    with isolated_root_logger() as root:
        exporter = SteamGameRecordingExporter(max_workers=1)

        log_files = list((config_dir / "logs").glob("*.log"))
        assert len(log_files) == 1

        exporter.logger.info("hello from the test")
        flush(root)

        assert "hello from the test" in log_files[0].read_text(encoding="utf-8")


def test_handles_non_ascii_log_records(config_dir):
    """Log files are UTF-8; Korean game names must not blow up the handler."""
    with isolated_root_logger() as root:
        exporter = SteamGameRecordingExporter(max_workers=1)

        exporter.logger.info("변환 완료: 검은사막 🎮")
        flush(root)

        log_file = next(iter((config_dir / "logs").glob("*.log")))
        assert "검은사막" in log_file.read_text(encoding="utf-8")
