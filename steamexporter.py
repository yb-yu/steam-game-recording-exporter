#!/usr/bin/env python3
"""
Steam Game Recording Exporter

Export Steam game recordings (.m4s + .mpd) to standard MP4 video files.
Supports Windows, macOS, and Linux with automatic Steam path detection.
"""

import os
import re
import sys
import time
import json
import glob
import queue
import contextlib
import shutil
import tempfile
import logging
import threading
import traceback
import platform
import subprocess
import concurrent.futures
from datetime import datetime, timezone
from typing import Any, Callable, Optional
import xml.etree.ElementTree as ET

try:
    import imageio_ffmpeg as iio
    import requests
    import typer
    import questionary
    from prompt_toolkit.keys import Keys
    from rich.console import Console, Group
    from rich.live import Live
    from rich.logging import RichHandler
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )
except ImportError as e:
    print(f"Missing required dependency: {e}")
    print("Please install required packages:")
    print("pip install imageio-ffmpeg requests typer rich questionary")
    sys.exit(1)


def force_utf8_console():
    """Make stdout/stderr tolerate non-ASCII output.

    Console messages contain emoji and game names in any language, while a
    legacy code page (cp1252, cp949) or a piped stdout would otherwise raise
    UnicodeEncodeError and abort the run.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding='utf-8', errors='replace')
        except (ValueError, OSError):
            pass


__version__ = "1.1.0"


force_utf8_console()

console = Console()

# ISO 8601 duration as used by DASH manifests, e.g. "PT1M30.500S"
_ISO_DURATION_RE = re.compile(
    r'^P(?:(?P<days>[\d.]+)D)?'
    r'(?:T(?:(?P<hours>[\d.]+)H)?(?:(?P<minutes>[\d.]+)M)?(?:(?P<seconds>[\d.]+)S)?)?$'
)


def parse_iso_duration(value: Optional[str]) -> float:
    """Convert an ISO 8601 duration string to seconds (0.0 when unparseable)."""
    if not value:
        return 0.0
    match = _ISO_DURATION_RE.match(value.strip())
    if not match:
        return 0.0
    parts = {k: float(v) for k, v in match.groupdict().items() if v}
    return (parts.get('days', 0.0) * 86400 + parts.get('hours', 0.0) * 3600
            + parts.get('minutes', 0.0) * 60 + parts.get('seconds', 0.0))


class SteamGameRecordingExporter:
    """
    Handles Steam path detection, recording discovery, and MP4 conversion.
    """

    CONFIG_DIR = os.path.join(
        os.environ.get('LOCALAPPDATA', os.path.expanduser("~")), 'SteamGameRecordingExporter'
    )
    GAME_IDS_FILE = os.path.join(CONFIG_DIR, 'GameIDs.json')
    SETTINGS_FILE = os.path.join(CONFIG_DIR, 'settings.json')
    STEAM_APP_DETAILS_URL = "https://store.steampowered.com/api/appdetails"
    CURRENT_VERSION = f"v{__version__}"
    ACTIVE_WRITE_WINDOW_SECONDS = 60

    # Relative cost of each conversion phase, used to build a single 0..1 progress
    # value per clip. Values must sum to 1.0.
    PHASE_WEIGHTS = (
        ('merge', 0.35),
        ('video', 0.25),
        ('audio', 0.10),
        ('mux', 0.30),
    )

    # A clip with a single session skips both concat passes and muxes the merged
    # streams directly, so its progress only has two phases.
    SINGLE_SESSION_WEIGHTS = (
        ('merge', 0.45),
        ('mux', 0.55),
    )

    def __init__(self, max_workers: int = None, verbose: bool = False):
        self.max_workers = max_workers or min(6, max(2, (os.cpu_count() or 1) // 2))
        self.game_ids = {}
        self.settings = {}
        self.setup_logging()
        if verbose:
            self.set_console_log_level(logging.DEBUG)
        self.load_game_ids()
        self.load_settings()
        saved_workers = self.settings.get('workers')
        if max_workers is None and isinstance(saved_workers, int) and saved_workers > 0:
            self.max_workers = saved_workers
        self._custom_record_cache = {}

    def setup_logging(self, verbose: bool = False):
        """Log everything to file; keep the console quiet so progress bars stay readable."""
        log_dir = os.path.join(self.CONFIG_DIR, 'logs')
        os.makedirs(log_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_file = os.path.join(log_dir, f"{timestamp}.log")

        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))

        # RichHandler writes through the shared console, so log lines scroll above
        # the live progress display instead of tearing through it.
        console_handler = RichHandler(console=console, show_path=False, rich_tracebacks=True,
                                      markup=False)
        console_handler.setLevel(logging.DEBUG if verbose else logging.WARNING)

        logging.basicConfig(level=logging.DEBUG, format='%(message)s', datefmt='[%X]',
                            handlers=[file_handler, console_handler], force=True)
        self.logger = logging.getLogger(__name__)
        self.log_file = log_file
        self._console_handler = console_handler

    def set_console_log_level(self, level: int):
        """Adjust how much reaches the console; the log file always keeps everything."""
        self._console_handler.setLevel(level)

    def auto_detect_steam_paths(self) -> list[str]:
        """
        Auto-detect Steam installation paths across different operating systems.

        Returns:
            List[str]: List of valid Steam userdata directory paths
        """
        possible_paths = []
        current_os = platform.system()

        if current_os == "Windows":
            # Standard Steam installation paths for Windows
            steam_paths = [
                "C:/Program Files (x86)/Steam",
                "C:/Program Files/Steam",
                "D:/Steam",
                "E:/Steam",
            ]

            # Check registry for Steam installation (Windows only)
            try:
                import winreg
                key_paths = [
                    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
                    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
                    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Valve\Steam"),
                ]

                for hkey, subkey in key_paths:
                    try:
                        with winreg.OpenKey(hkey, subkey) as key:
                            steam_path = winreg.QueryValueEx(key, "InstallPath")[0]
                            if steam_path and os.path.isdir(steam_path):
                                steam_paths.insert(0, steam_path)  # Prioritize registry paths
                    except (FileNotFoundError, OSError):
                        continue
            except ImportError:
                self.logger.warning("winreg not available, using default paths")

        elif current_os == "Darwin":  # macOS
            # Standard Steam installation paths for macOS
            home_dir = os.path.expanduser("~")
            steam_paths = [
                os.path.join(home_dir, "Library/Application Support/Steam"),
                "/Applications/Steam.app/Contents/MacOS",
                os.path.join(home_dir, "Applications/Steam.app/Contents/MacOS"),
            ]

        else:
            # Linux and other Unix-like systems
            home_dir = os.path.expanduser("~")
            steam_paths = [
                os.path.join(home_dir, ".steam/steam"),
                os.path.join(home_dir, ".local/share/Steam"),
                "/usr/share/steam",
                "/opt/steam",
            ]

        # Check each potential Steam path for userdata directory
        for steam_path in steam_paths:
            userdata_path = os.path.join(steam_path, "userdata")
            if os.path.isdir(userdata_path):
                # Verify it contains valid Steam ID directories
                if any(d.isdigit() for d in os.listdir(userdata_path)
                      if os.path.isdir(os.path.join(userdata_path, d))):
                    possible_paths.append(userdata_path)

        return possible_paths

    def find_steam_userdata_path(self) -> Optional[str]:
        """Find Steam userdata path automatically"""
        # Auto-detect paths
        detected_paths = self.auto_detect_steam_paths()
        if detected_paths:
            # Use the first valid path found
            userdata_path = detected_paths[0]
            self.logger.info(f"Auto-detected Steam userdata path: {userdata_path}")
            return userdata_path

        self.logger.error("Could not auto-detect Steam userdata path")
        return None

    def load_game_ids(self):
        """Load game ID to name mappings"""
        if os.path.exists(self.GAME_IDS_FILE):
            try:
                with open(self.GAME_IDS_FILE, 'r') as f:
                    self.game_ids = json.load(f)
            except Exception as e:
                self.logger.error(f"Error loading game IDs: {e}")
                self.game_ids = {}
        else:
            self.game_ids = {}

    def save_game_ids(self):
        """Save game ID mappings to file"""
        os.makedirs(self.CONFIG_DIR, exist_ok=True)
        with open(self.GAME_IDS_FILE, 'w') as f:
            json.dump(self.game_ids, f, indent=4)

    def load_settings(self):
        """Load saved interactive preferences, ignoring missing or invalid files."""
        try:
            with open(self.SETTINGS_FILE, 'r', encoding='utf-8') as f:
                settings = json.load(f)
            if isinstance(settings, dict):
                self.settings = settings
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError) as e:
            self.logger.warning(f"Could not load settings: {e}")

    def save_preferences(self, output_dir: str, workers: Optional[int] = None):
        """Remember safe interactive defaults; destructive choices are never saved."""
        self.settings['output_dir'] = os.path.abspath(os.path.expanduser(output_dir))
        if workers is not None:
            self.settings['workers'] = workers
        try:
            os.makedirs(self.CONFIG_DIR, exist_ok=True)
            with open(self.SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.settings, f, indent=4)
        except OSError as e:
            self.logger.warning(f"Could not save settings: {e}")

    def fetch_game_name_from_steam(self, game_id: str) -> Optional[str]:
        """Fetch game name from Steam API"""
        if not game_id.isdigit():
            return game_id

        url = f"{self.STEAM_APP_DETAILS_URL}?appids={game_id}&filters=basic"
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
            if str(game_id) in data and data[str(game_id)]['success']:
                return data[str(game_id)]['data']['name']
        except Exception as e:
            self.logger.warning(f"Failed to fetch game name for {game_id}: {e}")

        return None

    def get_game_name(self, game_id: str) -> str:
        """Get game name, fetching from Steam API if not cached"""
        if game_id in self.game_ids:
            return self.game_ids[game_id]

        name = self.fetch_game_name_from_steam(game_id)
        if name:
            self.game_ids[game_id] = name
            self.save_game_ids()
            return name

        # Fallback to game ID
        default_name = f"Game_{game_id}"
        self.game_ids[game_id] = default_name
        self.save_game_ids()
        return default_name

    def get_custom_record_path(self, userdata_dir: str) -> Optional[str]:
        """Get custom recording path from Steam config"""
        if userdata_dir in self._custom_record_cache:
            return self._custom_record_cache[userdata_dir]

        localconfig_path = os.path.join(userdata_dir, 'config', 'localconfig.vdf')
        if not os.path.exists(localconfig_path):
            self._custom_record_cache[userdata_dir] = None
            return None

        try:
            with open(localconfig_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()

            # Look for BackgroundRecordPath setting
            if '"BackgroundRecordPath"' in content:
                lines = content.split('\n')
                for line in lines:
                    if '"BackgroundRecordPath"' in line:
                        parts = line.split('"BackgroundRecordPath"')
                        if len(parts) > 1:
                            path_part = parts[1].strip().strip('"').strip()
                            if path_part and os.path.isdir(path_part):
                                self._custom_record_cache[userdata_dir] = path_part
                                return path_part

            self._custom_record_cache[userdata_dir] = None
            return None
        except Exception as e:
            self.logger.warning(f"Error reading custom record path from {localconfig_path}: {e}")
            self._custom_record_cache[userdata_dir] = None
            return None

    @staticmethod
    def _iter_subdirs(directory: str):
        """Yield immediate subdirectory paths, ignoring unreadable directories."""
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        yield entry.path
        except (OSError, PermissionError):
            return

    def find_session_mpd(self, clip_folder: str) -> list[str]:
        """
        Find all session.mpd files in a clip folder.

        Walks directories only and probes for the manifest by name, so the
        thousands of .m4s chunks in each data directory are never enumerated.
        """
        session_mpd_files = []
        pending = [clip_folder]
        while pending:
            current = pending.pop()
            candidate = os.path.join(current, 'session.mpd')
            if os.path.isfile(candidate):
                session_mpd_files.append(candidate)
            pending.extend(self._iter_subdirs(current))
        # Sorted so multi-session clips always concatenate in a stable order.
        return sorted(session_mpd_files)

    def has_session_mpd(self, clip_folder: str) -> bool:
        """Whether a folder holds at least one manifest, returning on the first hit."""
        pending = [clip_folder]
        while pending:
            current = pending.pop()
            if os.path.isfile(os.path.join(current, 'session.mpd')):
                return True
            pending.extend(self._iter_subdirs(current))
        return False

    def is_recording_active(self, clip_folder: str) -> bool:
        """Whether Steam has recently written a temporary chunk in this recording store."""
        cutoff = time.time() - self.ACTIVE_WRITE_WINDOW_SECONDS
        parent = os.path.dirname(os.path.normpath(clip_folder))
        scan_root = parent if os.path.basename(parent).lower() == 'video' else clip_folder
        pending = [scan_root]
        while pending:
            current = pending.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                        elif entry.name.endswith('.m4s.tmp'):
                            try:
                                if entry.stat().st_mtime >= cutoff:
                                    return True
                            except OSError:
                                return True
            except OSError:
                return True  # A changing or locked recording is not safe to read or delete.
        return False

    def partition_active_background_recordings(
        self, clip_folders: list[str]
    ) -> tuple[list[str], list[tuple[str, str]]]:
        """Skip every background clip in a video store while Steam writes to it."""
        background_groups: dict[str, list[str]] = {}
        for clip_folder in clip_folders:
            parent = os.path.dirname(os.path.normpath(clip_folder))
            if os.path.basename(parent).lower() == 'video':
                background_groups.setdefault(os.path.normcase(os.path.abspath(parent)), []).append(clip_folder)

        active_roots = {
            root for root in background_groups
            if self.is_recording_active(root)
        }
        reason = "Steam is actively writing this background recording store; stop the game, wait a minute, and rerun"
        skipped = [
            (clip_folder, reason)
            for root in active_roots
            for clip_folder in background_groups[root]
        ]
        skipped_paths = {clip_folder for clip_folder, _ in skipped}
        return [clip_folder for clip_folder in clip_folders if clip_folder not in skipped_paths], skipped

    def get_clip_duration(self, session_mpd_files: list[str]) -> float:
        """
        Total playback duration in seconds across the given manifests.

        Returns 0.0 when no manifest declares a duration; callers must treat that
        as "unknown" rather than "empty clip".
        """
        total = 0.0
        for session_mpd in session_mpd_files:
            try:
                root = ET.parse(session_mpd).getroot()
                total += parse_iso_duration(root.get('mediaPresentationDuration'))
            except Exception as e:
                self.logger.debug(f"Could not read duration from {session_mpd}: {e}")
        return total

    @staticmethod
    def _subprocess_flags() -> int:
        """Suppress the console window ffmpeg would otherwise flash on Windows."""
        return subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

    def _run_ffmpeg(self, cmd: list[str], total_seconds: float,
                    on_fraction: Optional[Callable[[float], None]] = None) -> None:
        """
        Run an ffmpeg command, reporting completion as a 0..1 fraction.

        Args:
            cmd: Full ffmpeg command; the last element must be the output path.
            total_seconds: Expected output duration, used to scale progress.
                           Pass 0 when unknown - progress then stays at 0 until the
                           command finishes.
            on_fraction: Called with monotonically increasing 0..1 values.

        Raises:
            subprocess.CalledProcessError: If ffmpeg exits non-zero.
        """
        # `-progress pipe:1` must precede the output path.
        full_cmd = cmd[:-1] + ['-progress', 'pipe:1', '-nostats', '-loglevel', 'error', cmd[-1]]

        proc = subprocess.Popen(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            creationflags=self._subprocess_flags(),
        )

        # Drain stderr on a side thread so a chatty ffmpeg can never deadlock on a full pipe.
        stderr_parts: list[str] = []

        def _drain_stderr():
            try:
                stderr_parts.append(proc.stderr.read() or '')
            except Exception:
                pass

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        last_fraction = 0.0
        try:
            for line in proc.stdout:
                key, _, value = line.strip().partition('=')
                # ffmpeg reports out_time_ms in microseconds (long-standing quirk),
                # so both keys are handled the same way.
                if key in ('out_time_us', 'out_time_ms') and value not in ('', 'N/A'):
                    if total_seconds > 0 and on_fraction:
                        try:
                            elapsed = int(value) / 1_000_000
                        except ValueError:
                            continue
                        fraction = min(0.99, max(last_fraction, elapsed / total_seconds))
                        if fraction > last_fraction:
                            last_fraction = fraction
                            on_fraction(fraction)
        finally:
            proc.stdout.close()
            proc.wait()
            stderr_thread.join(timeout=5)

        if proc.returncode != 0:
            raise subprocess.CalledProcessError(
                proc.returncode, full_cmd, stderr=''.join(stderr_parts)
            )

        if on_fraction:
            on_fraction(1.0)

    def get_clip_folders(self, userdata_path: str, steam_id: str = None,
                        media_type: str = "all", game_id: str = None) -> list[str]:
        """
        Get list of clip folders based on specified filters.

        Args:
            userdata_path: Path to Steam userdata directory
            steam_id: Specific Steam user ID to filter by (optional)
            media_type: Type of clips to include ('all', 'manual', 'background')
            game_id: Specific game ID to filter by (optional)

        Returns:
            List[str]: Sorted list of clip folder paths (newest first)
        """
        clip_folders = []

        # Get all Steam IDs if none specified
        steam_ids = [steam_id] if steam_id else [
            d for d in os.listdir(userdata_path)
            if os.path.isdir(os.path.join(userdata_path, d)) and d.isdigit()
        ]

        for sid in steam_ids:
            userdata_dir = os.path.join(userdata_path, sid)

            # Get potential clip directories
            clip_dirs = []

            # Default paths
            default_clips = os.path.join(userdata_dir, 'gamerecordings', 'clips')
            default_video = os.path.join(userdata_dir, 'gamerecordings', 'video')

            if os.path.isdir(default_clips) and media_type in ["all", "manual"]:
                clip_dirs.append(default_clips)
            if os.path.isdir(default_video) and media_type in ["all", "background"]:
                clip_dirs.append(default_video)

            # Custom paths
            custom_path = self.get_custom_record_path(userdata_dir)
            if custom_path:
                custom_clips = os.path.join(custom_path, 'clips')
                custom_video = os.path.join(custom_path, 'video')

                if os.path.isdir(custom_clips) and media_type in ["all", "manual"]:
                    clip_dirs.append(custom_clips)
                if os.path.isdir(custom_video) and media_type in ["all", "background"]:
                    clip_dirs.append(custom_video)

            # Scan clip directories
            for clip_dir in clip_dirs:
                try:
                    for folder_entry in os.scandir(clip_dir):
                        if folder_entry.is_dir() and "_" in folder_entry.name:
                            folder_path = folder_entry.path

                            # Verify it has session.mpd files
                            if self.has_session_mpd(folder_path):
                                # Filter by game ID if specified
                                if not game_id or f"_{game_id}_" in folder_entry.name:
                                    clip_folders.append(folder_path)
                except (OSError, PermissionError) as e:
                    self.logger.warning(f"Error scanning {clip_dir}: {e}")

        # Sort by datetime (newest first)
        clip_folders.sort(key=self.extract_datetime_from_folder_name, reverse=True)
        return clip_folders

    def parse_recorded_datetime(self, folder_path: str) -> Optional[datetime]:
        """
        Parse the recording datetime encoded in a clip folder name.

        Steam names clip folders like 'clip_<gameid>_<YYYYMMDD>_<HHMMSS>', where the
        trailing date/time is local time at the moment the clip was recorded.

        Returns:
            Optional[datetime]: Naive local datetime, or None if it cannot be parsed
        """
        folder_name = os.path.basename(folder_path)
        parts = folder_name.split('_')
        if len(parts) >= 3:
            try:
                datetime_str = parts[-2] + parts[-1]
                return datetime.strptime(datetime_str, "%Y%m%d%H%M%S")
            except ValueError:
                pass
        return None

    def extract_datetime_from_folder_name(self, folder_path: str) -> datetime:
        """Extract datetime from folder name"""
        return self.parse_recorded_datetime(folder_path) or datetime.min

    def set_windows_creation_time(self, file_path: str, dt: datetime) -> bool:
        """Set the Windows creation time (the 'Date created' column) of a file"""
        import ctypes
        from ctypes import wintypes

        # Windows FILETIME: 100-nanosecond intervals since 1601-01-01 UTC
        filetime = int(dt.timestamp() * 10_000_000) + 116_444_736_000_000_000
        creation_time = wintypes.FILETIME(filetime & 0xFFFFFFFF, filetime >> 32)

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE
        ]

        FILE_WRITE_ATTRIBUTES = 0x0100
        OPEN_EXISTING = 3
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        handle = kernel32.CreateFileW(
            file_path, FILE_WRITE_ATTRIBUTES, 0, None, OPEN_EXISTING, 0, None
        )
        if handle == INVALID_HANDLE_VALUE:
            self.logger.warning(f"Could not open {file_path} to set creation time")
            return False

        try:
            return bool(kernel32.SetFileTime(handle, ctypes.byref(creation_time), None, None))
        finally:
            kernel32.CloseHandle(handle)

    def set_macos_creation_time(self, file_path: str, dt: datetime) -> bool:
        """Set the macOS creation time (Finder's 'Date Created' column) of a file"""
        import ctypes
        import ctypes.util

        class Timespec(ctypes.Structure):
            _fields_ = [('tv_sec', ctypes.c_int64), ('tv_nsec', ctypes.c_int64)]

        class Attrlist(ctypes.Structure):
            _fields_ = [
                ('bitmapcount', ctypes.c_ushort),
                ('reserved', ctypes.c_uint16),
                ('commonattr', ctypes.c_uint32),
                ('volattr', ctypes.c_uint32),
                ('dirattr', ctypes.c_uint32),
                ('fileattr', ctypes.c_uint32),
                ('forkattr', ctypes.c_uint32),
            ]

        ATTR_BIT_MAP_COUNT = 5
        ATTR_CMN_CRTIME = 0x00000200

        libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
        libc.setattrlist.restype = ctypes.c_int
        libc.setattrlist.argtypes = [
            ctypes.c_char_p, ctypes.POINTER(Attrlist),
            ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ulong
        ]

        attrs = Attrlist(bitmapcount=ATTR_BIT_MAP_COUNT, commonattr=ATTR_CMN_CRTIME)
        crtime = Timespec(tv_sec=int(dt.timestamp()), tv_nsec=0)

        result = libc.setattrlist(
            os.fsencode(file_path), ctypes.byref(attrs),
            ctypes.byref(crtime), ctypes.sizeof(crtime), 0
        )
        if result != 0:
            errno = ctypes.get_errno()
            self.logger.warning(
                f"Could not set creation time on {file_path}: {os.strerror(errno)}"
            )
            return False
        return True

    def apply_recorded_timestamp(self, file_path: str, recorded_at: datetime):
        """
        Stamp an exported file with the time the clip was recorded.

        Sets the modification/access times on every platform, plus the creation
        time on Windows and macOS, so the exported MP4 sorts by recording time
        instead of export time. Linux has no API for setting a file's birth
        time, so modification time is all that can be set there.
        """
        try:
            epoch = recorded_at.timestamp()
            os.utime(file_path, (epoch, epoch))

            current_os = platform.system()
            if current_os == "Windows":
                self.set_windows_creation_time(file_path, recorded_at)
            elif current_os == "Darwin":
                self.set_macos_creation_time(file_path, recorded_at)

            self.logger.info(
                f"Timestamped {os.path.basename(file_path)} as {recorded_at.strftime('%Y-%m-%d %H:%M:%S')}"
            )
        except Exception as e:
            self.logger.warning(f"Could not set recorded timestamp on {file_path}: {e}")

    def sanitize_filename(self, filename: str) -> str:
        """Sanitize filename by replacing invalid characters"""
        # Windows invalid characters: < > : " | ? * \ /
        # Also replace spaces and other problematic characters
        invalid_chars = '<>:"|?*\\/ '
        sanitized = filename
        for char in invalid_chars:
            sanitized = sanitized.replace(char, '_')

        # Remove multiple consecutive underscores
        while '__' in sanitized:
            sanitized = sanitized.replace('__', '_')

        # Remove leading/trailing underscores
        sanitized = sanitized.strip('_')

        return sanitized

    def get_unique_filename(self, directory: str, filename: str) -> str:
        """Generate unique filename to avoid conflicts"""
        # Sanitize filename first
        filename = self.sanitize_filename(filename)

        base_name, ext = os.path.splitext(filename)
        counter = 1
        unique_filename = os.path.join(directory, filename)

        while os.path.exists(unique_filename):
            unique_filename = os.path.join(directory, f"{base_name}_{counter}{ext}")
            counter += 1

        return unique_filename

    def get_expected_output_filename(self, clip_folder: str, output_dir: str) -> str:
        """
        Get the expected output filename for a clip folder.

        Args:
            clip_folder: Path to the Steam clip folder
            output_dir: Output directory where MP4 would be saved

        Returns:
            str: Expected output file path
        """
        folder_basename = os.path.basename(clip_folder)
        parts = folder_basename.split('_')

        recorded_at = self.parse_recorded_datetime(clip_folder)
        formatted_date = recorded_at.strftime("%Y-%m-%d_%H-%M-%S") if recorded_at else "UnknownDate"

        game_id = parts[1] if len(parts) >= 2 else "Unknown"
        game_name = self.get_game_name(game_id)

        # Sanitize game name to match what would be saved
        base_filename = f"{self.sanitize_filename(game_name)}_{formatted_date}"
        # Get the exact filename (without checking for uniqueness)
        return os.path.join(output_dir, f"{base_filename}.mp4")

    def check_converted_exists(self, clip_folder: str, output_dir: str) -> Optional[str]:
        """
        Check if a converted MP4 file already exists for a clip folder.

        Args:
            clip_folder: Path to the Steam clip folder
            output_dir: Output directory where MP4s are saved

        Returns:
            Optional[str]: Path to existing MP4 file if found, None otherwise
        """
        expected_file = self.get_expected_output_filename(clip_folder, output_dir)

        # Check exact match
        if os.path.exists(expected_file):
            return expected_file

        # Check for numbered variations (e.g., filename_1.mp4, filename_2.mp4).
        # One glob beats stat-ing every candidate name in turn.
        base_name, ext = os.path.splitext(expected_file)
        numbered = glob.glob(f"{glob.escape(base_name)}_*{ext}")
        suffix_re = re.compile(re.escape(base_name) + r'_(\d+)' + re.escape(ext) + r'$')

        best = None
        best_index = None
        for path in numbered:
            match = suffix_re.match(path)
            if match:
                index = int(match.group(1))
                if best_index is None or index < best_index:
                    best, best_index = path, index

        return best

    def delete_source_folder(self, clip_folder: str) -> bool:
        """Safely delete source clip folder after successful conversion"""
        try:
            if os.path.exists(clip_folder):
                if self.is_recording_active(clip_folder):
                    self.logger.warning(f"Skipped deletion - recording is still active: {clip_folder}")
                    return False
                # Double-check this is a Steam clip folder (safety check)
                if any(f.endswith('.m4s') or f == 'session.mpd' for _, _, files in os.walk(clip_folder) for f in files):
                    shutil.rmtree(clip_folder)
                    self.logger.info(f"Deleted source folder: {clip_folder}")
                    return True
                else:
                    self.logger.warning(f"Skipped deletion - not a Steam clip folder: {clip_folder}")
                    return False
            return True  # Already deleted
        except PermissionError as e:
            self.logger.warning(f"Skipped deletion - source is in use: {clip_folder}: {e}")
            return False
        except Exception as e:
            self.logger.error(f"Failed to delete source folder {clip_folder}: {e}")
            return False

    def process_single_clip(self, clip_folder: str, output_dir: str, delete_source: bool = False,
                            progress_cb: Optional[Callable[[float, str], None]] = None
                            ) -> tuple[Optional[bool], str]:
        """
        Process a single Steam clip folder and convert to MP4.

        Args:
            clip_folder: Path to the Steam clip folder containing session.mpd
            output_dir: Directory where the converted MP4 will be saved
            delete_source: Whether to delete source folder if MP4 already exists
            progress_cb: Called as (fraction, stage) with fraction in 0..1 as work proceeds

        Returns:
            Tuple[Optional[bool], str]: True for success, False for failure, None when skipped
        """
        # Phase mix depends on whether the concat passes are needed; see below.
        weights = dict(self.PHASE_WEIGHTS)
        phase_order = [name for name, _ in self.PHASE_WEIGHTS]

        def use_single_session_weights():
            """Redistribute the concat phases' share once we know we can skip them."""
            nonlocal weights, phase_order
            weights = dict(self.SINGLE_SESSION_WEIGHTS)
            phase_order = [name for name, _ in self.SINGLE_SESSION_WEIGHTS]

        def phase_offset(phase: str) -> float:
            return sum(weights[p] for p in phase_order[:phase_order.index(phase)])

        def report(phase: str, phase_fraction: float, stage: str):
            """Translate a within-phase fraction into overall clip progress."""
            if not progress_cb:
                return
            overall = phase_offset(phase) + weights[phase] * min(1.0, max(0.0, phase_fraction))
            try:
                progress_cb(min(1.0, overall), stage)
            except Exception:
                pass  # A misbehaving UI must never fail a conversion

        stream_sources = []  # (stream_index, [(path, size), ...])
        try:
            report('merge', 0.0, "Checking")

            if self.is_recording_active(clip_folder):
                if progress_cb:
                    progress_cb(1.0, "Active recording skipped")
                return None, "Steam is still writing this recording; stop the game, wait a minute, and rerun"

            # Check if converted file already exists
            existing_file = self.check_converted_exists(clip_folder, output_dir)
            if existing_file:
                existing_filename = os.path.basename(existing_file)
                if progress_cb:
                    progress_cb(1.0, "Already exported")
                if delete_source:
                    if self.delete_source_folder(clip_folder):
                        return True, f"Already converted (deleted source): {existing_filename}"
                    else:
                        return True, f"Already converted (failed to delete source): {existing_filename}"
                else:
                    return True, f"Already converted (skipped): {existing_filename}"
            ffmpeg_path = iio.get_ffmpeg_exe()
            session_mpd_files = self.find_session_mpd(clip_folder)

            if not session_mpd_files:
                return False, f"No session.mpd files found in {clip_folder}"

            duration = self.get_clip_duration(session_mpd_files)

            temp_dir = os.path.join(output_dir, '.temp')
            os.makedirs(temp_dir, exist_ok=True)

            temp_video_paths = []
            temp_audio_paths = []
            temp_files_to_cleanup = []

            self.logger.debug(f"Output directory: {output_dir} "
                              f"(exists={os.path.exists(output_dir)}, "
                              f"writable={os.access(output_dir, os.W_OK)})")

            try:
                # One scandir per session yields the chunk list *and* their sizes
                # (Windows caches them during enumeration), instead of a glob plus
                # a stat call per chunk.
                total_bytes = 0
                for session_mpd in session_mpd_files:
                    data_dir = os.path.dirname(session_mpd)
                    init_paths = {
                        0: os.path.join(data_dir, 'init-stream0.m4s'),
                        1: os.path.join(data_dir, 'init-stream1.m4s'),
                    }

                    if not all(os.path.exists(p) for p in init_paths.values()):
                        return False, f"Missing initialization files in {data_dir}"

                    chunks = {0: [], 1: []}
                    with os.scandir(data_dir) as entries:
                        for entry in entries:
                            if not entry.name.endswith('.m4s'):
                                continue
                            if entry.name.startswith('chunk-stream0-'):
                                stream_index = 0
                            elif entry.name.startswith('chunk-stream1-'):
                                stream_index = 1
                            else:
                                continue
                            try:
                                size = entry.stat().st_size
                            except OSError:
                                size = 0
                            chunks[stream_index].append((entry.name, entry.path, size))

                    for stream_index in (0, 1):
                        init_path = init_paths[stream_index]
                        try:
                            init_size = os.path.getsize(init_path)
                        except OSError:
                            init_size = 0
                        # Sort by filename so chunks concatenate in capture order.
                        ordered = [(path, size) for _, path, size in sorted(chunks[stream_index])]
                        files = [(init_path, init_size)] + ordered
                        total_bytes += sum(size for _, size in files)
                        stream_sources.append((stream_index, files))

                if len(session_mpd_files) == 1:
                    # FFmpeg can read the fragments as one logical stream. This avoids
                    # writing full-size intermediate video/audio files and reading them back.
                    use_single_session_weights()
                    direct_sources = {}
                    for stream_index, files in stream_sources:
                        source_list = tempfile.NamedTemporaryFile(
                            mode='w', encoding='utf-8', newline='\n', delete=False,
                            suffix='.txt', dir=temp_dir,
                        )
                        temp_files_to_cleanup.append(source_list.name)
                        with source_list as f:
                            for path, _size in files:
                                absolute_path = os.path.abspath(path).replace(os.sep, '/')
                                f.write(f"file:{absolute_path}\n")
                        list_path = os.path.abspath(source_list.name).replace(os.sep, '/')
                        direct_sources[stream_index] = f"concatf:{list_path}"
                    video_source = direct_sources[0]
                    audio_source = direct_sources[1]
                    report('merge', 1.0, "Preparing fragments")
                else:
                    copied_bytes = 0
                    for stream_index, files in stream_sources:
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4", dir=temp_dir) as tmp_stream:
                            temp_files_to_cleanup.append(tmp_stream.name)
                            for path, _size in files:
                                try:
                                    with open(path, 'rb') as f:
                                        while True:
                                            block = f.read(4 * 1024 * 1024)
                                            if not block:
                                                break
                                            tmp_stream.write(block)
                                            copied_bytes += len(block)
                                            if total_bytes:
                                                report('merge', copied_bytes / total_bytes, "Merging chunks")
                                except FileNotFoundError:
                                    return None, f"Recording changed while being read; stop the game and rerun ({path})"

                            if stream_index == 0:
                                temp_video_paths.append(tmp_stream.name)
                            else:
                                temp_audio_paths.append(tmp_stream.name)

                    report('merge', 1.0, "Merging chunks")
                    video_list_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt', dir=temp_dir)
                    audio_list_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt', dir=temp_dir)
                    temp_files_to_cleanup.extend([video_list_file.name, audio_list_file.name])

                    with video_list_file as f:
                        for temp_video in temp_video_paths:
                            f.write(f"file '{temp_video}'\n")

                    with audio_list_file as f:
                        for temp_audio in temp_audio_paths:
                            f.write(f"file '{temp_audio}'\n")

                    concatenated_video = tempfile.NamedTemporaryFile(delete=False, suffix='.mp4', dir=temp_dir)
                    concatenated_audio = tempfile.NamedTemporaryFile(delete=False, suffix='.mp4', dir=temp_dir)
                    concatenated_video.close()
                    concatenated_audio.close()
                    temp_files_to_cleanup.extend([concatenated_video.name, concatenated_audio.name])

                    self._run_ffmpeg([
                        ffmpeg_path, '-y',
                        '-f', 'concat',
                        '-safe', '0',
                        '-i', video_list_file.name,
                        '-c', 'copy',
                        concatenated_video.name
                    ], duration, lambda frac: report('video', frac, "Joining video"))

                    self._run_ffmpeg([
                        ffmpeg_path, '-y',
                        '-f', 'concat',
                        '-safe', '0',
                        '-i', audio_list_file.name,
                        '-c', 'copy',
                        concatenated_audio.name
                    ], duration, lambda frac: report('audio', frac, "Joining audio"))

                    video_source = concatenated_video.name
                    audio_source = concatenated_audio.name

                folder_basename = os.path.basename(clip_folder)
                parts = folder_basename.split('_')

                recorded_at = self.parse_recorded_datetime(clip_folder)
                formatted_date = recorded_at.strftime("%Y-%m-%d_%H-%M-%S") if recorded_at else "UnknownDate"

                game_id = parts[1] if len(parts) >= 2 else "Unknown"
                game_name = self.get_game_name(game_id)

                base_filename = f"{game_name}_{formatted_date}"
                output_file = self.get_unique_filename(output_dir, f"{base_filename}.mp4")

                self.logger.debug(f"Output file will be: {output_file}")

                # Combine video and audio (copy only, no compression)
                cmd = [
                    ffmpeg_path, '-y',
                    '-i', video_source,
                    '-i', audio_source,
                    '-c', 'copy',
                    '-shortest',  # Handle duration mismatches
                    '-movflags', '+faststart',  # Playable before the whole file downloads
                ]

                # Embed the recording time so players/media libraries show it
                if recorded_at:
                    utc_recorded_at = recorded_at.astimezone(timezone.utc)
                    cmd += ['-metadata', f"creation_time={utc_recorded_at.strftime('%Y-%m-%dT%H:%M:%SZ')}"]

                cmd.append(output_file)

                self.logger.debug(f"FFmpeg command: {' '.join(cmd)}")
                self._run_ffmpeg(cmd, duration, lambda frac: report('mux', frac, "Writing MP4"))

                # Verify output file was created
                if os.path.exists(output_file):
                    # Stamp the file with the recording time, not the export time
                    if recorded_at:
                        self.apply_recorded_timestamp(output_file, recorded_at)

                    file_size = os.path.getsize(output_file)
                    self.logger.debug(f"Output file created successfully: {output_file} ({file_size} bytes)")
                    if progress_cb:
                        progress_cb(1.0, "Done")
                    return True, f"Successfully converted: {os.path.basename(output_file)}"
                else:
                    self.logger.error(f"Output file was not created: {output_file}")
                    return False, f"Output file was not created: {os.path.basename(output_file)}"

            finally:
                # Cleanup temporary files
                for temp_file in temp_files_to_cleanup:
                    try:
                        if os.path.exists(temp_file):
                            os.unlink(temp_file)
                            self.logger.debug(f"Cleaned up temp file: {temp_file}")
                    except Exception as e:
                        self.logger.warning(f"Error cleaning up {temp_file}: {e}")

                # Additional cleanup for any remaining temp files
                try:
                    system_temp_dir = tempfile.gettempdir()
                    for filename in os.listdir(system_temp_dir):
                        if filename.startswith('tmp') and (filename.endswith('.mp4') or filename.endswith('.txt')):
                            temp_file_path = os.path.join(system_temp_dir, filename)
                            try:
                                # Check if file is older than 1 hour to avoid deleting active files
                                if os.path.getctime(temp_file_path) < (datetime.now().timestamp() - 3600):
                                    os.unlink(temp_file_path)
                                    self.logger.debug(f"Cleaned up old temp file: {temp_file_path}")
                            except (OSError, PermissionError):
                                pass  # File might be in use by another process
                except Exception:
                    pass  # Don't let temp cleanup failure break the main process

                # Clean up temporary directory if empty
                try:
                    if os.path.exists(temp_dir) and not os.listdir(temp_dir):
                        os.rmdir(temp_dir)
                except Exception:
                    pass

        except subprocess.CalledProcessError as e:
            stderr = e.stderr
            if isinstance(stderr, bytes):
                stderr = stderr.decode('utf-8', errors='replace')
            source_changed = any(
                not os.path.exists(path)
                for _, files in stream_sources
                for path, _ in files
            )
            if source_changed or self.is_recording_active(clip_folder):
                return None, "Recording changed while FFmpeg was reading it; stop the game, wait a minute, and rerun"
            return False, f"FFmpeg error processing {clip_folder}: {stderr.strip() if stderr else str(e)}"
        except Exception as e:
            return False, f"Error processing {clip_folder}: {str(e)}"

    def describe_clip(self, clip_folder: str) -> str:
        """Short human label for a clip folder, e.g. 'Dota 2 - 2025-07-30 21:14:02'."""
        folder_name = os.path.basename(clip_folder)
        parts = folder_name.split('_')
        game_name = self.get_game_name(parts[1]) if len(parts) >= 2 else folder_name

        if len(parts) >= 3:
            try:
                dt_obj = datetime.strptime(parts[-2] + parts[-1], "%Y%m%d%H%M%S")
                return f"{game_name} - {dt_obj.strftime('%Y-%m-%d %H:%M:%S')}"
            except ValueError:
                pass
        return f"{game_name} - {folder_name}"

    def process_clips_batch(self, clip_folders: list[str], output_dir: str, delete_source: bool = False,
                            show_progress: bool = True) -> dict[str, Any]:
        """
        Process multiple clip folders concurrently, one live progress bar per worker.

        Args:
            clip_folders: List of clip folder paths to process
            output_dir: Directory where converted MP4s will be saved
            delete_source: Whether to delete source folders after successful conversion
            show_progress: Render the live progress display (disable for plain log output)

        Returns:
            Dict containing processing results with 'successful', 'failed', 'skipped', and 'total' keys
        """
        os.makedirs(output_dir, exist_ok=True)

        results = {
            'successful': [],
            'failed': [],
            'skipped': [],
            'total': len(clip_folders)
        }

        if not clip_folders:
            self.logger.warning("No clips to process")
            return results

        clip_folders, active_skipped = self.partition_active_background_recordings(clip_folders)
        if active_skipped:
            results['skipped'].extend(active_skipped)
            self.logger.warning(
                f"Skipped {len(active_skipped)} background recordings while Steam is writing to them"
            )
            if show_progress:
                console.print(
                    f"[yellow]Skipped {len(active_skipped)} active background recordings; "
                    "stop the game, wait a minute, and rerun to export them.[/]"
                )
        if not clip_folders:
            return results

        effective_workers = min(self.max_workers, len(clip_folders))

        self.logger.info(f"Processing {len(clip_folders)} clips with {effective_workers} workers...")

        overall_progress = Progress(
            TextColumn("[bold]{task.description}"),
            BarColumn(bar_width=None),
            MofNCompleteColumn(),
            TaskProgressColumn(),
            TextColumn("[green]{task.fields[ok]} ok[/] [red]{task.fields[failed]} failed[/]"),
            TimeElapsedColumn(),
            TextColumn("eta"),
            TimeRemainingColumn(),
            console=console,
            disable=not show_progress,
        )
        worker_progress = Progress(
            SpinnerColumn(style="cyan"),
            TextColumn("[cyan]{task.fields[slot]}"),
            BarColumn(bar_width=24),
            TaskProgressColumn(),
            TextColumn("[dim]{task.fields[stage]:<15}"),
            TextColumn("{task.description}"),
            console=console,
            disable=not show_progress,
        )

        # Overall bar is driven by the sum of per-clip fractions, so it advances
        # smoothly rather than jumping only when a whole clip lands.
        overall_task = overall_progress.add_task(
            "Overall", total=len(clip_folders), ok=0, failed=0
        )

        slots = queue.Queue()
        slot_tasks = {}
        for i in range(effective_workers):
            slot_name = f"W{i + 1}"
            slots.put(slot_name)
            slot_tasks[slot_name] = worker_progress.add_task(
                "[dim]idle", total=1.0, slot=slot_name, stage="waiting"
            )

        display = Group(
            Panel(worker_progress, title=f"Workers ({effective_workers})",
                  border_style="cyan", padding=(0, 1)),
            Panel(overall_progress, title="Total", border_style="green", padding=(0, 1)),
        )

        counters_lock = threading.Lock()
        # Finished clips are counted as whole units and only in-flight clips
        # contribute fractions, so the overall bar never drifts off by rounding.
        finished_count = 0
        active_fractions: dict[str, float] = {}

        def refresh_overall():
            overall_progress.update(
                overall_task, completed=finished_count + sum(active_fractions.values())
            )

        def run_clip(clip_folder: str) -> tuple[Optional[bool], str]:
            """Claim a worker slot, convert one clip, then release the slot."""
            nonlocal finished_count
            slot_name = slots.get()
            task_id = slot_tasks[slot_name]
            label = self.describe_clip(clip_folder)
            worker_progress.reset(task_id, total=1.0, description=label,
                                  slot=slot_name, stage="starting")
            with counters_lock:
                active_fractions[slot_name] = 0.0

            def on_progress(fraction: float, stage: str):
                worker_progress.update(task_id, completed=fraction, stage=stage)
                with counters_lock:
                    active_fractions[slot_name] = fraction
                    refresh_overall()

            try:
                return self.process_single_clip(clip_folder, output_dir, delete_source, on_progress)
            finally:
                # Whatever happened, this clip is done occupying the bar.
                with counters_lock:
                    active_fractions.pop(slot_name, None)
                    finished_count += 1
                    refresh_overall()
                worker_progress.update(task_id, completed=0.0, description="[dim]idle",
                                       stage="waiting")
                slots.put(slot_name)

        # Without the live display there is nothing to render, so skip Live entirely
        # rather than drawing empty panels around disabled progress bars.
        live = (Live(display, console=console, refresh_per_second=12, transient=False)
                if show_progress else contextlib.nullcontext())

        with live:
            # ThreadPoolExecutor (not processes) keeps this simple on Windows and is
            # fine here because the real work happens in ffmpeg subprocesses.
            with concurrent.futures.ThreadPoolExecutor(max_workers=effective_workers) as executor:
                future_to_clip = {
                    executor.submit(run_clip, clip_folder): clip_folder
                    for clip_folder in clip_folders
                }

                for future in concurrent.futures.as_completed(future_to_clip):
                    clip_folder = future_to_clip[future]
                    try:
                        success, message = future.result()
                    except Exception as e:
                        success, message = False, f"Unexpected error: {str(e)}"

                    with counters_lock:
                        if success is True:
                            results['successful'].append(clip_folder)
                            self.logger.info(f"[SUCCESS] {message}")
                            if show_progress:
                                console.print(f"  [green]✓[/] {message}")
                        elif success is None:
                            results['skipped'].append((clip_folder, message))
                            self.logger.warning(f"[SKIPPED] {os.path.basename(clip_folder)}: {message}")
                            if show_progress:
                                console.print(f"  [yellow]•[/] {os.path.basename(clip_folder)}: {message}")
                        else:
                            results['failed'].append((clip_folder, message))
                            self.logger.error(f"[FAILED] {os.path.basename(clip_folder)}: {message}")
                            if show_progress:
                                console.print(f"  [red]✗[/] {os.path.basename(clip_folder)}: {message}")
                        overall_progress.update(overall_task,
                                                ok=len(results['successful']),
                                                failed=len(results['failed']))

        # Delete source folders only after ALL conversions are complete and successful
        if delete_source and results['successful']:
            self.logger.info(f"Deleting {len(results['successful'])} source folders...")
            deleted_count = 0
            with Progress(TextColumn("[bold]{task.description}"), BarColumn(),
                          MofNCompleteColumn(), console=console, disable=not show_progress,
                          transient=True) as delete_progress:
                task_id = delete_progress.add_task("Deleting sources", total=len(results['successful']))
                for clip_folder in results['successful']:
                    if self.delete_source_folder(clip_folder):
                        deleted_count += 1
                    delete_progress.advance(task_id)
            self.logger.info(f"Successfully deleted {deleted_count}/{len(results['successful'])} source folders")
            if show_progress:
                console.print(f"  [yellow]🗑[/] Deleted {deleted_count}/{len(results['successful'])} source folders")

        return results

    def cleanup_existing_sources(self, clip_folders: list[str], output_dir: str, dry_run: bool = False,
                                 show_progress: bool = True) -> dict[str, Any]:
        """
        Check for already-converted clips and delete their source folders.
        This mode does not perform any conversion.

        Args:
            clip_folders: List of clip folder paths to check
            output_dir: Directory where converted MP4s are stored
            dry_run: If True, only show what would be deleted without actually deleting
            show_progress: Render a live progress bar while scanning

        Returns:
            Dict containing cleanup results with 'deleted', 'skipped', and 'total' keys
        """
        results = {
            'deleted': [],
            'skipped': [],
            'total': len(clip_folders)
        }

        if not clip_folders:
            self.logger.warning("No clips to check for cleanup")
            return results

        clip_folders, active_skipped = self.partition_active_background_recordings(clip_folders)
        results['skipped'].extend(active_skipped)
        if active_skipped:
            self.logger.warning(
                f"Skipped {len(active_skipped)} background recordings while Steam is writing to them"
            )
        if not clip_folders:
            return results

        mode_str = "DRY RUN - " if dry_run else ""
        self.logger.info(f"{mode_str}Checking {len(clip_folders)} clips for cleanup...")

        with Progress(
            TextColumn("[bold]{task.description}"),
            BarColumn(bar_width=None),
            MofNCompleteColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
            disable=not show_progress,
        ) as progress:
            task_id = progress.add_task(
                "Would delete" if dry_run else "Cleaning up", total=len(clip_folders)
            )

            for clip_folder in clip_folders:
                try:
                    existing_file = self.check_converted_exists(clip_folder, output_dir)
                    expected_filename = os.path.basename(self.get_expected_output_filename(clip_folder, output_dir))

                    if existing_file:
                        existing_filename = os.path.basename(existing_file)
                        if dry_run:
                            results['deleted'].append(clip_folder)
                            self.logger.info(f"[WOULD DELETE] {existing_filename} - source folder would be removed")
                        else:
                            if self.delete_source_folder(clip_folder):
                                results['deleted'].append(clip_folder)
                                self.logger.info(f"[DELETED] {existing_filename} - source folder removed")
                            else:
                                results['skipped'].append((clip_folder, "Failed to delete source"))
                                self.logger.warning(f"[SKIPPED] {existing_filename} - failed to delete source")
                    else:
                        clip_name = os.path.basename(clip_folder)
                        results['skipped'].append((clip_folder, f"No converted file found (expected: {expected_filename})"))
                        self.logger.info(f"[SKIPPED] {clip_name} - expected file not found: {expected_filename} in {output_dir}")
                except Exception as e:
                    error_msg = f"Error checking clip: {str(e)}"
                    results['skipped'].append((clip_folder, error_msg))
                    self.logger.error(f"[ERROR] {clip_folder}: {error_msg}")
                finally:
                    progress.advance(task_id)

        return results


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Export Steam game recordings to MP4. Run with no options for an interactive menu.",
)

