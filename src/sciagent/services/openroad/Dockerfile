# openroad: RTL-to-GDS digital design flow
# Uses official OpenROAD ORFS image with pre-built binaries
# Note: OpenROAD only provides amd64 images, runs under emulation on ARM
FROM --platform=linux/amd64 openroad/orfs:latest

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="OpenROAD RTL-to-GDS digital design environment"
LABEL org.opencontainers.image.licenses="BSD-3-Clause"

ENV PYTHONUNBUFFERED=1
ENV PATH="/OpenROAD-flow-scripts/tools/install/OpenROAD/bin:/OpenROAD-flow-scripts/tools/install/yosys/bin:${PATH}"

# Create workspace
WORKDIR /workspace

# Verify installation
RUN openroad -version && yosys -V

CMD ["openroad"]
