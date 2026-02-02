# gromacs: Molecular dynamics simulation package
# Uses conda for complex dependencies (FFTW, MPI, etc.)
# Size target: ~1.5GB
FROM continuumio/miniconda3:latest

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="GROMACS molecular dynamics simulation environment"
LABEL org.opencontainers.image.licenses="LGPL-2.1"

ENV PYTHONUNBUFFERED=1

# Install GROMACS and common dependencies from conda-forge
RUN conda install -y -c conda-forge -c bioconda \
    gromacs \
    numpy \
    scipy \
    matplotlib \
    mdanalysis \
    && conda clean -afy

# Create workspace
WORKDIR /workspace

# Verify installation
RUN gmx --version | head -5

CMD ["bash"]
