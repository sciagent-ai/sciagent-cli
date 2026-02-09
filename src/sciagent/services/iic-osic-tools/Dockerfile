# iic-osic-tools: All-in-one open-source IC design environment
# Uses official IIC-OSIC-TOOLS image with 80+ EDA tools
# Supports SKY130, GF180MCU, and IHP-SG13G2 PDKs
# Note: Image uses custom entrypoint, use --skip to run commands directly
FROM hpretl/iic-osic-tools:latest

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="IIC-OSIC-TOOLS: All-in-one open-source IC design for analog and digital flows"
LABEL org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONUNBUFFERED=1

# Set workspace directory
WORKDIR /workspace

# Default command - use --skip to bypass UI startup and run bash directly
CMD ["--skip", "bash"]
