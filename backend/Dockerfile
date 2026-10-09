# syntax=docker/dockerfile:1.7
#
# Reproducible build: base images are pinned by digest and dependencies are installed
# from uv.lock with --frozen (the build fails if the lockfile is out of date).
# Refresh digests deliberately (Dependabot opens PRs for both images).

FROM ghcr.io/astral-sh/uv:0.9.22@sha256:2320e6c239737dc73cccce393a8bb89eba2383d17018ee91a59773df802c20e6 AS uv

FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /srv
COPY backend/pyproject.toml backend/uv.lock backend/README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/app ./app
COPY backend/evals ./evals
RUN uv sync --frozen --no-dev

FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/srv/.venv/bin:$PATH" \
    DATA_DIR=/srv/data
RUN useradd --create-home --uid 10001 pagermind
WORKDIR /srv
COPY --from=build /srv /srv
COPY data/service_catalog.yaml ./data/service_catalog.yaml
USER pagermind
EXPOSE 8000

# Client IPs are derived in-app from X-Forwarded-For using TRUSTED_PROXY_HOPS, so uvicorn's
# proxy-header rewriting stays off. The worker overrides this command (see render.yaml).
CMD ["sh", "-c", "uvicorn --factory app.main:create_app --host 0.0.0.0 --port ${PORT:-8000} --timeout-graceful-shutdown 20"]
