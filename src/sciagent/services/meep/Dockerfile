# meep: FDTD electromagnetic simulation
# Uses conda for complex dependencies (MPI, HDF5, libctl)
# Size target: ~1.5GB
FROM continuumio/miniconda3:latest

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="MEEP FDTD electromagnetic simulation environment"
LABEL org.opencontainers.image.licenses="GPL-2.0"

ENV PYTHONUNBUFFERED=1

# Install MEEP and common dependencies from conda-forge
RUN conda install -y -c conda-forge \
    pymeep=*=mpi_mpich_* \
    numpy \
    scipy \
    matplotlib \
    h5py \
    && conda clean -afy

# Create workspace
WORKDIR /workspace

# Verify installation
RUN python -c "import meep as mp; print(f'meep {mp.__version__} ready')"

CMD ["python3"]
