---
layout: default
title: Reading Trajectories
nav_order: 10
---

# Reading Trajectories

The main repo does not include the full benchmark bundle. When you are working from a separate results archive, this page shows how to inspect the trajectories quickly.

## Directory layout

Each case study lives under a timestamped directory:

```text
icml_results_clean/<timestamp>/<task>/<cell_id>/
```

Examples from a benchmark bundle:

- `icml_results_clean/20260630T120254Z/photonics/photonics__sciagent-verifier-on-default__sonnet/`
- `icml_results_clean/20260630T135609Z/brca1_fitness_structure/brca1_fitness_structure__sciagent-verifier-on-default__sonnet/`
- `icml_results_clean/20260630T184838Z/cfd_fig3_kde/cfd_fig3_kde__sciagent-verifier-on-default__sonnet/`

Matching Claude Code baselines live alongside them as `__cc-bare__...` cells. In this benchmark shorthand, `cc-bare` means Claude Code without access to SciAgent's registry or compute subagents.

## What to read first

### SciAgent cells

| File | What it tells you |
|------|-------------------|
| `result.txt` | Final user-facing answer |
| `stdout.txt` | Terminal transcript |
| `provenance.jsonl` | Append-only audit trail: tool calls, compute launches, verifier output |
| `cost_breakdown.csv` | Cost split |
| `project/` | Working directory and generated artifacts |

### `cc-bare` cells

| File | What it tells you |
|------|-------------------|
| `result.txt` | Final user-facing answer |
| `stdout.txt` | Claude Code transcript in JSONL form |
| `project/` | Working directory and generated artifacts |

The important asymmetry is that the `cc-bare` runs do **not** have SciAgent provenance or `verification_result` events. That difference is the core audit-surface distinction highlighted in the retrospective report.

## Common inspection tasks

### 1. Check the final claim

Read `result.txt`.

Examples:

- Photonics SciAgent run on June 30, 2026 reports `MFE = 25.09%`
- Photonics `cc-bare` baseline on June 8, 2026 reports `MFE = 25.04%`
- BRCA1 SciAgent run on June 30, 2026 reports `mapping_rate = 1.00`
- CFD SciAgent run on June 30, 2026 reports `vol_weighted_mean_T_K = 296.2092`

### 2. Check whether the verifier accepted the run

Read `provenance.jsonl` and find the final `verification_result` event.

In the June 30, 2026 SciAgent runs:

- photonics: `verified`, confidence `0.75`
- brca1_fitness_structure: `verified`, confidence `0.78`
- cfd_fig3_kde: `verified`, confidence `0.91`

Those same runs contain structured evidence arrays such as `supporting_facts`, `missing_evidence`, and `fabrication_indicators`.

### 3. Find the real outputs

Look under `project/`.

Examples from the tracked runs:

- photonics: `project/_outputs/photonics/`
- BRCA1: `project/_outputs/`
- CFD: `project/_outputs/` plus `project/cfd_outputs/`

### 4. Compare SciAgent to `cc-bare`

Read the pair of `result.txt` files first, then compare:

- SciAgent: `provenance.jsonl`, verifier verdict, structured evidence
- `cc-bare`: `stdout.txt` JSONL transcript and project outputs only

For a rollup across all tasks, start with:

- `icml_results_clean/final_report/report.md`
- `icml_results_clean/final_report_cleaned_chatgpt/final_report_clean/report_clean.md`

## How this relates to the case studies

The case-study docs are meant to stand on their own. A trajectory bundle is companion evidence for readers who want to audit the underlying execution:

- what the model claimed
- what it ran
- which artifacts were produced
- whether the verifier accepted the evidence

That is the fastest route from a polished narrative to the underlying benchmark evidence when the archive is available.
