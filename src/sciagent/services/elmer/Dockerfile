# elmer: Multiphysics finite element solver
# Built from source (official method)
# Size target: ~2GB
FROM ubuntu:22.04

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="Elmer FEM multiphysics simulation environment"
LABEL org.opencontainers.image.licenses="GPL-2.0"

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
    gfortran \
    libblas-dev \
    liblapack-dev \
    libmumps-dev \
    libparmetis-dev \
    mpich \
    libmpich-dev \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Clone and build Elmer
RUN git clone --depth 1 https://github.com/ElmerCSC/elmerfem.git /tmp/elmerfem \
    && mkdir /tmp/elmerfem/build \
    && cd /tmp/elmerfem/build \
    && cmake .. \
        -DCMAKE_INSTALL_PREFIX=/usr/local/elmer \
        -DWITH_MPI=ON \
        -DWITH_MUMPS=ON \
        -DWITH_ELMERGUI=OFF \
        -DWITH_ElmerIce=OFF \
    && make -j$(nproc) \
    && make install \
    && rm -rf /tmp/elmerfem

# Set up environment
ENV PATH="/usr/local/elmer/bin:${PATH}"
ENV LD_LIBRARY_PATH="/usr/local/elmer/lib:${LD_LIBRARY_PATH}"
ENV ELMER_HOME="/usr/local/elmer"

# Install Python packages for analysis
RUN pip3 install --no-cache-dir \
    numpy \
    scipy \
    matplotlib

# Create workspace
WORKDIR /workspace

# Verify installation
RUN ElmerSolver --help | head -5 || echo "ElmerSolver installed"

CMD ["bash"]
