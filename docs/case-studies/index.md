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

These pages are written as narrative summaries of benchmarked reproductions. Each one answers four questions:

- what scientific task was attempted
- why that task matters for understanding SciAgent
- what happened in the audited run
- what the audit layer revealed beyond a simple pass/fail result

Throughout these pages, `cc-bare` means the Claude Code baseline without access to SciAgent's registry or compute subagents.

The full benchmark bundle is not shipped in the main repo. These case studies are meant to stand on their own as reader-facing summaries rather than file-by-file archive guides.

## Available Studies

| Study | Domain | Benchmark criterion | What it shows |
|-------|--------|---------------------|---------------|
| [AR Waveguide Metasurface](photonics.md) | Photonics / RCWA | `MFE >= 0.25` | Long-horizon scientific workflow and why verifier recursion matters |
| [BRCA1 Fitness-Structure Analysis](bioinformatics.md) | Bioinformatics | Mapping rate `>= 0.95` | A correct result with a caught scope downgrade in the audit trail |
| [Datacenter Temperature with OpenFOAM](datacenter-cfd.md) | CFD / OpenFOAM | Mean temperature in `294-298 K` | Clean end-to-end cloud execution with a strong verifier story |
| [Digital IC Synthesis](digital-ic.md) | EDA *(coming soon)* | - | OpenROAD-based benchmark in progress |
