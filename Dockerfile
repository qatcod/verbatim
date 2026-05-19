# Multi-stage build: produce a slim runtime image with verbatim-ai installed
# from PyPI (or local source when building from a checkout).
# Final image is python:3.12-slim + verbatim-ai installed via pip — about 200MB.
# Entry point is `verbatim`, so `docker run ghcr.io/qatcod/verbatim-ai <cmd>` works.

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
VOLUME ["/data"]

# Expose the web UI port (`verbatim serve` default).
EXPOSE 8765

ENTRYPOINT ["verbatim"]
CMD ["--help"]
