# syntax=docker/dockerfile:1
# Stage 1: build the SPA
FROM node:22-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# Stage 2: API + static SPA on one origin
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    FRONTEND_DIST_DIR=/app/frontend/dist
WORKDIR /app/backend
COPY backend/pyproject.toml ./
# Install the exact pinned runtime dependencies listed in pyproject.toml (no dev extras).
RUN python -c "import tomllib;print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/req.txt \
    && pip install -r /tmp/req.txt && rm /tmp/req.txt
COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./
COPY --from=frontend /build/dist /app/frontend/dist
RUN useradd --system --uid 10001 docsql && chown -R docsql /app
USER docsql
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
    CMD python -c "import os,urllib.request,sys; port=os.environ.get('PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/livez').status == 200 else 1)"
# Migrations are a RELEASE step (compose `command`, PaaS release phase), never run in the app lifespan.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
