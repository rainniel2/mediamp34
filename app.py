import os
import re
import json
import time
import uuid
import threading
import tempfile
from datetime import datetime
from urllib.parse import urlparse

import requests
from flask import Flask, render_template, request, jsonify, send_file
from PIL import Image

import yt_dlp

app = Flask(__name__)

# Raster formats we can actually convert between with Pillow. SVG is vector,
# so it's handled separately (passthrough only — no rasterizing/vectorizing).
CONVERTIBLE_IMAGE_FORMATS = {
    "png": "PNG",
    "jpg": "JPEG",
    "jpeg": "JPEG",
    "webp": "WEBP",
    "bmp": "BMP",
    "gif": "GIF",
}
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
SVG_EXT = ".svg"

DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "grabit-downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# In-memory job store. Fine for a single-process app (see Dockerfile:
# gunicorn runs with --workers 1) — a personal tool for a couple of people
# doesn't need anything heavier than this.
JOBS = {}
JOBS_LOCK = threading.Lock()


def _set_job(job_id, **fields):
    with JOBS_LOCK:
        JOBS[job_id].update(fields)


def schedule_cleanup(path, delay=120):
    """Delete a temp file a bit after it's been sent, so disk doesn't fill up."""
    def _remove():
        try:
            os.remove(path)
        except OSError:
            pass
    threading.Timer(delay, _remove).start()


def friendly_error(e):
    """
    Turn a raw exception into something short enough to show in the UI
    instead of a wall of yt-dlp/requests internals.
    """
    msg = str(e).strip()
    low = msg.lower()
    if "404" in msg or "not found" in low:
        return "That link doesn't exist (404, not found)."
    if "403" in msg or "forbidden" in low:
        return "That link is private or blocked (403, forbidden)."
    if "unsupported url" in low:
        return "That link isn't supported."
    if "unable to download webpage" in low or "name or service not known" in low:
        return "Couldn't reach that link. Check the URL and try again."
    if "confirm you" in low and "bot" in low:
        return ("YouTube is asking for a sign-in check on this video/IP. "
                "Add a cookies.txt file next to app.py (see README) and try again.")
    if "netscape format" in low:
        return ("cookies.txt isn't in a format yt-dlp can read (wrong export type, "
                "or edited by hand). Re-export it fresh with \"Get cookies.txt LOCALLY\" "
                "and don't open/save it in another editor first.")
    if "needs to be reloaded" in low:
        return ("YouTube changed something on their end that broke this yt-dlp "
                "version. This is fixed by updating yt-dlp, not by anything in this "
                "app's code, see README for how to force a fresh build.")
    # yt-dlp errors sometimes end with a long "report this issue" tail; drop it.
    msg = msg.split("; please report")[0].strip()
    return msg if len(msg) <= 160 else msg[:157] + "..."


def convert_image(local_path, download_name, target_format):
    """Convert a downloaded image file to another raster format with Pillow."""
    target_format = target_format.lower()
    pillow_fmt = CONVERTIBLE_IMAGE_FORMATS.get(target_format)
    if not pillow_fmt:
        raise ValueError(f"unsupported format '{target_format}'")

    with Image.open(local_path) as img:
        if pillow_fmt == "JPEG" and img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        new_path = os.path.splitext(local_path)[0] + f".{target_format}"
        img.save(new_path, pillow_fmt)

    if new_path != local_path:
        try:
            os.remove(local_path)
        except OSError:
            pass

    base, _ = os.path.splitext(download_name)
    return new_path, f"{base}.{target_format}"


# Path to an optional cookies.txt exported from a browser that's signed in
# to YouTube. Drop a file here (or set COOKIES_FILE) to get past YouTube's
# "Sign in to confirm you're not a bot" check. See README.
COOKIES_FILE = os.environ.get(
    "COOKIES_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
)

NETSCAPE_HEADERS = ("# Netscape HTTP Cookie File", "# HTTP Cookie File")


