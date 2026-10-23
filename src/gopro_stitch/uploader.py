"""Stream a video into a YouTube resumable upload without a local file.

We deliberately do NOT use google-api-python-client's MediaFileUpload: it needs
a seekable file, and our source is an ffmpeg pipe of unknown length. Instead we
speak the resumable protocol directly with ``requests``:

1. POST to open a session -> get an upload URL from the ``Location`` header.
2. PUT the body in fixed chunks with ``Content-Range`` headers. Because the
   total size isn't known until the stream ends, non-final chunks use
   ``bytes {start}-{end}/*`` and the final chunk carries the real total.
3. Each chunk is buffered in RAM so it can be re-sent if the network drops or
   the server acknowledges only part of it (the pipe cannot seek backwards).

A background thread reads the next chunk out of the pipe while the current one
is being sent, so ffmpeg keeps working during the PUT instead of blocking on a
full pipe for its entire duration.
"""

from __future__ import annotations

import io
import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import BinaryIO, Callable, Protocol

import requests

from . import config

log = logging.getLogger(__name__)

class SessionLike(Protocol):
    """Minimal interface we need from requests.Session (eases testing)."""

    def post(self, url: str, **kwargs) -> requests.Response: ...
    def put(self, url: str, **kwargs) -> requests.Response: ...


class UploadError(RuntimeError):
    """Raised when the upload cannot be completed."""


@dataclass
class VideoMetadata:
    title: str
    description: str = ""
    privacy_status: str = "unlisted"
    made_for_kids: bool = False
    category_id: str = "22"  # "People & Blogs" — a safe default for personal clips.

    def to_body(self) -> dict:
        return {
            "snippet": {
                "title": self.title,
                "description": self.description,
                "categoryId": self.category_id,
            },
            "status": {
                "privacyStatus": self.privacy_status,
                "selfDeclaredMadeForKids": self.made_for_kids,
            },
        }


ProgressCallback = Callable[[int], None]


