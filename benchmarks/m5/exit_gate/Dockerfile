# M5 exit-gate Dockerfile — multi-stage; same image for all 4 frameworks,
# pick at runtime via the FRAMEWORK env (lancelot|fastapi|robyn|blazeio).
#
# Build:
#   docker build -f benchmarks/m5/exit_gate/Dockerfile -t lancelot-exit-gate .
# Run (e.g. Lancelot):
#   docker run -e FRAMEWORK=lancelot -e PORT=8910 -e LC_UPSTREAM_URL=http://mock:8901 \
#              -p 8910:8910 lancelot-exit-gate

# -------- BUILDER STAGE --------
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# Build deps: Rust toolchain (for maturin) + C build for native wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl ca-certificates pkg-config \
    && rm -rf /var/lib/apt/lists/* \
    && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
       | sh -s -- -y --profile minimal --default-toolchain stable
ENV PATH="/root/.cargo/bin:${PATH}"

WORKDIR /build
RUN pip install --upgrade pip maturin

# Build Lancelot wheel from the repo.
COPY . /build/lancelot-src
RUN cd /build/lancelot-src \
    && maturin build --release --out /build/wheels

# -------- RUNTIME STAGE --------
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# Runtime needs only curl for the healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Peer framework deps first (cacheable).
COPY benchmarks/m5/exit_gate/requirements.txt /app/exit_gate_requirements.txt
RUN pip install --upgrade pip \
    && pip install -r /app/exit_gate_requirements.txt

# Pull the Lancelot wheel from the builder + vendored Blazeio source.
COPY --from=builder /build/wheels /tmp/wheels
COPY --from=builder /build/lancelot-src/researchfms/blazeio /tmp/blazeio
RUN pip install /tmp/wheels/*.whl \
    && pip install -e /tmp/blazeio/Blazeio \
    && rm -rf /tmp/wheels

# Bring in the exit-gate apps.
COPY benchmarks/m5/exit_gate /app

ENV FRAMEWORK=lancelot \
    PORT=8910 \
    LC_UPSTREAM_URL=http://127.0.0.1:8901 \
    LC_EMBED_SIZE=4096 \
    LC_BATCH_WINDOW_MS=50 \
    LC_BATCH_MAX=16

HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

# Same image for all 4 frameworks; FRAMEWORK env selects.
CMD ["sh", "-c", "exec python /app/app_${FRAMEWORK}.py"]
