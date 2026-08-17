---
layout: default
title: AR Waveguide Metasurface
parent: Case Studies
nav_order: 1
---

# AR Waveguide Metasurface Reproduction

**Paper**: "Design and Experimental Validation of a High-Efficiency Multi-Zone Metasurface Waveguide In-Coupler"
**Published**: Optical Materials Express, Vol. 15, No. 12, December 2025

## The task

Reproduce the paper's RCWA-based in-coupler optimization from the manuscript alone and show that the minimum field efficiency (MFE) clears the paper-level target of `0.25`.

## Why this case matters

This is the flagship long-horizon workflow in the benchmark set. The scientific answer is not the whole story: both SciAgent and the `cc-bare` baseline clear the headline threshold, but photonics is where the verifier's access to the real execution trail most clearly changes the quality of the audit.

## What the audited run did

SciAgent read the paper PDF, recovered the key optical and geometric parameters, and then built a staged RCWA workflow to optimize the three metasurface zones instead of stopping at a single one-shot script.

Core parameters recovered from the paper:

| Parameter | Value |
|-----------|-------|
| Wavelength | 532 nm |
| Grating period | 453 nm |
| Structure height | 250 nm |
| Beam widths | 110, 110, 100 nm |
| Pillar radii | 50, 85, 98 nm |

The run produced a multi-stage optimization flow, per-zone summaries, and efficiency curves that recovered the paper's intended qualitative behavior across field angles.

## Outcome against the benchmark criterion

![Metasurface Simulation Results](../images/case-studies/metasurface_results.png)

| Measure | SciAgent | `cc-bare` | Paper target |
|---------|----------|-----------|--------------|
| Minimum field efficiency | **25.09%** | 25.04% | **>= 25.0%** |
| Average coupling efficiency | **30.6%** | - | 31.0% |

SciAgent clears the benchmark threshold and lands very close to the paper's reported average coupling efficiency. The matching `cc-bare` run also passes, which is exactly why this case is useful: correctness alone does not distinguish the two systems.

## What the audit trail adds

The retrospective benchmark report makes photonics the clearest demonstration of verifier recursion:

- the audited SciAgent run was accepted as `verified` at confidence `0.75`
- the verifier recorded 18 supporting facts, 3 missing-evidence notes, and 3 warnings
- on the same task, withholding child-session logs from the verifier dropped the verdict to `insufficient`

That is the real lesson of this page. The main scientific result was reproducible either way; the stronger contribution is that SciAgent leaves a trail the verifier can actually interrogate across subagent boundaries.
