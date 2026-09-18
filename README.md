# gopro-youtube-stitch

Stitch a GoPro's chapter files (`GX010118.MP4`, `GX020118.MP4`, …) back into
whole recordings and stream them **straight to YouTube as unlisted** — without
ever writing the combined multi-gigabyte video to your disk.

ffmpeg concatenates the chapters losslessly (`-c copy`, no re-encode) and pipes
a fragmented MP4 into YouTube's resumable upload. The only thing that touches
disk is a tiny text file listing the chapters. Your SD card is never modified.

This is a personal, single-user, non-commercial tool. It uses **YouTube API
Services** — see the [Privacy Policy](PRIVACY.md) and
[Terms of Service](TERMS.md).

## Why "unlisted" needs a one-time audit

YouTube locks videos uploaded through **unverified** API projects to *private*,
and they can't be flipped to unlisted afterwards. To make `unlisted` actually
stick you must apply for a free YouTube API compliance audit for your Google
Cloud project (personal use is a valid reason). Until it's approved, uploads
still work but land as locked-private — the tool prints a reminder each run.

## Requirements

- macOS/Linux, Python 3.9+
- `ffmpeg` and `ffprobe` on your PATH — `brew install ffmpeg`

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Google Cloud setup (one-time)

1. Create a project at <https://console.cloud.google.com> and **enable the
   YouTube Data API v3**.
2. **OAuth consent screen** → External → add your own Google account as a *test
   user*.
3. **Credentials → Create credentials → OAuth client ID → Desktop app** →
   download the JSON.
4. Save it as `~/.config/gopro-stitch/client_secret.json`
   (or point `GOPRO_STITCH_CLIENT_SECRET` at it).
5. **Apply for the API audit** from the YouTube API compliance/audit form so
   uploads can be unlisted rather than locked-private. (See note above.)

The first upload opens a browser for consent once; the refresh token is cached
at `~/.config/gopro-stitch/token.json` (mode 600) for future runs.

## Usage

```bash
# List what's on the card and the exact ffmpeg commands, without uploading:
gopro-stitch --dry-run

# Real run: pick recordings, type a title/description for each, upload:
gopro-stitch

# Point at a different card/folder:
gopro-stitch --dcim "/Volumes/GoPro SD/DCIM/100GOPRO"

# Upload three recordings at once, with a smaller chunk to keep memory down:
gopro-stitch --parallel 3 --chunk-mb 64
```

Example session:

```
Found 3 recording(s):

  [1] GX0118 — 5 chapters, 18.2 GB, 42m, recorded 2026-07-05
  [2] GX0119 — 2 chapters, 7.9 GB, 18m, recorded 2026-07-06
  [3] GX0120 — 3 chapters, 11.4 GB, 27m, recorded 2026-07-06

Which to upload? [all / e.g. 1,3 / skip]: 2
— GX0119 —
  Title (required, blank to skip this video): Coast road, morning
  Description (optional). End with a single '.' on its own line:
  > Full GoPro run.
  > .
    uploading 7.9 GB / ~7.9 GB (100.0%)
    ✓ https://youtu.be/xxxxxxxxxxx
```

## Throughput

The join itself is already free — `-c copy` is a stream copy, so stitching runs
at disk-read speed. Wall-clock time is essentially all upload, and two knobs
address that:

- **Read-ahead** (`--read-ahead`, default 1): a background thread fills the next
  chunk while the current one is in flight, so ffmpeg keeps reading during a
  request instead of blocking on a full pipe for its whole duration. Pass `0`
  to read inline.
- **Parallel uploads** (`--parallel`, default 1): upload several recordings at
  the same time. This only helps if a single stream can't saturate your uplink
  — concurrent uploads share the same bandwidth. Titles for every recording are
  collected up front, so nothing stops to ask for input mid-run.

Peak memory is roughly `parallel × (read-ahead + 1) × chunk size`, so lower
`--chunk-mb` when raising `--parallel`: `--parallel 3 --chunk-mb 64` holds about
384 MB, against 1.5 GB at the default 256 MB chunk.

## Notes & limits

- **Quota**: `videos.insert` costs 1600 of the default 10,000 units/day → about
  **6 uploads/day**. A quota error means you've hit that daily cap.
- **Cleanup**: the tool never deletes anything. After confirming a video plays
  correctly on YouTube, clear the source chapters off the card yourself.
- **Resumable & resilient**: if the network drops mid-upload, the tool queries
  the server offset and resumes from where it left off.

## Tests

```bash
pytest
```

Covers chapter parsing/grouping (including orphaned sidecars and missing
chapters) and the resumable upload chunking, `Content-Range`, exact-boundary,
and partial-ack resume paths against a simulated server. Also covers the
read-ahead reader (identical chunk boundaries to reading inline, error
propagation) and parallel scheduling, including that concurrent uploads really
do overlap and that one failure doesn't take down the rest of the batch.
