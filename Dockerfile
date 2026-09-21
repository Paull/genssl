# syntax=docker/dockerfile:1.7

FROM oven/bun:1.3.14 AS frontend-build

WORKDIR /build
COPY package.json bun.lock tsconfig.json ./
RUN bun install --frozen-lockfile
COPY frontend ./frontend
COPY scripts/build.ts ./scripts/build.ts
COPY web/index.html ./web/index.html
RUN bun run build

FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CERT_HOST=0.0.0.0 \
    CERT_PORT=8080 \
    CERT_DB=/app/out/certificates.db \
    CERT_WEB_DIR=/app/web

RUN apt-get update \
    && apt-get install --no-install-recommends --yes bash ca-certificates openssl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system certmanager \
    && useradd --system --gid certmanager --home-dir /app --no-create-home --shell /usr/sbin/nologin certmanager

WORKDIR /app
COPY --from=frontend-build /build/web ./web
COPY server.py ca.cnf flush.sh gen_root_cert.sh gen_server_cert.sh gen_client_cert.sh ./
COPY scripts ./scripts

RUN chmod 0755 ./*.sh server.py ./scripts/*.sh ./scripts/local_cert.py \
    && mkdir -p /app/out \
    && chown --recursive certmanager:certmanager /app

USER certmanager
EXPOSE 8080
VOLUME ["/app/out"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3).read()"

ENTRYPOINT ["python3", "server.py"]
CMD ["--host", "0.0.0.0", "--port", "8080", "--db", "/app/out/certificates.db", "--web-dir", "/app/web"]
