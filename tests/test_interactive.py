import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import steamexporter
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from typer.testing import CliRunner


def stub_logging(exporter, verbose=False):
    exporter.logger = Mock()
    exporter.log_file = "test.log"
    exporter._console_handler = Mock()


class InteractiveSessionTests(unittest.TestCase):
    @patch("steamexporter.run_export", return_value=0)
    @patch("steamexporter.questionary.confirm", return_value=object())
    @patch("steamexporter.questionary.path", return_value=object())
    @patch("steamexporter.questionary.select", return_value=object())
    @patch(
        "steamexporter._ask",
        side_effect=["export", "all", "all", "all", "output", 2, False, True],
    )
    def test_all_games_reaches_export_with_integer_worker_default(
        self, ask, select, path, confirm, run_export
    ):
        clips = [
            "clip_10_20260731_120000",
            "clip_20_20260731_120001",
        ]
        exporter = Mock(max_workers=2)
        exporter.get_clip_folders.return_value = clips
        exporter.get_game_name.side_effect = lambda game_id: f"Game {game_id}"

        result = steamexporter.interactive_session(
            exporter, "userdata", "default-output", show_progress=True
        )

        output = os.path.abspath("output")
        self.assertEqual(result, 0)
        media_choices = select.call_args_list[1].kwargs["choices"]
        self.assertEqual(select.call_args_list[1].args[0], "Which Steam recordings?")
        self.assertEqual(media_choices[0].title, "Background recordings (default)")
        self.assertIn("may be overwritten", media_choices[0].description)
        self.assertEqual(media_choices[2].title, "Saved clips (manually created)")
        self.assertIn("HDD source: 1-2", select.call_args_list[-1].args[0])
        self.assertIn("HDD sources", select.call_args_list[-1].kwargs["choices"][0].description)
        self.assertEqual(select.call_args_list[-1].kwargs["default"], 2)
        exporter.save_preferences.assert_called_once_with(output, 2)
        run_export.assert_called_once_with(exporter, clips, output, False, True)

    @patch("steamexporter.run_export", return_value=0)
    @patch("steamexporter.questionary.confirm", return_value=object())
    @patch("steamexporter.questionary.path", return_value=object())
    @patch("steamexporter.questionary.select", return_value=object())
    @patch(
        "steamexporter._ask",
        side_effect=["export", "background", "all", "output", 1,
                     steamexporter._BACK, 2, False, True],
    )
    def test_back_from_delete_returns_to_worker_screen(
        self, ask, select, path, confirm, run_export
    ):
        clips = ["clip_10_20260731_120000"]
        exporter = Mock(max_workers=1)
        exporter.get_clip_folders.return_value = clips

        result = steamexporter.interactive_session(
            exporter, "userdata", "default-output", show_progress=True
        )

        worker_prompts = [
            call.args[0] for call in select.call_args_list
            if call.args[0].startswith("How many parallel workers?")
        ]
        self.assertEqual(result, 0)
        self.assertEqual(len(worker_prompts), 2)
        run_export.assert_called_once_with(
            exporter, clips, os.path.abspath("output"), False, True
        )


class PromptNavigationTests(unittest.TestCase):
    def test_escape_returns_back_sentinel(self):
        with create_pipe_input() as pipe_input:
            question = steamexporter.questionary.select(
                "Test prompt",
                choices=["One", "Two"],
                input=pipe_input,
                output=DummyOutput(),
            )
            pipe_input.send_text("\x1b")
            answer = steamexporter._ask(question, allow_back=True)

        self.assertIs(answer, steamexporter._BACK)


