# Agentic OS image — runs the Python orchestrator AND the generated
# Playwright + TypeScript tests, so the image carries Python 3.13 + Node.js LTS
# + Playwright (Chromium + browser dependencies). The Java/Maven toolchain was
# retired in favour of npm/Playwright per ADR-0002 (issue #389).
#
# Architecture: the OS runs in Docker; the SUT is external (web/API URL +
# optional DB), never started by the OS. See
# docs/adr/ADR-0001-os-in-docker-sut-external.md.
#
# The SUT is NOT in this image; no docker-in-docker.
#
# Build:   docker build -t agentic-os:dev .
# Smoke:   docker run --rm agentic-os:dev doctor
# Compose / volumes / networking / healthcheck land in #353–#355.

# --- Pinned versions (override with --build-arg) ----------------------------
# Base digest pins python:3.13-slim-bookworm (linux/amd64) for reproducibility.
# Refresh with: docker buildx imagetools inspect python:3.13-slim-bookworm
ARG PYTHON_IMAGE=python:3.13-slim-bookworm@sha256:e4fa1f978c539608a10cdf74700ac32a3f719dfc6e8b6b6001da82deb36302a2
# Node.js LTS major (installed from the gpg-signed NodeSource apt repo).
ARG NODE_MAJOR=22
# Playwright release whose browser builds we install. Keep this in lock-step
# with the generated framework's package.json (templates/playwright-ts-framework,
# issue #369): @playwright/test ^1.49.0.
ARG PLAYWRIGHT_VERSION=1.49.0

# ============================================================================
# Stage 1 — Python wheel build (kept separate so build tooling never ships).
# ============================================================================
FROM ${PYTHON_IMAGE} AS pybuild

WORKDIR /build
RUN pip install --no-cache-dir --upgrade pip build

# Only what the wheel needs: package sources + project metadata + readme.
COPY pyproject.toml README.md ./
COPY scripts/agentic-os/agentic_os ./scripts/agentic-os/agentic_os
RUN python -m build --wheel --outdir /dist

# ============================================================================
# Stage 2 — Runtime: Python + Node.js LTS + Playwright (Chromium).
# ============================================================================
FROM ${PYTHON_IMAGE} AS runtime

ARG NODE_MAJOR
ARG PLAYWRIGHT_VERSION

LABEL org.opencontainers.image.title="Agentic OS" \
      org.opencontainers.image.description="Agentic web-testing orchestrator (Python) + Playwright/TypeScript test toolchain. SUT is external." \
      org.opencontainers.image.source="https://github.com/holi87/agentic-web-testing"

ENV DEBIAN_FRONTEND=noninteractive \
    # Shared, world-readable Playwright browser dir so the non-root runtime
    # user reuses the browsers warmed at build time.
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONUNBUFFERED=1

# --- Node.js LTS (gpg-signed NodeSource apt repo) + base utilities ----------
# git is required at runtime: the patch-apply gate runs `git apply`
# (gates/patch_gate.py) and the SUT git integration (sut_repo.py / repair.py)
# shells out to git. slim-bookworm does not ship it.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git; \
    mkdir -p /etc/apt/keyrings; \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg; \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends nodejs; \
    rm -rf /var/lib/apt/lists/*; \
    node -v; \
    npm -v

# --- Playwright browsers + system deps via the Playwright CLI ---------------
# Install the pinned @playwright/test globally (gives the `playwright` CLI and
# matches the generated framework's runner), then install the Chromium build
# plus its apt shared libraries into $PLAYWRIGHT_BROWSERS_PATH. The dir is made
# world-readable so the non-root runtime user can launch the browser.
RUN set -eux; \
    npm install -g "@playwright/test@${PLAYWRIGHT_VERSION}"; \
    # The previous layer cleared the apt lists; `--with-deps` shells out to
    # `apt-get install`, so refresh them first, then clean up again.
    apt-get update; \
    playwright install --with-deps chromium; \
    rm -rf /var/lib/apt/lists/*; \
    chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH"; \
    playwright --version

# --- Agentic OS: install the wheel, then drop in the repo tree --------------
# The wheel gives PyYAML + an importable `agentic_os`; the repo tree provides
# the CLI shim, dashboard templates, prompts and config example that live
# outside the Python package.
COPY --from=pybuild /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl

WORKDIR /app
COPY pyproject.toml README.md run-tests.sh ./
COPY scripts ./scripts
COPY docs ./docs
# Config: ship the example + supporting files only — NEVER the operator's live
# config/agentic-os.yml (it is provided at runtime via volume/env, #354/#355).
COPY config/agentic-os.yml.example ./config/agentic-os.yml.example
COPY config/prompts ./config/prompts
COPY config/provider-rates.yml config/skills.yml ./config/
# Default baked config so `doctor` is green out of the box; a mounted
# config/ volume overrides it.
RUN cp config/agentic-os.yml.example config/agentic-os.yml

# --- Non-root runtime user --------------------------------------------------
# The runtime root is pre-created and owned by `agentic` so that a fresh NAMED
# volume mounted on it (compose, #353/#354) inherits uid/gid 10001 ownership on
# first init. Without this, Docker creates the volume root-owned and the
# non-root process cannot write state.db. The artifact dirs (reports/bugs/
# evidence) are bind mounts whose ownership the host/Docker Desktop maps.
RUN set -eux; \
    groupadd --gid 10001 agentic; \
    useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash agentic; \
    mkdir -p /app/agentic-os-runtime; \
    chown -R agentic:agentic /app
USER agentic

# Dashboard port (compose publishes it — #353).
EXPOSE 8765

# The CLI shim sets PYTHONPATH and dispatches to `python -m agentic_os`; with
# no git in the container it resolves the repo root via its own location.
ENTRYPOINT ["/app/scripts/agentic-os.sh"]
CMD ["doctor"]
