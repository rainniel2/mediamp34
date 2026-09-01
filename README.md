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

- This runs entirely on your own machine — nothing is uploaded anywhere else.
- Files are held in a temp folder just long enough to hand them to your
  browser, then deleted automatically.
- `yt-dlp` (the extraction library this uses) is updated constantly to keep
  up with sites changing their pages. If a specific link stops working,
  try: `pip install -U yt-dlp`.
- Only download things you actually have the right to — your own uploads,
  Creative-Commons/public-domain content, or files you own. Some sites'
  terms of service restrict downloading, and copyright law varies by
  country, so that part's on you to check.

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

