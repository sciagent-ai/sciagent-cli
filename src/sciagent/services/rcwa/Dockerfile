# S4/RCWA simulation container
# Tool only - agent fetches docs on-demand
FROM ubuntu:22.04

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="S4/RCWA electromagnetic simulation environment"
LABEL org.opencontainers.image.licenses="GPL-2.0"

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install dependencies
RUN apt-get update && apt-get install -y \
    build-essential gfortran git python3 python3-pip python3-dev \
    libopenblas-dev liblapack-dev libfftw3-dev libsuitesparse-dev \
    libboost-all-dev liblua5.2-dev lua5.2 swig pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Build S4
WORKDIR /opt
RUN pip3 install --no-cache-dir numpy matplotlib && \
    git clone https://github.com/phoebe-p/S4.git && \
    cd S4 && \
    ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "amd64" ]; then LIB_DIR="/usr/lib/x86_64-linux-gnu"; \
    elif [ "$ARCH" = "arm64" ]; then LIB_DIR="/usr/lib/aarch64-linux-gnu"; \
    else LIB_DIR="/usr/lib"; fi && \
    sed -i 's/^CFLAGS.*/CFLAGS = -O3 -fPIC/' Makefile && \
    sed -i 's/^CXXFLAGS.*/CXXFLAGS = -O3 -fPIC -std=c++11/' Makefile && \
    sed -i 's|^BOOST_INC.*|BOOST_INC = -I/usr/include|' Makefile && \
    sed -i 's|^BOOST_LIBS.*|BOOST_LIBS = -lboost_serialization|' Makefile && \
    sed -i 's|^FFTW3_INC.*|FFTW3_INC = -I/usr/include|' Makefile && \
    sed -i "s|^FFTW3_LIB.*|FFTW3_LIB = -L$LIB_DIR|" Makefile && \
    sed -i 's|^PTHREAD_INC.*|PTHREAD_INC = -I/usr/include|' Makefile && \
    sed -i "s|^PTHREAD_LIB.*|PTHREAD_LIB = -L$LIB_DIR -lpthread|" Makefile && \
    sed -i 's|^LUA_INC.*|LUA_INC = -I/usr/include/lua5.2|' Makefile && \
    sed -i "s|^LUA_LIB.*|LUA_LIB = -L$LIB_DIR|" Makefile && \
    sed -i 's|^CHOLMOD_INC.*|CHOLMOD_INC = -I/usr/include/suitesparse|' Makefile && \
    sed -i "s|^CHOLMOD_LIB.*|CHOLMOD_LIB = -L$LIB_DIR -lcholmod|" Makefile && \
    sed -i 's|^OPENBLAS_INC.*|OPENBLAS_INC = -I/usr/include/openblas|' Makefile && \
    sed -i "s|^OPENBLAS_LIB.*|OPENBLAS_LIB = -L$LIB_DIR -lopenblas|" Makefile && \
    make clean && make S4_pyext

WORKDIR /workspace
CMD ["python3"]
