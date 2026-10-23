"""Command-line entry point: scan the SD card, then stitch + upload each video.

Flow:
    1. Scan the DCIM folder and group chapters into recordings.
    2. Show the list (chapters, size, duration, date) and ask which to upload.
    3. Prompt for a title/description for each chosen recording.
    4. Stitch the chapters with ffmpeg and stream the result straight to
       YouTube, optionally several recordings at a time.
Nothing is ever written to disk except a tiny ffmpeg concat list, and the SD
card is never modified.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import datetime as dt
import logging
import sys
import threading
import time
from dataclasses import dataclass
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

REPAINT_INTERVAL_SECONDS = 0.5
LOG_PERCENT_STEP = 1.0


class ProgressReporter:
    """Byte progress for one or more concurrent uploads, safe across threads.

    On a terminal the whole block of rows is repainted in place. When stderr is
    redirected (a log file, ``tee``) cursor movement would be noise, so each row
    is appended as a plain line every ``LOG_PERCENT_STEP`` percent instead.
    """

    def __init__(self, labels: list[str], totals: list[int], stream=None) -> None:
        self._labels = labels
        self._totals = totals
        self._uploaded = [0] * len(labels)
        self._status: list[str | None] = [None] * len(labels)
        self._stream = stream if stream is not None else sys.stderr
        self._lock = threading.Lock()
        self._tty = bool(getattr(self._stream, "isatty", lambda: False)())
        self._rows_painted = 0
        self._last_paint = 0.0
        self._last_logged = [-LOG_PERCENT_STEP] * len(labels)

    def callback(self, slot: int) -> uploader.ProgressCallback:
        """Return an on_progress callback bound to one upload slot."""

        def report(uploaded: int) -> None:
            with self._lock:
                self._uploaded[slot] = uploaded
                self._emit(slot)

        return report

    def finish(self, slot: int, status: str) -> None:
        """Replace a slot's progress with its final one-line outcome."""
        with self._lock:
            self._status[slot] = status
            self._emit(slot, force=True)

    def _row(self, slot: int) -> str:
        if self._status[slot] is not None:
            return f"{self._labels[slot]}  {self._status[slot]}"
        total = self._totals[slot]
        uploaded = self._uploaded[slot]
        share = f"{self._percent(slot):5.1f}%" if total > 0 else "  ?  "
        return (
            f"{self._labels[slot]}  uploading {human_size(uploaded)} / "
            f"~{human_size(total)} ({share})"
        )

    def _percent(self, slot: int) -> float:
        total = self._totals[slot]
        if total <= 0:
            return 0.0
        return min(100.0, self._uploaded[slot] / total * 100)

    def _emit(self, slot: int, force: bool = False) -> None:
        if self._tty:
            self._repaint(force)
        else:
            self._log(slot, force)

    def _repaint(self, force: bool) -> None:
        now = time.monotonic()
        if not force and now - self._last_paint < REPAINT_INTERVAL_SECONDS:
            return
        self._last_paint = now
        parts = []
        if self._rows_painted:
            parts.append(f"\033[{self._rows_painted}A")
        for slot in range(len(self._labels)):
            parts.append(f"\r\033[K    {self._row(slot)}\n")
        self._stream.write("".join(parts))
        self._stream.flush()
        self._rows_painted = len(self._labels)

    def _log(self, slot: int, force: bool) -> None:
        percent = self._percent(slot)
        if not force and percent - self._last_logged[slot] < LOG_PERCENT_STEP:
            return
        self._last_logged[slot] = percent
        self._stream.write(f"    {self._row(slot)}\n")
        self._stream.flush()

    def message(self, text: str) -> None:
        """Print a one-off note without corrupting the rows painted in place."""
        with self._lock:
            if not (self._tty and self._rows_painted):
                self._stream.write(f"  {text}\n")
                self._stream.flush()
                return
            # Write the note over the top row, then draw the rows again below it.
            self._stream.write(f"\033[{self._rows_painted}A\r\033[K  {text}\n")
            self._rows_painted = 0
            self._repaint(force=True)


class ProgressLogHandler(logging.Handler):
    """Route log records (e.g. upload retries) through the progress display."""

    def __init__(self, reporter: ProgressReporter) -> None:
        super().__init__(level=logging.WARNING)
        self._reporter = reporter

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._reporter.message(f"⚠ {self.format(record)}")
        except Exception:
            self.handleError(record)


# ---------- upload one group ----------

@dataclass
class UploadJob:
    """One recording queued for upload, with the slot it reports progress on."""

    slot: int
    group: VideoGroup
    metadata: uploader.VideoMetadata


def upload_job(
    job: UploadJob,
    creds,
    session: requests.Session,
    progress: ProgressReporter,
    chunk_size: int,
    read_ahead: int,
) -> str | None:
    """Stitch a job and stream it to YouTube. Returns the URL, or None on failure."""
    try:
        upload_url = uploader.start_session(
            session, auth.bearer_header(creds), job.metadata
        )
        with stitcher.stitch_stream(job.group) as proc:
            assert proc.stdout is not None
            video_json = uploader.stream_upload(
                session,
                upload_url,
                proc.stdout,
                on_progress=progress.callback(job.slot),
                chunk_size=chunk_size,
                read_ahead=read_ahead,
            )
            # Confirm ffmpeg finished cleanly before we trust the upload.
            stitcher.finalize(proc)
    except (uploader.UploadError, stitcher.StitchError, auth.AuthError) as exc:
        progress.finish(job.slot, f"✗ Failed: {exc}")
        return None

    url = uploader.video_url(video_json)
    progress.finish(job.slot, f"✓ {url}")
    return url


