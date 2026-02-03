# qiskit: IBM Quantum computing SDK
# Includes: Qiskit core, Aer simulator, visualization tools
FROM python:3.11-slim

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="Qiskit - IBM open-source SDK for quantum computing"
LABEL org.opencontainers.image.licenses="Apache-2.0"

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

# Install Qiskit and dependencies
RUN pip install --no-cache-dir \
    qiskit>=1.0 \
    qiskit-aer>=0.14 \
    numpy>=1.26 \
    scipy>=1.12 \
    matplotlib>=3.8 \
    pylatexenc \
    && rm -rf ~/.cache/pip

# Create workspace
WORKDIR /workspace

# Verify installation
RUN python -c "import qiskit; from qiskit import QuantumCircuit; from qiskit_aer import AerSimulator; print(f'Qiskit {qiskit.__version__} ready')"

CMD ["python3"]
