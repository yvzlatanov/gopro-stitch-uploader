"""Central configuration: paths, constants, and small helpers."""

from __future__ import annotations

import os
from pathlib import Path

# Default location a GoPro SD card mounts to on macOS. Override with --dcim.
DEFAULT_DCIM = Path("/Volumes/GoPro SD/DCIM/100GOPRO")

# Where OAuth artifacts live. token.json is written after first consent.
CONFIG_DIR = Path(
    os.environ.get("GOPRO_STITCH_CONFIG_DIR", Path.home() / ".config" / "gopro-stitch")
)
TOKEN_PATH = CONFIG_DIR / "token.json"

# client_secret.json downloaded from Google Cloud (OAuth desktop client).
# Overridable so the user can point at wherever they saved it.
CLIENT_SECRET_PATH = Path(
    os.environ.get("GOPRO_STITCH_CLIENT_SECRET", CONFIG_DIR / "client_secret.json")
)

# Only scope we need: upload on the user's behalf.
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"

YOUTUBE_RESUMABLE_URL = "https://www.googleapis.com/upload/youtube/v3/videos"

# Google requires resumable chunks to be a multiple of 256 KB. We buffer a full
# chunk in RAM so we can replay it if the server reports a partial write (the
# ffmpeg pipe cannot seek backwards). 256 MB keeps request overhead low while
# staying comfortably within memory on a laptop.
CHUNK_SIZE = 256 * 1024 * 1024  # must stay a multiple of 256 * 1024
UPLOAD_GRANULARITY = 256 * 1024

# Chunks the reader thread may run ahead of the uploader. Each queued chunk is
# CHUNK_SIZE of RAM on top of the one currently being sent, so 1 means ffmpeg
# fills the next chunk while the current one is in flight at a peak of two.
READ_AHEAD_CHUNKS = 1

# Stints uploaded at the same time. One is the safe default: concurrent uploads
# share the same uplink, so this only helps when a single stream cannot
# saturate it. Peak memory is roughly parallel * (READ_AHEAD_CHUNKS + 1) chunks.
DEFAULT_PARALLEL_UPLOADS = 1


def normalize_chunk_size(megabytes: int) -> int:
    """Convert a chunk size in MB to bytes, enforcing the 256 KB granularity."""
    if megabytes < 1:
        raise ValueError("chunk size must be at least 1 MB")
    size = megabytes * 1024 * 1024
    if size % UPLOAD_GRANULARITY:
        raise ValueError(
            f"chunk size must be a multiple of {UPLOAD_GRANULARITY // 1024} KB"
        )
    return size

# GoPro chapter file prefixes: GX = HERO6+ (HEVC), GH = older HERO models.
CHAPTER_PREFIXES = ("GX", "GH")
VIDEO_EXTENSION = ".MP4"

# Networking retry policy for transient upload failures. A multi-hour upload
# has to survive more than a momentary blip: at six attempts the backoff runs
# out after about a minute, which is long enough to lose hours of work to a
# brief outage. The window bounds how long a single chunk may keep retrying.
MAX_RETRIES = 10
INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0
RETRY_WINDOW_SECONDS = 900.0

# Without a timeout a half-open connection blocks the upload forever: the
# socket never errors, so no retry is triggered and the transfer simply stops.
# While a chunk is being sent the *connect* timeout is the one in force, applied
# per block written (see uploader._put_chunk), so it bounds a stalled send
# rather than the time a whole chunk takes; the socket timeout then bounds the
# wait for the server's reply.
CONNECT_TIMEOUT_SECONDS = 15.0
SOCKET_TIMEOUT_SECONDS = 120.0
REQUEST_TIMEOUT = (CONNECT_TIMEOUT_SECONDS, SOCKET_TIMEOUT_SECONDS)


def ensure_config_dir() -> None:
    """Create the config directory with private permissions if missing."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        CONFIG_DIR.chmod(0o700)
    except OSError:
        # Non-fatal: some filesystems don't support chmod.
        pass
