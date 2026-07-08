"""Discover GoPro chapter files on the SD card and group them into videos.

GoPro splits long recordings into ~4 GB chapters named like::

    GX010118.MP4   chapter 01 of video 0118
    GX020118.MP4   chapter 02 of video 0118
    GX030118.MP4   chapter 03 of video 0118

The two-letter prefix (GX/GH) identifies the encoder, the next two digits are
the chapter number, and the final four digits are the video id. Chapters that
share a video id belong to one recording and are concatenated in chapter order.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import config


@dataclass(frozen=True)
class Chapter:
    """A single GoPro chapter file."""

    path: Path
    prefix: str
    chapter: int
    video_id: str

    @property
    def size_bytes(self) -> int:
        return self.path.stat().st_size


@dataclass
class VideoGroup:
    """All chapters that make up one logical recording."""

    video_id: str
    prefix: str
    chapters: list[Chapter]
    # Populated lazily by enrich_group(); left at defaults for pure grouping.
    duration_seconds: float | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(c.size_bytes for c in self.chapters)

    @property
    def paths(self) -> list[Path]:
        return [c.path for c in self.chapters]

    @property
    def is_contiguous(self) -> bool:
        """True when chapters run 1, 2, 3 ... with no gap."""
        numbers = [c.chapter for c in self.chapters]
        return numbers == list(range(1, len(numbers) + 1))

    @property
    def recorded_mtime(self) -> float:
        """Modification time of the first chapter, used as the recording date."""
        return self.chapters[0].path.stat().st_mtime


def parse_chapter(path: Path) -> Chapter | None:
    """Parse a GoPro chapter filename, or return None if it doesn't match.

    Expected stem shape: ``<2-letter prefix><2-digit chapter><4-digit id>``.
    """
    if path.suffix.upper() != config.VIDEO_EXTENSION:
        return None
    stem = path.stem
    if len(stem) != 8:
        return None
    prefix = stem[:2].upper()
    if prefix not in config.CHAPTER_PREFIXES:
        return None
    chapter_str, video_id = stem[2:4], stem[4:8]
    if not (chapter_str.isdigit() and video_id.isdigit()):
        return None
    return Chapter(
        path=path,
        prefix=prefix,
        chapter=int(chapter_str),
        video_id=video_id,
    )


def group_chapters(paths: list[Path]) -> list[VideoGroup]:
    """Group chapter paths into videos, sorted by chapter within each group.

    Pure function over a list of paths — no filesystem calls beyond what the
    caller already did to list them — so it is straightforward to unit test.
    Groups are returned sorted by video id for stable, predictable output.
    """
    buckets: dict[str, list[Chapter]] = {}
    for path in paths:
        chapter = parse_chapter(path)
        if chapter is None:
            continue
        buckets.setdefault(chapter.video_id, []).append(chapter)

    groups: list[VideoGroup] = []
    for video_id in sorted(buckets):
        chapters = sorted(buckets[video_id], key=lambda c: c.chapter)
        group = VideoGroup(
            video_id=video_id,
            prefix=chapters[0].prefix,
            chapters=chapters,
        )
        if not group.is_contiguous:
            present = ", ".join(f"{c.chapter:02d}" for c in chapters)
            group.warnings.append(
                f"Missing chapter(s): only {present} present — join may be incomplete."
            )
        groups.append(group)
    return groups


def scan_dcim(dcim: Path) -> list[VideoGroup]:
    """Find every GoPro chapter under ``dcim`` and group them."""
    if not dcim.exists():
        raise FileNotFoundError(f"DCIM folder not found: {dcim}")
    mp4s = [p for p in sorted(dcim.iterdir()) if p.is_file()]
    return group_chapters(mp4s)


def probe_duration(path: Path) -> float | None:
    """Return a chapter's duration in seconds via ffprobe, or None on failure."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except (subprocess.CalledProcessError, KeyError, ValueError, json.JSONDecodeError):
        return None


def enrich_group(group: VideoGroup) -> VideoGroup:
    """Fill in total duration by probing each chapter. Mutates and returns group."""
    total = 0.0
    ok = True
    for chapter in group.chapters:
        seconds = probe_duration(chapter.path)
        if seconds is None:
            ok = False
            break
        total += seconds
    group.duration_seconds = total if ok else None
    return group