MEDIA_TYPES = ("all", "manual", "background")

# questionary drives the arrow-key menus; keep its palette close to the rich theme.
PROMPT_STYLE = questionary.Style([
    ('qmark', 'fg:#00afaf bold'),
    ('question', 'bold'),
    ('answer', 'fg:#00afaf bold'),
    ('pointer', 'fg:#00afaf bold'),
    ('highlighted', 'fg:#00afaf bold'),
    ('selected', 'fg:#5fd700'),
    ('separator', 'fg:#6c6c6c'),
    ('instruction', 'fg:#6c6c6c'),
])

_BACK = object()


def _ask(question, allow_back: bool = False):
    """Run a prompt, making Esc go back when the caller has a previous step."""
    @question.application.key_bindings.add(Keys.Escape, eager=True)
    def escape(event):
        event.app.exit(result=_BACK if allow_back else None)

    answer = question.ask()
    if answer is None:
        console.print("[yellow]Cancelled.[/]")
        raise typer.Exit(1)
    return answer


def default_output_dir() -> str:
    """Platform-appropriate default export directory."""
    if platform.system() == "Linux":
        return os.path.expanduser("~/Videos")
    return os.path.expanduser("~/Desktop")


def print_banner():
    console.print(
        f"[bold cyan]Steam Game Recording Exporter[/] [dim]v{__version__}[/] "
        f"[dim]- export Steam recordings to MP4[/]"
    )


