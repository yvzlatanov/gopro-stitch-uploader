"""Unit tests for the resumable streaming upload logic (mocked HTTP)."""

import io

import pytest

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

    def post(self, url, **kwargs):
        return FakeResponse(200, headers={"Location": "https://upload/session/abc"})

    def put(self, url, **kwargs):
        self.put_count += 1
        headers = kwargs.get("headers", {})
        data = kwargs.get("data", b"") or b""
        content_range = headers["Content-Range"]
        self.content_ranges.append(content_range)

        spec, total_str = content_range.replace("bytes ", "").split("/")

        # Zero-length probe/finalize: "*/N" or "*/*".
        if spec == "*":
            if total_str != "*" and self.received >= int(total_str):
                return FakeResponse(200, json_body={"id": "vid123"})
            return FakeResponse(
                308, headers={"Range": f"bytes=0-{self.received - 1}"}
            )

        start = int(spec.split("-")[0])
        length = len(data)

        # Optionally accept only half of the very first chunk.
        if self.partial_first_put and self.put_count == 1:
            length = length // 2

        if start <= self.received:
            self.received = max(self.received, start + length)

        if total_str != "*" and self.received >= int(total_str):
            return FakeResponse(200, json_body={"id": "vid123"})
        return FakeResponse(308, headers={"Range": f"bytes=0-{self.received - 1}"})


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
