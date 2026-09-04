# Coolify/Docker image. Deps installed with uv, no pip.
# Runs as non-root user bridge. exec_run stays opt-in via ENABLE_EXEC_RUN
# and, when enabled, runs inside this container as bridge, which reduces
# its blast radius versus root but is still a real shell.
FROM python:3.13-slim AS base

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-editable

COPY src ./src
COPY README.md ./

# Non-root app user plus state dir for the default
# TASK_STATE_PATH=/var/lib/opencode-mcp-bridge/tasks.json.
# /app stays root-owned (bridge reads it); only the state dir is chowned.
RUN useradd --system --create-home --home-dir /home/bridge --shell /usr/sbin/nologin bridge \
    && mkdir -p /var/lib/opencode-mcp-bridge \
    && chown bridge:bridge /var/lib/opencode-mcp-bridge \
    && chmod 0750 /var/lib/opencode-mcp-bridge

ENV TASK_STATE_PATH=/var/lib/opencode-mcp-bridge/tasks.json

USER bridge

EXPOSE 8087

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8087/health', timeout=4)"

CMD ["/app/.venv/bin/python", "-m", "opencode_mcp_bridge.server"]
