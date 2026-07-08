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
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import BinaryIO, Callable, Protocol

import requests

from . import config


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
    return session.put(upload_url, headers=headers, data=chunk)


def _query_offset(
    session: SessionLike, upload_url: str, total: int | None
) -> int:
    """Ask the server how many bytes it already has. Returns next byte to send."""
    total_str = str(total) if total is not None else "*"
    headers = {"Content-Range": f"bytes */{total_str}"}
    response = session.put(upload_url, headers=headers, data=b"")
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

    for attempt in range(config.MAX_RETRIES):
        try:
            sub = chunk[send_from - offset :]
            response = _put_chunk(session, upload_url, sub, send_from, total)
        except requests.RequestException:
            response = None

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

        # Transient failure (network error or 5xx): back off then re-sync offset.
        time.sleep(min(backoff, config.MAX_BACKOFF_SECONDS))
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
        except requests.RequestException:
            pass

    raise UploadError(
        f"Gave up on chunk at offset {offset} after {config.MAX_RETRIES} retries."
    )


def stream_upload(
    session: SessionLike,
    upload_url: str,
    stream: BinaryIO,
    on_progress: ProgressCallback | None = None,
) -> dict:
    """Read ``stream`` to EOF, uploading it in chunks. Returns the video JSON.

    ``on_progress`` receives the cumulative number of bytes uploaded so far.
    """
    offset = 0
    final_response: requests.Response | None = None

    while True:
        chunk = _read_chunk(stream, config.CHUNK_SIZE)
        is_last = len(chunk) < config.CHUNK_SIZE  # short read => end of stream
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
        except requests.RequestException:
            response = None
        if response is not None and response.status_code in (200, 201):
            return response
        time.sleep(min(backoff, config.MAX_BACKOFF_SECONDS))
        backoff *= 2
    raise UploadError("Failed to finalize upload after full chunks were sent.")


def video_url(video_json: dict) -> str:
    """Extract the watch URL from a completed upload response."""
    video_id = video_json.get("id", "")
    return f"https://youtu.be/{video_id}" if video_id else "(unknown video id)"
