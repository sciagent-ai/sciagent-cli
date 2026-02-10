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
---

# Build Service Workflow

When the user asks to build a new service package, follow these steps in order:

## 1. Check Prerequisites

- Read `registry.yaml` to see if the service is already defined
- Check if `services/{package}/Dockerfile` already exists
- Look at existing Dockerfiles (e.g., `services/meep/Dockerfile`) for patterns

## 2. Research Official Sources

**Use web search to find official installation instructions.** Scientific software often has specific requirements that aren't obvious.

Look up:
- **Official installation docs** - pip, conda, or source build?
- **Recommended base image** - some packages have official Docker images (e.g., OpenFOAM, FEniCS)
- **System dependencies** - many scientific packages need specific libraries (BLAS, MPI, etc.)
- **License** - needed for the Dockerfile label and registry entry
- **Known issues** - version conflicts, deprecated methods, architecture gotchas

This step prevents trial-and-error builds and ensures we use the recommended approach.

## 3. Create Package Files

### Dockerfile
Create `services/{package}/Dockerfile` (or `{package}/Dockerfile` for new packages) following this pattern:
- Use appropriate base image (conda for complex deps, python:slim for simple)
- Add LABEL for org.opencontainers.image.source, description, licenses
- Set PYTHONUNBUFFERED=1
- Install package and dependencies
- Set WORKDIR /workspace
- Add verification step (RUN command that tests import/execution)
- Set appropriate CMD

### .dockerignore
Create `services/{package}/.dockerignore`:
```
__pycache__
*.pyc
*.pyo
.git
.gitignore
*.md
.DS_Store
```

## 4. Update Registry (if needed)

If the package is not in `registry.yaml`, add an entry following the existing format:
```yaml
  package-name:
    description: "Brief description"
    image: ghcr.io/sciagent-ai/package-name
    dockerfile: services/package-name/Dockerfile
    license: MIT  # or GPL-3.0, BSD-3-Clause, Apache-2.0, LGPL-2.1, etc.
    runtime: python3  # or bash
    workdir: /workspace
    capabilities:
      - "Capability 1"
      - "Capability 2"
    example: |
      # Example usage code
```

**Note:** The `license` field should match the SPDX identifier used in the Dockerfile label.

## 5. Build, Push, and Verify

**Architecture Note:** Images are built for the host machine's architecture only. If building on Apple Silicon (ARM64), the image will only run natively on ARM64 systems. Intel/AMD users would need emulation (slow) or a separate build. For multi-arch support in the future, use:
```bash
docker buildx build --platform linux/amd64,linux/arm64 -t ghcr.io/sciagent-ai/{package}:latest --push .
```

Execute these steps in order, using the TodoWrite tool to track progress:

1. **Build locally**:
   ```bash
   cd services/{package} && docker build -t ghcr.io/sciagent-ai/{package}:latest .
   ```

2. **Push to GHCR**:
   ```bash
   docker push ghcr.io/sciagent-ai/{package}:latest
   ```

3. **Remove local image(s)**:
   ```bash
   docker rmi ghcr.io/sciagent-ai/{package}:latest
   ```

4. **Pull from GHCR to verify**:
   ```bash
   docker pull ghcr.io/sciagent-ai/{package}:latest
   ```

5. **Test the image**:
   ```bash
   docker run --rm ghcr.io/sciagent-ai/{package}:latest <verification-command>
   ```

6. **Cleanup** (optional):
   ```bash
   docker rmi ghcr.io/sciagent-ai/{package}:latest
   ```

## 6. Capture Package Manifest

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

## 7. Report Results

Summarize:
- Image location: `ghcr.io/sciagent-ai/{package}:latest`
- Files created
- Packages captured
- Verification status
- Any issues encountered
