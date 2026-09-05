import base64
import hmac
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

import requests
import yt_dlp
from flask import Flask, jsonify, render_template, request, send_file, session

APP_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "mediamp34-downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

ACCESS_PIN = os.environ.get("ACCESS_PIN", "").strip()
MAX_DOWNLOAD_MB = max(50, int(os.environ.get("MAX_DOWNLOAD_MB", "1024")))
MAX_DURATION_SEC = max(60, int(os.environ.get("MAX_DURATION_SEC", "3600")))
CLEANUP_AFTER_SEC = max(60, int(os.environ.get("CLEANUP_AFTER_SEC", "600")))
YTDLP_PLAYER_CLIENT = os.environ.get("YTDLP_PLAYER_CLIENT", "").strip()

LOG = logging.getLogger(__name__)

JOBS = {}
JOBS_LOCK = threading.Lock()

AUTO_FALLBACK_CLIENTS = ["web_safari", "tv", "android_vr", "android"]
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}
URL_RE = re.compile(r"^https?://", re.I)

COOKIE_PATH = Path(tempfile.gettempdir()) / "mediamp34-youtube-cookies.txt"


def pin_enabled():
    return bool(ACCESS_PIN)


def unlocked():
    return not pin_enabled() or session.get("unlocked") is True


def cleanup_path(path: Path):
    try:
        if path.is_file() or path.is_symlink():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def delayed_cleanup(path: Path, delay: int = CLEANUP_AFTER_SEC):
    def worker():
        time.sleep(delay)
        cleanup_path(path)
    threading.Thread(target=worker, daemon=True).start()


def cleanup_all_job_files(job_id: str):
    for p in DOWNLOAD_DIR.glob(f"{job_id}*"):
        cleanup_path(p)


def set_job(job_id, **updates):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job.update(updates)


def get_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def remove_old_jobs():
    cutoff = time.time() - 3600
    with JOBS_LOCK:
        stale = [jid for jid, j in JOBS.items() if j.get("created", 0) < cutoff]
        for jid in stale:
            JOBS.pop(jid, None)
            cleanup_all_job_files(jid)


def validate_url(url: str):
    return bool(url and URL_RE.match(url))


def is_youtube_url(url: str):
    try:
        from urllib.parse import urlparse
        return urlparse(url).hostname in YOUTUBE_HOSTS
    except Exception:
        return False


def write_cookie_secret_if_present():
    encoded = os.environ.get("YTDLP_COOKIES_B64", "").strip()
    if not encoded:
        return None
    try:
        raw = base64.b64decode(encoded, validate=True)
        COOKIE_PATH.write_bytes(raw)
        return str(COOKIE_PATH)
    except Exception as exc:
        LOG.warning("Could not decode YTDLP_COOKIES_B64: %s", exc)
        cleanup_path(COOKIE_PATH)
        return None


def base_ydl_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "restrictfilenames": False,
        "windowsfilenames": True,
        "geo_bypass": True,
        "socket_timeout": 15,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 1,
    }
    if YTDLP_PLAYER_CLIENT:
        opts["extractor_args"] = {
            "youtube": {"player_client": [x.strip() for x in YTDLP_PLAYER_CLIENT.split(",") if x.strip()]}
        }
    cookies = write_cookie_secret_if_present()
    if cookies:
        opts["cookiefile"] = cookies
    return opts


def looks_like_botcheck(exc):
    s = str(exc).lower()
    needles = (
        "confirm you’re not a bot",
        "confirm you're not a bot",
        "confirm you are not a bot",
        "sign in to confirm",
        "po token",
        "requires a token",
        "not a bot",
    )
    return any(n in s for n in needles)


def extract_with_retry(url, opts, download=False):
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=download)
    except Exception as first:
        if not is_youtube_url(url) or YTDLP_PLAYER_CLIENT or not looks_like_botcheck(first):
            raise
        retry = dict(opts)
        retry["extractor_args"] = {"youtube": {"player_client": AUTO_FALLBACK_CLIENTS}}
        with yt_dlp.YoutubeDL(retry) as ydl:
            return ydl.extract_info(url, download=download)


