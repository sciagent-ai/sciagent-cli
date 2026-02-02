# cvxpy: Convex optimization modeling for Python
# Extends scipy-base for numerical foundation
# Size target: ~800MB (adds ~200MB to scipy-base)
FROM ghcr.io/sciagent-ai/scipy-base:latest

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="CVXPY convex optimization environment"
LABEL org.opencontainers.image.licenses="MIT"

# Install build dependencies needed for some solvers
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Install CVXPY and common solvers
RUN pip install --no-cache-dir \
    cvxpy>=1.5 \
    osqp>=0.6 \
    ecos>=2.0 \
    scs>=3.2 \
    clarabel>=0.6 \
    && rm -rf ~/.cache/pip

# Verify installation
RUN python -c "import cvxpy as cp; import numpy as np; x = cp.Variable(); prob = cp.Problem(cp.Minimize((x-1)**2)); prob.solve(); print(f'cvxpy {cp.__version__} ready, test solution: x={x.value:.4f}')"

CMD ["python3"]
