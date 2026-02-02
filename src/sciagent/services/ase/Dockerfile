# ase: Atomic Simulation Environment
# Python library for working with atoms, molecules, and materials
# Uses conda for scientific dependencies
FROM continuumio/miniconda3:latest

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="ASE - Atomic Simulation Environment for atomistic simulations"
LABEL org.opencontainers.image.licenses="LGPL-2.1"

ENV PYTHONUNBUFFERED=1

# Install ASE and common dependencies from conda-forge
RUN conda install -y -c conda-forge \
    ase \
    numpy \
    scipy \
    matplotlib \
    && conda clean -afy

# Create workspace
WORKDIR /workspace

# Verify installation
RUN python -c "import ase; print(f'ASE {ase.__version__} ready')"

CMD ["python3"]
