# openfoam: Computational Fluid Dynamics
# Uses official OpenFOAM foundation image
# Size target: ~2GB
FROM opencfd/openfoam-default:2406

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="OpenFOAM CFD simulation environment"
LABEL org.opencontainers.image.licenses="GPL-3.0"

# Source OpenFOAM environment in bashrc for all sessions
RUN echo "source /usr/lib/openfoam/openfoam2406/etc/bashrc" >> /etc/bash.bashrc

# Create workspace
WORKDIR /workspace

# Copy a simple test case for verification
RUN cp -r /usr/lib/openfoam/openfoam2406/tutorials/incompressible/icoFoam/cavity/cavity /workspace/cavity

# Verify installation
RUN bash -c "source /usr/lib/openfoam/openfoam2406/etc/bashrc && blockMesh -help > /dev/null && echo 'OpenFOAM 2406 ready'"

CMD ["/bin/bash", "-l"]
