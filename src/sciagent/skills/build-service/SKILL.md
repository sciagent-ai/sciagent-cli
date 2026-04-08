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

## 5. Choose Build Method

**Ask the user which build method to use:**

| Method | Platform | Use When |
|--------|----------|----------|
| **GitHub Actions** (Recommended) | linux/amd64 | Production builds, cloud execution (SkyPilot) |
| **Local Docker** | Host arch only | Quick iteration, testing Dockerfile changes |
| **Local Buildx** | linux/amd64 | Cross-compile on Mac (slower, but works) |

### Option A: GitHub Actions (Recommended for Cloud)

This builds on GitHub's linux/amd64 runners and pushes to GHCR. Required for SkyPilot cloud execution.

**First-time setup:** Ensure `.github/workflows/build-images.yml` exists (see Section 5.1).

**Trigger a build:**
```bash
# Via GitHub CLI
gh workflow run build-images.yml -f images="{package}" -f push=true

# Or via GitHub UI: Actions → Build and Push Docker Images → Run workflow
```

**Monitor progress:**
```bash
gh run list --workflow=build-images.yml --limit=5
gh run watch  # Watch latest run
```

### Option B: Local Docker (Quick Iteration)

Builds for host architecture only. Use for testing Dockerfile changes before pushing.

```bash
cd services/{package} && docker build -t ghcr.io/sciagent-ai/{package}:latest .
docker run --rm ghcr.io/sciagent-ai/{package}:latest <verification-command>
```

**Warning:** Images built on Mac ARM won't work on SkyPilot cloud (x86_64).

### Option C: Local Buildx (Cross-compile)

Cross-compiles for linux/amd64 on Mac. Slower due to emulation but works without GitHub Actions.

```bash
docker buildx build --platform linux/amd64 \
  -t ghcr.io/sciagent-ai/{package}:latest \
  --push \
  services/{package}
```

---

## 5.1 GitHub Actions Workflow Setup

If `.github/workflows/build-images.yml` doesn't exist, create it:

```yaml
name: Build and Push Docker Images

on:
  workflow_dispatch:
    inputs:
      images:
        description: 'Service to build (e.g., openfoam, scipy-base)'
        required: true
      push:
        description: 'Push to ghcr.io'
        type: boolean
        default: true

env:
  REGISTRY: ghcr.io
  IMAGE_PREFIX: ghcr.io/${{ github.repository_owner }}

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Login to GHCR
        if: inputs.push
        uses: docker/login-action@v3
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Build and push
        uses: docker/build-push-action@v5
        with:
          context: ./src/sciagent/services
          file: ./src/sciagent/services/${{ inputs.images }}/Dockerfile
          platforms: linux/amd64
          push: ${{ inputs.push }}
          tags: |
            ${{ env.IMAGE_PREFIX }}/${{ inputs.images }}:latest
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

**Commit and push the workflow:**
```bash
git add .github/workflows/build-images.yml
git commit -m "Add GitHub Actions workflow for building Docker images"
git push
```

---

## 5.2 Verify After Build

After GitHub Actions completes (or local push):

1. **Pull from GHCR to verify:**
   ```bash
   docker pull ghcr.io/sciagent-ai/{package}:latest
   ```

2. **Check architecture:**
   ```bash
   docker inspect ghcr.io/sciagent-ai/{package}:latest | grep Architecture
   # Should show: "Architecture": "amd64"
   ```

3. **Test the image:**
   ```bash
   docker run --rm ghcr.io/sciagent-ai/{package}:latest <verification-command>
   ```

4. **Cleanup (optional):**
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
