# paraview: Scientific visualization and data analysis
# Multi-arch: amd64 uses official binary, arm64 uses apt package
# Size: ~1.2GB (amd64), ~800MB (arm64)
FROM ubuntu:22.04

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="ParaView scientific visualization with headless rendering"
LABEL org.opencontainers.image.licenses="BSD-3-Clause"

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install common dependencies
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

# Architecture-specific installation
ARG TARGETARCH
ENV PARAVIEW_VERSION=5.13.3

RUN if [ "$TARGETARCH" = "amd64" ]; then \
      # amd64: Use official ParaView binary with OSMesa (headless rendering)
      echo "Installing ParaView ${PARAVIEW_VERSION} from official binary (amd64)..." && \
      wget -q "https://www.paraview.org/files/v5.13/ParaView-${PARAVIEW_VERSION}-osmesa-MPI-Linux-Python3.10-x86_64.tar.gz" -O /tmp/paraview.tar.gz && \
      tar -xzf /tmp/paraview.tar.gz -C /opt && \
      rm /tmp/paraview.tar.gz && \
      mv /opt/ParaView-${PARAVIEW_VERSION}-osmesa-MPI-Linux-Python3.10-x86_64 /opt/paraview && \
      echo "export PATH=/opt/paraview/bin:\$PATH" >> /etc/profile.d/paraview.sh && \
      echo "# NOTE: LD_LIBRARY_PATH intentionally NOT set here - breaks system curl" >> /etc/profile.d/paraview.sh && \
      echo "# ParaView wrapper scripts set LD_LIBRARY_PATH only when running ParaView binaries" >> /etc/profile.d/paraview.sh && \
      echo "export PYTHONPATH=/opt/paraview/lib/python3.10/site-packages:\$PYTHONPATH" >> /etc/profile.d/paraview.sh; \
    elif [ "$TARGETARCH" = "arm64" ]; then \
      # arm64: Use apt package (older version but works)
      echo "Installing ParaView from apt (arm64)..." && \
      apt-get update && \
      apt-get install -y --no-install-recommends paraview python3-paraview && \
      rm -rf /var/lib/apt/lists/* && \
      echo "Note: arm64 uses apt package, version may differ from amd64"; \
    else \
      echo "Unsupported architecture: $TARGETARCH" && exit 1; \
    fi

# Set up environment for amd64 (arm64 uses system paths)
# NOTE: Do NOT set LD_LIBRARY_PATH globally - ParaView bundles libcurl.so.4
# which breaks system curl (needed by SkyPilot). Instead, use wrapper scripts.
ENV PATH="/opt/paraview/bin:${PATH}"
ENV PYTHONPATH="/opt/paraview/lib/python3.10/site-packages:${PYTHONPATH}"

# Create wrapper scripts that set LD_LIBRARY_PATH only for ParaView binaries
RUN if [ -d /opt/paraview/bin ]; then \
      mkdir -p /usr/local/bin && \
      for bin in pvpython pvbatch paraview; do \
        if [ -f /opt/paraview/bin/$bin ]; then \
          mv /opt/paraview/bin/$bin /opt/paraview/bin/${bin}.real && \
          echo '#!/bin/bash' > /opt/paraview/bin/$bin && \
          echo 'export LD_LIBRARY_PATH="/opt/paraview/lib:${LD_LIBRARY_PATH}"' >> /opt/paraview/bin/$bin && \
          echo 'exec /opt/paraview/bin/'${bin}'.real "$@"' >> /opt/paraview/bin/$bin && \
          chmod +x /opt/paraview/bin/$bin; \
        fi; \
      done; \
    fi

# Create workspace
WORKDIR /workspace

# Verify installation
RUN pvpython --version || python3 -c "import paraview; print('ParaView available')"

CMD ["/bin/bash"]
