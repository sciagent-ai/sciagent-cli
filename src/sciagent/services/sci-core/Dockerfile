# sci-core: Core scientific computing foundation
# Extends scipy-base with additional math, optimization, and ML packages
FROM ghcr.io/sciagent-ai/scipy-base:latest

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="Core scientific computing - NumPy, SciPy, Pandas, ML, optimization, networks"
LABEL org.opencontainers.image.licenses="MIT"

# Install additional system dependencies (igraph requires libxml2)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Install additional scientific packages (scipy-base already has numpy, scipy, pandas, matplotlib, pint, h5py, tqdm, pyyaml, ipython)
RUN pip install --no-cache-dir \
    # Visualization
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
    netcdf4>=1.6 \
    xarray>=2024.1 \
    && rm -rf ~/.cache/pip

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
