---
layout: default
title: Datacenter Temperature with OpenFOAM
parent: Case Studies
nav_order: 3
---

# Simulating Datacenter Temperature Distribution with OpenFOAM

**Paper**: Barestrand et al., "Modelling Convective Heat Transfer of Air in a Data Center using OpenFOAM — Evaluation of the Boussinesq Buoyancy Approximation"
**Published**: OpenFOAM Journal, Vol. 3 (2021), [doi:10.51560/ofj.v3.59](https://doi.org/10.51560/ofj.v3.59)

This page reflects the tracked benchmark run from **June 30, 2026**.

## The challenge

Reproduce Fig. 3 from the paper, using the manuscript PDF and the provided OpenFOAM case files, while keeping the CFD run on a cloud cluster and validating the final temperature statistics against the paper's coarse-grid regime.

## Prompt

```text
Reproduce Fig 3 (typical Boussinesq, 62K grid) from the manuscript on
datacenter temperature distribution. Cap the solver at 1000 iterations
so wall clock stays under ~60 min on a small instance. The KDE shape
is what matters, not residual convergence.

Identify the OpenFOAM environment and boundary conditions from
Manuscript.pdf and the case files in the project folder.

Pull all intermediate and final results back into the project folder.
```

## Trajectory snapshot

The benchmark pair for this task is:

- `icml_results_clean/20260630T184838Z/cfd_fig3_kde/cfd_fig3_kde__sciagent-verifier-on-default__sonnet/`
- `icml_results_clean/20260630T184838Z/cfd_fig3_kde/cfd_fig3_kde__cc-bare__sonnet/`

On **June 30, 2026**, the SciAgent run reported a volume-weighted mean temperature of **296.2092 K** and received a `verified` verdict at confidence `0.91`.
The `cc-bare` baseline for the same task reported **295.333 K**.

## What SciAgent did

### Phase 1: Recover the CFD recipe

SciAgent read both `Manuscript.pdf` and the provided `CaseFiles/` bundle to determine:

- solver: `buoyantBoussinesqSimpleFoam`
- target grid: coarse "c" case
- post-processing target: volume-weighted temperature KDE
- iteration budget: 1000 SIMPLE iterations

### Phase 2: Launch cloud compute

The tracked run used the `openfoam-swak4foam-2012` service and executed the meshing and solver workflow on a SkyPilot-backed cluster. The summary in `result.txt` reports roughly **335 seconds** of solver wall-clock time for the 1000-iteration run.

### Phase 3: Bring results back locally

The useful output split is:

- `project/_outputs/` for the high-level deliverables
- `project/cfd_outputs/` for the extracted CSV and solver logs

That matches the actual artifact layout in the tracked run better than earlier `_outputs/workspace/...` summaries.

## Results

![Fig 3 KDE Reproduction](../images/case-studies/datacenter_cfd_fig3_kde.png)

### Verification metrics from `verification_stats.txt`

| Metric | Value |
|--------|-------|
| Cell count | **61,811** |
| Paper comparison count | **61,927** |
| Cell count error | **0.19%** |
| Volume-weighted mean temperature | **296.2092 K** |
| Temperature range | **290.9645 K to 302.9278 K** |
| Total volume | **117.5916 m³** |
| KDE covariance factor | **0.1** |
| Mean temperature in target range 294-298 K | **YES** |

### Deliverable summary

The final summary on **June 30, 2026** reported:

- Fig. 3 KDE reproduced
- target temperature range satisfied
- coarse-grid cell count matched within 0.19%
- characteristic bimodal datacenter temperature structure recovered

## Generated artifacts

The tracked SciAgent run produced:

- `project/_outputs/fig3_kde.png`
- `project/_outputs/verification_stats.txt`
- `project/cfd_outputs/T_V_data.csv`
- `project/cfd_outputs/cell_count.txt`
- `project/cfd_outputs/logs/log_blockMesh.txt`
- `project/cfd_outputs/logs/log_snappy.txt`
- `project/cfd_outputs/logs/log_checkMesh.txt`
- `project/cfd_outputs/logs/log_solver.txt`

## Why this case matters

This is the clearest end-to-end cloud-compute example in the repo:

- the workflow reads a paper and a nontrivial case bundle
- runs real OpenFOAM on a remote cluster
- materializes the outputs back into the project directory
- verifies the result through SciAgent's provenance and verifier pipeline

For raw inspection, start with:

- `result.txt`
- `stdout.txt`
- `provenance.jsonl`
- `project/_outputs/verification_stats.txt`
- `project/cfd_outputs/`
