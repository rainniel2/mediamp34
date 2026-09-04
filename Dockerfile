FROM python:3.12-slim

# ffmpeg is required for audio extraction and video+audio merging
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# yt-dlp's extractor breaks against YouTube every few weeks and gets patched
# just as fast — but the layer above only reruns pip install when
# requirements.txt itself changes, so an ordinary code push would otherwise
# keep reusing whatever yt-dlp version was cached from the last build.
# Putting this AFTER "COPY . ." means it reruns (and fetches the latest
# release) on every deploy that changes any file in the repo, which is
# every normal push, without needing "Clear build cache & Deploy".
RUN pip install --no-cache-dir -U "yt-dlp[default]"

ENV PORT=10000
EXPOSE 10000

# 1 worker keeps memory use low on free-tier hosting; each download request
# is already handled with streaming/blocking calls to yt-dlp, so extra
# workers mostly help with concurrent users, not per-download speed.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 1 --timeout 300 app:app"]
