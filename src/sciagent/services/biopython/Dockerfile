# biopython: Computational biology and bioinformatics toolkit
# Extends sci-core with Biopython for sequence analysis and bioinformatics
FROM ghcr.io/sciagent-ai/sci-core:latest

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="Biopython - Computational biology and bioinformatics toolkit"
LABEL org.opencontainers.image.licenses="BSD-3-Clause"

# Install Biopython (sci-core already has numpy, scipy, matplotlib, pandas, etc.)
RUN pip install --no-cache-dir \
    biopython>=1.86 \
    && rm -rf ~/.cache/pip

# Verify installation
RUN python -c "from Bio import SeqIO, Seq; from Bio.Seq import Seq; s = Seq('ATGCGATCGATCG'); print(f'Biopython ready: {s.complement()}')"

CMD ["python3"]
