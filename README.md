# grabit

A tiny local website: paste a link, pick Auto / Video / Audio, get the file.
Works on direct image/video/audio/document links, on most sites `yt-dlp`
supports, and as a generic "just download whatever this link points to"
fallback for anything else (PDFs, Office docs, zips, APKs, you name it).

## 1. Install requirements

You need **Python 3.9+** and **ffmpeg** (ffmpeg is only needed for the
"Audio (MP3)" mode and for merging separate video+audio streams).

```bash
# macOS
brew install ffmpeg

# Windows (using winget)
winget install ffmpeg

# Linux (Debian/Ubuntu)
sudo apt install ffmpeg
```

Then install the Python packages:

```bash
cd media-downloader
pip install -r requirements.txt
```

## 2. Run it

```bash
python app.py
```

Open **http://localhost:5000** in your browser.

## 3. Use it

1. Paste a link into the box.
2. Choose **Auto** (best available), **Video**, or **Audio (MP3)**.
3. Click **Download** to preview the title/thumbnail. For direct image links
   (jpg, png, gif, webp, bmp, svg) a **format** dropdown appears so you can
   convert the image to PNG, JPG, WEBP, BMP, GIF, or **PDF** on the way out.
   SVGs are vector files and are always saved as-is.
4. Once found, an estimated file size shows next to the title (updates as
   you switch Video/Audio or change quality). It's an estimate — exact for
   direct file links, approximate for `yt-dlp` sites since sources report
   sizes inconsistently.
5. Click **Save file**, the file saves through your browser's normal
   download, and the box clears itself so the next link can be pasted
   straight in.

**Documents, archives, and other non-media files** (PDF, Word/Excel/
PowerPoint, ZIP, APK, EPUB, etc.) work the same way — paste the link, click
Download, then Save file. There's no format conversion for these (a ZIP
stays a ZIP), just a clean pass-through download. If the link doesn't have
a recognizable extension (a hashed CDN URL, say) and isn't a site `yt-dlp`
recognizes, the app asks the server what the file actually is via its
`Content-Type` before giving up.

## Optional: PIN-protect downloads

Anyone with the deployed URL can use this app. If you don't want that, set
an `ACCESS_PIN` environment variable and the app will ask for it before
starting any download (previewing a link's title/size is still free — only
the actual download is gated). Once someone enters it correctly, that
device stays unlocked for 30 days.

This is **basic protection**, on purpose: it keeps a leaked or crawled link
from being used by random visitors. It is not hardened against a
determined attacker (no rate-limiting/lockout on wrong attempts), so don't
rely on it for anything sensitive.

To turn it on:

1. In Render, go to your service → **Environment** → add:
   - `ACCESS_PIN` — whatever PIN/passphrase you want people to enter.
   - `SECRET_KEY` — a random string used to sign the "unlocked" cookie.
     Generate one with:
     ```bash
     python3 -c "import secrets; print(secrets.token_hex(32))"
     ```
     **Set this explicitly.** If you leave it unset, the app still works,
     but it generates a random key every time the process starts — and
     Render's free tier restarts the app after ~15 minutes of no traffic,
     which would silently log everyone out each time that happens.
2. Redeploy. Locally, the same variables work via your shell or a `.env`
   loader of your choice — this app doesn't require one, just export them
   before running `python app.py`.

Leave `ACCESS_PIN` unset (the default) and the app behaves exactly as
before, with no PIN prompt anywhere.

## Notes

- This runs entirely on your own machine, nothing is uploaded anywhere else.
- Files are held in a temp folder just long enough to hand them to your
  browser, then deleted automatically.
- `yt-dlp` (the extraction library this uses) is updated constantly to keep
  up with sites changing their pages. If a specific link stops working,
  try: `pip install -U yt-dlp`.
- Only download things you actually have the right to: your own uploads,
  Creative-Commons/public-domain content, or files you own. Some sites'
  terms of service restrict downloading, and copyright law varies by
  country, so that part's on you to check.

## "Sign in to confirm you're not a bot" (YouTube)

YouTube has stepped up bot detection, especially for requests coming from
server/cloud IPs rather than a home internet connection, and it's rolling
out a "PO Token" requirement on top of that for more and more of its
internal "clients" (web, android, tv, etc.). This app handles it in three
layers, each kicking in only if the one before it wasn't enough:

1. **yt-dlp's own defaults.** The app doesn't hardcode which client to
   pretend to be — yt-dlp's maintainers update that default every release to
   match whatever currently dodges YouTube's checks best, and this app
   always installs the latest yt-dlp on every deploy (see the Dockerfile
   note below), so that stays current on its own.
