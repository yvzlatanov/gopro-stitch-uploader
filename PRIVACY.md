# Privacy Policy — GoPro Stitch Uploader

This is a personal-use, single-user command-line tool. It is not distributed
and has no users other than its developer.

## What this tool does

It reads video files from the developer's own GoPro SD card, concatenates
them locally with ffmpeg, and uploads the result via the YouTube Data API
only to the developer's own YouTube channel, using the developer's own
Google OAuth credentials. No combined video file is ever written to disk —
the upload streams directly from the concatenation process.

## Use of YouTube API Services

This tool uses YouTube API Services. By using this tool, the developer
(its only user) acknowledges the
[YouTube Terms of Service](https://www.youtube.com/t/terms) and the
[Google Privacy Policy](https://policies.google.com/privacy).

## Data collected and stored

- **OAuth credentials**: an OAuth 2.0 refresh token issued by Google is
  stored locally on the developer's own machine, at
  `~/.config/gopro-stitch/token.json` (file permissions restricted to the
  developer's own user account). This token is never transmitted anywhere
  except directly to Google's servers to authorize API calls.
- **No other data** — personal, third-party, or otherwise — is collected,
  stored, transmitted, or shared by this tool. There is no analytics,
  tracking, or advertising of any kind.

## Data deletion and access revocation

- To delete the locally stored token, delete
  `~/.config/gopro-stitch/token.json`.
- To revoke this application's access to your Google Account at any time,
  visit [Google Account → Security → Third-party apps with account
  access](https://myaccount.google.com/permissions) and remove
  "GoPro Stitch Uploader".

## Contact

y.zlatanov@dveconstructions.com
