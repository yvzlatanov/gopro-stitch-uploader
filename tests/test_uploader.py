"""Unit tests for the resumable streaming upload logic (mocked HTTP)."""

import io
import logging
import socket
import threading
import time

import pytest
import requests

from gopro_stitch import config, uploader


# ---------- low-level helpers ----------

def test_read_chunk_accumulates_across_short_reads():
    class ShortStream(io.RawIOBase):
        def __init__(self, data, piece):
            self.data = data
            self.piece = piece
            self.pos = 0

        def readable(self):
            return True

        def read(self, size=-1):
            out = self.data[self.pos : self.pos + min(self.piece, size)]
            self.pos += len(out)
            return out

    stream = ShortStream(b"x" * 1000, piece=7)
    chunk = uploader._read_chunk(stream, 1000)
    assert len(chunk) == 1000


def test_read_chunk_returns_short_at_eof():
    stream = io.BytesIO(b"abc")
    assert uploader._read_chunk(stream, 1000) == b"abc"
    assert uploader._read_chunk(stream, 1000) == b""


def test_parse_ack_end():
    assert uploader._parse_ack_end("bytes=0-524287") == 524287
    assert uploader._parse_ack_end(None) == -1
    assert uploader._parse_ack_end("garbage") == -1


# ---------- read-ahead ----------

def drain(reader):
    chunks = []
    with reader as source:
        while True:
            chunk = source.read()
            if chunk is None:
                break
            chunks.append(chunk)
    return chunks


def test_prefetcher_yields_same_chunks_as_inline_reader():
    data = b"a" * 700
    prefetched = drain(uploader.ChunkPrefetcher(io.BytesIO(data), 256, depth=2))
    inline = drain(uploader.InlineChunkReader(io.BytesIO(data), 256))
    assert prefetched == inline
    assert [len(c) for c in prefetched] == [256, 256, 188]


def test_prefetcher_emits_empty_chunk_on_exact_boundary():
    # The uploader relies on a trailing zero-length read to finalize.
    chunks = drain(uploader.ChunkPrefetcher(io.BytesIO(b"b" * 512), 256))
    assert [len(c) for c in chunks] == [256, 256, 0]


def test_prefetcher_surfaces_reader_errors():
    class Exploding(io.RawIOBase):
        def readable(self):
            return True

        def read(self, size=-1):
            raise OSError("pipe died")

    with pytest.raises(OSError, match="pipe died"):
        drain(uploader.ChunkPrefetcher(Exploding(), 256))


def test_prefetcher_close_stops_reader_thread():
    reader = uploader.ChunkPrefetcher(io.BytesIO(b"c" * 10_000), 256, depth=1)
    with reader:
        assert reader.read() is not None
    assert not reader._thread.is_alive()


def test_prefetcher_rejects_zero_chunk_size():
    with pytest.raises(ValueError):
        uploader.ChunkPrefetcher(io.BytesIO(b""), 0)


# ---------- fake resumable server ----------

