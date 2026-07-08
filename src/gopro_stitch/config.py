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

# GoPro chapter file prefixes: GX = HERO6+ (HEVC), GH = older HERO models.
CHAPTER_PREFIXES = ("GX", "GH")
VIDEO_EXTENSION = ".MP4"

# Networking retry policy for transient upload failures.
MAX_RETRIES = 6
INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0


def ensure_config_dir() -> None:
    """Create the config directory with private permissions if missing."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        CONFIG_DIR.chmod(0o700)
    except OSError:
        # Non-fatal: some filesystems don't support chmod.
        pass