def friendly_error(exc):
    msg = str(exc).strip()
    low = msg.lower()
    LOG.warning("yt-dlp/download error: %s", msg)
    if "sign in to confirm" in low or "not a bot" in low or "po token" in low:
        return "YouTube blocked this request from the server. Try another video, or configure YTDLP_COOKIES_B64 on Render."
    if "unsupported url" in low:
        return "That link is not supported. Paste a YouTube URL or a direct media link."
    if "private video" in low or "members-only" in low or "login required" in low:
        return "That video is private or requires a login."
    if "video unavailable" in low or "this video is not available" in low:
        return "That video is unavailable."
    if "ffmpeg" in low:
        return "FFmpeg could not process this media. Make sure the Render service is using the supplied Dockerfile."
    if len(msg) > 220:
        msg = msg[:217] + "..."
    return msg or "Download failed."


def quality_options(info):
    heights = set()
    for f in info.get("formats") or []:
        h = f.get("height")
        if isinstance(h, int) and h > 0 and h <= 1080:
            heights.add(h)
    ordered = sorted(heights, reverse=True)
    return [{"value": str(h), "label": f"{h}p"} for h in ordered] + [{"value": "best", "label": "Best available"}]


def best_audio_size(info):
    candidates = [
        f.get("filesize") or f.get("filesize_approx")
        for f in (info.get("formats") or [])
        if f.get("vcodec") == "none" and (f.get("acodec") not in (None, "none"))
    ]
    return max((x for x in candidates if isinstance(x, int)), default=None)


def check_info_limits(info):
    duration = info.get("duration")
    if isinstance(duration, (int, float)) and duration > MAX_DURATION_SEC:
        raise ValueError(f"This video is longer than the {MAX_DURATION_SEC // 60}-minute limit for this server.")


def human_size(n):
    if not n:
        return None
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return None


def prepare_download_options(job_id, mode, quality):
    output = DOWNLOAD_DIR / f"{job_id}.%(ext)s"
    opts = base_ydl_opts()
    opts.update({
        "outtmpl": str(output),
        "progress_hooks": [],
        "continuedl": False,
    })

    def progress_hook(d):
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            pct = round(done * 100 / total, 1) if total else None
            set_job(job_id, status="downloading", percent=pct, downloaded=done, total=total,
                    speed=d.get("speed"), eta=d.get("eta"), stage="downloading")
        elif status == "finished":
            set_job(job_id, percent=99, stage="processing")

    opts["progress_hooks"] = [progress_hook]

    if mode == "audio":
        opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        })
    else:
        if quality and quality.isdigit():
            h = min(int(quality), 1080)
            fmt = f"bv*[height<={h}]+ba/b[height<={h}]/b"
        else:
            fmt = "bv*[height<=1080]+ba/b[height<=1080]/b"
        opts.update({
            "format": fmt,
            "merge_output_format": "mp4",
        })
    return opts


def locate_output(job_id, mode):
    candidates = [p for p in DOWNLOAD_DIR.glob(f"{job_id}.*") if p.is_file() and not p.name.endswith(".part")]
    if mode == "audio":
        mp3 = DOWNLOAD_DIR / f"{job_id}.mp3"
        if mp3.exists():
            return mp3
    if candidates:
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[0]
    return None