def print_clip_list(exporter: "SteamGameRecordingExporter", clip_folders: list[str]):
    """Render the discovered clips as a numbered table."""
    from rich.table import Table

    table = Table(title=f"{len(clip_folders)} clips found", title_justify="left",
                  header_style="bold cyan", box=None, padding=(0, 2))
    table.add_column("#", justify="right", style="dim")
    table.add_column("Game")
    table.add_column("Recorded")
    table.add_column("Type", style="dim")

    for i, clip_folder in enumerate(clip_folders, 1):
        folder_name = os.path.basename(clip_folder)
        parts = folder_name.split('_')
        game_name = exporter.get_game_name(parts[1]) if len(parts) >= 2 else "Unknown"

        recorded = folder_name
        if len(parts) >= 3:
            try:
                dt_obj = datetime.strptime(parts[-2] + parts[-1], "%Y%m%d%H%M%S")
                recorded = dt_obj.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass

        # Steam stores saved clips under 'clips' and rolling recordings under 'video'.
        kind = "background" if os.sep + "video" + os.sep in clip_folder else "saved clip"
        table.add_row(str(i), game_name, recorded, kind)

    console.print(table)


def group_clips_by_game(exporter: "SteamGameRecordingExporter",
                        clip_folders: list[str]) -> dict[str, list[str]]:
    """Map game id -> clip folders, preserving the newest-first ordering."""
    grouped: dict[str, list[str]] = {}
    for clip_folder in clip_folders:
        parts = os.path.basename(clip_folder).split('_')
        game_id = parts[1] if len(parts) >= 2 else "Unknown"
        grouped.setdefault(game_id, []).append(clip_folder)
    return grouped


