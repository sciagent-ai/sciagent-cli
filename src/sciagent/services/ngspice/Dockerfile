# ngspice: Open-source circuit simulator (SPICE)
# Includes: ngspice CLI, shared library, and PySpice Python bindings
# Size target: ~500MB
FROM python:3.11-slim

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="NGSpice circuit simulation environment with PySpice"
LABEL org.opencontainers.image.licenses="BSD-3-Clause"

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

# Install ngspice and system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ngspice \
    libngspice0 \
    libngspice0-dev \
    # For building Python extensions if needed
    gcc \
    # Useful utilities
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Install Python packages for circuit simulation
RUN pip install --no-cache-dir \
    numpy>=1.26 \
    scipy>=1.12 \
    matplotlib>=3.8 \
    PySpice>=1.5 \
    && rm -rf ~/.cache/pip

# Create workspace
WORKDIR /workspace

# Verify installation
RUN ngspice --version && python -c "import PySpice; print('ngspice + PySpice ready')"

CMD ["python3"]
