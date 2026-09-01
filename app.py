import os
import time
import uuid
import threading
import tempfile
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
        ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
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
