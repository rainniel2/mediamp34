# MediaMP34 — Render + yt-dlp

A small password-protected Flask web app for personal/occasional media downloads. It embeds yt-dlp through its Python API and uses FFmpeg for MP3 extraction and video/audio merging.

## Deploy on Render

Create a Render **Web Service** from this repository and choose Docker. The included `Dockerfile` installs FFmpeg, Node.js, and an up-to-date yt-dlp prerelease build. Render supplies the `$PORT` environment variable automatically; the container binds to it.

Set these environment variables in Render:

- `ACCESS_PIN` — the PIN for starting downloads.
- `SECRET_KEY` — a long random string used to sign the PIN session cookie.
- `MAX_DOWNLOAD_MB` — optional, default `1024`.
- `MAX_DURATION_SEC` — optional, default `3600`.
- `YTDLP_PLAYER_CLIENT` — optional; leave unset to use yt-dlp's current defaults plus one automatic fallback for YouTube bot checks.
- `YTDLP_COOKIES_B64` — optional; base64-encoded Netscape-format cookies file if a legitimate login/age/region-gated use case requires it. Never commit cookies to GitHub.

To create a base64 secret locally:

```bash
base64 -w 0 cookies.txt
```

On Windows PowerShell:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes('cookies.txt'))
```

## Local run

Requires Python 3.10+ plus the `ffmpeg` binary. Install packages and run:

```bash
python -m pip install -r requirements.txt
python app.py
```

## Temporary files

Downloads are written under the operating system temp directory and are deleted when the file response closes, with a delayed cleanup fallback. The job store also removes stale job files.

## YouTube / yt-dlp note

yt-dlp's maintainers document Python embedding through `YoutubeDL` and recommend FFmpeg/ffprobe plus `yt-dlp-ejs` and a supported JavaScript runtime for current YouTube support. The Dockerfile therefore installs FFmpeg and Node.js and installs `yt-dlp[default,curl-cffi]`. The project intentionally does not ship browser cookies.

A cloud-hosted IP can still be blocked by YouTube even when the application code is correct. No downloader can guarantee every YouTube URL will work from every Render IP range.

Use the tool only for media you are legally and contractually permitted to download.
