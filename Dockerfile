# Olympus i Docker: serveren (olympus web) bygget oppå llama.cpp sitt offisielle serverbilde (samme biblioteker).
#   docker compose up -d                                                    (Vulkan: AMD/Intel via /dev/dri, eller CPU)
#   docker compose -f docker-compose.yml -f docker-compose.nvidia.yml up -d (NVIDIA, CUDA)
# Variant med LLAMA_IMAGE: server-vulkan (standard) | server-cuda | server (bare CPU).
ARG LLAMA_IMAGE=ghcr.io/ggml-org/llama.cpp:server-vulkan
FROM ${LLAMA_IMAGE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
ENV PATH=/opt/venv/bin:/app:$PATH \
    LD_LIBRARY_PATH=/app \
    OLYMPUS_HOME=/data/olympus \
    OLYMPUS_WEB_HOST=0.0.0.0 \
    HOME=/data
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir . \
    && useradd -m -d /data olympus && mkdir -p /data && chown olympus /data
USER olympus
WORKDIR /data
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fsS -o /dev/null http://127.0.0.1:8787/ || exit 1
ENTRYPOINT []
CMD ["olympus", "web"]
