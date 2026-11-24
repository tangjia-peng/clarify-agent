FROM ghcr.io/astral-sh/uv:0.11.7 AS uv
FROM node:24-bookworm-slim AS node
FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libstdc++6 libatomic1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/
COPY --from=node /usr/local/ /usr/local/

ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin \
    WEB_HOST=0.0.0.0 PORT=8765 WEB_WORKSPACE_ROOT=/data/workspaces \
    MCP_FETCH_COMMAND=mcp-server-fetch MCP_FILESYSTEM_COMMAND=mcp-server-filesystem \
    FETCH_ALLOW_PRIVATE_IPS=false MCP_FETCH_NO_JS=true

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project \
    && uv tool install 'mcp-server-fetch==2026.11.11' \
    && npm install --global --omit=dev '@modelcontextprotocol/server-filesystem@2.3.0' \
    && groupadd --gid 10001 mellow \
    && useradd --uid 10001 --gid mellow --no-create-home mellow \
    && mkdir -p /data/workspaces \
    && chown -R mellow:mellow /data

COPY src ./src
COPY web ./web
COPY knowledge ./knowledge
COPY web_main.py ./

ENV PATH="/app/.venv/bin:${PATH}"
USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8765')+'/health',timeout=4)"
CMD ["python", "web_main.py"]
