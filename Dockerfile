# Multi-stage build: produce a slim runtime image with verbatim-ai installed
# from PyPI (or local source when building from a checkout).
# Final image is python:3.12-slim + verbatim-ai installed via pip — about 200MB.
# Default CMD serves verbatim's MCP tools over HTTP (`verbatim-mcp --transport http`) -- that is
# the one thing a container is good for that a laptop-local stdio client is not: being reachable
# over a network. Override the CMD to run any other `verbatim` or `verbatim-mcp` invocation
# instead, e.g. `docker run ghcr.io/qatcod/verbatim-ai verbatim serve --host 0.0.0.0`.

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --upgrade pip build && \
    python -m build --wheel && \
    pip wheel --wheel-dir /wheels dist/*.whl


FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="verbatim-ai" \
      org.opencontainers.image.description="The AI memory layer for engineering teams" \
      org.opencontainers.image.source="https://github.com/qatcod/verbatim" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.documentation="https://qatcod.github.io/verbatim/"

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VERBATIM_DB_PATH=/data/state.db

RUN groupadd --system verbatim && \
    useradd --system --gid verbatim --create-home --home-dir /home/verbatim verbatim && \
    mkdir -p /data && chown -R verbatim:verbatim /data

COPY --from=builder /wheels /tmp/wheels
RUN pip install --no-index --find-links /tmp/wheels verbatim-ai && \
    rm -rf /tmp/wheels

USER verbatim
WORKDIR /home/verbatim

# State (the SQLite state graph verbatim builds from every extraction) lives here and MUST be on
# a mounted volume: a state graph nobody backs up is one you lose. VERBATIM_DB_PATH (set above)
# is the env var verbatim/store.py actually reads to resolve the DB path -- not "VERBATIM_DB".
VOLUME ["/data"]

# Expose the web UI port (`verbatim serve` default) and the MCP HTTP port below.
EXPOSE 8765
EXPOSE 8081

# Default to HTTP, not stdio: a container is only reachable over the network, so HTTP is the only
# transport that makes sense as its default here -- the opposite of the CLI, where stdio stays
# default because Claude Code and Cursor already invoke it with no arguments. That asymmetry is
# deliberate on both sides; do not "fix" it into agreement.
#
# --host 0.0.0.0 is explicit and deliberate here too: `verbatim-mcp`'s own --host default is
# 127.0.0.1 (binding only loopback, so a laptop try-it-out run doesn't open the LAN by surprise),
# which inside a container means unreachable from outside it -- the opposite failure from the
# CLI's. A container needs every interface; that is the one thing containers are for.
#
# verbatim-mcp refuses to start without VERBATIM_MCP_TOKEN set (see mcp_server.main /
# mcp_http.build_app) -- an unconfigured container fails loudly on boot instead of serving a
# team's extracted state anonymously. Pass -e VERBATIM_MCP_TOKEN=<token> at `docker run`.
CMD ["verbatim-mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "8081"]