def _json_cookies_to_netscape(cookies):
    """Convert a JSON cookie export (Cookie-Editor, EditThisCookie, etc.) into
    the tab-separated Netscape format yt-dlp actually requires."""
    far_future = str(int(time.time()) + 60 * 60 * 24 * 365)
    lines = ["# Netscape HTTP Cookie File", "# auto-converted from JSON by grabit", ""]
    for c in cookies:
        domain = c.get("domain", "")
        name = c.get("name", "")
        if not domain or not name:
            continue
        host_only = c.get("hostOnly", not domain.startswith("."))
        flag = "FALSE" if host_only else "TRUE"
        path = c.get("path") or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        expiry = c.get("expirationDate") or c.get("expiry")
        expiry = str(int(float(expiry))) if expiry else far_future
        value = c.get("value", "")
        lines.append("\t".join([domain, flag, path, secure, expiry, name, value]))
    return "\n".join(lines) + "\n"


def _looks_like_domain(s):
    s = (s or "").strip()
    return bool(s) and "." in s and " " not in s and not s.replace(".", "").isdigit()


def _expiry_to_unix(expires_raw, far_future):
    s = (expires_raw or "").strip()
    if not s or s.lower() == "session":
        return far_future
    try:
        return str(int(float(s)))  # already a unix timestamp
    except ValueError:
        pass
    try:
        iso = s[:-1] + "+00:00" if s.endswith("Z") else s
        return str(int(datetime.fromisoformat(iso).timestamp()))
    except ValueError:
        return far_future


def _devtools_table_to_netscape(raw):
    """
    Convert a copy-paste of Chrome/Firefox DevTools' Application > Cookies
    table (Name, Value, Domain, Path, Expires, Size, HttpOnly, Secure, ...)
    into Netscape format. This is a very common way people grab cookies
    without a dedicated export extension.
    """
    far_future = str(int(time.time()) + 60 * 60 * 24 * 365)
    lines = ["# Netscape HTTP Cookie File",
              "# auto-converted from a DevTools cookie-table paste by grabit", ""]
    for line in raw.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 4:
            continue
        name, value, domain, path = (fields + ["", "", "", ""])[:4]
        expires_raw = fields[4] if len(fields) > 4 else ""
        secure_flag = fields[7] if len(fields) > 7 else ""
        name, domain = name.strip(), domain.strip()
        if not name or not domain:
            continue
        host_only = not domain.startswith(".")
        flag = "FALSE" if host_only else "TRUE"
        secure = "TRUE" if secure_flag.strip() else "FALSE"
        expiry = _expiry_to_unix(expires_raw, far_future)
        lines.append("\t".join([domain, flag, path.strip() or "/", secure, expiry, name, value]))
    return "\n".join(lines) + "\n"


