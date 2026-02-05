# pyoptools: Optical design and ray tracing simulation
# Extends scipy-base for numerical foundation
FROM ghcr.io/sciagent-ai/scipy-base:latest

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="pyOpTools optical simulation and ray tracing environment"
LABEL org.opencontainers.image.licenses="GPL-3.0"

# Install build dependencies needed for Cython compilation
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    libeigen3-dev \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Install pyOpTools from GitHub (latest dev version has NumPy API fixes)
RUN pip install --no-cache-dir \
    cython \
    setuptools_scm \
    && pip install --no-cache-dir \
    "git+https://github.com/cihologramas/pyoptools.git" \
    && rm -rf ~/.cache/pip

# Verify installation
RUN python -c "import pyoptools; from pyoptools.raytrace.ray import Ray; from pyoptools.raytrace.system import System; print(f'pyoptools ready')"

CMD ["python3"]
