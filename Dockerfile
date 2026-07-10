FROM node:22-bookworm-slim AS frontend-build
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    FRONTEND_DIST=/app/static \
    BRIDGEGUARD_DATA_DIR=/data

RUN groupadd --system bridgeguard \
    && useradd --system --gid bridgeguard --home-dir /app bridgeguard \
    && mkdir -p /app /data \
    && chown -R bridgeguard:bridgeguard /app /data

WORKDIR /app
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=bridgeguard:bridgeguard backend/app ./app
COPY --from=frontend-build --chown=bridgeguard:bridgeguard /build/frontend/dist ./static

USER bridgeguard
EXPOSE 8000
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=3)"

CMD ["sh", "-c", "gunicorn -k uvicorn.workers.UvicornWorker app.main:app --bind 0.0.0.0:${PORT} --timeout 300 --workers 1 --access-logfile - --error-logfile -"]

