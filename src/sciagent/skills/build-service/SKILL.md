---
name: build-service
description: "Build and publish Docker services to container registry (GHCR)"
triggers:
  - "build.*(service|container|docker|image)"
  - "create.*(service|container)"
  - "add.*to.*registry"
  - "dockerize"
  - "publish.*ghcr"
  - "new.*service"
  - "rebuild.*for.*(cloud|skypilot|aws|gcp)"
  - "fix.*arch"
  - "multi.?arch"
---

# Build Service Workflow

## Architecture Awareness

**IMPORTANT:** SkyPilot (cloud) only supports `linux/amd64`. Local Mac uses `linux/arm64`.

| Build Target | Architecture | Use Case |
|--------------|--------------|----------|
| Cloud (SkyPilot) | linux/amd64 | AWS, GCP, Azure VMs |
| Local Mac (M1/M2/M3) | linux/arm64 | Development, testing |
| **Both (Recommended)** | linux/amd64,linux/arm64 | Works everywhere |

**Always build multi-arch** via GitHub Actions to support both cloud and local development.

---

## Quick Reference: Build Commands

### Rebuild Single Service (Multi-Arch)
```bash
gh workflow run build-images.yml -f images="paraview"
```

### Rebuild Multiple Services
```bash
gh workflow run build-images.yml -f images="openfoam,paraview,meep"
```

### Rebuild ALL Services
```bash
gh workflow run build-images.yml -f images="all"
```

### Monitor Build Progress
```bash
gh run list --workflow=build-images.yml --limit=5
gh run watch  # Watch latest run
```

---

## Full Workflow: Building a New Service

### 1. Check Prerequisites

- Read `src/sciagent/services/registry.yaml` to see if the service exists
- Check if `src/sciagent/services/{package}/Dockerfile` already exists
- Look at existing Dockerfiles for patterns

### 2. Research Official Sources

**Use web search to find official installation instructions.** Scientific software often has specific requirements that aren't obvious.

Look up:
- **Official installation docs** - pip, conda, or source build?
- **Recommended base image** - some packages have official Docker images (e.g., OpenFOAM, FEniCS)
- **System dependencies** - many scientific packages need specific libraries (BLAS, MPI, OpenGL, etc.)
- **License** - needed for the Dockerfile label and registry entry
- **Known issues** - version conflicts, deprecated methods
- **Architecture support** - check if binaries exist for both amd64 and arm64

This step prevents trial-and-error builds and ensures we use the recommended approach.

### 3. Check for Architecture-Specific Downloads

**IMPORTANT:** If the Dockerfile downloads pre-built binaries, check if both architectures are available.

**Problem Pattern:**
```dockerfile
# ❌ This only works on one architecture
RUN wget https://example.com/package-x86_64.tar.gz
```

**Solution 1: Use TARGETARCH (if both binaries exist)**
```dockerfile
# ✓ Multi-arch compatible
ARG TARGETARCH
RUN if [ "$TARGETARCH" = "amd64" ]; then \
      wget https://example.com/package-x86_64.tar.gz; \
    elif [ "$TARGETARCH" = "arm64" ]; then \
      wget https://example.com/package-aarch64.tar.gz; \
    fi
```

**Solution 2: Use package manager (handles arch automatically)**
```dockerfile
# ✓ Package managers handle architecture
RUN apt-get install -y package-name
# or
RUN pip install package-name
# or
RUN conda install package-name
```

**Solution 3: Build from source (works on any arch)**
```dockerfile
# ✓ Source builds work everywhere
RUN git clone https://github.com/org/package && \
    cd package && \
    make && make install
```

### 4. Create Dockerfile

Create `src/sciagent/services/{package}/Dockerfile`:

```dockerfile
# {package}: Brief description
FROM python:3.11-slim

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/sciagent-cli"
LABEL org.opencontainers.image.description="{Package description}"
LABEL org.opencontainers.image.licenses="{LICENSE}"

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    <system-deps> \
    && rm -rf /var/lib/apt/lists/*

# Install package
RUN pip install --no-cache-dir {package}

WORKDIR /workspace

# Verify installation
RUN python -c "import {package}; print(f'{package} version: {{package}.__version__}')"

CMD ["/bin/bash"]
```

