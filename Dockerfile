FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg/ffprobe are required by yt-dlp for MP3 extraction and for merging
# separate video + audio streams. Node.js provides a supported JS runtime for
# yt-dlp-ejs, which current YouTube extraction can require.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install --pre -U "yt-dlp[default,curl-cffi]" \
    && python -m pip install -r requirements.txt

COPY . .

ENV PORT=10000
EXPOSE 10000

# One worker keeps RAM use predictable on a small personal Render instance.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 2 --timeout 900 --graceful-timeout 30 app:app"]
