import os
import uuid
import threading
import tempfile
import mimetypes
from urllib.parse import urlparse

import requests
from flask import Flask, render_template, request, jsonify, send_file, after_this_request

import yt_dlp

app = Flask(__name__)

DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "grabit-downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def schedule_cleanup(path, delay=60):
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
        })
    except Exception as e:
        return jsonify({"error": f"Couldn't read that link ({e})"}), 400


@app.route("/api/download", methods=["POST"])
def download():
    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    mode = data.get("mode", "auto")  # "auto" | "video" | "audio"

    if not url:
        return jsonify({"error": "Paste a link first."}), 400

    file_id = uuid.uuid4().hex

    # --- Path 1: direct file link (image, pdf, raw media file) ---
    if looks_like_direct_file(url) and mode != "audio":
        try:
            r = requests.get(url, stream=True, timeout=30)
            r.raise_for_status()
        except requests.RequestException as e:
            return jsonify({"error": f"Couldn't fetch that file ({e})"}), 400

        ext = os.path.splitext(urlparse(url).path)[1] or ""
        local_path = os.path.join(DOWNLOAD_DIR, f"{file_id}{ext}")
        with open(local_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

        download_name = os.path.basename(urlparse(url).path) or f"download{ext}"

        @after_this_request
        def _cleanup(response):
            schedule_cleanup(local_path)
            return response

        return send_file(local_path, as_attachment=True, download_name=download_name)

    # --- Path 2: everything else goes through yt-dlp ---
    outtmpl = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")
    ydl_opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
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
        ydl_opts.update({
            # Cap at 1080p instead of grabbing an absolute-best (often 4K)
            # stream — much faster and still looks great for most links.
            "format": "bestvideo*[height<=1080]+bestaudio/best[height<=1080]/best",
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
    except Exception as e:
        return jsonify({"error": f"Download failed ({e})"}), 400

    if not os.path.exists(filename):
        return jsonify({"error": "Download finished but the file went missing. Try again."}), 500

    download_name = os.path.basename(filename)

    @after_this_request
    def _cleanup(response):
        schedule_cleanup(filename)
        return response

    return send_file(filename, as_attachment=True, download_name=download_name)


if __name__ == "__main__":
    # Local dev only — in production (Render etc.) gunicorn runs this instead,
    # see Dockerfile.
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", debug=True, port=port, threaded=True)
