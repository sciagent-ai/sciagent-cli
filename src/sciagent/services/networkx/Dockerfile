# networkx: Graph and network analysis library
# Includes: NetworkX with visualization and scientific computing support
FROM python:3.11-slim

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="NetworkX - Python package for complex network analysis"
LABEL org.opencontainers.image.licenses="BSD-3-Clause"

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

# Install system dependencies
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

# Install NetworkX and common dependencies
RUN pip install --no-cache-dir \
    networkx>=3.0 \
    numpy>=1.26 \
    scipy>=1.12 \
    matplotlib>=3.8 \
    pandas>=2.2 \
    && rm -rf ~/.cache/pip

# Create workspace
WORKDIR /workspace

# Verify installation
RUN python -c "import networkx as nx; G = nx.Graph(); G.add_edge(1, 2); print(f'NetworkX {nx.__version__} ready')"

CMD ["python3"]
