# openfoam-swak4foam-2012

ABI-matched OpenFOAM v2012 + swak4Foam image. Companion to
[`openfoam-swak4foam`](../openfoam-swak4foam) (which targets v2406).

## When to use this image instead of `openfoam-swak4foam`

Pick this image whenever a case file requires **v2012 ABI semantics**. The
specific symptoms that drive the split:

| Case file uses... | v2406 behavior | Why this image fixes it |
|---|---|---|
| `outletMappedUniformInletHeatAddition` | BC removed; only `outletMappedUniformInlet` remains | v2012 base still ships it |
| `omegaWallFunction { blending binomial2; }` | Renamed to `binomial`; `binomial2` is unparseable | v2012 base accepts `binomial2` |
| snappyHexMesh STL patches | Default patch type changed `patch` → `wall` | v2012 base keeps `patch` |
| `groovyBC` `valueExpression` referencing `$dictVar` | Segfault inside `Foam::dictionary::csearch` (v2206+ dictionary ABI break) | swak4Foam pinned to a pre-v2206 commit, ABI matches |
| `decomposeParDict { method metis; }` | Fatal: stock `libmetisDecomp.so` is the dummy stub | metisDecomp rebuilt against system libmetis |

## Pinned versions

| Component | Pin | Source |
|---|---|---|
| OpenFOAM base | `opencfd/openfoam-default:2012` | Docker Hub |
| Ubuntu (inside base) | 20.04 (glibc 2.31) — forward-compatible with SkyPilot's 22.04 hosts | upstream |
| swak4Foam | hg `495d94c458976b821598e28eee8a49ab6f0a4901` (tag `vcompile/p2106`, 2021-07-02) | hg.code.sf.net/p/openfoam-extend/swak4Foam |
| system metis | apt `libmetis-dev` (Ubuntu 20.04) | apt |
| Architectures | `linux/amd64` only (base manifest is amd64-only) | upstream |

### Why `vcompile/p2106` for swak4Foam

swak4Foam carries no `compile/p2012` tag. `vcompile/p2106` is the most recent
tagged compile-checkpoint that pre-dates the v2206-era dictionary ABI break,
which is what corrupts `groovyBC` `$var` lookups. v2012 sits one ESI release
below v2106 in the same pre-v2206 ABI window; swak4Foam's `#if OPENFOAM_VERSION`
guards span this. The CI smoke test (see below) gates the build, so a regression
on this pin would block the push.

To try a different pin:

```bash
docker buildx build \
  --build-arg SWAK4FOAM_REV=<other-hg-sha> \
  -f services/openfoam-swak4foam-2012/Dockerfile \
  src/sciagent/services
```

## Design notes

### Why metis is rebuilt

The stock `opencfd/openfoam-default:2012` image only ships the dummy stub
`libmetisDecomp.so`. Calling `decomposePar -method metis` against the stub
exits with `FOAM FATAL ERROR: metisDecomp not loaded`. The Dockerfile installs
`libmetis-dev` (apt) and re-runs `wmake libso` against
`$WM_PROJECT_DIR/src/parallel/decompose/metisDecomp` with `METIS_ARCH_PATH=/usr`,
which links the real implementation. The smoke test (`smoke.sh`) verifies the
result with `nm -D --defined-only ... | c++filt | grep 'T Foam::metisDecomp'`.

### Where the swak4Foam libs live

swak4Foam's `AllwmakeAll` writes to `$FOAM_USER_LIBBIN`/`$FOAM_USER_APPBIN` by
default. In the v2406 image those resolve under `/root/OpenFOAM/user-v2406/...`,
which is **not** on the default loader path — every user had to hand-patch
`LD_LIBRARY_PATH`. This image hijacks `FOAM_USER_LIBBIN=$FOAM_SITE_LIBBIN`
(and `FOAM_USER_APPBIN=$FOAM_SITE_APPBIN`) for the build, so libs land at
`$FOAM_INST/site/.../platforms/.../lib`, which **is** on `LD_LIBRARY_PATH`
the moment the bashrc is sourced. `libgroovyBC.so`, `libswakFunctionObjects.so`,
`libswak4FoamParsers.so`, etc. are discoverable everywhere by default.

### Why a separate service name (not just a tag)

`sciagent.tools.atomic.compute.ComputeTool.execute` resolves a service to its
image with a hardcoded `:latest` tag (`compute.py:256`). A tag variant on the
same repo is not reachable from the tool's API today — a separate service name
is the least-invasive way to make the v2012 build selectable.

### SkyPilot compatibility

- Container glibc 2.31 (Ubuntu 20.04) < SkyPilot host glibc 2.35 (Ubuntu 22.04) — forward-compatible.
- Base manifest is amd64-only; SkyPilot uses amd64 nodes natively (no QEMU).
- All verification runs **inside the container**, both in the Dockerfile build
  and in the CI workflow — nothing on the local machine, nothing host-side.

## CI

The build is handled by a dedicated job in `.github/workflows/build-images.yml`:
`build-openfoam-swak4foam-2012`. It runs the load → smoke → push pattern:

1. `docker buildx build --load --platform linux/amd64` (no push)
2. `docker run --rm <image> /usr/local/bin/sciagent-smoke`
3. `docker buildx build --push --platform linux/amd64` (cache reused from step 1)

Step 2 fails the workflow before any push if any verification regression
appears. The same `smoke.sh` runs as a Dockerfile `RUN` step during the build,
so the image will not even tag locally if the checks regress — defense in depth.

## Local reproduction

```bash
# Build (about 30-60 min on a laptop, mostly swak4Foam compilation).
docker buildx build --load --platform linux/amd64 \
  -f src/sciagent/services/openfoam-swak4foam-2012/Dockerfile \
  -t openfoam-swak4foam-2012:dev \
  src/sciagent/services

# Run the full smoke.
docker run --rm openfoam-swak4foam-2012:dev /usr/local/bin/sciagent-smoke

# Run the actual data-center case files.
docker run --rm -v "$PWD/CaseFiles/steady_compressible:/workspace" \
  openfoam-swak4foam-2012:dev \
  bash -lc 'blockMesh && decomposePar && mpirun -np 2 buoyantSimpleFoam -parallel'
```

The smoke test exercises every regression vector in a single `decomposePar`
pass against a hand-built 4-cell case: `groovyBC` with `$var` + `{patch_outlet}`
lookups, `outletMappedUniformInletHeatAddition`, `omegaWallFunction blending
binomial2`, `method metis`, and `-help` on all four buoyant solvers
(`buoyantSimpleFoam`, `buoyantBoussinesqSimpleFoam`, `buoyantPimpleFoam`,
`buoyantBoussinesqPimpleFoam`).
