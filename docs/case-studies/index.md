---
layout: default
title: Case Studies
nav_order: 9
has_children: true
---

# Case Studies

Real-world examples of SciAgent reproducing scientific research from peer-reviewed publications.

---

## Overview

Each study includes the prompt given to SciAgent, the workflow it followed (paper analysis → setup → run → validation), the artifacts produced, and a numerical comparison against the published values.

For the audit-grade benchmark artifacts themselves, read the trajectories under `icml_results_clean/`. A SciAgent cell usually includes:

- `result.txt` for the final answer
- `stdout.txt` for the terminal transcript
- `provenance.jsonl` for the append-only audit trail
- `project/` for the working directory and generated outputs

The matching `cc-bare` cells keep `result.txt`, `stdout.txt`, and the project workspace, but do not include SciAgent provenance by construction. See [Reading Trajectories](../reading-trajectories.md) for a practical walkthrough.

## Available Studies

| Study | Domain | Stack |
|-------|--------|-------|
| [AR Waveguide Metasurface](photonics.md) | Photonics / RCWA | S4 (Stanford RCWA) |
| [BRCA1 Fitness-Structure Analysis](bioinformatics.md) | Bioinformatics | BioPython, SciPy |
| [Datacenter Temperature with OpenFOAM](datacenter-cfd.md) | CFD / OpenFOAM | OpenFOAM v2012, SkyPilot |
| [Digital IC Synthesis](digital-ic.md) | EDA *(coming soon)* | OpenROAD |
