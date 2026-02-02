# scipy-base: Foundation image for scientific Python
# Includes: numpy, scipy, matplotlib, pandas, ipython
# Size target: ~800MB
FROM python:3.11-slim

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="Scientific Python base image"
LABEL org.opencontainers.image.licenses="MIT"

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

# Install system dependencies for scientific computing
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Required for numpy/scipy
    libopenblas0 \
    libgomp1 \
    # Required for matplotlib
    libfreetype6 \
    libpng16-16 \
    # Useful utilities
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Install core scientific Python stack
RUN pip install --no-cache-dir \
    numpy>=1.26 \
    scipy>=1.12 \
    matplotlib>=3.8 \
    pandas>=2.2 \
    ipython>=8.0 \
    # Commonly needed utilities
    pint \
    tqdm \
    h5py \
    pyyaml \
    && rm -rf ~/.cache/pip

# Create workspace
WORKDIR /workspace

# Verify installation
RUN python -c "import numpy; import scipy; import matplotlib; import pandas; print('scipy-base ready')"

CMD ["python3"]