def run_export(exporter: "SteamGameRecordingExporter", clip_folders: list[str],
               output_dir: str, delete_source: bool, show_progress: bool) -> int:
    """Convert the given clips and print a summary. Returns a process exit code."""
    console.print(f"\n[bold]Exporting {len(clip_folders)} clips[/] -> [cyan]{output_dir}[/]\n")

    start_time = datetime.now()
    results = exporter.process_clips_batch(clip_folders, output_dir, delete_source,
                                           show_progress=show_progress)
    elapsed = datetime.now() - start_time

    console.print(f"\n[bold green]Done[/] in {elapsed}")
    console.print(f"  Converted: [green]{len(results['successful'])}[/]/{results['total']}")

    if results.get('skipped'):
        console.print(f"  Skipped:   [yellow]{len(results['skipped'])}[/]/{results['total']}")
        for clip_folder, reason in results['skipped'][:20]:
            console.print(f"    [yellow]•[/] {os.path.basename(clip_folder)}: {reason}")

    if results['failed']:
        console.print(f"  Failed:    [red]{len(results['failed'])}[/]/{results['total']}")
        for clip_folder, error in results['failed']:
            console.print(f"    [red]•[/] {os.path.basename(clip_folder)}: {error}")

    console.print(f"[dim]Log: {exporter.log_file}[/]")
    return 0 if not results['failed'] else 1


