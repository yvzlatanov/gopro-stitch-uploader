"""Unit tests for GoPro chapter parsing and grouping (pure, no filesystem)."""

from pathlib import Path

from gopro_stitch import scanner


def P(name: str) -> Path:
    return Path("/Volumes/GoPro SD/DCIM/100GOPRO") / name


def test_parse_valid_gx_chapter():
    chapter = scanner.parse_chapter(P("GX010118.MP4"))
    assert chapter is not None
    assert chapter.prefix == "GX"
    assert chapter.chapter == 1
    assert chapter.video_id == "0118"


def test_parse_gh_prefix_supported():
    chapter = scanner.parse_chapter(P("GH020045.MP4"))
    assert chapter is not None
    assert chapter.prefix == "GH"
    assert chapter.chapter == 2
    assert chapter.video_id == "0045"


def test_parse_lowercase_extension():
    assert scanner.parse_chapter(P("GX010118.mp4")) is not None


def test_parse_rejects_non_chapter_files():
    assert scanner.parse_chapter(P("GX010118.THM")) is None
    assert scanner.parse_chapter(P("GL010118.LRV")) is None
    assert scanner.parse_chapter(P("leinfo.sav")) is None
    assert scanner.parse_chapter(P("random.MP4")) is None


def test_group_orders_chapters_and_groups_by_video():
    paths = [
        P("GX030118.MP4"),
        P("GX010118.MP4"),
        P("GX020118.MP4"),
        P("GX010119.MP4"),
        P("GX020119.MP4"),
    ]
    groups = scanner.group_chapters(paths)
    assert [g.video_id for g in groups] == ["0118", "0119"]
    first = groups[0]
    assert [c.chapter for c in first.chapters] == [1, 2, 3]
    assert first.is_contiguous
    assert not first.warnings


def test_group_ignores_sidecar_and_junk_files():
    paths = [
        P("GX010120.MP4"),
        P("GX010120.THM"),
        P("GL010120.LRV"),
        P("leinfo.sav"),
    ]
    groups = scanner.group_chapters(paths)
    assert len(groups) == 1
    assert len(groups[0].chapters) == 1


def test_missing_middle_chapter_flags_warning():
    paths = [P("GX010118.MP4"), P("GX030118.MP4")]  # chapter 2 absent
    groups = scanner.group_chapters(paths)
    assert len(groups) == 1
    assert not groups[0].is_contiguous
    assert groups[0].warnings


def test_paths_preserved_in_chapter_order():
    paths = [P("GX020118.MP4"), P("GX010118.MP4")]
    group = scanner.group_chapters(paths)[0]
    assert group.paths == [P("GX010118.MP4"), P("GX020118.MP4")]
