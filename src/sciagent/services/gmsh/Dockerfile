# gmsh: 3D finite element mesh generator
# Uses conda for complex dependencies (OpenCASCADE, FLTK, etc.)
# Size target: ~1GB
FROM continuumio/miniconda3:latest

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="Gmsh 3D finite element mesh generator with Python API"
LABEL org.opencontainers.image.licenses="GPL-2.0"

ENV PYTHONUNBUFFERED=1

# Install gmsh and common dependencies from conda-forge
RUN conda install -y -c conda-forge \
    python-gmsh \
    numpy \
    scipy \
    matplotlib \
    && conda clean -afy

# Create workspace
WORKDIR /workspace

# Verify installation
RUN python -c "import gmsh; gmsh.initialize(); print(f'gmsh {gmsh.GMSH_API_VERSION} ready'); gmsh.finalize()"

CMD ["python3"]
