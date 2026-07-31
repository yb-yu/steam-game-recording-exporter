"""Already-converted detection, source deletion and the cleanup-only mode."""

import os


class TestCheckConvertedExists:
    def test_detects_exact_match(self, exporter, named_games, output_dir):
        clip = os.path.join("anywhere", "clip_570_20250102_030405")
        target = output_dir / "Dota_2_2025-01-02_03-04-05.mp4"
        target.write_bytes(b"x")

        assert exporter.check_converted_exists(clip, str(output_dir)) == str(target)

    def test_detects_numbered_variant(self, exporter, named_games, output_dir):
        clip = os.path.join("anywhere", "clip_570_20250102_030405")
        target = output_dir / "Dota_2_2025-01-02_03-04-05_3.mp4"
        target.write_bytes(b"x")

        assert exporter.check_converted_exists(clip, str(output_dir)) == str(target)

    def test_none_when_missing(self, exporter, named_games, output_dir):
        clip = os.path.join("anywhere", "clip_570_20250102_030405")
        assert exporter.check_converted_exists(clip, str(output_dir)) is None

    def test_does_not_match_a_different_clip(self, exporter, named_games, output_dir):
        (output_dir / "Dota_2_2025-01-02_03-04-05.mp4").write_bytes(b"x")
        other = os.path.join("anywhere", "clip_570_20250109_090909")
        assert exporter.check_converted_exists(other, str(output_dir)) is None


class TestDeleteSourceFolder:
    def test_deletes_a_real_clip_folder(self, exporter, steam_tree):
        clip = steam_tree.add_clip("clip_570_20250102_030405")

        assert exporter.delete_source_folder(str(clip)) is True
        assert not clip.exists()

    def test_deletes_when_only_m4s_chunks_are_present(self, exporter, tmp_path):
        clip = tmp_path / "clip_570_20250102_030405"
        clip.mkdir()
        (clip / "chunk-stream0-00001.m4s").write_bytes(b"\x00")

        assert exporter.delete_source_folder(str(clip)) is True
        assert not clip.exists()

    def test_refuses_folders_that_are_not_recordings(self, exporter, tmp_path):
        """Safety net: without .m4s/session.mpd inside, nothing is removed."""
        folder = tmp_path / "important_docs"
        folder.mkdir()
        (folder / "taxes.pdf").write_bytes(b"x")

        assert exporter.delete_source_folder(str(folder)) is False
        assert (folder / "taxes.pdf").exists()

    def test_missing_folder_is_a_no_op_success(self, exporter, tmp_path):
        assert exporter.delete_source_folder(str(tmp_path / "gone")) is True


class TestCleanupExistingSources:
    def test_deletes_sources_that_have_an_mp4(self, exporter, named_games, steam_tree, output_dir):
        clip = steam_tree.add_clip("clip_570_20250102_030405")
        (output_dir / "Dota_2_2025-01-02_03-04-05.mp4").write_bytes(b"x")

        results = exporter.cleanup_existing_sources([str(clip)], str(output_dir))

        assert results["deleted"] == [str(clip)]
        assert results["skipped"] == []
        assert results["total"] == 1
        assert not clip.exists()

    def test_dry_run_reports_without_deleting(self, exporter, named_games, steam_tree, output_dir):
        clip = steam_tree.add_clip("clip_570_20250102_030405")
        (output_dir / "Dota_2_2025-01-02_03-04-05.mp4").write_bytes(b"x")

        results = exporter.cleanup_existing_sources([str(clip)], str(output_dir), dry_run=True)

        assert results["deleted"] == [str(clip)]
        assert clip.exists()

    def test_skips_clips_without_an_mp4(self, exporter, named_games, steam_tree, output_dir):
        clip = steam_tree.add_clip("clip_570_20250102_030405")

        results = exporter.cleanup_existing_sources([str(clip)], str(output_dir))

        assert results["deleted"] == []
        assert len(results["skipped"]) == 1
        skipped_path, reason = results["skipped"][0]
        assert skipped_path == str(clip)
        assert "Dota_2_2025-01-02_03-04-05.mp4" in reason
        assert clip.exists()

    def test_empty_input(self, exporter, output_dir):
        results = exporter.cleanup_existing_sources([], str(output_dir))
        assert results == {"deleted": [], "skipped": [], "total": 0}

    def test_mixed_batch(self, exporter, named_games, steam_tree, output_dir):
        converted = steam_tree.add_clip("clip_570_20250102_030405")
        pending = steam_tree.add_clip("clip_730_20250103_040506")
        (output_dir / "Dota_2_2025-01-02_03-04-05.mp4").write_bytes(b"x")

        results = exporter.cleanup_existing_sources(
            [str(converted), str(pending)], str(output_dir)
        )

        assert results["deleted"] == [str(converted)]
        assert [p for p, _ in results["skipped"]] == [str(pending)]
        assert not converted.exists()
        assert pending.exists()
