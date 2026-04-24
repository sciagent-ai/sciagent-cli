# openfoam-swak4foam-2012: ABI-matched OpenFOAM v2012 + swak4Foam
#
# Companion to services/openfoam-swak4foam (which targets v2406). This image
# exists because case files written for v2012 hit several v2406 regressions
# that cannot be patched around without rewriting the case:
#
#   * outletMappedUniformInletHeatAddition  -- removed in v2406
#   * omegaWallFunction "blending binomial2" -- renamed to "binomial" in v2406
#   * snappyHexMesh STL patch default       -- changed from "patch" to "wall"
#   * groovyBC valueExpression "$var" lookup -- segfaults in
#     Foam::dictionary::csearch under v2406 because of a dictionary ABI break
#   * libmetisDecomp.so                     -- the stock image ships only the
#     dummy stub; "method metis" in decomposeParDict fails fatally
#
# The first four are fixed by pinning the OpenFOAM base to :2012. The metis
# stub is fixed by the rebuild step below.
#
# SkyPilot compatibility: opencfd/openfoam-default:2012 is built on Ubuntu
# 20.04 (glibc 2.31). SkyPilot hosts run Ubuntu 22.04+ (kernel 5.15+, glibc
# 2.35), so the container glibc is older than the host -- forward-compatible.
# The :2012 manifest is amd64-only; SkyPilot uses amd64 nodes natively, so
# this matches without QEMU.

FROM opencfd/openfoam-default:2012

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="OpenFOAM v2012 with swak4Foam, ABI-matched and metis-enabled"
LABEL org.opencontainers.image.licenses="GPL-3.0"

ENV DEBIAN_FRONTEND=noninteractive
ENV FOAM_INST=/usr/lib/openfoam/openfoam2012

RUN echo "source ${FOAM_INST}/etc/bashrc" >> /etc/bash.bashrc

