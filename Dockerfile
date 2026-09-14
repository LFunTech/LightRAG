ARG APT_DEBIAN_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian
ARG APT_SECURITY_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian-security
ARG PYPI_INDEX_URL=https://mirror.f123.pub/repository/pypi/simple
ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG RUSTUP_DIST_SERVER=https://mirrors.tuna.tsinghua.edu.cn/rustup
ARG RUSTUP_UPDATE_ROOT=https://mirrors.tuna.tsinghua.edu.cn/rustup/rustup
ARG RUSTUP_INIT_URL=https://mirrors.tuna.tsinghua.edu.cn/rustup/rustup/dist/x86_64-unknown-linux-gnu/rustup-init

# Frontend build stage. The Woodpecker path uses Kaniko on an amd64 runner,
# so keep this stage free of BuildKit-only platform directives.
FROM docker-hub.f123.pub/base/bun:1-lightrag-9114c058aeae@sha256:9114c058aeae42162ee16dd5084b95fe9473970bb6bcb5b232ab1630f0546895 AS frontend-builder

ARG NPM_REGISTRY

ENV NPM_CONFIG_REGISTRY=${NPM_REGISTRY}

WORKDIR /app

# Copy frontend source code
COPY lightrag_webui/ ./lightrag_webui/

# Build frontend assets for inclusion in the API package
RUN cd lightrag_webui \
    && bun install --frozen-lockfile --registry "$NPM_CONFIG_REGISTRY" \
    && bun run build

# Python build stage - using uv for faster package installation
FROM docker-hub.f123.pub/base/uv:python3.12-bookworm-slim-lightrag-e5b65587bce7@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 AS builder

ARG APT_DEBIAN_MIRROR
ARG APT_SECURITY_MIRROR
ARG PYPI_INDEX_URL
ARG RUSTUP_DIST_SERVER
ARG RUSTUP_UPDATE_ROOT
ARG RUSTUP_INIT_URL

ENV DEBIAN_FRONTEND=noninteractive
ENV UV_SYSTEM_PYTHON=1
ENV UV_COMPILE_BYTECODE=1
ENV UV_DEFAULT_INDEX=${PYPI_INDEX_URL}
ENV PIP_INDEX_URL=${PYPI_INDEX_URL}
ENV RUSTUP_DIST_SERVER=${RUSTUP_DIST_SERVER}
ENV RUSTUP_UPDATE_ROOT=${RUSTUP_UPDATE_ROOT}

WORKDIR /app

