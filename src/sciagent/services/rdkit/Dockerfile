# rdkit: Cheminformatics toolkit for Python
# Extends scipy-base for numerical/visualization integration
FROM ghcr.io/sciagent-ai/scipy-base:latest

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="RDKit cheminformatics environment"
LABEL org.opencontainers.image.licenses="MIT"

# Install system dependencies for RDKit Draw module
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxrender1 \
    libxext6 \
    libexpat1 \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Install RDKit and useful chemistry extensions
RUN pip install --no-cache-dir \
    rdkit>=2024.3.1 \
    && rm -rf ~/.cache/pip

# Verify installation
RUN python -c "import rdkit; from rdkit import Chem; from rdkit.Chem import AllChem, Descriptors, Draw; print(f'rdkit {rdkit.__version__} ready')"

CMD ["python3"]