def start_session(
    session: SessionLike,
    auth_header: dict[str, str],
    metadata: VideoMetadata,
) -> str:
    """Open a resumable upload session and return its upload URL."""
    params = {"uploadType": "resumable", "part": "snippet,status"}
    headers = {
        **auth_header,
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": "video/*",
    }
    response = session.post(
        config.YOUTUBE_RESUMABLE_URL,
        params=params,
        headers=headers,
        json=metadata.to_body(),
        timeout=config.REQUEST_TIMEOUT,
    )
    if response.status_code not in (200, 201):
        raise UploadError(
            f"Failed to open upload session ({response.status_code}): {response.text}"
        )
    location = response.headers.get("Location")
    if not location:
        raise UploadError("Upload session response had no Location header.")
    return location


def _read_chunk(stream: BinaryIO, size: int) -> bytes:
    """Read exactly ``size`` bytes, accumulating across short pipe reads.

    Returns fewer than ``size`` bytes only at end of stream. This guarantees
    every non-final chunk is a full ``CHUNK_SIZE`` (a 256 KB multiple), which the
    resumable protocol requires.
    """
    buf = bytearray()
    while len(buf) < size:
        piece = stream.read(size - len(buf))
        if not piece:
            break
        buf.extend(piece)
    return bytes(buf)


class _StreamEnd:
    """Sentinel queued once the source stream is exhausted."""


_END = _StreamEnd()


class InlineChunkReader:
    """Read chunks on the calling thread — the no-read-ahead fallback."""

    def __init__(self, stream: BinaryIO, chunk_size: int) -> None:
        self._stream = stream
        self._chunk_size = chunk_size
        self._done = False

    def __enter__(self) -> InlineChunkReader:
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def read(self) -> bytes | None:
        if self._done:
            return None
        chunk = _read_chunk(self._stream, self._chunk_size)
        if len(chunk) < self._chunk_size:
            self._done = True
        return chunk

    def close(self) -> None:
        self._done = True


class ChunkPrefetcher:
    """Read fixed-size chunks from a stream on a background thread.

    Uploading is network-bound, so without read-ahead the ffmpeg pipe stays
    blocked for the whole duration of every PUT and the concat only makes
    progress between requests. This overlaps the two: ``depth`` chunks may sit
    in the queue while another is being sent, for a peak of ``depth + 1``
    chunks resident in memory.

    Chunk boundaries are identical to reading inline, so the upload protocol is
    unchanged: every chunk is a full ``chunk_size`` except the last.
    """

    def __init__(self, stream: BinaryIO, chunk_size: int, depth: int = 1) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self._stream = stream
        self._chunk_size = chunk_size
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, depth))
        self._error: BaseException | None = None
        self._closing = threading.Event()
        self._thread = threading.Thread(
            target=self._fill, name="gopro-stitch-prefetch", daemon=True
        )

    def __enter__(self) -> ChunkPrefetcher:
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> bool:
        self.close()
        return False

    def _fill(self) -> None:
        try:
            while not self._closing.is_set():
                chunk = _read_chunk(self._stream, self._chunk_size)
                self._put(chunk)
                if len(chunk) < self._chunk_size:
                    break  # Short read means the stream ended.
        except BaseException as exc:  # Re-raised in the consumer via read().
            self._error = exc
        finally:
            self._put(_END)

    def _put(self, item) -> None:
        """Queue an item, giving up if the consumer has stopped draining."""
        while not self._closing.is_set():
            try:
                self._queue.put(item, timeout=0.1)
                return
            except queue.Full:
                continue

    def read(self) -> bytes | None:
        """Return the next chunk, or None once the stream is exhausted."""
        item = self._queue.get()
        if isinstance(item, _StreamEnd):
            if self._error is not None:
                raise self._error
            return None
        return item

    def close(self) -> None:
        """Stop the reader thread and release any chunks still queued."""
        self._closing.set()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        if self._thread.is_alive():
            self._thread.join(timeout=5)


def _parse_ack_end(range_header: str | None) -> int:
    """Given a ``Range: bytes=0-N`` header, return N (last byte the server has).

    Returns -1 when the server reports nothing received yet.
    """
    if not range_header:
        return -1
    try:
        # Format is "bytes=0-524287".
        return int(range_header.split("-")[-1])
    except (ValueError, IndexError):
        return -1


def _put_chunk(
    session: SessionLike,
    upload_url: str,
    chunk: bytes,
    offset: int,
    total: int | None,
) -> requests.Response:
    """PUT one chunk with the correct Content-Range. ``total`` None means '*'."""
    end = offset + len(chunk) - 1
    total_str = str(total) if total is not None else "*"
    if len(chunk) == 0:
        # Zero-length finalize/probe: range has no byte span.
        content_range = f"bytes */{total_str}"
    else:
        content_range = f"bytes {offset}-{end}/{total_str}"
    headers = {"Content-Range": content_range}
    # urllib3 writes the body while the *connect* timeout is in force, and
    # sendall() treats a timeout as a deadline for the whole call — so a bytes
    # body had CONNECT_TIMEOUT_SECONDS to transfer the entire chunk. From a file
    # object it goes out in small blocks with the timeout applied to each, which
    # catches a stalled connection without capping a slow but steady one. An
    # empty body stays bytes: requests would send an empty stream chunked.
    body = io.BytesIO(chunk) if chunk else b""
    return session.put(
        upload_url, headers=headers, data=body, timeout=config.REQUEST_TIMEOUT
    )


def _query_offset(
    session: SessionLike, upload_url: str, total: int | None
) -> int:
    """Ask the server how many bytes it already has. Returns next byte to send."""
    total_str = str(total) if total is not None else "*"
    headers = {"Content-Range": f"bytes */{total_str}"}
    response = session.put(
        upload_url, headers=headers, data=b"", timeout=config.REQUEST_TIMEOUT
    )
    if response.status_code in (200, 201):
        return -2  # Sentinel: server considers the upload already complete.
    return _parse_ack_end(response.headers.get("Range")) + 1


def _send_with_resume(
    session: SessionLike,
    upload_url: str,
    chunk: bytes,
    offset: int,
    total: int | None,
) -> tuple[requests.Response, int]:
    """Send one chunk, retrying transient failures and honouring partial acks.

    Returns the final response for this chunk and the byte offset the server has
    acknowledged up to (exclusive). Raises UploadError after exhausting retries.
    """
    backoff = config.INITIAL_BACKOFF_SECONDS
    chunk_end_exclusive = offset + len(chunk)
    send_from = offset
    deadline = time.monotonic() + config.RETRY_WINDOW_SECONDS

    for attempt in range(config.MAX_RETRIES):
        if attempt and time.monotonic() > deadline:
            break
        try:
            sub = chunk[send_from - offset :]
            response = _put_chunk(session, upload_url, sub, send_from, total)
        except requests.RequestException as exc:
            response = None
            failure = f"{type(exc).__name__}: {exc}"

        if response is not None:
            status = response.status_code
            if status in (200, 201):
                return response, chunk_end_exclusive
            if status == 308:
                acked_end = _parse_ack_end(response.headers.get("Range"))
                acked_next = acked_end + 1
                if acked_next >= chunk_end_exclusive:
                    return response, chunk_end_exclusive
                # Server got only part of the chunk: resend the tail.
                if acked_next > send_from:
                    send_from = acked_next
                    backoff = config.INITIAL_BACKOFF_SECONDS
                    continue
            elif status not in (500, 502, 503, 504):
                raise UploadError(
                    f"Upload chunk failed ({status}): {response.text[:500]}"
                )
            failure = f"HTTP {status}: {response.text[:200]}"

        # Transient failure (network error or 5xx): back off then re-sync offset.
        delay = min(backoff, config.MAX_BACKOFF_SECONDS)
        log.warning(
            "Chunk at offset %d: attempt %d/%d failed (%s); retrying from byte %d "
            "in %.0fs",
            offset, attempt + 1, config.MAX_RETRIES, failure, send_from, delay,
        )
        time.sleep(delay)
        backoff *= 2
        try:
            resynced = _query_offset(session, upload_url, total)
            if resynced == -2:
                # Server already has the whole upload.
                return response if response is not None else _put_chunk(
                    session, upload_url, b"", chunk_end_exclusive, total
                ), chunk_end_exclusive
            if resynced >= 0:
                send_from = max(send_from, resynced)
        except requests.RequestException as exc:
            log.warning("Could not re-sync upload offset (%s)", exc)

    raise UploadError(
        f"Gave up on chunk at offset {offset} after {config.MAX_RETRIES} attempts "
        f"over {config.RETRY_WINDOW_SECONDS:.0f}s."
    )


def stream_upload(
    session: SessionLike,
    upload_url: str,
    stream: BinaryIO,
    on_progress: ProgressCallback | None = None,
    chunk_size: int | None = None,
    read_ahead: int | None = None,
) -> dict:
    """Read ``stream`` to EOF, uploading it in chunks. Returns the video JSON.

    ``on_progress`` receives the cumulative number of bytes uploaded so far.
    ``read_ahead`` is how many chunks the reader thread may buffer ahead of the
    upload; 0 disables read-ahead and reads inline.
    """
    size = config.CHUNK_SIZE if chunk_size is None else chunk_size
    depth = config.READ_AHEAD_CHUNKS if read_ahead is None else read_ahead
    reader = (
        ChunkPrefetcher(stream, size, depth)
        if depth > 0
        else InlineChunkReader(stream, size)
    )
    offset = 0
    final_response: requests.Response | None = None

    with reader as chunks:
        while True:
            chunk = chunks.read()
            if chunk is None:
                break
            is_last = len(chunk) < size  # short read => end of stream
            total = offset + len(chunk) if is_last else None

            if len(chunk) == 0 and offset > 0:
                # Stream ended exactly on a chunk boundary: finalize with total known.
                final_response = _finalize_boundary(session, upload_url, offset)
                break

            final_response, acked = _send_with_resume(
                session, upload_url, chunk, offset, total
            )
            offset = acked
            if on_progress is not None:
                on_progress(offset)

            if is_last:
                break

    if final_response is None or final_response.status_code not in (200, 201):
        code = final_response.status_code if final_response is not None else "none"
        raise UploadError(f"Upload did not complete cleanly (status {code}).")
    return final_response.json()


def _finalize_boundary(
    session: SessionLike, upload_url: str, total: int
) -> requests.Response:
    """Finalize when the stream length is an exact multiple of the chunk size.

    All bytes are already on the server; this zero-length PUT tells it the total
    so it can mark the upload complete.
    """
    backoff = config.INITIAL_BACKOFF_SECONDS
    for _ in range(config.MAX_RETRIES):
        try:
            response = _put_chunk(session, upload_url, b"", total, total)
        except requests.RequestException as exc:
            response = None
            failure = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code in (200, 201):
                return response
            failure = f"HTTP {response.status_code}"
        log.warning("Finalizing the upload failed (%s); retrying", failure)
        time.sleep(min(backoff, config.MAX_BACKOFF_SECONDS))
        backoff *= 2
    raise UploadError("Failed to finalize upload after full chunks were sent.")


def video_url(video_json: dict) -> str:
    """Extract the watch URL from a completed upload response."""
    video_id = video_json.get("id", "")
    return f"https://youtu.be/{video_id}" if video_id else "(unknown video id)"
