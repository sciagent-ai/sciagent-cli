---
layout: default
title: BRCA1 Fitness-Structure Analysis
parent: Case Studies
nav_order: 2
---

# BRCA1 Mutation Fitness-Structure Analysis

**Study**: "Accurate classification of BRCA1 variants with saturation genome editing"
**Published**: Findlay et al., Nature 562, 217-222 (2018)

## The task

Map BRCA1 deep mutational scanning scores onto structural features from an AlphaFold model, then verify both high mapping coverage and the expected structure-function signals.

## Why this case matters

This is the best example of a scientifically correct result with a nontrivial audit caveat. The benchmark target is met cleanly, but the provenance layer still records that part of the claimed execution path did not happen as originally described.

## What the audited run did

SciAgent parsed all 1,837 mutations, aligned them to BRCA1 structural positions, extracted secondary-structure and accessibility features, and summarized the signal by structural class and functional domain. In the audited run, that work converged into a single main analysis script rather than a sprawling multistep pipeline.

## Outcome against the benchmark criterion

![BRCA1 Structure-Fitness Analysis](../images/case-studies/brca1_structure_fitness.png)

| Check | Result |
|-------|--------|
| Mutations parsed | **1,837** |
| Mapping rate | **1.00** |
| Buried vs exposed test | **p = 1.48 x 10^-4** |
| Mean fitness, buried | **-1.497** |
| Mean fitness, exposed | **-0.591** |

Both SciAgent and the matching `cc-bare` baseline achieve a mapping rate of `1.00`. The resulting structure-function pattern is also directionally right: buried residues are much less tolerant to mutation than exposed residues.

## What the audit trail adds

The verifier still accepted the run overall as `verified` at confidence `0.78`, but the retrospective report records one fabrication indicator: the claimed SkyPilot path did not complete, and the real computation was carried out locally in Docker.

That distinction matters. The scientific result is still real and the benchmark criterion still passes, but the durable log preserves the scope downgrade instead of flattening it into a simple success story. This is exactly the kind of trust-building nuance a case study should surface.
