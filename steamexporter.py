#!/usr/bin/env python3
"""
Steam Game Recording Exporter

Export Steam game recordings (.m4s + .mpd) to standard MP4 video files.
Supports Windows, macOS, and Linux with automatic Steam path detection.
"""

import os
import sys
import json
import glob
import tempfile
import argparse
import logging
import traceback
import platform
import subprocess
import concurrent.futures
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional, Tuple
import xml.etree.ElementTree as ET

try:
    import imageio_ffmpeg as iio
    import requests
except ImportError as e:
    print(f"Missing required dependency: {e}")
    print("Please install required packages:")
    print("pip install imageio-ffmpeg requests")
    sys.exit(1)


class SteamGameRecordingExporter:
    """
    Handles Steam path detection, recording discovery, and MP4 conversion.
    """

    CONFIG_DIR = os.path.join(
        os.environ.get('LOCALAPPDATA', os.path.expanduser("~")), 'SteamGameRecordingExporter'
    )
    GAME_IDS_FILE = os.path.join(CONFIG_DIR, 'GameIDs.json')
    STEAM_APP_DETAILS_URL = "https://store.steampowered.com/api/appdetails"
    CURRENT_VERSION = "v1.0.0"

    def __init__(self, max_workers: int = None):
        self.max_workers = max_workers or min(6, max(2, (os.cpu_count() or 1) // 2))
        self.game_ids = {}
        self.setup_logging()
        self.load_game_ids()
        self._custom_record_cache = {}

    def setup_logging(self):
        """Setup logging configuration"""
        log_dir = os.path.join(self.CONFIG_DIR, 'logs')
        os.makedirs(log_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_file = os.path.join(log_dir, f"{timestamp}.log")

        # Create stream handler with proper encoding
        import io
        if platform.system() == "Windows":
            # Force UTF-8 encoding on Windows to handle unicode characters
            stdout_wrapper = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
            stream_handler = logging.StreamHandler(stdout_wrapper)
        else:
            stream_handler = logging.StreamHandler(sys.stdout)

        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s %(levelname)s: %(message)s',
            handlers=[
                logging.FileHandler(log_file, encoding='utf-8'),
                stream_handler
            ]
        )
        self.logger = logging.getLogger(__name__)

    def auto_detect_steam_paths(self) -> List[str]:
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

    def find_session_mpd(self, clip_folder: str) -> List[str]:
        """Find all session.mpd files in a clip folder"""
        session_mpd_files = []
        for root, _, files in os.walk(clip_folder):
            if 'session.mpd' in files:
                session_mpd_files.append(os.path.join(root, 'session.mpd'))
        return session_mpd_files

    def get_clip_folders(self, userdata_path: str, steam_id: str = None,
                        media_type: str = "all", game_id: str = None) -> List[str]:
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
                            if self.find_session_mpd(folder_path):
                                # Filter by game ID if specified
                                if not game_id or f"_{game_id}_" in folder_entry.name:
                                    clip_folders.append(folder_path)
                except (OSError, PermissionError) as e:
                    self.logger.warning(f"Error scanning {clip_dir}: {e}")

        # Sort by datetime (newest first)
        clip_folders.sort(key=self.extract_datetime_from_folder_name, reverse=True)
        return clip_folders

    def extract_datetime_from_folder_name(self, folder_path: str) -> datetime:
        """Extract datetime from folder name"""
        folder_name = os.path.basename(folder_path)
        parts = folder_name.split('_')
        if len(parts) >= 3:
            try:
                datetime_str = parts[-2] + parts[-1]
                return datetime.strptime(datetime_str, "%Y%m%d%H%M%S")
            except ValueError:
                pass
        return datetime.min

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

        if len(parts) >= 3:
            try:
                datetime_str = parts[-2] + parts[-1]
                dt_obj = datetime.strptime(datetime_str, "%Y%m%d%H%M%S")
                formatted_date = dt_obj.strftime("%Y-%m-%d_%H-%M-%S")
            except ValueError:
                formatted_date = "UnknownDate"
        else:
            formatted_date = "UnknownDate"

        game_id = parts[1] if len(parts) >= 2 else "Unknown"
        game_name = self.get_game_name(game_id)

        base_filename = f"{game_name}_{formatted_date}"
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

        # Check for numbered variations (e.g., filename_1.mp4, filename_2.mp4)
        base_name, ext = os.path.splitext(expected_file)
        counter = 1
        while counter <= 100:  # Reasonable upper limit
            numbered_file = f"{base_name}_{counter}{ext}"
            if os.path.exists(numbered_file):
                return numbered_file
            counter += 1

        return None

    def delete_source_folder(self, clip_folder: str) -> bool:
        """Safely delete source clip folder after successful conversion"""
        try:
            import shutil
            if os.path.exists(clip_folder):
                # Double-check this is a Steam clip folder (safety check)
                if any(f.endswith('.m4s') or f == 'session.mpd' for root, dirs, files in os.walk(clip_folder) for f in files):
                    shutil.rmtree(clip_folder)
                    self.logger.info(f"Deleted source folder: {clip_folder}")
                    return True
                else:
                    self.logger.warning(f"Skipped deletion - not a Steam clip folder: {clip_folder}")
                    return False
            return True  # Already deleted
        except Exception as e:
            self.logger.error(f"Failed to delete source folder {clip_folder}: {e}")
            return False

    def process_single_clip(self, clip_folder: str, output_dir: str, delete_source: bool = False) -> Tuple[bool, str]:
        """
        Process a single Steam clip folder and convert to MP4.

        Args:
            clip_folder: Path to the Steam clip folder containing session.mpd
            output_dir: Directory where the converted MP4 will be saved
            delete_source: Whether to delete source folder if MP4 already exists

        Returns:
            Tuple[bool, str]: (success_flag, result_message)
        """
        try:
            # Check if converted file already exists
            existing_file = self.check_converted_exists(clip_folder, output_dir)
            if existing_file:
                existing_filename = os.path.basename(existing_file)
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

            temp_dir = os.path.join(output_dir, '.temp')
            os.makedirs(temp_dir, exist_ok=True)

            temp_video_paths = []
            temp_audio_paths = []
            temp_files_to_cleanup = []

            self.logger.info(f"Output directory: {output_dir}")
            self.logger.info(f"Directory exists: {os.path.exists(output_dir)}")
            self.logger.info(f"Directory writable: {os.access(output_dir, os.W_OK)}")
            
            try:
                for session_mpd in session_mpd_files:
                    data_dir = os.path.dirname(session_mpd)
                    init_video = os.path.join(data_dir, 'init-stream0.m4s')
                    init_audio = os.path.join(data_dir, 'init-stream1.m4s')

                    if not (os.path.exists(init_video) and os.path.exists(init_audio)):
                        return False, f"Missing initialization files in {data_dir}"

                    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4", dir=temp_dir) as tmp_video:
                        with open(init_video, 'rb') as f:
                            tmp_video.write(f.read())

                        for chunk in sorted(glob.glob(os.path.join(data_dir, 'chunk-stream0-*.m4s'))):
                            with open(chunk, 'rb') as f:
                                tmp_video.write(f.read())

                        temp_video_paths.append(tmp_video.name)
                        temp_files_to_cleanup.append(tmp_video.name)

                    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4", dir=temp_dir) as tmp_audio:
                        with open(init_audio, 'rb') as f:
                            tmp_audio.write(f.read())

                        for chunk in sorted(glob.glob(os.path.join(data_dir, 'chunk-stream1-*.m4s'))):
                            with open(chunk, 'rb') as f:
                                tmp_audio.write(f.read())

                        temp_audio_paths.append(tmp_audio.name)
                        temp_files_to_cleanup.append(tmp_audio.name)

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

                subprocess.run([
                    ffmpeg_path, '-y',
                    '-f', 'concat',
                    '-safe', '0',
                    '-i', video_list_file.name,
                    '-c', 'copy',
                    '-movflags', '+faststart',
                    concatenated_video.name
                ], check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)

                subprocess.run([
                    ffmpeg_path, '-y',
                    '-f', 'concat',
                    '-safe', '0',
                    '-i', audio_list_file.name,
                    '-c', 'copy',
                    concatenated_audio.name
                ], check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)

                folder_basename = os.path.basename(clip_folder)
                parts = folder_basename.split('_')

                if len(parts) >= 3:
                    try:
                        datetime_str = parts[-2] + parts[-1]
                        dt_obj = datetime.strptime(datetime_str, "%Y%m%d%H%M%S")
                        formatted_date = dt_obj.strftime("%Y-%m-%d_%H-%M-%S")
                    except ValueError:
                        formatted_date = "UnknownDate"
                else:
                    formatted_date = "UnknownDate"

                game_id = parts[1] if len(parts) >= 2 else "Unknown"
                game_name = self.get_game_name(game_id)

                base_filename = f"{game_name}_{formatted_date}"
                output_file = self.get_unique_filename(output_dir, f"{base_filename}.mp4")

                self.logger.info(f"Output file will be: {output_file}")

                # Combine video and audio (copy only, no compression)
                cmd = [
                    ffmpeg_path, '-y',
                    '-i', concatenated_video.name,
                    '-i', concatenated_audio.name,
                    '-c', 'copy',
                    '-shortest',  # Handle duration mismatches
                    output_file
                ]

                self.logger.info(f"FFmpeg command: {' '.join(cmd)}")
                subprocess.run(cmd, check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)

                # Verify output file was created
                if os.path.exists(output_file):
                    file_size = os.path.getsize(output_file)
                    self.logger.info(f"Output file created successfully: {output_file} ({file_size} bytes)")
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
                    temp_dir = tempfile.gettempdir()
                    for filename in os.listdir(temp_dir):
                        if filename.startswith('tmp') and (filename.endswith('.mp4') or filename.endswith('.txt')):
                            temp_file_path = os.path.join(temp_dir, filename)
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
            return False, f"FFmpeg error processing {clip_folder}: {e.stderr.decode() if e.stderr else str(e)}"
        except Exception as e:
            return False, f"Error processing {clip_folder}: {str(e)}"

    def process_clips_batch(self, clip_folders: List[str], output_dir: str, delete_source: bool = False) -> Dict[str, any]:
        """
        Process multiple clip folders concurrently using multiprocessing.

        Args:
            clip_folders: List of clip folder paths to process
            output_dir: Directory where converted MP4s will be saved
            delete_source: Whether to delete source folders after successful conversion

        Returns:
            Dict containing processing results with 'successful', 'failed', and 'total' keys
        """
        os.makedirs(output_dir, exist_ok=True)

        results = {
            'successful': [],
            'failed': [],
            'total': len(clip_folders)
        }

        if not clip_folders:
            self.logger.warning("No clips to process")
            return results

        effective_workers = self.max_workers

        self.logger.info(f"Processing {len(clip_folders)} clips with {effective_workers} workers...")

        # Process clips concurrently (using ThreadPoolExecutor for better Windows compatibility)
        with concurrent.futures.ThreadPoolExecutor(max_workers=effective_workers) as executor:
            # Submit all jobs
            future_to_clip = {
                executor.submit(self.process_single_clip, clip_folder, output_dir, delete_source): clip_folder
                for clip_folder in clip_folders
            }

            # Collect results as they complete
            for future in concurrent.futures.as_completed(future_to_clip):
                clip_folder = future_to_clip[future]
                try:
                    success, message = future.result()
                    if success:
                        results['successful'].append(clip_folder)
                        self.logger.info(f"[SUCCESS] {message}")
                    else:
                        results['failed'].append((clip_folder, message))
                        self.logger.error(f"[FAILED] {message}")
                except Exception as e:
                    error_msg = f"Unexpected error: {str(e)}"
                    results['failed'].append((clip_folder, error_msg))
                    self.logger.error(f"[FAILED] {clip_folder}: {error_msg}")

        # Delete source folders only after ALL conversions are complete and successful
        if delete_source and results['successful']:
            self.logger.info(f"Deleting {len(results['successful'])} source folders...")
            deleted_count = 0
            for clip_folder in results['successful']:
                if self.delete_source_folder(clip_folder):
                    deleted_count += 1
            self.logger.info(f"Successfully deleted {deleted_count}/{len(results['successful'])} source folders")

        return results

    def cleanup_existing_sources(self, clip_folders: List[str], output_dir: str) -> Dict[str, any]:
        """
        Check for already-converted clips and delete their source folders.
        This mode does not perform any conversion.

        Args:
            clip_folders: List of clip folder paths to check
            output_dir: Directory where converted MP4s are stored

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

        self.logger.info(f"Checking {len(clip_folders)} clips for cleanup...")

        for clip_folder in clip_folders:
            try:
                existing_file = self.check_converted_exists(clip_folder, output_dir)
                if existing_file:
                    existing_filename = os.path.basename(existing_file)
                    if self.delete_source_folder(clip_folder):
                        results['deleted'].append(clip_folder)
                        self.logger.info(f"[DELETED] {existing_filename} - source folder removed")
                    else:
                        results['skipped'].append((clip_folder, "Failed to delete source"))
                        self.logger.warning(f"[SKIPPED] {existing_filename} - failed to delete source")
                else:
                    clip_name = os.path.basename(clip_folder)
                    results['skipped'].append((clip_folder, "No converted file found"))
                    self.logger.info(f"[SKIPPED] {clip_name} - no converted file found")
            except Exception as e:
                error_msg = f"Error checking clip: {str(e)}"
                results['skipped'].append((clip_folder, error_msg))
                self.logger.error(f"[ERROR] {clip_folder}: {error_msg}")

        return results


def main():
    """Main CLI interface for Steam Game Recording Exporter."""
    print(f"Steam Game Recording Exporter v1.0.0 - Export Steam Recordings to MP4")
    print("=" * 75)
    print()

    parser = argparse.ArgumentParser(
        description="Steam Game Recording Exporter CLI - Export Steam game recordings to MP4 with multiprocessing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --list-clips                    # List all available recordings
  %(prog)s --process-all                   # Export all recordings
  %(prog)s --game-id 570                   # Export recordings for Dota 2
  %(prog)s --steam-id 76561198000000000    # Export recordings for specific Steam ID
  %(prog)s --media-type manual             # Export only manual clips
  %(prog)s --output ~/Videos/Clips         # Set custom output directory
  %(prog)s --workers 4                     # Use 4 worker processes
  %(prog)s --delete-source                 # Delete source folders after export
  %(prog)s --cleanup-only                  # Only delete source for already-exported recordings
        """)

    parser.add_argument('--list-clips', action='store_true',
                       help='List all available clips without processing')
    parser.add_argument('--process-all', action='store_true',
                       help='Process all clips')
    parser.add_argument('--steam-id', type=str,
                       help='Process clips for specific Steam ID')
    parser.add_argument('--game-id', type=str,
                       help='Process clips for specific game ID')
    parser.add_argument('--media-type', choices=['all', 'manual', 'background'],
                       default='all', help='Type of clips to process')
    parser.add_argument('--output', type=str,
                       help='Output directory for converted clips')
    parser.add_argument('--workers', type=int,
                       help='Number of worker processes (default: auto-detect)')
    parser.add_argument('--userdata-path', type=str,
                       help='Manual Steam userdata path')
    parser.add_argument('--detect-paths', action='store_true',
                       help='Show auto-detected Steam paths and exit')
    parser.add_argument('--delete-source', action='store_true',
                       help='Delete original Steam clip folders after successful conversion')
    parser.add_argument('--cleanup-only', action='store_true',
                       help='Only delete source folders for already-converted clips (no conversion)')

    args = parser.parse_args()

    # Initialize exporter
    exporter = SteamGameRecordingExporter(max_workers=args.workers)


    # Handle path detection
    if args.detect_paths:
        detected_paths = exporter.auto_detect_steam_paths()
        if detected_paths:
            print("Auto-detected Steam userdata paths:")
            for i, path in enumerate(detected_paths, 1):
                print(f"  {i}. {path}")
        else:
            print("No Steam userdata paths detected.")
        return

    # Find userdata path
    userdata_path = args.userdata_path or exporter.find_steam_userdata_path()
    if not userdata_path:
        print("❌ Error: Could not find Steam userdata path.")
        print()
        print("Possible solutions:")
        print("1. Use --userdata-path to specify the path manually:")
        print("   python steamexporter.py --userdata-path \"/path/to/steam/userdata\" --list-clips")
        print()
        print("2. Use --detect-paths to see what paths were checked:")
        print("   python steamexporter.py --detect-paths")
        print()
        print("3. Ensure Steam is installed and has been run at least once")
        return 1

    # Get output directory
    if args.output:
        output_dir = args.output
    else:
        # Set platform-appropriate default export path
        if platform.system() == "Darwin":  # macOS
            output_dir = os.path.expanduser("~/Desktop")
        elif platform.system() == "Windows":
            output_dir = os.path.expanduser("~/Desktop")
        else:  # Linux and other Unix-like systems
            output_dir = os.path.expanduser("~/Videos")

    # Get clip folders based on filters
    clip_folders = exporter.get_clip_folders(
        userdata_path=userdata_path,
        steam_id=args.steam_id,
        media_type=args.media_type,
        game_id=args.game_id
    )

    if not clip_folders:
        print("🔍 No clips found matching the specified criteria.")
        print()
        print("Possible reasons:")
        print("• Steam game recording is not enabled")
        print("• No clips have been recorded yet")
        print("• Clips are in a different Steam user directory")
        print()
        print("To record clips:")
        print("1. Enable 'Game Recording' in Steam Settings")
        print("2. Press F11 during gameplay for manual clips")
        print("3. Enable 'Background Recording' for automatic highlights")
        return 1

    # List clips if requested
    if args.list_clips:
        print(f"📋 Found {len(clip_folders)} clips:")
        print()
        for i, clip_folder in enumerate(clip_folders, 1):
            folder_name = os.path.basename(clip_folder)
            parts = folder_name.split('_')
            game_id = parts[1] if len(parts) >= 2 else "Unknown"
            game_name = exporter.get_game_name(game_id)

            # Extract date info for display
            if len(parts) >= 3:
                try:
                    datetime_str = parts[-2] + parts[-1]
                    dt_obj = datetime.strptime(datetime_str, "%Y%m%d%H%M%S")
                    formatted_date = dt_obj.strftime("%Y-%m-%d %H:%M:%S")
                    print(f"  {i:3d}. {game_name}")
                    print(f"       📅 {formatted_date}")
                except ValueError:
                    print(f"  {i:3d}. {game_name} - {folder_name}")
            else:
                print(f"  {i:3d}. {game_name} - {folder_name}")
        return 0

    # Cleanup mode - only delete sources for already-converted clips
    if args.cleanup_only:
        print(f"🧹 Cleanup mode: Checking {len(clip_folders)} clips for already-converted sources...")
        print()

        start_time = datetime.now()

        results = exporter.cleanup_existing_sources(clip_folders, output_dir)

        end_time = datetime.now()

        # Print summary
        print(f"\n✅ Cleanup completed in {end_time - start_time}")
        print(f"📊 Results: {len(results['deleted'])}/{results['total']} source folders deleted")

        if results['skipped']:
            print(f"⏭️  Skipped: {len(results['skipped'])}/{results['total']}")
            print("\nSkipped clips:")
            for clip_folder, reason in results['skipped']:
                print(f"  • {os.path.basename(clip_folder)}: {reason}")

        if results['deleted']:
            print(f"\n🎉 Successfully cleaned up {len(results['deleted'])} source folders!")

        return 0

    # Process clips
    if args.process_all or clip_folders:
        print(f"🚀 Processing {len(clip_folders)} clips to: {output_dir}")
        print()

        start_time = datetime.now()

        results = exporter.process_clips_batch(clip_folders, output_dir, args.delete_source)

        end_time = datetime.now()

        # Print summary
        print(f"\n✅ Processing completed in {end_time - start_time}")
        print(f"📊 Results: {len(results['successful'])}/{results['total']} successful")

        if results['failed']:
            print(f"❌ Failed: {len(results['failed'])}/{results['total']}")
            print("\nFailed clips:")
            for clip_folder, error in results['failed']:
                print(f"  • {os.path.basename(clip_folder)}: {error}")

        if results['successful']:
            print(f"\n🎉 Successfully converted {len(results['successful'])} clips!")

        return 0 if not results['failed'] else 1
    else:
        print("💡 Use --process-all to process all clips or --list-clips to see available clips.")
        return 1


if __name__ == "__main__":
    # Windows multiprocessing protection
    if platform.system() == "Windows":
        import multiprocessing
        multiprocessing.freeze_support()

    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        traceback.print_exc()
        sys.exit(1)