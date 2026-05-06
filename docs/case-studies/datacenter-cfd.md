---
layout: default
title: Datacenter Temperature with OpenFOAM
parent: Case Studies
nav_order: 3
---

# Simulating Datacenter Temperature Distribution with OpenFOAM

**Paper**: Barestrand et al., "Datacenter CFD with OpenFOAM"
**Published**: OpenFOAM Journal, Vol. 3 (2021), [doi:10.51560/ofj.v3.59](https://doi.org/10.51560/ofj.v3.59)

---

## The Challenge

Reproduce Fig 3 of the paper, the room-temperature distribution in a datacenter, starting from the manuscript PDF and the author's OpenFOAM case bundle. Run the heavy CFD on a cloud cluster (not the laptop), then analyze the saved fields locally. Cap the SIMPLE solver at 500 iterations so cloud wall-clock stays under ~10 minutes; the KDE shape is the target, not residual convergence.

## Prompt

```
Quick reproduction of Fig 3 (typical Boussinesq, 62K grid). Cap the SIMPLE
solver at 500 iterations so wallclock stays under ~10 min on a small
instance. The KDE shape is what matters, not residual convergence.

Identify the OpenFOAM environment and boundary conditions from
Manuscript.pdf and the case files in the project folder.

When the solver finishes, pull the final time-step T field, the solver
log, and the KDE plot back into the project folder. Then sky down the
cluster, do this even on error.
```

## What SciAgent Did

**Phase 1: Paper Analysis**
Read the manuscript and the author-provided case bundle to extract the simulation recipe with no human walkthrough of the physics:

| Choice | Value | Source |
|--------|-------|--------|
| Solver | buoyantBoussinesqSimpleFoam | Manuscript + case files |
| Turbulence | k-epsilon | constant/turbulenceProperties |
| Rack BCs | outletMappedUniformInletHeatAddition | 0.org/T |
| Supply T | 291.45 K | Manuscript Sec. 2 |
| KDE bandwidth | covariance_factor = 0.1 | kdePlot.py |

**Phase 2: Stage Modified Case**
Wrote a coarse blockMeshDict (47x24x50, ~62k cells after snappyHexMesh) and a 500-iteration controlDict. Everything else preserved verbatim from the paper's steady_incompressible case.

**Phase 3: Cloud Compute**
Stood up a fresh compute cluster on demand and ran the meshing + solver pipeline there:

```
blockMesh -> snappyHexMesh -> checkMesh -> buoyantBoussinesqSimpleFoam
                                                     |
                                       writeCellVolumes -> T, V, logs
```

500 SIMPLE iterations completed in ~2.5 min wall-clock. Result fields materialized to the local project folder; cluster torn down afterwards.

**Phase 4: Local KDE Analysis**
Parsed the saved temperature and cell-volume fields (61,808 cells), computed the volume-weighted Gaussian KDE with the paper's bandwidth, and rendered the plot. Sim and analysis as separate stages means the same fields can be re-analyzed (different bandwidth, different slice) without re-running the solver.

## Results

![Fig 3 KDE Reproduction](../images/case-studies/datacenter_cfd_fig3_kde.png)

### KDE Comparison vs. Paper Fig 3 ("c: 62k" curve)

| Metric | This run | Paper (c: 62k) |
|--------|----------|----------------|
| T range | 291.08 to 302.89 K | ~291 to 303 K |
| Supply T | 291.45 K | 291.45 K |
| KDE peak T | 291.47 K | ~292 K |
| KDE peak density | 30.84 m^3 | ~30 m^3 |
| T_mean (vol-weighted) | 295.40 K | ~295 to 296 K |
| Cell count | 61,808 | ~62,000 |

**KDE shape matches qualitatively**: dominant peak at the supply temperature, broad tail to ~303 K from hot-aisle recirculation, with the broadened intermediate-temperature volume the paper attributes to numerical diffusion at this resolution.

### Mesh Quality (checkMesh)

| Metric | Value |
|--------|-------|
| Cells | 61,808 |
| Max non-orthogonality | 48.1 deg |
| Mean non-orthogonality | 4.0 deg |
| Mesh OK | yes |

## Validation

| Check | Status |
|-------|--------|
| Solver matches paper | PASS |
| Coarse grid ~62k cells | PASS (61,808) |
| 500-iter cap honored | PASS |
| Boundary conditions match manuscript | PASS |
| KDE shape vs Fig 3 | PASS |
| Cluster torn down on completion | PASS |

## Generated Artifacts

- `_outputs/fig3_kde.png` - Fig 3 reproduction
- `_outputs/fig3_kde_stats.json` - numerical summary (n_cells, T range, peak, T_mean)
- `_outputs/case_staged/` - modified blockMeshDict and capped controlDict
- `_outputs/fig3_Tfield/T` - final temperature field
- `_outputs/fig3_Tfield/V` - cell volumes
- `_outputs/fig3_Tfield/solver.log` - SIMPLE solver log (500 iterations)
- `_outputs/fig3_Tfield/checkMesh.log` - mesh quality report

## Execution

- **Time**: ~14 minutes
- **Iterations**: 64 agent turns
- **Services**: `openfoam-swak4foam-2012` (containerized OpenFOAM v2012 + swak4foam, run on a SkyPilot c6i.2xlarge cluster)
