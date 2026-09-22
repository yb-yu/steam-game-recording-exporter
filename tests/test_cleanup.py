"""Already-converted detection and safe source cleanup."""

import os

import pytest


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


@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize("suffix", ["", "_3"])
def test_layout_switch_finds_existing_exports(exporter, named_games, output_dir, grouped, suffix):
    exporter.group_by_game = grouped
    clip = os.path.join("anywhere", "clip_570_20250102_030405")
    # The existing export is in the opposite layout to the current setting.
    directory = output_dir if grouped else output_dir / "Dota_2"
    directory.mkdir(exist_ok=True)
    existing = directory / f"Dota_2_2025-01-02_03-04-05{suffix}.mp4"
    existing.write_bytes(b"export")

    assert exporter.check_converted_exists(clip, str(output_dir)) == str(existing)


@pytest.mark.parametrize("dry_run", [False, True])
def test_cleanup_finds_mixed_layouts_and_keeps_unexported_sources(
    exporter, named_games, steam_tree, output_dir, dry_run
):
    flat = steam_tree.add_clip("clip_570_20250102_030405")
    grouped = steam_tree.add_clip("clip_730_20250103_040506")
    pending = steam_tree.add_clip("clip_730_20250104_040506")
    (output_dir / "Dota_2_2025-01-02_03-04-05.mp4").write_bytes(b"flat")
    game_folder = output_dir / "Counter-Strike_2"
    game_folder.mkdir()
    (game_folder / "Counter-Strike_2_2025-01-03_04-05-06_2.mp4").write_bytes(b"grouped")
    # Directories and unrelated nested exports must not count as converted MP4s.
    (game_folder / "Counter-Strike_2_2025-01-04_04-05-06.mp4").mkdir()
    (game_folder / "Counter-Strike_2_2025-01-04_04-05-06_1.mp4").mkdir()
    unrelated = output_dir / "Other Game"
    unrelated.mkdir()
    (unrelated / "Counter-Strike_2_2025-01-04_04-05-06.mp4").write_bytes(b"unrelated")

    results = exporter.cleanup_existing_sources(
        [str(flat), str(grouped), str(pending)], str(output_dir), dry_run=dry_run
    )

    assert results["deleted"] == [str(flat), str(grouped)]
    assert [path for path, _ in results["skipped"]] == [str(pending)]
    assert flat.exists() == dry_run and grouped.exists() == dry_run
    assert pending.exists()
