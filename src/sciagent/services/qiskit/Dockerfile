# qiskit: IBM Quantum computing SDK
# Extends sci-core with Qiskit and Aer simulator
FROM ghcr.io/sciagent-ai/sci-core:latest

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="Qiskit - IBM open-source SDK for quantum computing"
LABEL org.opencontainers.image.licenses="Apache-2.0"

# Install Qiskit (sci-core already has numpy, scipy, matplotlib)
RUN pip install --no-cache-dir \
    qiskit>=1.0 \
    qiskit-aer>=0.14 \
    pylatexenc \
    && rm -rf ~/.cache/pip

# Verify installation
RUN python -c "import qiskit; from qiskit import QuantumCircuit; from qiskit_aer import AerSimulator; print(f'Qiskit {qiskit.__version__} ready')"

CMD ["python3"]
