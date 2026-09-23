# syntax=docker/dockerfile:1
# Feature Flag API — production image for DigitalOcean App Platform.
# Runs as non-root; SQLite data dir is the only writable runtime path.

FROM python:3.13-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /code

# Dependency layer first (better build cache)
COPY requirements.txt /code/
RUN pip install --no-cache-dir -r requirements.txt

# Application source (see .dockerignore — no .git, .env, caches, local DBs)
COPY . /code/

# Non-root user (UID/GID > 10000). Keep /code root-owned so the process cannot
# mutate application files. Only /data is writable (SQLite persistence).
RUN groupadd -g 10001 appgroup \
    && useradd -u 10001 -g appgroup --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /data \
    && chown -R appuser:appgroup /data

USER appuser

EXPOSE 8080

# --proxy-headers: App Platform terminates TLS and forwards client metadata.
# forwarded-allow-ips limited to private/link-local ranges used by platform
# proxies — avoid trusting arbitrary public clients with '*'.
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips=10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7,fe80::/10"]
