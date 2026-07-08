"""Losslessly concatenate GoPro chapters into a single MP4 stream on stdout.

The whole point of this tool is to never write the combined video to disk, so
ffmpeg streams a fragmented MP4 to ``pipe:1`` and the uploader reads it chunk by
chunk. ``-c copy`` means no re-encoding: the join runs at disk-read speed and is
bit-for-bit lossless.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .scanner import VideoGroup


class StitchError(RuntimeError):
    """Raised when ffmpeg/ffprobe is missing or the concat process fails."""


def check_ffmpeg_available() -> None:
    """Verify ffmpeg and ffprobe are on PATH, else raise a helpful error."""
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if missing:
        raise StitchError(
            f"Required tool(s) not found on PATH: {', '.join(missing)}. "
            "Install with: brew install ffmpeg"
        )


def _write_concat_list(group: VideoGroup, directory: Path) -> Path:
    """Write ffmpeg's concat demuxer list file. Tiny — the only disk artifact."""
    list_path = directory / f"concat_{group.video_id}.txt"
    lines = []
    for chapter in group.chapters:
        # The concat demuxer requires single quotes escaped as '\''.
        safe = str(chapter.path).replace("'", "'\\''")
        lines.append(f"file '{safe}'")
    list_path.write_text("\n".join(lines) + "\n")
    return list_path


def build_ffmpeg_command(list_path: Path) -> list[str]:
    """Return the ffmpeg argv that concatenates to a fragmented MP4 on stdout.

    - ``-c copy``: stream copy, no re-encode (lossless, fast).
    - ``-map 0:v:0 -map 0:a:0``: keep only video + audio, dropping GoPro's
      telemetry (gpmd) and timecode streams that YouTube ignores and that trip
      up concat with DTS warnings.
    - ``-write_tmcd 0``: don't emit the auto-generated timecode track YouTube
      would ignore anyway, leaving a clean video+audio stream.
    - ``+frag_keyframe+empty_moov``: fragmented MP4 so the moov atom doesn't need
      a final seek — required to write MP4 to a non-seekable pipe.
    """
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-c",
        "copy",
        "-write_tmcd",
        "0",
        "-movflags",
        "+frag_keyframe+empty_moov+default_base_moof",
        "-f",
        "mp4",
        "pipe:1",
    ]


@contextmanager
def stitch_stream(group: VideoGroup) -> Iterator[subprocess.Popen]:
    """Context manager yielding a running ffmpeg process producing MP4 on stdout.

    The caller reads from ``proc.stdout``. On exit we verify ffmpeg's return
    code and surface any stderr; a nonzero exit raises StitchError so a broken
    stream can never be silently uploaded. The concat list file is cleaned up
    automatically.
    """
    check_ffmpeg_available()
    with tempfile.TemporaryDirectory(prefix="gopro-stitch-") as tmp:
        list_path = _write_concat_list(group, Path(tmp))
        cmd = build_ffmpeg_command(list_path)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        try:
            yield proc
        finally:
            # If the consumer stopped early, make sure ffmpeg doesn't linger.
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


def finalize(proc: subprocess.Popen) -> None:
    """Drain ffmpeg's stderr and raise if it exited nonzero.

    Call this after the stdout stream reaches EOF and the upload finished, so a
    concat failure that produced a truncated stream is turned into an error
    instead of a silently incomplete upload.
    """
    stderr_output = b""
    if proc.stderr is not None:
        stderr_output = proc.stderr.read()
    return_code = proc.wait()
    if return_code != 0:
        message = stderr_output.decode("utf-8", errors="replace").strip()
        raise StitchError(
            f"ffmpeg exited with code {return_code}: {message or '(no stderr)'}"
        )