2. **An automatic one-time retry.** If a download still hits the bot-check
   wall, the app automatically retries once with a broader mix of clients
   (`web_safari`, `tv`, `android_vr`, `android`) before giving up.
3. **Cookies**, for the cases neither of the above can get past (age/region
   gated videos, or an IP that's been rate-limited hard). See below.

If you want to force a specific client yourself instead of relying on (1)
and (2) — e.g. you've found one that reliably works for your use case — set
the `YTDLP_PLAYER_CLIENT` environment variable to a comma-separated list,
e.g. `YTDLP_PLAYER_CLIENT=tv,web_safari,android`. This disables the
automatic retry, since at that point you've made an explicit choice.

If it still shows up after all of that:

1. Update `yt-dlp`: `pip install -U yt-dlp`. YouTube changes things often and
   old versions break first. (On the deployed version, this now happens
   automatically on every push — see the Dockerfile note under "The page
   needs to be reloaded" below.)
2. As a fallback, export cookies from a browser where you're signed in to
   YouTube (an extension like "Get cookies.txt LOCALLY" works well), save the
   file as `cookies.txt`, and drop it in the same folder as `app.py` (or set
   the `COOKIES_FILE` environment variable to point at it). The app will pick
   it up automatically. Use a throwaway/secondary Google account, not your
   main one, since the file grants that account's session to the app.
   - If you see `does not look like a Netscape format cookies file`, the app
     now auto-detects and repairs the three most common causes: a JSON
     export instead of Netscape format, a missing `# Netscape HTTP Cookie
     File` header line, and a raw copy-paste from the browser's DevTools
     "Application > Cookies" table (different column order and date
     format entirely). If it still fails, re-export a fresh file rather
     than hand-editing the old one.
   - Treat `cookies.txt` like a password: it's a live login session for
     whatever account you exported it from. Don't paste its contents into
     a chat, email, or issue tracker, copy the file directly. If it's ever
     exposed, sign out of that account everywhere to invalidate it.

## "The page needs to be reloaded" (YouTube)

YouTube periodically changes something in its player that breaks the current
`yt-dlp` extractor. This isn't a bug in this app; it's fixed upstream by a
`yt-dlp` update, usually within a day or two of it starting.

This app already depends on `yt-dlp[default]`, which includes the
`yt-dlp-ejs` package YouTube's playback checks now require. The Dockerfile
also has a dedicated `pip install -U "yt-dlp[default]"` step positioned
*after* the app code is copied in, so it re-runs and grabs whatever the
latest release is on every deploy that changes any file in the repo — which
is every normal push. You shouldn't need to manually clear the build cache
for yt-dlp staleness anymore.

If you still hit this error on a deployed instance:

1. Trigger a **Manual Deploy > Clear build cache & deploy** on Render just
   in case (covers the base OS/ffmpeg layer, though that rarely goes stale).
2. Locally, run `pip install -U "yt-dlp[default]"` and try again.
3. If it's still broken right after updating, it's a live yt-dlp bug;
   check https://github.com/yt-dlp/yt-dlp/issues for the current status.
4. If you're deploying to a cloud host (Render, Railway, etc.), the block is
   often tied to that provider's IP range being flagged, not your code, so it
   may pass locally but fail once deployed.

## 4. Deploy it so a friend can use it too

This is packaged with a `Dockerfile` so it can run on any host that
supports Docker. **Render** has a free tier that works well for this:

1. **Push this folder to a GitHub repo.**
   ```bash
   git init
   git add .
   git commit -m "media downloader"
   git branch -M main
   git remote add origin https://github.com/<you>/media-downloader.git
   git push -u origin main
   ```
2. Go to **https://render.com** → sign up/log in → **New +** → **Web Service**.
3. Connect your GitHub account and pick the `media-downloader` repo.
4. Render will detect the `Dockerfile` automatically — set:
   - **Instance Type:** Free
   - Leave build/start commands blank (the Dockerfile handles both)
5. Click **Create Web Service**. First build takes a few minutes.
6. Once it's live, Render gives you a URL like
   `https://media-downloader-xxxx.onrender.com` — send that to your friend.

**Things to know about the free tier:**
- It spins down after ~15 minutes with no traffic, so the first request
  after a quiet period takes 30-60 seconds to "wake up." Totally normal.
- Free instances have limited RAM (512MB) — fine for typical clips, but a
  very large/long 4K video download could struggle. The app already caps
  video quality at 1080p to help with this.
- Anyone with the link can use it, so don't post the URL somewhere public
  if you only want you and your friend using it.