def run_download(job_id, url, mode, quality):
    try:
        check = get_job(job_id)
        if not check:
            return
        set_job(job_id, status="downloading", stage="starting", percent=0)
        opts = prepare_download_options(job_id, mode, quality)
        info = extract_with_retry(url, opts, download=True)
        check_info_limits(info or {})
        path = locate_output(job_id, mode)
        if not path or not path.exists():
            raise FileNotFoundError("The media was downloaded but the output file could not be found.")
        max_bytes = MAX_DOWNLOAD_MB * 1024 * 1024
        size = path.stat().st_size
        if size > max_bytes:
            cleanup_path(path)
            raise ValueError(f"The resulting file is larger than the {MAX_DOWNLOAD_MB} MB server limit.")
        if mode == "audio":
            ext = ".mp3"
        else:
            ext = path.suffix.lower() or ".mp4"
        title = info.get("title") or "download"
        safe_title = re.sub(r"[\\/:*?\"<>|\r\n]+", "-", title).strip(" .-") or "download"
        filename = f"{safe_title}{ext}"
        set_job(job_id, status="ready", stage="ready", percent=100, path=str(path),
                filename=filename, filesize=size, title=title, thumbnail=info.get("thumbnail"))
        delayed_cleanup(path)
    except Exception as exc:
        cleanup_all_job_files(job_id)
        set_job(job_id, status="error", stage="error", error=friendly_error(exc))


@app.route("/")
def index():
    return render_template("index.html", pin_required=pin_enabled())


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "yt_dlp": yt_dlp.version.__version__})


@app.route("/api/config")
def api_config():
    return jsonify({"pin_required": pin_enabled(), "max_download_mb": MAX_DOWNLOAD_MB, "max_duration_sec": MAX_DURATION_SEC})


@app.route("/api/unlock", methods=["POST"])
def api_unlock():
    if not pin_enabled():
        session.permanent = True
        session["unlocked"] = True
        return jsonify({"ok": True})
    data = request.get_json(silent=True) or {}
    pin = str(data.get("pin", ""))
    if hmac.compare_digest(pin, ACCESS_PIN):
        session.permanent = True
        session["unlocked"] = True
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Incorrect PIN."}), 401


@app.route("/api/info", methods=["POST"])
def api_info():
    remove_old_jobs()
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    if not validate_url(url):
        return jsonify({"error": "Paste a complete http:// or https:// URL."}), 400
    try:
        opts = base_ydl_opts()
        opts.update({"skip_download": True, "extract_flat": False})
        info = extract_with_retry(url, opts, download=False)
        check_info_limits(info)
        return jsonify({
            "title": info.get("title") or "Untitled",
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "uploader": info.get("uploader"),
            "webpage_url": info.get("webpage_url") or url,
            "qualities": quality_options(info),
            "audio_filesize": best_audio_size(info),
        })
    except Exception as exc:
        return jsonify({"error": friendly_error(exc)}), 400


@app.route("/api/download", methods=["POST"])
def api_download():
    if not unlocked():
        return jsonify({"error": "PIN required.", "needs_pin": True}), 401
    remove_old_jobs()
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    mode = str(data.get("mode", "video")).lower()
    quality = str(data.get("quality", "best"))
    if not validate_url(url):
        return jsonify({"error": "Paste a valid URL."}), 400
    if mode not in {"video", "audio"}:
        return jsonify({"error": "Invalid download mode."}), 400
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {"created": time.time(), "status": "queued", "percent": 0, "stage": "queued"}
    threading.Thread(target=run_download, args=(job_id, url, mode, quality), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/progress/<job_id>")
def api_progress(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Download job not found."}), 404
    safe = {k: v for k, v in job.items() if k not in {"path"}}
    return jsonify(safe)


@app.route("/api/file/<job_id>")
def api_file(job_id):
    job = get_job(job_id)
    if not job or job.get("status") != "ready":
        return jsonify({"error": "The file is not ready."}), 404
    if not unlocked():
        return jsonify({"error": "PIN required.", "needs_pin": True}), 401
    path = Path(job["path"])
    if not path.exists():
        return jsonify({"error": "The temporary file has already expired. Please download it again."}), 410
    response = send_file(path, as_attachment=True, download_name=job.get("filename") or path.name)
    response.call_on_close(lambda: cleanup_path(path))
    return response


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