class SettingsTests(unittest.TestCase):
    @patch("steamexporter.run_cleanup", return_value=0)
    def test_cli_overrides_environment_then_saved_defaults(self, run_cleanup):
        with tempfile.TemporaryDirectory() as temp:
            config_dir = os.path.join(temp, "config")
            os.makedirs(config_dir)
            saved_output = os.path.join(temp, "saved")
            env_output = os.path.join(temp, "environment")
            cli_output = os.path.join(temp, "cli")
            settings_file = os.path.join(config_dir, "settings.json")
            with open(settings_file, "w", encoding="utf-8") as f:
                steamexporter.json.dump(
                    {"output_dir": saved_output, "workers": 2}, f
                )

            userdata = os.path.join(temp, "userdata")
            dash = os.path.join(
                userdata, "123", "gamerecordings", "clips",
                "clip_570_20260731_120000", "dash",
            )
            os.makedirs(dash)
            with open(os.path.join(dash, "session.mpd"), "w", encoding="utf-8") as f:
                f.write("<MPD/>")

            cls = steamexporter.SteamGameRecordingExporter
            args = ["--userdata-path", userdata, "--cleanup-only", "--dry-run"]
            runner = CliRunner()
            with patch.object(cls, "CONFIG_DIR", config_dir), \
                    patch.object(cls, "GAME_IDS_FILE", os.path.join(config_dir, "GameIDs.json")), \
                    patch.object(cls, "SETTINGS_FILE", settings_file), \
                    patch.object(cls, "setup_logging", stub_logging):
                environment = runner.invoke(
                    steamexporter.app,
                    args,
                    env={
                        "STEAM_EXPORTER_OUTPUT_DIR": env_output,
                        "STEAM_EXPORTER_WORKERS": "3",
                    },
                )
                self.assertEqual(environment.exit_code, 0, environment.output)
                exporter, _, output, _, _ = run_cleanup.call_args.args
                self.assertEqual(output, env_output)
                self.assertEqual(exporter.max_workers, 3)

                run_cleanup.reset_mock()
                command_line = runner.invoke(
                    steamexporter.app,
                    args + ["--output", cli_output, "--workers", "1"],
                    env={
                        "STEAM_EXPORTER_OUTPUT_DIR": env_output,
                        "STEAM_EXPORTER_WORKERS": "3",
                    },
                )
                self.assertEqual(command_line.exit_code, 0, command_line.output)
                exporter, _, output, _, _ = run_cleanup.call_args.args
                self.assertEqual(output, cli_output)
                self.assertEqual(exporter.max_workers, 1)

                run_cleanup.reset_mock()
                saved = runner.invoke(
                    steamexporter.app,
                    args,
                    env={
                        "STEAM_EXPORTER_OUTPUT_DIR": "",
                        "STEAM_EXPORTER_WORKERS": "",
                    },
                )
                self.assertEqual(saved.exit_code, 0, saved.output)
                exporter, _, output, _, _ = run_cleanup.call_args.args
                self.assertEqual(output, saved_output)
                self.assertEqual(exporter.max_workers, 2)

    def test_environment_default_does_not_start_a_noninteractive_export(self):
        with tempfile.TemporaryDirectory() as temp:
            cls = steamexporter.SteamGameRecordingExporter
            config_dir = os.path.join(temp, "config")
            with patch.object(cls, "CONFIG_DIR", config_dir), \
                    patch.object(cls, "GAME_IDS_FILE", os.path.join(config_dir, "GameIDs.json")), \
                    patch.object(cls, "SETTINGS_FILE", os.path.join(config_dir, "settings.json")), \
                    patch.object(cls, "setup_logging", stub_logging):
                result = CliRunner().invoke(
                    steamexporter.app,
                    ["--userdata-path", temp],
                    env={"STEAM_EXPORTER_OUTPUT_DIR": os.path.join(temp, "output")},
                )

        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("No action given", result.output)


