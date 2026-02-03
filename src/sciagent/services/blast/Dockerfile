# blast: NCBI BLAST+ sequence alignment tools
# Based on official NCBI Docker image with Python/Biopython integration
FROM ncbi/blast:2.17.0

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="NCBI BLAST+ - Basic Local Alignment Search Tool for sequence similarity searching"
LABEL org.opencontainers.image.licenses="Unlicense"

ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive

# Install Python and Biopython for BLAST result parsing
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Install Biopython for parsing BLAST results
RUN pip3 install --no-cache-dir \
    biopython>=1.86 \
    && rm -rf ~/.cache/pip

# Create workspace
WORKDIR /workspace

# Verify installation
RUN blastn -version && \
    blastp -version && \
    python3 -c "from Bio.Blast import NCBIXML; print('BLAST+ and Biopython ready')"

CMD ["/bin/bash"]
