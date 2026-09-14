ARG APT_DEBIAN_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian
ARG APT_SECURITY_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian-security
ARG PYPI_INDEX_URL=https://mirror.f123.pub/repository/pypi/simple
ARG NPM_REGISTRY=https://registry.npmmirror.com

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

# Final stage
# Pin to bookworm: keeps the release image on Python 3.12 while
# avoiding Debian trixie's perl 5.40.x exposure (CVE-2026-12087, no patch yet),
# and keeps the runtime base on the stable Debian release.
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
ENV UV_COMPILE_BYTECODE=1
ENV UV_DEFAULT_INDEX=${PYPI_INDEX_URL}
ENV PIP_INDEX_URL=${PYPI_INDEX_URL}

ENV PATH=/app/.venv/bin:/root/.local/bin:$PATH

# Copy project metadata first and install third-party dependencies without the
# project package to keep the dependency layer cacheable.
COPY pyproject.toml .
COPY setup.py .
COPY uv.lock .
RUN uv sync --frozen --no-dev --extra api --extra offline --no-install-project --no-editable

# Copy project sources after the dependency layer.
COPY lightrag/ ./lightrag/
COPY --from=frontend-builder /app/lightrag/api/webui ./lightrag/api/webui

# Install dependencies with uv sync (uses locked versions from uv.lock)
# and ensure pip is available for runtime installs.
RUN uv sync --frozen --no-dev --extra api --extra offline --no-editable \
    && /app/.venv/bin/python -m ensurepip --upgrade

# Create persistent data directories AFTER package installation
RUN mkdir -p /app/data/rag_storage /app/data/inputs /app/data/prompts

ENV WORKING_DIR=/app/data/rag_storage
ENV INPUT_DIR=/app/data/inputs
ENV PROMPT_DIR=/app/data/prompts

# Create a non-root user (CIS Docker 4.1) and install gosu for privilege drop.
# Fixed UID/GID 1000 gives predictable ownership for bind-mounts / PVCs.
# libcairo2 is the native library cairosvg (SVG->PNG rasterization for native
# markdown images) binds to via cffi at runtime; cairosvg installs fine without
# it but svg2png() fails with "no library called cairo-2 was found".
# chown -R /app makes the venv (pipmaster installs packages at runtime) and
# data dirs writable.
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
