"""Unit tests for the CLI's scheduling and progress reporting."""

import io
import threading
from contextlib import contextmanager

import pytest

from gopro_stitch import __main__ as cli
from gopro_stitch import uploader


# ---------- parallelism ----------

@pytest.mark.parametrize(
    "requested,jobs,expected",
    [
        (1, 3, 1),
        (3, 3, 3),
        (8, 2, 2),  # never spawn more workers than there is work
        (0, 3, 1),  # nonsense input still runs sequentially
        (-4, 3, 1),
    ],
)
def test_plan_parallelism(requested, jobs, expected):
    assert cli.plan_parallelism(requested, jobs) == expected


# ---------- running jobs ----------

class FakeGroup:
    def __init__(self, video_id, total_bytes=1000):
        self.video_id = video_id
        self.prefix = "GX"
        self.total_bytes = total_bytes


@pytest.fixture
def stub_upload(monkeypatch):
    """Replace auth, ffmpeg and the HTTP upload with instrumented no-ops."""
    state = {"concurrent": 0, "peak": 0, "fail": set()}
    lock = threading.Lock()
    both_started = threading.Barrier(2, timeout=5)

    @contextmanager
    def fake_stitch_stream(group):
        class Proc:
            stdout = io.BytesIO(b"")

        yield Proc()

    def fake_stream_upload(session, url, stream, on_progress=None, **kwargs):
        with lock:
            state["concurrent"] += 1
            state["peak"] = max(state["peak"], state["concurrent"])
        try:
            if state["wait_for_overlap"]:
                # Deadlocks (and fails the test) unless jobs really do overlap.
                both_started.wait()
            if url in state["fail"]:
                raise uploader.UploadError("boom")
            if on_progress is not None:
                on_progress(1000)
            return {"id": f"vid-{url}"}
        finally:
            with lock:
                state["concurrent"] -= 1

    state["wait_for_overlap"] = False
    monkeypatch.setattr(cli.auth, "bearer_header", lambda creds: {})
    monkeypatch.setattr(cli.uploader, "start_session", lambda s, h, m: m.title)
    monkeypatch.setattr(cli.stitcher, "stitch_stream", fake_stitch_stream)
    monkeypatch.setattr(cli.stitcher, "finalize", lambda proc: None)
    monkeypatch.setattr(cli.uploader, "stream_upload", fake_stream_upload)
    return state


def make_jobs(titles):
    return [
        cli.UploadJob(
            slot=slot,
            group=FakeGroup(f"011{slot}"),
            metadata=uploader.VideoMetadata(title=title),
        )
        for slot, title in enumerate(titles)
    ]


def run(jobs, parallel, stream=None):
    progress = cli.ProgressReporter(
        [job.metadata.title for job in jobs],
        [job.group.total_bytes for job in jobs],
        stream=stream if stream is not None else io.StringIO(),
    )
    return cli.run_jobs(jobs, creds=object(), progress=progress,
                        parallel=parallel, chunk_size=256, read_ahead=1)


def test_run_jobs_sequential_keeps_one_upload_at_a_time(stub_upload):
    results = run(make_jobs(["one", "two"]), parallel=1)
    assert results == ["https://youtu.be/vid-one", "https://youtu.be/vid-two"]
    assert stub_upload["peak"] == 1


def test_run_jobs_parallel_overlaps_uploads(stub_upload):
    stub_upload["wait_for_overlap"] = True
    results = run(make_jobs(["one", "two"]), parallel=2)
    assert stub_upload["peak"] == 2
    assert all(url is not None for url in results)


def test_run_jobs_results_stay_in_job_order(stub_upload):
    results = run(make_jobs(["a", "b", "c"]), parallel=3)
    assert results == [f"https://youtu.be/vid-{t}" for t in ("a", "b", "c")]


def test_run_jobs_isolates_a_failure(stub_upload):
    stub_upload["fail"] = {"two"}
    stream = io.StringIO()
    results = run(make_jobs(["one", "two", "three"]), parallel=3, stream=stream)
    assert results[1] is None
    assert results[0] is not None and results[2] is not None
    assert "✗ Failed: boom" in stream.getvalue()


# ---------- progress ----------

def make_reporter(labels, totals):
    # A StringIO has no isatty(), so this exercises the redirected-output path.
    return cli.ProgressReporter(labels, totals, stream=io.StringIO())


def test_progress_logs_each_slot_independently():
    reporter = make_reporter(["GX0118", "GX0119"], [1000, 2000])
    reporter.callback(0)(500)
    reporter.callback(1)(1000)
    output = reporter._stream.getvalue()
    assert "GX0118" in output and "50.0%" in output
    assert "GX0119" in output


def test_progress_throttles_sub_percent_updates():
    reporter = make_reporter(["GX0118"], [100_000])
    for uploaded in range(0, 500, 100):  # every step is well under 1%
        reporter.callback(0)(uploaded)
    assert reporter._stream.getvalue().count("\n") == 1


def test_progress_finish_always_prints_status():
    reporter = make_reporter(["GX0118"], [1000])
    reporter.callback(0)(10)
    reporter.finish(0, "✓ https://youtu.be/abc")
    assert "https://youtu.be/abc" in reporter._stream.getvalue()


def test_progress_handles_unknown_total():
    reporter = make_reporter(["GX0118"], [0])
    reporter.callback(0)(123)
    reporter.finish(0, "done")
    assert "?" in reporter._stream.getvalue()


def test_progress_repaints_in_place_on_a_tty():
    class Tty(io.StringIO):
        def isatty(self):
            return True

    reporter = cli.ProgressReporter(["A", "B"], [100, 100], stream=Tty())
    reporter.finish(0, "✓ one")
    reporter.finish(1, "✓ two")
    output = reporter._stream.getvalue()
    # Second paint must move the cursor back up over the first block.
    assert "\033[2A" in output
    assert "✓ one" in output and "✓ two" in output
