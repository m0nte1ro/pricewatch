FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PRICEWATCH_DATA_DIR=/var/lib/pricewatch \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pricewatch-browsers \
    HOME=/var/lib/pricewatch

WORKDIR /opt/pricewatch

# Keep dependency/browser layers cached when only application code changes.
COPY requirements.lock requirements-browser.lock ./
RUN pip install --no-cache-dir -r requirements-browser.lock \
    && python -m playwright install --with-deps --only-shell chromium \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 pricewatch \
    && useradd --uid 10001 --gid pricewatch --home-dir /var/lib/pricewatch \
        --no-create-home --shell /usr/sbin/nologin pricewatch \
    && install -d -m 0700 -o pricewatch -g pricewatch /var/lib/pricewatch \
    && chmod -R a+rX /opt/pricewatch-browsers

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini pyproject.toml LICENSE ./
COPY scripts/backup.py ./scripts/backup.py
COPY deploy/docker-entrypoint.sh /usr/local/bin/pricewatch-entrypoint
RUN chmod 0755 /usr/local/bin/pricewatch-entrypoint

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).read()"]

# Startup fixes fresh bind-mount ownership, then drops to UID 10001 before migrations/server.
ENTRYPOINT ["pricewatch-entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