class FakeResponse:
    def __init__(self, status_code, headers=None, json_body=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self._json = json_body
        self.text = text

    def json(self):
        return self._json


class FakeServer:
    """Simulates YouTube's resumable endpoint, tracking received bytes.

    ``partial_first_put`` makes the server ack only half of the first PUT to
    exercise the resume-the-tail path.
    """

    def __init__(self, partial_first_put=False):
        self.received = 0
        self.partial_first_put = partial_first_put
        self.put_count = 0
        self.content_ranges = []

    def _incomplete(self):
        # Google omits Range entirely until it has actually stored something.
        if self.received == 0:
            return FakeResponse(308)
        return FakeResponse(308, headers={"Range": f"bytes=0-{self.received - 1}"})

    def post(self, url, **kwargs):
        return FakeResponse(200, headers={"Location": "https://upload/session/abc"})

    def put(self, url, **kwargs):
        self.put_count += 1
        headers = kwargs.get("headers", {})
        data = kwargs.get("data", b"") or b""
        if hasattr(data, "read"):
            data = data.read()
        content_range = headers["Content-Range"]
        self.content_ranges.append(content_range)

        spec, total_str = content_range.replace("bytes ", "").split("/")

        # Zero-length probe/finalize: "*/N" or "*/*".
        if spec == "*":
            if total_str != "*" and self.received >= int(total_str):
                return FakeResponse(200, json_body={"id": "vid123"})
            return self._incomplete()

        start = int(spec.split("-")[0])
        length = len(data)

        # Optionally accept only half of the very first chunk.
        if self.partial_first_put and self.put_count == 1:
            length = length // 2

        if start <= self.received:
            self.received = max(self.received, start + length)

        if total_str != "*" and self.received >= int(total_str):
            return FakeResponse(200, json_body={"id": "vid123"})
        return self._incomplete()


@pytest.fixture
def small_chunks(monkeypatch):
    monkeypatch.setattr(config, "CHUNK_SIZE", 256)
    return 256


def test_start_session_returns_upload_url():
    server = FakeServer()
    url = uploader.start_session(
        server, {"Authorization": "Bearer t"}, uploader.VideoMetadata(title="hi")
    )
    assert url == "https://upload/session/abc"


def test_start_session_raises_without_location():
    class NoLocation:
        def post(self, url, **kwargs):
            return FakeResponse(200, headers={})

        def put(self, url, **kwargs):  # pragma: no cover
            raise AssertionError

    with pytest.raises(uploader.UploadError):
        uploader.start_session(NoLocation(), {}, uploader.VideoMetadata(title="x"))


def test_stream_upload_multiple_full_chunks(small_chunks):
    server = FakeServer()
    stream = io.BytesIO(b"y" * 700)  # 256 + 256 + 188
    result = uploader.stream_upload(server, "https://upload/session/abc", stream)
    assert result == {"id": "vid123"}
    assert server.received == 700
    # Last chunk carries the real total; earlier chunks use '*'.
    assert server.content_ranges[0].endswith("/*")
    assert server.content_ranges[-1].endswith("/700")


def test_stream_upload_exact_boundary_length(small_chunks):
    server = FakeServer()
    stream = io.BytesIO(b"z" * 512)  # exactly 2 * 256
    result = uploader.stream_upload(server, "https://upload/session/abc", stream)
    assert result == {"id": "vid123"}
    assert server.received == 512
    # A zero-length finalize with the real total closes it out.
    assert server.content_ranges[-1] == "bytes */512"


def test_stream_upload_resumes_partial_ack(small_chunks):
    server = FakeServer(partial_first_put=True)
    stream = io.BytesIO(b"w" * 600)
    result = uploader.stream_upload(server, "https://upload/session/abc", stream)
    assert result == {"id": "vid123"}
    assert server.received == 600


def test_progress_callback_reports_cumulative_bytes(small_chunks):
    server = FakeServer()
    stream = io.BytesIO(b"y" * 700)
    seen = []
    uploader.stream_upload(
        server, "https://upload/session/abc", stream, on_progress=seen.append
    )
    assert seen == sorted(seen)  # monotonic
    assert seen[-1] == 700


@pytest.mark.parametrize("read_ahead", [0, 1, 3])
def test_stream_upload_matches_across_read_ahead_depths(read_ahead):
    server = FakeServer()
    stream = io.BytesIO(b"y" * 700)
    result = uploader.stream_upload(
        server,
        "https://upload/session/abc",
        stream,
        chunk_size=256,
        read_ahead=read_ahead,
    )
    assert result == {"id": "vid123"}
    assert server.received == 700
    assert server.content_ranges[-1].endswith("/700")


def test_stream_upload_honours_explicit_chunk_size():
    server = FakeServer()
    uploader.stream_upload(
        server, "https://upload/session/abc", io.BytesIO(b"y" * 300), chunk_size=256
    )
    assert server.content_ranges[0] == "bytes 0-255/*"


# ---------- resilience ----------

def test_every_request_sets_a_timeout():
    """A missing timeout lets a half-open connection stall the upload forever."""
    server = FakeServer()
    seen = []

    class Recording:
        def post(self, url, **kwargs):
            seen.append(kwargs.get("timeout"))
            return server.post(url, **kwargs)

        def put(self, url, **kwargs):
            seen.append(kwargs.get("timeout"))
            return server.put(url, **kwargs)

    recorder = Recording()
    uploader.start_session(recorder, {}, uploader.VideoMetadata(title="x"))
    uploader.stream_upload(
        recorder, "https://upload/session/abc", io.BytesIO(b"q" * 512), chunk_size=256
    )
    assert seen and all(t == config.REQUEST_TIMEOUT for t in seen)


def test_retries_survive_a_transient_outage(monkeypatch):
    """Six attempts ran out in about a minute; an outage must not lose the chunk."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    server = FakeServer()
    outage = {"remaining": 5}

    class Flaky:
        def put(self, url, **kwargs):
            if outage["remaining"] > 0:
                outage["remaining"] -= 1
                raise requests.RequestException("connection reset")
            return server.put(url, **kwargs)

    response, acked = uploader._send_with_resume(
        Flaky(), "https://upload/session/abc", b"p" * 256, 0, 256
    )
    assert acked == 256
    assert response.status_code == 200


def test_retries_stop_once_the_window_closes(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    monkeypatch.setattr(config, "RETRY_WINDOW_SECONDS", 0.0)

    class Dead:
        def put(self, url, **kwargs):
            raise requests.RequestException("down")

    with pytest.raises(uploader.UploadError, match="Gave up"):
        uploader._send_with_resume(Dead(), "https://upload/session/abc", b"p", 0, 1)


def test_metadata_body_shape():
    body = uploader.VideoMetadata(
        title="My Ride", description="line1\nline2"
    ).to_body()
    assert body["snippet"]["title"] == "My Ride"
    assert body["status"]["privacyStatus"] == "unlisted"
    assert body["status"]["selfDeclaredMadeForKids"] is False


def test_video_url():
    assert uploader.video_url({"id": "abc"}) == "https://youtu.be/abc"
    assert "unknown" in uploader.video_url({})


# ---------- send timeout ----------

class SlowReceiver:
    """A real local HTTP endpoint that drains request bodies at a throttled rate.

    Fake sessions can't catch the send-timeout bug: it lives in how urllib3 and
    the socket layer apply the timeout while a body is being written.
    """

    def __init__(self, piece=64 * 1024, pause=0.01):
        self.piece = piece
        self.pause = pause
        self.received = 0
        self.listener = socket.socket()
        # A tiny receive window keeps the client's send blocked on our pace.
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.url = f"http://127.0.0.1:{self.listener.getsockname()[1]}/upload"
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        conn, _ = self.listener.accept()
        with conn:
            head = b""
            while b"\r\n\r\n" not in head:
                head += conn.recv(4096)
            head, body = head.split(b"\r\n\r\n", 1)
            length = int(
                next(
                    line.split(b":", 1)[1]
                    for line in head.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
            )
            self.received = len(body)
            while self.received < length:
                data = conn.recv(min(self.piece, length - self.received))
                if not data:
                    return
                self.received += len(data)
                time.sleep(self.pause)
            conn.sendall(
                b"HTTP/1.1 308 Resume Incomplete\r\n"
                + f"Range: bytes=0-{self.received - 1}\r\n".encode()
                + b"Content-Length: 0\r\n\r\n"
            )

    def close(self):
        self.listener.close()


def test_chunk_send_is_not_capped_by_the_connect_timeout(monkeypatch):
    """A steady but slow upload must not time out just because it takes a while.

    urllib3 writes the body under the *connect* timeout, and sendall() treats a
    timeout as a deadline for the whole call. Passing the chunk as bytes gave
    every chunk CONNECT_TIMEOUT_SECONDS to transfer in full: on a ~14 Mbps
    uplink a 256 MB chunk was cut off after 15 s, every time.
    """
    monkeypatch.setattr(config, "REQUEST_TIMEOUT", (0.2, 5.0))
    receiver = SlowReceiver()
    chunk = b"c" * (8 * 1024 * 1024)  # ~1.3 s to drain at the throttled pace
    try:
        with requests.Session() as session:
            response = uploader._put_chunk(session, receiver.url, chunk, 0, None)
    finally:
        receiver.close()
    assert response.status_code == 308
    assert receiver.received == len(chunk)


# ---------- retry diagnostics ----------

def test_retry_logs_why_a_chunk_attempt_failed(monkeypatch, caplog):
    """Swallowed exceptions left a stuck upload with nothing in the log."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    server = FakeServer()
    outage = {"remaining": 1}

    class Flaky:
        def put(self, url, **kwargs):
            if outage["remaining"] > 0:
                outage["remaining"] -= 1
                raise requests.ConnectionError("The write operation timed out")
            return server.put(url, **kwargs)

    with caplog.at_level(logging.WARNING, logger="gopro_stitch.uploader"):
        uploader._send_with_resume(
            Flaky(), "https://upload/session/abc", b"p" * 256, 0, 256
        )
    assert "The write operation timed out" in caplog.text
    assert "offset 0" in caplog.text


def test_retry_logs_a_server_error_status(monkeypatch, caplog):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    server = FakeServer()
    calls = {"n": 0}

    class Overloaded:
        def put(self, url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeResponse(503, text="backend busy")
            return server.put(url, **kwargs)

    with caplog.at_level(logging.WARNING, logger="gopro_stitch.uploader"):
        uploader._send_with_resume(
            Overloaded(), "https://upload/session/abc", b"p" * 256, 0, 256
        )
    assert "503" in caplog.text


def test_retry_logs_a_308_that_acknowledged_nothing_new(monkeypatch, caplog):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    server = FakeServer()
    calls = {"n": 0}

    class Stingy:
        def put(self, url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeResponse(308)  # nothing stored yet
            return server.put(url, **kwargs)

    with caplog.at_level(logging.WARNING, logger="gopro_stitch.uploader"):
        _, acked = uploader._send_with_resume(
            Stingy(), "https://upload/session/abc", b"p" * 256, 0, 256
        )
    assert acked == 256
    assert "308" in caplog.text
