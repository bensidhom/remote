FROM node:22-bookworm-slim AS frontend-build
ENV NODE_OPTIONS=--max-old-space-size=768
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS sqlite-build
ARG SQLITE_AUTOCONF_VERSION=3530400
ARG SQLITE_SHA3_256=454e45f61c6bd75b7420e7190732dea03ce6639c63ada47bbc592f67fc340338
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /tmp/sqlite-build
RUN curl --fail --location --proto '=https' --tlsv1.2 \
      "https://www.sqlite.org/2026/sqlite-autoconf-${SQLITE_AUTOCONF_VERSION}.tar.gz" \
      --output sqlite.tar.gz \
    && python -c "import hashlib, pathlib; p=pathlib.Path('sqlite.tar.gz'); actual=hashlib.sha3_256(p.read_bytes()).hexdigest(); expected='${SQLITE_SHA3_256}'; assert actual == expected, (actual, expected)" \
    && tar -xzf sqlite.tar.gz \
    && cd "sqlite-autoconf-${SQLITE_AUTOCONF_VERSION}" \
    && ./configure --prefix=/usr/local --disable-static --enable-shared \
    && make -j2 \
    && make DESTDIR=/sqlite-root install

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    FRONTEND_DIST=/app/static \
    BRIDGEGUARD_DATA_DIR=/data

RUN groupadd --system --gid 10001 bridgeguard \
    && useradd --system --uid 10001 --gid bridgeguard --home-dir /app bridgeguard \
    && mkdir -p /app /data \
    && chown -R bridgeguard:bridgeguard /app /data

WORKDIR /app
COPY --from=sqlite-build /sqlite-root/usr/local/ /usr/local/
RUN ldconfig \
    && python -c "import sqlite3; expected=(3, 53, 4); assert sqlite3.sqlite_version_info == expected, (sqlite3.sqlite_version_info, expected)"
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
RUN python -c "import platform; machine=platform.machine().lower(); assert machine in {'aarch64','arm64'} or __import__('pye57')"
COPY --chown=bridgeguard:bridgeguard backend/app ./app
COPY --chown=bridgeguard:bridgeguard backend/scripts ./scripts
COPY --from=frontend-build --chown=bridgeguard:bridgeguard /build/frontend/dist ./static

USER bridgeguard
EXPOSE 8000
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["sh", "-c", "gunicorn -k uvicorn.workers.UvicornWorker app.main:app --bind 0.0.0.0:${PORT} --timeout 300 --workers 1 --access-logfile - --error-logfile -"]