# Build deps + system metis (the metisDecomp link target) + OpenFOAM source.
# opencfd/openfoam-default:2012 is a runtime image; it does not ship src/ by
# default, so the openfoam2012-source apt package is required to rebuild the
# metisDecomp libso below.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bison \
        flex \
        mercurial \
        make \
        g++ \
        binutils \
        python3 \
        python-is-python3 \
        libmetis-dev \
        openfoam2012-source \
    && rm -rf /var/lib/apt/lists/*

# ---- Rebuild metisDecomp against system libmetis -------------------------
# The stock image ships a stub libmetisDecomp.so that throws at runtime when
# decomposePar uses method metis. Setting METIS_ARCH_PATH=/usr (where the
# apt libmetis-dev headers + .so live) and rebuilding the metisDecomp libso
# yields a real implementation linked to /usr/lib/x86_64-linux-gnu/libmetis.so.
#
# `set -ex` makes every command echo so a failure inside the bash subshell is
# attributable from the buildx log. The `find` fallback handles the case where
# opencfd later ships the source tree at a different path.
# NOTE on `set -e`: OpenFOAM's etc/bashrc invokes internal helper functions
# (_foamAddLibAuto, etc.) that `return 1` as normal control flow when an
# optional ThirdParty package is absent. With `set -e` active during sourcing,
# those returns kill the shell. So we source first WITHOUT strict mode, then
# turn `set -exo pipefail` on for our own commands. `pipefail` is essential
# so `wmake ... | tee log` reports wmake's exit code, not tee's.
#
# Three things have to be right for the rebuild to succeed:
#
#   1. The lnInclude symlink farm under decompositionMethods must contain
#      metisLikeDecomp.H. The opencfd openfoam2012-source apt package ships
#      the .C/.H files but not the wmake-generated lnInclude links, so we
#      populate it explicitly with `wmakeLnInclude -update`.
#
#   2. metisDecomp/Make/options uses $(METIS_INC_DIR) and $(METIS_LIB_DIR)
#      verbatim (verified against gitlab.com/openfoam/core/openfoam @ v2012).
#      Setting METIS_ARCH_PATH alone does NOT propagate to those vars after
#      etc/bashrc has already been sourced; we set them directly.
#
#   3. Success check must distinguish the real rebuild from the dummy stub.
#      The base image ships the stub at $FOAM_LIBBIN/dummy/libmetisDecomp.so;
#      the real rebuild lands at $FOAM_LIBBIN/libmetisDecomp.so (top-level).
#      Both export Foam::metisDecomp symbols (the stub just throws), so we
#      use `ldd` for the libmetis dependency as the discriminating check.
RUN bash -c '\
    source ${FOAM_INST}/etc/bashrc; \
    set -exo pipefail; \
    SRC="$WM_PROJECT_DIR/src/parallel/decompose/metisDecomp"; \
    [ -d "$SRC" ] || { echo "FAIL: $SRC missing (openfoam2012-source not installed?)"; exit 1; }; \
    DM="$WM_PROJECT_DIR/src/parallel/decompose/decompositionMethods"; \
    wmakeLnInclude -update "$DM"; \
    [ -e "$DM/lnInclude/metisLikeDecomp.H" ] \
        || { echo "FAIL: metisLikeDecomp.H not in $DM/lnInclude (apt source package incomplete?)"; \
             find "$DM" -name "metisLikeDecomp*" -ls; exit 1; }; \
    export METIS_ARCH_PATH=/usr; \
    export METIS_INC_DIR=/usr/include; \
    export METIS_LIB_DIR=/usr/lib/x86_64-linux-gnu; \
    cd "$SRC"; \
    wclean || true; \
    wmake libso 2>&1 | tee /tmp/metisDecomp_build.log; \
    LIB="$FOAM_LIBBIN/libmetisDecomp.so"; \
    [ -f "$LIB" ] \
        || { echo "FAIL: $LIB not produced (the dummy stub at $FOAM_LIBBIN/dummy/ does not count)"; \
             ls -la "$FOAM_LIBBIN" | head; exit 1; }; \
    ldd "$LIB" | grep -q "libmetis\\.so" \
        || { echo "FAIL: $LIB has no libmetis.so dependency (still effectively a stub)"; \
             ldd "$LIB"; exit 1; }; \
    echo "OK: real metisDecomp at $LIB, links libmetis"'

# ---- swak4Foam, pinned for v2012 compatibility ---------------------------
# vcompile/p2106 (hg 495d94c458976b821598e28eee8a49ab6f0a4901, 2021-07-02) is
# the most recent tagged compile-checkpoint that pre-dates v2206-era dictionary
# ABI changes. v2012 is one ESI release older than v2106 and shares the same
# pre-v2206 dictionary ABI; swak4Foam's #if OPENFOAM_VERSION guards span this
# window. Override SWAK4FOAM_REV at build time to test a newer commit; CI will
# re-run the smoke test against it before allowing a push.
ARG SWAK4FOAM_REV=495d94c458976b821598e28eee8a49ab6f0a4901
RUN hg clone http://hg.code.sf.net/p/openfoam-extend/swak4Foam /opt/swak4Foam \
    && cd /opt/swak4Foam \
    && hg update -r ${SWAK4FOAM_REV} \
    && hg log -r . --template 'swak4Foam pinned at {node} ({date|isodate}) "{desc|firstline}"\n'

# Install into FOAM_SITE_*BIN by hijacking FOAM_USER_*BIN for the build.
# FOAM_SITE_LIBBIN is on LD_LIBRARY_PATH after sourcing the bashrc, so the
# resulting libs (libgroovyBC.so, libswakFunctionObjects.so, ...) are visible
# to every solver/utility without per-user shell tweaks. The v2406 image left
# them under /root/OpenFOAM/user-v2406/... and forced manual sourcing.
WORKDIR /opt/swak4Foam
# Source bashrc WITHOUT strict mode (see metis step above for rationale).
RUN bash -c '\
    source ${FOAM_INST}/etc/bashrc; \
    set -e; \
    export FOAM_USER_LIBBIN=$FOAM_SITE_LIBBIN; \
    export FOAM_USER_APPBIN=$FOAM_SITE_APPBIN; \
    mkdir -p $FOAM_USER_LIBBIN $FOAM_USER_APPBIN; \
    export WM_NCOMPPROCS=2; \
    ./AllwmakeAll > /tmp/swak4foam_build1.log 2>&1 || true; \
    ./AllwmakeAll > /tmp/swak4foam_build2.log 2>&1 || true; \
    echo "swak4Foam build passes complete (verification step gates success)"'

# Smoke-test script. Lives in /usr/local/bin so users can re-run it inside a
# running container, and so the same script gates both the in-image build
# (RUN below) and the workflow-level docker-run check before push.
COPY openfoam-swak4foam-2012/smoke.sh /usr/local/bin/sciagent-smoke
RUN chmod +x /usr/local/bin/sciagent-smoke

# ---- Verification (any failure here fails the image build) ---------------
RUN /usr/local/bin/sciagent-smoke

WORKDIR /workspace

CMD ["/bin/bash", "-l"]
