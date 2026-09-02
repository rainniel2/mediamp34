# grabit

A tiny local website: paste a link, pick Auto / Video / Audio, get the file.
Works on direct image/video/audio links and on most sites `yt-dlp` supports.

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
   convert the image to PNG, JPG, WEBP, BMP, or GIF on the way out. SVGs are
   vector files and are always saved as-is.
4. Click **Save file**, the file saves through your browser's normal download.

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
server/cloud IPs rather than a home internet connection. This app already
tries a couple of workarounds automatically (asking YouTube for its TV/Safari
app clients instead of the regular website client, which usually avoids the
check entirely). If it still shows up:

1. Update `yt-dlp`: `pip install -U yt-dlp`. YouTube changes things often and
   old versions break first.
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
3. If you're deploying to a cloud host (Render, Railway, etc.), the block is
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