def resolved_cookies_file():
    """
    Return a path to a cookies file yt-dlp can actually load, fixing the two
    most common reasons for "does not look like a Netscape format cookies
    file": the export being JSON instead of Netscape, and the required
    header comment being missing. Returns None if there's no cookies file.
    """
    if not os.path.isfile(COOKIES_FILE):
        return None
    try:
        with open(COOKIES_FILE, "r", encoding="utf-8-sig") as f:
            raw = f.read()
    except OSError:
        return None

    first_nonblank = next((ln for ln in raw.splitlines() if ln.strip()), "")
    if first_nonblank.startswith(NETSCAPE_HEADERS):
        return COOKIES_FILE  # already correct, nothing to do

    stripped = raw.lstrip()
    fixed_path = COOKIES_FILE + ".fixed.txt"

    if stripped.startswith("[") or stripped.startswith("{"):
        # A JSON cookie export saved with a .txt extension — convert it.
        try:
            data = json.loads(raw)
            cookies = data if isinstance(data, list) else data.get("cookies", [])
            with open(fixed_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(_json_cookies_to_netscape(cookies))
            return fixed_path
        except Exception:
            return COOKIES_FILE  # couldn't fix it; let yt-dlp raise its own error

    if "\t" in raw:
        data_line = next(
            (ln for ln in raw.splitlines() if ln.strip() and not ln.strip().startswith(("#", "$"))),
            ""
        )
        fields = data_line.split("\t")

        if len(fields) == 7 and fields[1] in ("TRUE", "FALSE") and fields[3] in ("TRUE", "FALSE"):
            # Proper 7-column Netscape rows, just missing the header line.
            with open(fixed_path, "w", encoding="utf-8", newline="\n") as f:
                f.write("# Netscape HTTP Cookie File\n" + raw)
            return fixed_path

        if len(fields) >= 8 and _looks_like_domain(fields[2]):
            # A DevTools "Application > Cookies" table pasted as tab-separated
            # text (Name, Value, Domain, Path, Expires, ...) — different
            # column order and date format than Netscape entirely.
            with open(fixed_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(_devtools_table_to_netscape(raw))
            return fixed_path

        # Unrecognized tab-separated shape: best-effort, assume it's just
        # missing the header and let yt-dlp's own error surface if it isn't.
        with open(fixed_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("# Netscape HTTP Cookie File\n" + raw)
        return fixed_path

    return COOKIES_FILE


def base_ydl_opts():
    """
    Options shared by every yt-dlp call. YouTube increasingly blocks the
    default "web" client as a bot; asking for the tv/web_safari/android
    clients first avoids that in most cases without needing cookies at all.
    If a cookies.txt is present, it's used as a fallback for the harder cases.
    """
    opts = {
        "extractor_args": {"youtube": {"player_client": ["tv", "web_safari", "android"]}},
    }
    cookies_path = resolved_cookies_file()
    if cookies_path:
        opts["cookiefile"] = cookies_path
    return opts


def looks_like_direct_file(url):
    """Quick check for links that just point straight at a media/image file."""
    path = urlparse(url).path.lower()
    return path.endswith((
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg",
        ".mp4", ".mov", ".webm", ".mkv",
        ".mp3", ".wav", ".ogg", ".m4a", ".flac",
        ".pdf",
    ))


def build_quality_options(meta):
    """
    Turn yt-dlp's raw format list into a short, deduped list of resolutions
    the user can actually pick between, e.g. [{"value": "1080", "label":
    "1080p"}, ...] plus a trailing "Best available" option.
    """
    seen_heights = set()
    heights = []
    for f in meta.get("formats") or []:
        height = f.get("height")
        vcodec = f.get("vcodec")
        if not height or vcodec in (None, "none"):
            continue
        if height in seen_heights:
            continue
        seen_heights.add(height)
        heights.append(height)

    heights.sort(reverse=True)
    result = [{"value": str(h), "label": f"{h}p"} for h in heights]
    result.append({"value": "best", "label": "Best available"})
    return result


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def info():
    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Paste a link first."}), 400

    # Direct file links: skip yt-dlp entirely, just describe the file.
    if looks_like_direct_file(url):
        filename = os.path.basename(urlparse(url).path) or "file"
        ext = os.path.splitext(urlparse(url).path)[1].lower()
        is_svg = ext == SVG_EXT
        is_raster_image = ext in IMAGE_EXTS
        is_image = is_raster_image or is_svg

        formats = []
        if is_raster_image:
            formats = ["original"] + sorted(
                {f for f in CONVERTIBLE_IMAGE_FORMATS if f != "jpeg"}
            )

        return jsonify({
            "title": filename,
            "thumbnail": url if is_raster_image or is_svg else None,
            "duration": None,
            "uploader": None,
            "direct": True,
            "is_image": is_image,
            "is_svg": is_svg,
            "formats": formats,
            "qualities": [],
        })

    try:
        ydl_opts = {
            "quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            **base_ydl_opts(),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            meta = ydl.extract_info(url, download=False)
        return jsonify({
            "title": meta.get("title") or "Untitled",
            "thumbnail": meta.get("thumbnail"),
            "duration": meta.get("duration"),
            "uploader": meta.get("uploader") or meta.get("channel"),
            "direct": False,
            "is_image": False,
            "is_svg": False,
            "formats": [],
            "qualities": build_quality_options(meta),
        })
    except Exception as e:
        return jsonify({"error": friendly_error(e)}), 400


def _run_direct_download(job_id, url, target_format=None):
    try:
        r = requests.get(url, stream=True, timeout=30)
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        ext = os.path.splitext(urlparse(url).path)[1] or ""
        local_path = os.path.join(DOWNLOAD_DIR, f"{job_id}{ext}")

        done = 0
        with open(local_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
                done += len(chunk)
                percent = round(done / total * 100, 1) if total else None
                _set_job(job_id, percent=percent, downloaded=done, total=total or None)

        download_name = os.path.basename(urlparse(url).path) or f"download{ext}"

        if target_format and target_format != "original":
            _set_job(job_id, percent=99, stage="processing")
            try:
                local_path, download_name = convert_image(local_path, download_name, target_format)
            except Exception as e:
                _set_job(job_id, status="error",
                          error=f"Couldn't convert to {target_format.upper()} ({e}).")
                return

        _set_job(job_id, status="finished", percent=100, filename=local_path,
                  download_name=download_name)
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else None
        if code == 404:
            _set_job(job_id, status="error", error="That link doesn't exist (404, not found).")
        elif code == 403:
            _set_job(job_id, status="error", error="That link is private or blocked (403, forbidden).")
        else:
            _set_job(job_id, status="error", error=f"The server rejected that link (HTTP {code}).")
    except requests.exceptions.RequestException:
        _set_job(job_id, status="error",
                  error="Couldn't reach that link. Check the URL and try again.")
    except Exception as e:
        _set_job(job_id, status="error", error=friendly_error(e))


def _run_ytdlp_download(job_id, url, mode, quality):
    outtmpl = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes") or 0
            percent = round(downloaded / total * 100, 1) if total else None
            _set_job(
                job_id,
                percent=percent,
                downloaded=downloaded,
                total=total,
                speed=d.get("speed"),
                eta=d.get("eta"),
            )
        elif d.get("status") == "finished":
            # yt-dlp still has to mux/convert after this; reflect that.
            _set_job(job_id, percent=99, stage="processing")

    ydl_opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
        **base_ydl_opts(),
    }

    if mode == "audio":
        ydl_opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        })
    else:
        if quality and quality != "best":
            height_cap = f"[height<={quality}]"
        else:
            height_cap = ""
        ydl_opts.update({
            "format": f"bestvideo*{height_cap}+bestaudio/best{height_cap}/best",
            "merge_output_format": "mp4",
        })

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            meta = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(meta)
            if mode == "audio":
                base, _ = os.path.splitext(filename)
                candidate = base + ".mp3"
                if os.path.exists(candidate):
                    filename = candidate

        if not os.path.exists(filename):
            _set_job(job_id, status="error", error="Download finished but the file went missing. Try again.")
            return

        _set_job(job_id, status="finished", percent=100, filename=filename,
                  download_name=os.path.basename(filename))
    except Exception as e:
        _set_job(job_id, status="error", error=friendly_error(e))


@app.route("/api/start", methods=["POST"])
def start():
    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    mode = data.get("mode", "auto")          # "auto" | "video" | "audio"
    quality = data.get("quality", "best")    # e.g. "1080", "720", "best"
    img_format = data.get("format", "original")  # "original" | "png" | "jpg" | ...

    if not url:
        return jsonify({"error": "Paste a link first."}), 400

    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "downloading",
            "percent": 0,
            "created": time.time(),
        }

    if looks_like_direct_file(url) and mode != "audio":
        t = threading.Thread(target=_run_direct_download, args=(job_id, url, img_format), daemon=True)
    else:
        t = threading.Thread(target=_run_ytdlp_download, args=(job_id, url, mode, quality), daemon=True)
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/api/progress/<job_id>")
def progress(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job."}), 404
    # Don't leak the server filesystem path to the client.
    safe = {k: v for k, v in job.items() if k != "filename"}
    return jsonify(safe)


@app.route("/api/result/<job_id>")
def result(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job."}), 404
    if job.get("status") != "finished":
        return jsonify({"error": "Not ready yet."}), 409

    filename = job["filename"]
    download_name = job.get("download_name") or os.path.basename(filename)

    response = send_file(filename, as_attachment=True, download_name=download_name)
    schedule_cleanup(filename)
    with JOBS_LOCK:
        JOBS.pop(job_id, None)
    return response


if __name__ == "__main__":
    # Local dev only — in production (Render etc.) gunicorn runs this instead,
    # see Dockerfile.
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", debug=True, port=port, threaded=True)
