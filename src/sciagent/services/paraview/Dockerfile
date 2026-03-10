# paraview: Scientific visualization and data analysis
# Uses official ParaView binary release with OSMesa (headless rendering)
# Size: ~1.2GB
FROM ubuntu:22.04

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="ParaView scientific visualization with headless rendering"
LABEL org.opencontainers.image.licenses="BSD-3-Clause"

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    ca-certificates \
    libgl1-mesa-glx \
    libglu1-mesa \
    libxt6 \
    libxrender1 \
    libxcursor1 \
    libxft2 \
    libxinerama1 \
    libgomp1 \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Download and install ParaView 5.13.3 OSMesa binary
ENV PARAVIEW_VERSION=5.13.3
RUN wget -q "https://www.paraview.org/files/v5.13/ParaView-${PARAVIEW_VERSION}-osmesa-MPI-Linux-Python3.10-x86_64.tar.gz" -O /tmp/paraview.tar.gz \
    && tar -xzf /tmp/paraview.tar.gz -C /opt \
    && rm /tmp/paraview.tar.gz \
    && mv /opt/ParaView-${PARAVIEW_VERSION}-osmesa-MPI-Linux-Python3.10-x86_64 /opt/paraview

# Set up environment
ENV PATH="/opt/paraview/bin:${PATH}"
ENV LD_LIBRARY_PATH="/opt/paraview/lib:${LD_LIBRARY_PATH}"
ENV PYTHONPATH="/opt/paraview/lib/python3.10/site-packages:${PYTHONPATH}"

# Create workspace
WORKDIR /workspace

# Verify installation (light check - full verification at runtime)
RUN ls -la /opt/paraview/bin/pvpython /opt/paraview/bin/pvbatch /opt/paraview/bin/pvserver \
    && echo "ParaView ${PARAVIEW_VERSION} binaries installed"

CMD ["/bin/bash"]