### 5. Create .dockerignore

Create `src/sciagent/services/{package}/.dockerignore`:
```
__pycache__
*.pyc
*.pyo
.git
.gitignore
*.md
.DS_Store
```

### 6. Update Registry

Add to `src/sciagent/services/registry.yaml`:
```yaml
  {package-name}:
    description: "{Brief description}"
    image: ghcr.io/sciagent-ai/{package-name}
    dockerfile: services/{package-name}/Dockerfile
    license: MIT
    runtime: python3
    workdir: /workspace
    packages:
      - {main-package}
      - numpy
    capabilities:
      - "Capability 1"
      - "Capability 2"
    example: |
      import {package}
      # Example code
```

### 7. Build with GitHub Actions (Recommended)

**Always use GitHub Actions for production builds** - it builds both amd64 and arm64.

```bash
# Commit your changes first
git add src/sciagent/services/{package}/
git commit -m "Add {package} service"
git push

# Trigger multi-arch build
gh workflow run build-images.yml -f images="{package}"

# Monitor progress
gh run watch
```

### 8. Verify the Build

After GitHub Actions completes:

```bash
# Check image exists with both architectures
docker manifest inspect ghcr.io/sciagent-ai/{package}:latest

# Test on local Mac (uses arm64)
docker run --rm ghcr.io/sciagent-ai/{package}:latest <verify-command>

# Test via SkyPilot (uses amd64)
python -c "
from sciagent.tools.atomic.compute import ComputeTool
compute = ComputeTool()
result = compute.execute(
    service='{package}',
    command='<verify-command>',
    backend='skypilot',
    background=False
)
print(result.output)
"
```

---

## Troubleshooting

### Image fails on SkyPilot but works locally

**Cause:** Image was built on Mac (arm64 only), SkyPilot needs amd64.

**Fix:** Rebuild via GitHub Actions:
```bash
gh workflow run build-images.yml -f images="{package}"
```

### Multi-arch build fails for one architecture

**Cause:** Dockerfile downloads architecture-specific binary that doesn't exist for both.

**Fix:** Check Section 3 for solutions (TARGETARCH, package manager, or build from source).

### Build takes too long

**Cause:** Large dependencies or building from source.

**Solutions:**
- Use pre-built wheels: `pip install --prefer-binary`
- Use smaller base image: `python:3.11-slim` instead of `python:3.11`
- Split into layers for better caching

---

---

## 9. Capture Package Manifest

After successful build and verification, record installed packages in the registry.

**For Python runtimes:**
```bash
docker run --rm ghcr.io/sciagent-ai/{package}:latest pip list --format=freeze | cut -d= -f1
```

**For Julia runtimes:**
```bash
docker run --rm ghcr.io/sciagent-ai/{package}:latest julia -e 'using Pkg; for (k,v) in Pkg.dependencies(); println(k); end'
```

**Update registry.yaml** with the key packages (not every transitive dependency, just the main ones):

```yaml
{package-name}:
  # ... existing fields ...
  packages:
    - main-package
    - numpy
    - scipy
  extends: scipy-base  # if Dockerfile uses FROM ghcr.io/sciagent-ai/scipy-base
```

- `packages`: List of key importable packages in the container
- `extends`: The base sciagent service this was built on (null if external base like python:slim)

This enables the agent to determine which container has the libraries needed for a task.

---

## 10. Report Results

Summarize:
- Image location: `ghcr.io/sciagent-ai/{package}:latest`
- Architectures built: `linux/amd64`, `linux/arm64` (or just one)
- Files created/modified
- Packages captured
- Verification status
- Any issues encountered

---

## Reference: Current Services Needing Arch Fixes

These services download architecture-specific binaries and may need updates for full multi-arch support:

| Service | Current Binary | Fix Needed |
|---------|----------------|------------|
| paraview | x86_64 only | Add arm64 URL or build from source |
| dwsim | amd64 only | Check if arm64 available |

Check with:
```bash
grep -r "wget\|curl" src/sciagent/services/*/Dockerfile | grep -E "x86_64|amd64|arm64|aarch64"
```
