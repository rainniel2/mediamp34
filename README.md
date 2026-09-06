# grabit

A tiny local website: paste a link, pick Auto / Video / Audio, get the file.
Works on direct image/video/audio/document links, on most sites `yt-dlp`
supports, and as a generic "just download whatever this link points to"
fallback for anything else (PDFs, Office docs, zips, APKs, you name it).

**Things to know about the free tier:**
- It spins down after ~15 minutes with no traffic, so the first request
  after a quiet period takes 30-60 seconds to "wake up." Totally normal.
- Free instances have limited RAM (512MB) — fine for typical clips, but a
  very large/long 4K video download could struggle. The app already caps
  video quality at 1080p to help with this.
- Anyone with the link can use it, so don't post the URL somewhere public
  if you only want you and your friend using it.