class ActiveRecordingTests(unittest.TestCase):
    def setUp(self):
        self.exporter = steamexporter.SteamGameRecordingExporter.__new__(
            steamexporter.SteamGameRecordingExporter
        )
        self.exporter.logger = Mock()
        self.exporter.max_workers = 2

    @staticmethod
    def make_recording(root, name, active=False):
        folder = os.path.join(root, name)
        dash = os.path.join(folder, "dash")
        os.makedirs(dash)
        for filename in (
            "session.mpd",
            "init-stream0.m4s",
            "init-stream1.m4s",
            "chunk-stream0-00001.m4s",
            "chunk-stream1-00001.m4s",
        ):
            with open(os.path.join(dash, filename), "wb") as f:
                f.write(b"data")
        if active:
            with open(os.path.join(dash, "chunk-stream0-00002.m4s.tmp"), "wb") as f:
                f.write(b"still recording")
        return folder

    def test_active_video_store_skips_background_but_not_saved_clips(self):
        with tempfile.TemporaryDirectory() as temp:
            video = os.path.join(temp, "video")
            clips = os.path.join(temp, "clips")
            old_background = self.make_recording(video, "bg_10_20260731_120000")
            self.make_recording(video, "bg_20_20260731_120001", active=True)
            saved_clip = self.make_recording(clips, "clip_10_20260731_120002")
            output = os.path.join(temp, "output")
            self.exporter.describe_clip = Mock(side_effect=os.path.basename)
            self.exporter.process_single_clip = Mock(return_value=(True, "done"))

            results = self.exporter.process_clips_batch(
                [old_background, saved_clip], output, show_progress=False
            )

            self.assertEqual(results["successful"], [saved_clip])
            self.assertEqual(results["failed"], [])
            self.assertEqual(results["skipped"][0][0], old_background)
            self.assertEqual(self.exporter.process_single_clip.call_count, 1)
            self.assertEqual(self.exporter.process_single_clip.call_args.args[0], saved_clip)

    def test_active_recording_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = self.make_recording(
                os.path.join(temp, "video"), "bg_10_20260731_120000", active=True
            )

            self.assertFalse(self.exporter.delete_source_folder(folder))
            self.assertTrue(os.path.isdir(folder))
            self.exporter.logger.warning.assert_called_once()

    def test_chunk_disappearing_during_read_is_skipped_and_temp_is_cleaned(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = self.make_recording(
                os.path.join(temp, "clips"), "clip_10_20260731_120000"
            )
            missing = os.path.join(folder, "dash", "chunk-stream0-00001.m4s")
            output = os.path.join(temp, "output")
            def fail_after_chunk_disappears(*args, **kwargs):
                os.remove(missing)
                raise steamexporter.subprocess.CalledProcessError(
                    1, args[0], stderr=f"No such file or directory: {missing}"
                )

            self.exporter.check_converted_exists = Mock(return_value=None)
            self.exporter.get_clip_duration = Mock(return_value=0.0)
            self.exporter.get_game_name = Mock(return_value="Test Game")
            self.exporter._run_ffmpeg = Mock(side_effect=fail_after_chunk_disappears)
            success, message = self.exporter.process_single_clip(folder, output)

            self.assertIsNone(success)
            self.assertIn("changed while FFmpeg was reading", message)
            ffmpeg_command = self.exporter._run_ffmpeg.call_args.args[0]
            self.assertTrue(ffmpeg_command[ffmpeg_command.index('-i') + 1].startswith('concatf:'))
            self.assertFalse(os.path.exists(os.path.join(output, ".temp")))

    def test_multi_session_recording_keeps_concat_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = os.path.join(temp, "clips", "clip_10_20260731_120000")
            for session in ("session-1", "session-2"):
                dash = os.path.join(folder, session)
                os.makedirs(dash)
                for filename in (
                    "session.mpd",
                    "init-stream0.m4s",
                    "init-stream1.m4s",
                    "chunk-stream0-00001.m4s",
                    "chunk-stream1-00001.m4s",
                ):
                    with open(os.path.join(dash, filename), "wb") as f:
                        f.write(b"data")

            output = os.path.join(temp, "output")
            self.exporter.check_converted_exists = Mock(return_value=None)
            self.exporter.get_clip_duration = Mock(return_value=0.0)
            self.exporter.get_game_name = Mock(return_value="Test Game")

            def fake_ffmpeg(command, *args):
                with open(command[-1], "wb") as f:
                    f.write(b"output")

            self.exporter._run_ffmpeg = Mock(side_effect=fake_ffmpeg)
            success, _ = self.exporter.process_single_clip(folder, output)

            self.assertTrue(success)
            self.assertEqual(self.exporter._run_ffmpeg.call_count, 3)
            first_command = self.exporter._run_ffmpeg.call_args_list[0].args[0]
            self.assertIn("concat", first_command)
            self.assertFalse(any(part.startswith("concatf:") for part in first_command))


if __name__ == "__main__":
    unittest.main()
