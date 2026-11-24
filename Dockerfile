# session-timer — single-process FastAPI app with in-memory state.
#
# IMPORTANT: run exactly ONE container/worker per deployment.  All
# session state lives in the process' memory (see DESIGN.md): multiple
# workers or replicas behind one URL space would each serve their own
# unrelated, empty sessions.

FROM python:3.14-slim

# Pinned uv binary (matches the uv.lock revision in this repo).
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /usr/local/bin/

WORKDIR /app

# Runtime dependencies come from pyproject.toml, pinned by uv.lock.
# Phase 1 installs ONLY the dependencies, so the (slow) dep layer is
# cached as long as pyproject.toml/uv.lock are unchanged; source edits
# only invalidate phase 2.
COPY pyproject.toml uv.lock DESIGN.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Phase 2: the project itself (static assets are package data, see
# [tool.setuptools.package-data] in pyproject.toml).  --no-editable
# installs a real copy into the venv instead of a link back to the
# build-context source.
COPY session_timer/ ./session_timer/

# The commit the image was built from (CI passes GIT_VERSION —
# `git describe --tags --dirty --always`: "0.2.0" on a release tag,
# "0.2.0-3-g943694e" past it, "-dirty" appended for uncommitted
# changes — plus GIT_HASH, the full sha; both default to "unknown"
# for local docker builds).  Exposed to the app via the environment
# (config.GIT_VERSION/GIT_HASH -> /api/banner -> the admin ☰ menu) and
# as OCI labels for `docker inspect`.  Kept AFTER the dependency
# phase: a hash-only change re-runs only this cheap layer.
ARG GIT_VERSION=unknown
ARG GIT_HASH=unknown
ENV SESSION_TIMER_GIT_VERSION=${GIT_VERSION} \
    SESSION_TIMER_GIT_HASH=${GIT_HASH}
LABEL org.opencontainers.image.revision=${GIT_HASH} \
      org.opencontainers.image.version=${GIT_VERSION}

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# Run unprivileged.  uv sync installs into /app/.venv, owned by root —
# fine: the app only reads its own files and listens on a port.
RUN useradd --system --create-home appuser \
    && mkdir -p /data && chown appuser:appuser /data
USER appuser

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

# Liveness probe against the dynamic /healthz endpoint: exercises the
# real request path (middleware, routing, the store) rather than a
# static file.  GETs never create a session (creation is lazy, on
# mutation), so probing is safe.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]

# Single worker on purpose (see header comment).  TLS is expected to be
# terminated by a reverse proxy in front of the container.
# --no-server-header: do not advertise "Server: uvicorn" to probers
# (URL-secrecy model; see the root handler's empty 404s in main.py).
CMD ["uvicorn", "session_timer.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]