def run_cleanup(exporter: "SteamGameRecordingExporter", clip_folders: list[str],
                output_dir: str, dry_run: bool, show_progress: bool) -> int:
    """Delete source folders for already-exported clips. Returns a process exit code."""
    mode = "[yellow]DRY RUN[/] " if dry_run else ""
    console.print(f"\n{mode}[bold]Checking {len(clip_folders)} clips[/] against [cyan]{output_dir}[/]\n")

    start_time = datetime.now()
    results = exporter.cleanup_existing_sources(clip_folders, output_dir, dry_run=dry_run,
                                                show_progress=show_progress)
    elapsed = datetime.now() - start_time

    verb = "WOULD BE deleted" if dry_run else "deleted"
    console.print(f"\n[bold green]Done[/] in {elapsed}")
    console.print(f"  Source folders {verb}: [green]{len(results['deleted'])}[/]/{results['total']}")

    if results['skipped']:
        console.print(f"  Skipped: [yellow]{len(results['skipped'])}[/]/{results['total']}")
        for clip_folder, reason in results['skipped'][:20]:
            console.print(f"    [yellow]•[/] {os.path.basename(clip_folder)}: {reason}")
        if len(results['skipped']) > 20:
            console.print(f"    [dim]... and {len(results['skipped']) - 20} more (see log)[/]")

    if dry_run and results['deleted']:
        console.print("\n[dim]Re-run without --dry-run to actually delete them.[/]")

    console.print(f"[dim]Log: {exporter.log_file}[/]")
    return 0


