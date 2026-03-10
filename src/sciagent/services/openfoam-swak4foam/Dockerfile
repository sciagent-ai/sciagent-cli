# openfoam-swak4foam: OpenFOAM with swak4Foam expression-based field manipulation
# Extends openfoam with swak4Foam toolbox
# Size: ~3GB
FROM opencfd/openfoam-default:2406

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="OpenFOAM CFD with swak4Foam expression toolkit"
LABEL org.opencontainers.image.licenses="GPL-3.0"

ENV DEBIAN_FRONTEND=noninteractive

# Source OpenFOAM environment in bashrc for all sessions
RUN echo "source /usr/lib/openfoam/openfoam2406/etc/bashrc" >> /etc/bash.bashrc

# Install swak4Foam build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    bison \
    flex \
    mercurial \
    make \
    g++ \
    python3 \
    python-is-python3 \
    && rm -rf /var/lib/apt/lists/*

# Clone swak4Foam develop branch (supports OpenFOAM v2406)
RUN hg clone -b develop http://hg.code.sf.net/p/openfoam-extend/swak4Foam /opt/swak4Foam

# Set up environment and build swak4Foam
WORKDIR /opt/swak4Foam

# Build swak4Foam (run twice as recommended)
# 2 parallel jobs with 6GB RAM available
RUN bash -c "source /usr/lib/openfoam/openfoam2406/etc/bashrc && \
    export WM_NCOMPPROCS=2 && \
    ./AllwmakeAll > /tmp/swak4foam_build1.log 2>&1; \
    ./AllwmakeAll > /tmp/swak4foam_build2.log 2>&1; \
    echo 'Build completed (check verification step for success)'"

# Verify key utilities are built (show log tail on failure)
RUN bash -c "source /usr/lib/openfoam/openfoam2406/etc/bashrc && \
    (which funkySetFields && which funkyDoCalc && echo 'swak4Foam installed successfully') || \
    (echo '=== BUILD LOG 1 TAIL ===' && tail -100 /tmp/swak4foam_build1.log && \
     echo '=== BUILD LOG 2 TAIL ===' && tail -100 /tmp/swak4foam_build2.log && exit 1)"

# Add swak4Foam to bashrc
RUN echo "# swak4Foam" >> /etc/bash.bashrc

# Return to workspace
WORKDIR /workspace

CMD ["/bin/bash", "-l"]
