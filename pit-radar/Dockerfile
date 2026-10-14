# PIT Radar: one container serving the API, the 3D front end and the in-process scheduler.

# --- Stage 1: fetch the pinned map libraries (MapLibre GL CSP build + deck.gl) into static/vendor ----------------
FROM node:22-slim AS vendor
WORKDIR /v
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY scripts/vendor.mjs scripts/vendor.mjs
RUN node scripts/vendor.mjs

# --- Stage 2: Python app ------------------------------------------------------------------------------------------
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_ENV=production \
    PIT_DATA_DIR=/data
WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY alembic.ini pyproject.toml ./
COPY alembic ./alembic
COPY app ./app
COPY config ./config
COPY prompts ./prompts
COPY schemas ./schemas
COPY data ./data
COPY scripts ./scripts
COPY static ./static
COPY --from=vendor /v/static/vendor ./static/vendor
# Railway mounts volumes owned by root, so the process runs as the image's default (root) user to write /data.
RUN mkdir -p /data
EXPOSE 8000
CMD ["sh", "scripts/start.sh"]
