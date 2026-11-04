# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm AS build

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Dependencies first, so source edits do not invalidate this layer.
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim-bookworm

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin ipping

COPY --from=build /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    IPPING_HOST=0.0.0.0 \
    IPPING_PORT=8000

USER ipping
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/v1/health/live' % os.environ['IPPING_PORT'], timeout=4)"]

# `migrate` is a separate one-shot run (see the stack's compose file); the server never migrates.
ENTRYPOINT ["ipping-backend"]
CMD ["serve"]
