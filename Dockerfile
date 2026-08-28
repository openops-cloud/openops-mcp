# syntax=docker/dockerfile:1
#
# The MCP server as its own container, serving the http transport for external clients.
#
# The stdio transport is not what this image is for: the API image vendors this repository
# and spawns it per chat request. This image runs alongside the API as a separate service,
# reached by agents at ${OPENOPS_MCP_RESOURCE_URL} and reaching the API at ${OPENOPS_API_URL}.
#
# Two stages so the runtime never carries uv, the lockfile, or a package cache. The
# environment is resolved from uv.lock exactly as CI does (--frozen), so the image cannot
# quietly drift from what was tested.

ARG PYTHON_VERSION=3.13

# ---- Builder stage: resolve the environment from the lockfile ----
FROM python:${PYTHON_VERSION}-alpine AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9.0 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Only the manifests: the environment layer stays cached across source-only changes.
COPY pyproject.toml uv.lock ./

# --no-install-project: the server runs from source via main.py, the same way the API
# image runs it, so the package itself is not installed into the environment.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# ---- Final stage: runtime only ----
FROM python:${PYTHON_VERSION}-alpine

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    # The container is the http deployment. stdio is the API's business.
    MCP_TRANSPORT=http \
    MCP_HTTP_HOST=0.0.0.0 \
    MCP_HTTP_PORT=3020 \
    # No outbound call to pypi.org at startup, and no ASCII banner in collected logs.
    FASTMCP_CHECK_FOR_UPDATES=off \
    FASTMCP_SHOW_SERVER_BANNER=false

# No shell login, no home directory: the process needs neither.
RUN addgroup -S -g 10001 openops \
    && adduser -S -u 10001 -G openops -H -s /sbin/nologin openops

WORKDIR /app

COPY --from=builder --chown=openops:openops /app/.venv ./.venv
COPY --chown=openops:openops main.py ./
COPY --chown=openops:openops openops_mcp ./openops_mcp

ARG VERSION=unknown
ENV OPENOPS_MCP_VERSION=$VERSION

LABEL service=openops-mcp \
      org.opencontainers.image.title="OpenOps MCP" \
      org.opencontainers.image.description="MCP server exposing OpenOps API operations as tools" \
      org.opencontainers.image.source="https://github.com/openops-cloud/openops-mcp" \
      org.opencontainers.image.version=$VERSION

USER openops

EXPOSE 3020

ENTRYPOINT ["python", "main.py"]
