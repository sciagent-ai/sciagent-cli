# biopython: Computational biology and bioinformatics toolkit
# Includes: Bio module for sequence analysis, structure parsing, phylogenetics
FROM python:3.11-slim

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="Biopython - Computational biology and bioinformatics toolkit"
LABEL org.opencontainers.image.licenses="BSD-3-Clause"

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Required for numpy/biopython C extensions
    libopenblas0 \
    libgomp1 \
    # Useful utilities
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Install Biopython and common dependencies
RUN pip install --no-cache-dir \
    biopython>=1.86 \
    numpy>=1.26 \
    matplotlib>=3.8 \
    && rm -rf ~/.cache/pip

# Create workspace
WORKDIR /workspace

# Verify installation
RUN python -c "from Bio import SeqIO, Seq; from Bio.Seq import Seq; s = Seq('ATGCGATCGATCG'); print(f'Biopython ready: {s.complement()}')"

CMD ["python3"]