def plan_parallelism(requested: int, job_count: int) -> int:
    """Clamp the requested worker count to something sensible for this run."""
    return max(1, min(requested, job_count))


def run_jobs(
    jobs: list[UploadJob],
    creds,
    progress: ProgressReporter,
    parallel: int,
    chunk_size: int,
    read_ahead: int,
) -> list[str | None]:
    """Upload every job, up to ``parallel`` at a time. Order of results matches jobs."""
    if parallel <= 1:
        with requests.Session() as session:
            return [
                upload_job(job, creds, session, progress, chunk_size, read_ahead)
                for job in jobs
            ]

    def worker(job: UploadJob) -> str | None:
        # requests.Session isn't thread-safe, so each worker gets its own.
        with requests.Session() as session:
            return upload_job(job, creds, session, progress, chunk_size, read_ahead)

    with futures.ThreadPoolExecutor(
        max_workers=parallel, thread_name_prefix="gopro-upload"
    ) as pool:
        return list(pool.map(worker, jobs))


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
    parser.add_argument(
        "--parallel",
        type=int,
        default=config.DEFAULT_PARALLEL_UPLOADS,
        metavar="N",
        help=(
            "Upload N recordings at the same time (default: "
            f"{config.DEFAULT_PARALLEL_UPLOADS}). Only helps when one upload "
            "cannot saturate your uplink; they share the same bandwidth."
        ),
    )
    parser.add_argument(
        "--chunk-mb",
        type=int,
        default=config.CHUNK_SIZE // (1024 * 1024),
        metavar="MB",
        help=(
            "Resumable upload chunk size in MB (default: "
            f"{config.CHUNK_SIZE // (1024 * 1024)}). Lower it when running "
            "several uploads in parallel to cap memory."
        ),
    )
    parser.add_argument(
        "--read-ahead",
        type=int,
        default=config.READ_AHEAD_CHUNKS,
        metavar="N",
        help=(
            "Chunks to buffer ahead of the upload so ffmpeg keeps reading "
            f"during each request (default: {config.READ_AHEAD_CHUNKS}, "
            "0 disables). Peak memory is (N + 1) chunks per upload."
        ),
    )
    parser.add_argument(
        "--test-auth",
        action="store_true",
        help=(
            "Run only the Google OAuth consent flow, then exit. "
            "No SD card scan, no stitching, no upload — useful for confirming "
            "credentials work or for capturing a consent-screen screenshot."
        ),
    )
    return parser


def run_test_auth() -> int:
    print("Requesting OAuth credentials — a browser window will open for consent…")
    try:
        creds = auth.get_credentials()
    except auth.AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"✓ Authenticated. Token cached at {config.TOKEN_PATH}")
    print(f"  Scope: {config.YOUTUBE_UPLOAD_SCOPE}")
    print(f"  Token valid: {creds.valid}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.test_auth:
        return run_test_auth()

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

    try:
        chunk_size = config.normalize_chunk_size(args.chunk_mb)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    read_ahead = max(0, args.read_ahead)

    print(AUDIT_WARNING)

    # Ask for every title up front so parallel uploads never fight over stdin,
    # and so a long run doesn't stop halfway waiting for input.
    jobs: list[UploadJob] = []
    for index in selected:
        group = groups[index]
        metadata = prompt_metadata(group)
        if metadata is None:
            continue
        jobs.append(UploadJob(slot=len(jobs), group=group, metadata=metadata))

    if not jobs:
        print("Nothing to upload. Done.")
        return 0

    # Authenticate once: workers share the credentials rather than racing to
    # refresh them (or each opening a browser).
    try:
        creds = auth.get_credentials()
    except auth.AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    parallel = plan_parallelism(args.parallel, len(jobs))
    if parallel > 1:
        print(f"\nUploading {len(jobs)} recording(s), {parallel} at a time.\n")

    progress = ProgressReporter(
        labels=[f"{job.group.prefix}{job.group.video_id}" for job in jobs],
        totals=[job.group.total_bytes for job in jobs],
    )
    # Retries are otherwise silent: a stuck upload would look frozen in the log.
    package_log = logging.getLogger("gopro_stitch")
    log_handler = ProgressLogHandler(progress)
    package_log.addHandler(log_handler)
    try:
        results = run_jobs(jobs, creds, progress, parallel, chunk_size, read_ahead)
    finally:
        package_log.removeHandler(log_handler)

    uploaded_bytes = sum(
        job.group.total_bytes for job, url in zip(jobs, results) if url is not None
    )

    if uploaded_bytes:
        print(
            f"\nDone. Uploaded {human_size(uploaded_bytes)} of source footage. "
            "The SD card was not modified — clear those files yourself after "
            "checking the videos play correctly on YouTube."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
