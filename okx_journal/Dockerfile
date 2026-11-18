# Build context: a directory with pyproject.toml, uv.lock, README.md and src/
# (the repository root, or the bundle produced by `make app-bundle`).
# Not BUILD_FROM: recent Supervisors ignore build.yaml and pass the Alpine HA base image
# as BUILD_FROM, which lacks Python 3.12 and glibc wheels for pyarrow.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

FROM ${PYTHON_IMAGE} AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE}
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    HTH_DATA_DIR=/data \
    MPLCONFIGDIR=/tmp/matplotlib \
    PYTHONUNBUFFERED=1
LABEL io.hass.type="addon" io.hass.version="0.1.0"
WORKDIR /data
CMD ["ha-trading-helper", "journal", "serve"]