def interactive_session(exporter: "SteamGameRecordingExporter", userdata_path: str,
                        output_dir: str, show_progress: bool) -> int:
    """Arrow-key driven menu covering every action the flags expose."""
    console.print("[dim]Esc: back one step · Ctrl+C: cancel[/]")
    state = "action"
    action = media_type = scope = None
    delete_source = False
    available_clips = clip_folders = selected_clips = []
    grouped = {}

    while True:
        if state == "action":
            action = _ask(questionary.select(
                "What do you want to do?",
                choices=[
                    questionary.Choice("Export recordings to MP4", value="export"),
                    questionary.Choice("List recordings", value="list"),
                    questionary.Choice("Clean up sources of already-exported recordings", value="cleanup"),
                    questionary.Choice("Show detected Steam paths", value="paths"),
                    questionary.Choice("Quit", value="quit"),
                ],
                style=PROMPT_STYLE,
                use_shortcuts=False,
            ))
            if action == "quit":
                return 0
            if action == "paths":
                detected = exporter.auto_detect_steam_paths()
                if detected:
                    console.print("\n[bold]Detected Steam userdata paths:[/]")
                    for i, path in enumerate(detected, 1):
                        console.print(f"  {i}. [cyan]{path}[/]")
                else:
                    console.print("[yellow]No Steam userdata paths detected.[/]")
                return 0
            state = "media"
            continue

        if state == "media":
            media_type = _ask(questionary.select(
                "Which Steam recordings?",
                choices=[
                    questionary.Choice(
                        "Background recordings (default)", value="background",
                        description="Rolling history from Record in Background; temporary and may be overwritten.",
                    ),
                    questionary.Choice(
                        "Everything Steam has stored", value="all",
                        description="Background recordings plus manually saved clips.",
                    ),
                    questionary.Choice(
                        "Saved clips (manually created)", value="manual",
                        description="Clips created with Clip > Save in Steam; kept permanently.",
                    ),
                ],
                style=PROMPT_STYLE,
            ), allow_back=True)
            if media_type is _BACK:
                state = "action"
                continue

            with console.status("[cyan]Scanning Steam recordings...[/]"):
                available_clips = exporter.get_clip_folders(
                    userdata_path=userdata_path, media_type=media_type
                )
            if not available_clips:
                console.print("[yellow]No recordings found for that filter.[/]")
                return 1

            clip_folders = available_clips
            grouped = group_clips_by_game(exporter, available_clips)
            if len(grouped) > 1:
                state = "game"
            elif action == "list":
                print_clip_list(exporter, clip_folders)
                return 0
            else:
                state = "scope"
            continue

        if state == "game":
            game_choices = [
                questionary.Choice(f"All games ({len(available_clips)} clips)", value="all")
            ]
            for game_id, folders in sorted(
                grouped.items(), key=lambda item: len(item[1]), reverse=True
            ):
                game_choices.append(questionary.Choice(
                    f"{exporter.get_game_name(game_id)} ({len(folders)} clips)", value=game_id
                ))
            selected_game = _ask(questionary.select(
                "Which game?", choices=game_choices, style=PROMPT_STYLE
            ), allow_back=True)
            if selected_game is _BACK:
                state = "media"
                continue
            clip_folders = (
                available_clips
                if selected_game == "all" else grouped[selected_game]
            )
            if action == "list":
                print_clip_list(exporter, clip_folders)
                return 0
            state = "scope"
            continue

        if state == "scope":
            scope = _ask(questionary.select(
                f"Which of the {len(clip_folders)} clips?",
                choices=[
                    questionary.Choice("All of them", value="all"),
                    questionary.Choice("Pick individually", value="pick"),
                ],
                style=PROMPT_STYLE,
            ), allow_back=True)
            if scope is _BACK:
                state = "game" if len(grouped) > 1 else "media"
                continue
            if scope == "pick":
                state = "pick"
            else:
                selected_clips = clip_folders
                state = "output"
            continue

        if state == "pick":
            picked = _ask(questionary.checkbox(
                "Select clips (space to toggle, enter to confirm)",
                choices=[questionary.Choice(
                    exporter.describe_clip(folder), value=folder,
                    checked=folder in selected_clips,
                )
                         for folder in clip_folders],
                style=PROMPT_STYLE,
            ), allow_back=True)
            if picked is _BACK:
                state = "scope"
                continue
            if not picked:
                console.print("[yellow]Nothing selected.[/]")
                return 1
            selected_clips = picked
            state = "output"
            continue

        if state == "output":
            selected_output = _ask(questionary.path(
                "Output directory", default=output_dir, only_directories=True,
                style=PROMPT_STYLE,
            ), allow_back=True)
            if selected_output is _BACK:
                state = "pick" if scope == "pick" else "scope"
                continue
            output_dir = os.path.abspath(os.path.expanduser(selected_output.strip()))
            state = "dry_run" if action == "cleanup" else "workers"
            continue

        if state == "dry_run":
            dry_run = _ask(questionary.confirm(
                "Dry run first (show what would be deleted, delete nothing)?",
                default=True, style=PROMPT_STYLE,
            ), allow_back=True)
            if dry_run is _BACK:
                state = "output"
                continue
            exporter.save_preferences(output_dir)
            return run_cleanup(
                exporter, selected_clips, output_dir, dry_run, show_progress
            )

        if state == "workers":
            worker_hints = {
                1: "Best for HDD sources; avoids seeking between recordings.",
                2: "Balanced for HDDs or mixed storage.",
                4: "Good for SSD/NVMe sources; can slow down an HDD.",
                6: "Fast SSD/NVMe sources only; useful with many clips.",
                8: "Very fast SSD/NVMe sources only; often slower on one drive.",
            }
            workers = _ask(questionary.select(
                "How many parallel workers? (HDD source: 1-2, SSD source: 2-4)",
                choices=[questionary.Choice(
                    f"{count}" + (" (current default)" if count == exporter.max_workers else ""),
                    value=count,
                    description=worker_hints.get(count, "Custom worker count."),
                ) for count in sorted({1, 2, 4, 6, 8, exporter.max_workers})],
                default=exporter.max_workers,
                style=PROMPT_STYLE,
            ), allow_back=True)
            if workers is _BACK:
                state = "output"
                continue
            exporter.max_workers = int(workers)
            exporter.save_preferences(output_dir, exporter.max_workers)
            state = "delete"
            continue

        if state == "delete":
            delete_source = _ask(questionary.confirm(
                "Delete the original Steam folders after a successful export?",
                default=delete_source, style=PROMPT_STYLE,
            ), allow_back=True)
            if delete_source is _BACK:
                state = "workers"
                continue
            state = "confirm"
            continue

        console.print()
        proceed = _ask(questionary.confirm(
            f"Export {len(selected_clips)} clips to {output_dir} "
            f"with {exporter.max_workers} workers"
            + (" and delete sources" if delete_source else "") + "?",
            default=True, style=PROMPT_STYLE,
        ), allow_back=True)
        if proceed is _BACK:
            state = "delete"
            continue
        if not proceed:
            console.print("[yellow]Cancelled.[/]")
            return 1
        return run_export(
            exporter, selected_clips, output_dir, delete_source, show_progress
        )


