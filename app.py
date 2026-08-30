import os
import time
import uuid
import threading
import tempfile
from urllib.parse import urlparse

import requests
from flask import Flask, render_template, request, jsonify, send_file

import yt_dlp

app = Flask(__name__)

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
        return jsonify({
            "title": filename,
            "thumbnail": url if url.lower().split("?")[0].endswith(
                (".jpg", ".jpeg", ".png", ".gif", ".webp")
            ) else None,
            "duration": None,
            "uploader": None,
            "direct": True,
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
            "qualities": build_quality_options(meta),
        })
    except Exception as e:
        return jsonify({"error": f"Couldn't read that link ({e})"}), 400


def _run_direct_download(job_id, url):
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
        _set_job(job_id, status="finished", percent=100, filename=local_path,
                  download_name=download_name)
    except Exception as e:
        _set_job(job_id, status="error", error=f"Couldn't fetch that file ({e})")


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
        _set_job(job_id, status="error", error=f"Download failed ({e})")


@app.route("/api/start", methods=["POST"])
def start():
    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    mode = data.get("mode", "auto")        # "auto" | "video" | "audio"
    quality = data.get("quality", "best")  # e.g. "1080", "720", "best"

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
        t = threading.Thread(target=_run_direct_download, args=(job_id, url), daemon=True)
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
