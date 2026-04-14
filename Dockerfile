FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    JUPITER_SENTINEL_RUNTIME_DIR=/app/data/runtime \
    JUPITER_SENTINEL_STATE_FILE=/app/data/state.json \
    JUPITER_SENTINEL_HEALTHCHECK_ENABLED=true \
    JUPITER_SENTINEL_HEALTH_HOST=0.0.0.0 \
    JUPITER_SENTINEL_HEALTH_PORT=8080

WORKDIR /app

COPY . .

RUN pip install --upgrade pip \
    && pip install .

RUN mkdir -p /app/data /app/keys /app/logs

EXPOSE 8080
VOLUME ["/app/data", "/app/keys"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5).read()"]

CMD ["jupiter-sentinel", "start", "--foreground"]
