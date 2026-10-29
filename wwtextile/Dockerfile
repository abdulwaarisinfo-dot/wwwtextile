# NOT BUILT OR RUN in the development environment (no Docker available there). Review before first use.
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr poppler-utils tzdata \
    && rm -rf /var/lib/apt/lists/* && useradd -r -m -d /app ww
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY wwtextile ./wwtextile
USER ww
ENV WW_ENV=production WW_DB_PATH=/data/wwtextile.db WW_UPLOAD_DIR=/data/uploads
VOLUME /data
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1
CMD ["sh","-c","exec python -m wwtextile.cli demo --host 0.0.0.0 --port ${PORT:-8000}"]