# Install system deps (Rust is required by some wheels)
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i \
            -e "s|https://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
            -e "s|http://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
            -e "s|https://security.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
            -e "s|http://security.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
            -e "s|https://deb.debian.org/debian|${APT_DEBIAN_MIRROR}|g" \
            -e "s|http://deb.debian.org/debian|${APT_DEBIAN_MIRROR}|g" \
            /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i \
            -e "s|https://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
            -e "s|http://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
            -e "s|https://security.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
            -e "s|http://security.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
            -e "s|https://deb.debian.org/debian|${APT_DEBIAN_MIRROR}|g" \
            -e "s|http://deb.debian.org/debian|${APT_DEBIAN_MIRROR}|g" \
            /etc/apt/sources.list; \
    fi; \
    printf 'Acquire::Retries "5";\nAcquire::http::Timeout "30";\nAcquire::https::Timeout "30";\n' > /etc/apt/apt.conf.d/80-ci-retries; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        curl \
        build-essential \
        pkg-config; \
    rm -rf /var/lib/apt/lists/*; \
    rustup_init=/tmp/rustup-init; \
    curl --retry 5 --retry-delay 3 --connect-timeout 15 --max-time 180 \
        --proto '=https' --tlsv1.2 -sSfL \
        "${RUSTUP_INIT_URL}" \
        -o "${rustup_init}"; \
    chmod +x "${rustup_init}"; \
    "${rustup_init}" -y --no-modify-path --profile minimal; \
    rm -f "${rustup_init}"

ENV PATH="/root/.cargo/bin:/root/.local/bin:${PATH}"

# Ensure shared data directory exists for uv caches
RUN mkdir -p /root/.local/share/uv

# Copy project metadata and sources
COPY pyproject.toml .
COPY setup.py .
COPY uv.lock .

# Install base, API, and offline extras without the project to improve caching
RUN uv sync --frozen --no-dev --extra api --extra offline --no-install-project --no-editable

# Copy project sources after dependency layer
COPY lightrag/ ./lightrag/

# Include pre-built frontend assets from the previous stage
COPY --from=frontend-builder /app/lightrag/api/webui ./lightrag/api/webui

# Sync project in non-editable mode and ensure pip is available for runtime installs
RUN uv sync --frozen --no-dev --extra api --extra offline --no-editable \
    && /app/.venv/bin/python -m ensurepip --upgrade

# Prepare offline cache directory, pre-populate tiktoken data, and download the
# pinned spaCy model wheels for the docx smart_heading engine parameter.
# Use uv run to execute commands from the virtual environment
RUN mkdir -p /app/data/tiktoken /app/spacy_models \
    && uv run lightrag-download-cache --cache-dir /app/data/tiktoken --spacy-dir /app/spacy_models || status=$?; \
    if [ -n "${status:-}" ] && [ "$status" -ne 0 ] && [ "$status" -ne 2 ]; then exit "$status"; fi

# Final stage
# Pin to bookworm: keeps Python 3.12 (venv compat with the builder stage) while
# avoiding Debian trixie's perl 5.40.x exposure (CVE-2026-12087, no patch yet),
# and aligns the final Debian release with the builder (also bookworm).
FROM docker-hub.f123.pub/base/python:3.12-slim-bookworm-lightrag-782412e85d0f@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254

ARG APT_DEBIAN_MIRROR
ARG APT_SECURITY_MIRROR
ARG PYPI_INDEX_URL
ARG LIGHTRAG_IMAGE_SOURCE="https://github.com/LFunTech/LightRAG"
ARG LIGHTRAG_IMAGE_REVISION=""
ARG LIGHTRAG_IMAGE_VERSION=""

LABEL org.opencontainers.image.source=$LIGHTRAG_IMAGE_SOURCE \
      org.opencontainers.image.revision=$LIGHTRAG_IMAGE_REVISION \
      org.opencontainers.image.version=$LIGHTRAG_IMAGE_VERSION

WORKDIR /app

# Install uv for package management
COPY --from=docker-hub.f123.pub/base/uv:latest-lightrag-b485bd65cc2c@sha256:b485bd65cc2cf1c9a93b3554012c9c3778cf7b1b5fd3d3096ce9e1226c97e1e6 /uv /usr/local/bin/uv

ENV UV_SYSTEM_PYTHON=1
ENV UV_DEFAULT_INDEX=${PYPI_INDEX_URL}
ENV PIP_INDEX_URL=${PYPI_INDEX_URL}

# Copy installed packages and application code
COPY --from=builder /root/.local /root/.local
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/lightrag ./lightrag
COPY pyproject.toml .
COPY setup.py .
COPY uv.lock .

# Ensure the installed scripts are on PATH
ENV PATH=/app/.venv/bin:/root/.local/bin:$PATH

# Install dependencies with uv sync (uses locked versions from uv.lock)
# and ensure pip is available for runtime installs. The pinned spaCy model
# wheels (docx smart_heading) MUST be installed after uv sync — sync is exact
# and would remove packages that are not in the lock. Kaniko cannot use
# BuildKit bind mounts, so copy the wheels from the builder and delete them
# in the same layer after installation.
COPY --from=builder /app/spacy_models /tmp/spacy_models
RUN uv sync --frozen --no-dev --extra api --extra offline --no-editable \
    && /app/.venv/bin/python -m ensurepip --upgrade \
    && /app/.venv/bin/python -m pip install --no-index --no-cache-dir \
        --find-links=/tmp/spacy_models zh_core_web_sm en_core_web_sm \
    && rm -rf /tmp/spacy_models

# Create persistent data directories AFTER package installation
RUN mkdir -p /app/data/rag_storage /app/data/inputs /app/data/prompts /app/data/tiktoken

# Copy offline cache into the newly created directory
COPY --from=builder /app/data/tiktoken /app/data/tiktoken

# Point to the prepared cache
ENV TIKTOKEN_CACHE_DIR=/app/data/tiktoken
ENV WORKING_DIR=/app/data/rag_storage
ENV INPUT_DIR=/app/data/inputs
ENV PROMPT_DIR=/app/data/prompts

# Create a non-root user (CIS Docker 4.1) and install gosu for privilege drop.
# Fixed UID/GID 1000 gives predictable ownership for bind-mounts / PVCs.
# libcairo2 is the native library cairosvg (SVG->PNG rasterization for native
# markdown images) binds to via cffi at runtime; cairosvg installs fine without
# it but svg2png() fails with "no library called cairo-2 was found".
# chown -R /app MUST run after every data COPY above so the venv (pipmaster
# installs packages at runtime), data dirs, and the tiktoken cache are writable.
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i \
            -e "s|https://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
            -e "s|http://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
            -e "s|https://security.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
            -e "s|http://security.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
            -e "s|https://deb.debian.org/debian|${APT_DEBIAN_MIRROR}|g" \
            -e "s|http://deb.debian.org/debian|${APT_DEBIAN_MIRROR}|g" \
            /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i \
            -e "s|https://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
            -e "s|http://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
            -e "s|https://security.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
            -e "s|http://security.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
            -e "s|https://deb.debian.org/debian|${APT_DEBIAN_MIRROR}|g" \
            -e "s|http://deb.debian.org/debian|${APT_DEBIAN_MIRROR}|g" \
            /etc/apt/sources.list; \
    fi; \
    printf 'Acquire::Retries "5";\nAcquire::http::Timeout "30";\nAcquire::https::Timeout "30";\n' > /etc/apt/apt.conf.d/80-ci-retries; \
    apt-get update; \
    apt-get install -y --no-install-recommends gosu libcairo2; \
    rm -rf /var/lib/apt/lists/*; \
    groupadd -g 1000 lightrag; \
    useradd -u 1000 -g lightrag -m -d /home/lightrag -s /usr/sbin/nologin lightrag; \
    chown -R lightrag:lightrag /app /home/lightrag

# HOME and cache dirs for the non-root user so pipmaster's runtime pip installs
# never fall back to an unwritable /root or a missing HOME.
ENV HOME=/home/lightrag \
    XDG_CACHE_HOME=/home/lightrag/.cache \
    PIP_CACHE_DIR=/home/lightrag/.cache/pip \
    UV_CACHE_DIR=/home/lightrag/.cache/uv

# Entrypoint starts as root, fixes mount ownership, then drops to lightrag.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Expose API port
EXPOSE 9621

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "lightrag.api.lightrag_server"]
