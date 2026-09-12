# ── Commit this image is built from, shown by /health ─────────────────────────
# Dokploy does not pass the commit as a build arg (Dokploy/dokploy#4006 is still
# open), but it clones the repository with its .git, which ends up in the build
# context. Read it from there. Should a DOKPLOY_COMMIT_* build arg arrive one
# day, it wins. Without .git (a tarball, an archive), "unknown" rather than a
# failed build.
FROM alpine:3.22 AS revision
RUN apk add --no-cache git
ARG DOKPLOY_COMMIT_HASH=
ARG DOKPLOY_COMMIT_MESSAGE=
WORKDIR /src
COPY . .
RUN git config --global --add safe.directory /src && mkdir /revision && \
    { [ -n "$DOKPLOY_COMMIT_HASH" ] && echo "$DOKPLOY_COMMIT_HASH" || git rev-parse HEAD 2>/dev/null || echo unknown; } > /revision/sha && \
    { [ -n "$DOKPLOY_COMMIT_MESSAGE" ] && echo "$DOKPLOY_COMMIT_MESSAGE" || git log -1 --pretty=%s 2>/dev/null || echo unknown; } > /revision/message

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SPLITTICKET_HOST=0.0.0.0 \
    SPLITTICKET_PORT=8787 \
    SPLITTICKET_DATA_DIR=/data

# Non-root user: the service only needs to write to /data.
RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /app

# SQLite database, receipt photos and Tricount device credentials.
RUN mkdir -p /data && chown -R appuser:appuser /data /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# Only these two small files leave the first stage: .git never reaches the image.
COPY --from=revision /revision ./revision

USER appuser

EXPOSE 8787

VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8787/health').read()" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"]
