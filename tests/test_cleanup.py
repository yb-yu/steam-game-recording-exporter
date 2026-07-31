"""Already-converted detection and safe source cleanup."""

import os


def test_finds_exact_and_numbered_exports(exporter, named_games, output_dir):
    clip = os.path.join("anywhere", "clip_570_20250102_030405")
    exact = output_dir / "Dota_2_2025-01-02_03-04-05.mp4"
    exact.write_bytes(b"x")
    assert exporter.check_converted_exists(clip, str(output_dir)) == str(exact)

    exact.unlink()
    numbered = output_dir / "Dota_2_2025-01-02_03-04-05_3.mp4"
    numbered.write_bytes(b"x")
    assert exporter.check_converted_exists(clip, str(output_dir)) == str(numbered)
    assert exporter.check_converted_exists(
        os.path.join("anywhere", "clip_570_20250109_090909"), str(output_dir)
    ) is None


def test_deletes_a_recording_folder(exporter, steam_tree):
    clip = steam_tree.add_clip("clip_570_20250102_030405")
    assert exporter.delete_source_folder(str(clip)) is True
    assert not clip.exists()


def test_refuses_to_delete_an_unrelated_folder(exporter, tmp_path):
    folder = tmp_path / "important_docs"
    folder.mkdir()
    document = folder / "taxes.pdf"
    document.write_bytes(b"x")

    assert exporter.delete_source_folder(str(folder)) is False
    assert document.exists()


def test_cleanup_dry_run_keeps_the_source(exporter, named_games, steam_tree, output_dir):
    clip = steam_tree.add_clip("clip_570_20250102_030405")
    (output_dir / "Dota_2_2025-01-02_03-04-05.mp4").write_bytes(b"x")

    results = exporter.cleanup_existing_sources(
        [str(clip)], str(output_dir), dry_run=True
    )

    assert results["deleted"] == [str(clip)]
    assert clip.exists()


def test_cleanup_deletes_converted_and_skips_pending(
    exporter, named_games, steam_tree, output_dir
):
    converted = steam_tree.add_clip("clip_570_20250102_030405")
    pending = steam_tree.add_clip("clip_730_20250103_040506")
    (output_dir / "Dota_2_2025-01-02_03-04-05.mp4").write_bytes(b"x")

    results = exporter.cleanup_existing_sources(
        [str(converted), str(pending)], str(output_dir)
    )

    assert results["deleted"] == [str(converted)]
    assert [path for path, _ in results["skipped"]] == [str(pending)]
    assert not converted.exists()
    assert pending.exists()
