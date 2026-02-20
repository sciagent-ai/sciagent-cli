# sci-core: Core scientific computing foundation
# Covers most scientific computing tasks
FROM python:3.11-slim

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="Core scientific computing - NumPy, SciPy, Pandas, ML, optimization, networks"
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
    # Required for igraph
    libxml2 \
    # Useful utilities
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Install core scientific Python stack
RUN pip install --no-cache-dir \
    # Fundamentals
    numpy>=1.26 \
    scipy>=1.12 \
    pandas>=2.2 \
    matplotlib>=3.8 \
    seaborn>=0.13 \
    # Math & Symbolic
    sympy>=1.12 \
    mpmath>=1.3 \
    # Optimization
    cvxpy>=1.4 \
    optuna>=3.5 \
    scikit-optimize>=0.10 \
    # Networks & Graphs
    networkx>=3.2 \
    igraph>=0.11 \
    # Data & ML
    scikit-learn>=1.4 \
    statsmodels>=0.14 \
    # File formats
    h5py>=3.10 \
    netcdf4>=1.6 \
    xarray>=2024.1 \
    # Utilities
    ipython>=8.0 \
    pint \
    tqdm \
    pyyaml \
    && rm -rf ~/.cache/pip

# Create workspace
WORKDIR /workspace

# Verify installation
RUN python -c "\
import numpy, scipy, pandas, matplotlib, seaborn; \
import sympy, mpmath; \
import cvxpy, optuna; \
import networkx, igraph; \
import sklearn, statsmodels; \
import h5py, netCDF4, xarray; \
print('sci-core ready')"

CMD ["python3"]
