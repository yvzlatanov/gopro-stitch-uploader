"""Command-line entry point: scan the SD card, then stitch + upload each video.

Flow:
    1. Scan the DCIM folder and group chapters into recordings.
    2. Show the list (chapters, size, duration, date) and ask which to upload.
    3. For each chosen recording, prompt for a title/description, then stitch the
       chapters with ffmpeg and stream the result straight to YouTube.
Nothing is ever written to disk except a tiny ffmpeg concat list, and the SD
card is never modified.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import requests

from . import auth, config, scanner, stitcher, uploader
from .scanner import VideoGroup


# ---------- formatting helpers ----------

def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def recorded_date(group: VideoGroup) -> str:
    return dt.datetime.fromtimestamp(group.recorded_mtime).strftime("%Y-%m-%d")


# ---------- interactive helpers ----------

def print_groups(groups: list[VideoGroup]) -> None:
    print(f"\nFound {len(groups)} recording(s):\n")
    for index, group in enumerate(groups, start=1):
        chapters = len(group.chapters)
        line = (
            f"  [{index}] {group.prefix}{group.video_id} — "
            f"{chapters} chapter{'s' if chapters != 1 else ''}, "
            f"{human_size(group.total_bytes)}, "
            f"{human_duration(group.duration_seconds)}, "
            f"recorded {recorded_date(group)}"
        )
        print(line)
        for warning in group.warnings:
            print(f"        ⚠ {warning}")
    print()


def parse_selection(raw: str, count: int) -> list[int]:
    """Turn '1,3' / 'all' / 'skip' into a list of 0-based indexes."""
    raw = raw.strip().lower()
    if raw in ("", "skip", "none", "q", "quit"):
        return []
    if raw == "all":
        return list(range(count))
    chosen: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part.isdigit():
            raise ValueError(f"Not a number: {part!r}")
        number = int(part)
        if not 1 <= number <= count:
            raise ValueError(f"Out of range: {number}")
        if number - 1 not in chosen:
            chosen.append(number - 1)
    return chosen


def prompt_selection(count: int) -> list[int]:
    while True:
        raw = input("Which to upload? [all / e.g. 1,3 / skip]: ")
        try:
            return parse_selection(raw, count)
        except ValueError as exc:
            print(f"  {exc}. Try again.")


def prompt_metadata(group: VideoGroup) -> uploader.VideoMetadata | None:
    print(f"\n— {group.prefix}{group.video_id} —")
    title = input("  Title (required, blank to skip this video): ").strip()
    if not title:
        print("  Skipped.")
        return None
    print("  Description (optional). End with a single '.' on its own line:")
    lines: list[str] = []
    while True:
        try:
            line = input("  > ")
        except EOFError:
            break
        if line.strip() == ".":
            break
        lines.append(line)
    return uploader.VideoMetadata(title=title, description="\n".join(lines))


# ---------- progress ----------

class ProgressPrinter:
    """Single-line byte progress written to stderr."""

    def __init__(self, estimated_total: int) -> None:
        self.estimated_total = estimated_total

    def __call__(self, uploaded: int) -> None:
        if self.estimated_total > 0:
            pct = min(100.0, uploaded / self.estimated_total * 100)
            bar = f"{pct:5.1f}%"
        else:
            bar = "  ?  "
        sys.stderr.write(
            f"\r    uploading {human_size(uploaded)} / "
            f"~{human_size(self.estimated_total)} ({bar})   "
        )
        sys.stderr.flush()


# ---------- upload one group ----------

def upload_group(
    group: VideoGroup,
    metadata: uploader.VideoMetadata,
    session: requests.Session,
) -> None:
    """Stitch a group and stream it to YouTube, printing the resulting link."""
    creds = auth.get_credentials()
    upload_url = uploader.start_session(
        session, auth.bearer_header(creds), metadata
    )
    progress = ProgressPrinter(group.total_bytes)

    with stitcher.stitch_stream(group) as proc:
        assert proc.stdout is not None
        video_json = uploader.stream_upload(
            session, upload_url, proc.stdout, on_progress=progress
        )
        # Confirm ffmpeg finished cleanly before we trust the upload.
        stitcher.finalize(proc)

    sys.stderr.write("\n")
    print(f"    ✓ {uploader.video_url(video_json)}")


# ---------- dry run ----------

def show_ffmpeg_commands(groups: list[VideoGroup], selected: list[int]) -> None:
    print("\nDry run — commands that would run (no upload, no auth):\n")
    for index in selected:
        group = groups[index]
        files = " ".join(f"'{p}'" for p in group.paths)
        print(f"  {group.prefix}{group.video_id}: concat {len(group.chapters)} file(s)")
        print(f"    inputs: {files}")
        print(
            "    ffmpeg -f concat -safe 0 -i <list> -map 0:v:0 -map 0:a:0 "
            "-c copy -movflags +frag_keyframe+empty_moov -f mp4 pipe:1  ->  YouTube\n"
        )


# ---------- main ----------

AUDIT_WARNING = (
    "NOTE: Until your Google Cloud project passes the YouTube API audit, uploads "
    "from it are locked to PRIVATE regardless of the 'unlisted' request. Apply for "
    "the audit to make unlisted stick (see README)."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gopro-stitch",
        description="Stitch GoPro chapters and stream them to YouTube as unlisted.",
    )
    parser.add_argument(
        "--dcim",
        type=Path,
        default=config.DEFAULT_DCIM,
        help=f"GoPro DCIM folder (default: {config.DEFAULT_DCIM})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List groups and show ffmpeg commands without uploading.",
    )
    parser.add_argument(
        "--no-probe",
        action="store_true",
        help="Skip ffprobe duration lookup (faster listing).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        stitcher.check_ffmpeg_available()
    except stitcher.StitchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        groups = scanner.scan_dcim(args.dcim)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not groups:
        print(f"No GoPro chapter files found in {args.dcim}.")
        return 0

    if not args.no_probe:
        print("Probing durations…", file=sys.stderr)
        for group in groups:
            scanner.enrich_group(group)

    print_groups(groups)
    selected = prompt_selection(len(groups))
    if not selected:
        print("Nothing selected. Done.")
        return 0

    if args.dry_run:
        show_ffmpeg_commands(groups, selected)
        return 0

    print(AUDIT_WARNING)

    uploaded_bytes = 0
    with requests.Session() as session:
        for index in selected:
            group = groups[index]
            metadata = prompt_metadata(group)
            if metadata is None:
                continue
            try:
                upload_group(group, metadata, session)
                uploaded_bytes += group.total_bytes
            except (uploader.UploadError, stitcher.StitchError, auth.AuthError) as exc:
                print(f"    ✗ Failed: {exc}", file=sys.stderr)

    if uploaded_bytes:
        print(
            f"\nDone. Uploaded {human_size(uploaded_bytes)} of source footage. "
            "The SD card was not modified — clear those files yourself after "
            "checking the videos play correctly on YouTube."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