@app.command()
def cli(
    ctx: typer.Context,
    list_clips: bool = typer.Option(False, "--list-clips", "-l",
                                    help="List all available recordings without exporting."),
    process_all: bool = typer.Option(False, "--process-all", "-a",
                                     help="Export every recording that matches the filters."),
    steam_id: Optional[str] = typer.Option(None, "--steam-id",
                                           help="Only recordings for this Steam ID."),
    game_id: Optional[str] = typer.Option(None, "--game-id",
                                          help="Only recordings for this game ID."),
    media_type: str = typer.Option("all", "--media-type",
                                   help="Recording type: all, manual (manually saved clips), or background."),
    output: Optional[str] = typer.Option(None, "--output", "-o",
                                         envvar="STEAM_EXPORTER_OUTPUT_DIR",
                                         help="Output directory for exported MP4s."),
    workers: Optional[int] = typer.Option(None, "--workers", "-w", min=1,
                                          envvar="STEAM_EXPORTER_WORKERS",
                                          help="Parallel workers; HDD source: 1-2, SSD source: 2-4."),
    userdata_path: Optional[str] = typer.Option(None, "--userdata-path",
                                                help="Steam userdata path, if auto-detection fails."),
    detect_paths: bool = typer.Option(False, "--detect-paths",
                                      help="Show auto-detected Steam paths and exit."),
    delete_source: bool = typer.Option(False, "--delete-source",
                                       help="Delete original Steam folders after a successful export."),
    cleanup_only: bool = typer.Option(False, "--cleanup-only",
                                      help="Only delete sources of already-exported recordings."),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="With --cleanup-only, show what would be deleted."),
    interactive: bool = typer.Option(False, "--interactive", "-i",
                                     help="Force the arrow-key menu."),
    no_progress: bool = typer.Option(False, "--no-progress",
                                     help="Disable live progress bars (useful for CI and piping)."),
    verbose: bool = typer.Option(False, "--verbose", "-v",
                                 help="Also print debug logs to the console."),
    version: bool = typer.Option(False, "--version", help="Show version and exit."),
):
    """Export Steam game recordings (.m4s + .mpd) to standard MP4 files."""
    if version:
        console.print(f"steamexporter {__version__}")
        raise typer.Exit()

    if media_type not in MEDIA_TYPES:
        console.print(f"[red]Invalid --media-type '{media_type}'.[/] "
                      f"Choose one of: {', '.join(MEDIA_TYPES)}")
        raise typer.Exit(2)

    if dry_run and not cleanup_only:
        console.print("[red]--dry-run requires --cleanup-only.[/]")
        raise typer.Exit(2)

    print_banner()

    exporter = SteamGameRecordingExporter(max_workers=workers, verbose=verbose)
    show_progress = not no_progress and console.is_terminal

    # With no progress display, per-clip log lines are the only feedback there is.
    if not show_progress and not verbose:
        exporter.set_console_log_level(logging.INFO)

    if detect_paths:
        detected = exporter.auto_detect_steam_paths()
        if detected:
            console.print("\n[bold]Detected Steam userdata paths:[/]")
            for i, path in enumerate(detected, 1):
                console.print(f"  {i}. [cyan]{path}[/]")
        else:
            console.print("[yellow]No Steam userdata paths detected.[/]")
        raise typer.Exit(0 if detected else 1)

    resolved_userdata = userdata_path or exporter.find_steam_userdata_path()
    if not resolved_userdata:
        console.print("[red]Could not find the Steam userdata path.[/]\n")
        console.print("Try one of:")
        console.print('  1. Pass it manually: [cyan]steamexporter --userdata-path "/path/to/steam/userdata"[/]')
        console.print("  2. See what was checked: [cyan]steamexporter --detect-paths[/]")
        console.print("  3. Make sure Steam is installed and has been run at least once")
        raise typer.Exit(1)

    saved_output = exporter.settings.get('output_dir')
    if output:
        resolved_output = os.path.abspath(os.path.expanduser(output))
    elif isinstance(saved_output, str) and saved_output:
        resolved_output = saved_output
    else:
        resolved_output = default_output_dir()

    # An output path from the environment is a prompt default, not an instruction
    # to start exporting without an explicit action.
    output_source = ctx.get_parameter_source("output")
    output_was_explicit = output_source is not None and output_source.name == "COMMANDLINE"

    # No action flag on an interactive terminal means the user wants the menu.
    wants_menu = interactive or not any(
        [list_clips, process_all, cleanup_only, game_id, steam_id, output_was_explicit,
         media_type != "all", delete_source]
    )
    if wants_menu:
        # On Windows even NUL reports isatty() == True, so check stdout as well
        # before trying to draw a menu.
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            console.print("[red]No action given, and this is not an interactive terminal.[/]")
            console.print("Pass an action instead, e.g. [cyan]--process-all[/], "
                          "[cyan]--list-clips[/] or [cyan]--cleanup-only[/].")
            raise typer.Exit(2)
        try:
            raise typer.Exit(interactive_session(exporter, resolved_userdata,
                                                 resolved_output, show_progress))
        except typer.Exit:
            raise
        except Exception as e:
            # prompt_toolkit needs a real console; mintty/Git Bash and some CI
            # terminals cannot provide one.
            exporter.logger.debug(f"Interactive menu unavailable: {e!r}")
            console.print(f"[red]The interactive menu cannot run in this terminal.[/] ({e})")
            console.print("Run it from Windows Terminal, PowerShell or cmd, or use the "
                          "flags directly - see [cyan]--help[/].")
            raise typer.Exit(2)

    with console.status("[cyan]Scanning Steam recordings...[/]"):
        clip_folders = exporter.get_clip_folders(
            userdata_path=resolved_userdata,
            steam_id=steam_id,
            media_type=media_type,
            game_id=game_id,
        )

    if not clip_folders:
        console.print("[yellow]No clips found matching those filters.[/]\n")
        console.print("Possible reasons:")
        console.print("  • Steam game recording is not enabled")
        console.print("  • Nothing has been recorded yet")
        console.print("  • The clips live under a different Steam user directory")
        raise typer.Exit(1)

    if list_clips:
        print_clip_list(exporter, clip_folders)
        raise typer.Exit(0)

    if cleanup_only:
        raise typer.Exit(run_cleanup(exporter, clip_folders, resolved_output,
                                     dry_run, show_progress))

    raise typer.Exit(run_export(exporter, clip_folders, resolved_output,
                                delete_source, show_progress))


def main():
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    # Windows multiprocessing protection
    if platform.system() == "Windows":
        import multiprocessing
        multiprocessing.freeze_support()

    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Operation cancelled by user.[/]")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]Unexpected error:[/] {e}")
        traceback.print_exc()
        sys.exit(1)
