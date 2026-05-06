---
layout: default
title: What's New in v2.0
nav_order: 1.5
---

# What's New in v2.0

{: .note }
Looking for v1.0 docs? They live on the [`release/v1.0` branch on GitHub](https://github.com/sciagent-ai/sciagent-cli/tree/release/v1.0/docs). This page describes what changed since.

v2.0 is a major release. The codebase grew from a single-process agent with file I/O into a **multi-substrate runtime** with cloud compute, durable provenance, and registry-backed orchestration. v1.0 stays available as a separate marketing-tag artifact (`tag v1.0`, branch `release/v1.0`); semver starts at v2.0.0.

## Headlines

- **Cloud compute via SkyPilot** — Run scientific simulations on cloud clusters without leaving the agent. New tools: `compute_run`, `compute_exec`, `compute_cluster`, `materialize`, `materialize_workspace`. See [Cloud Compute](cloud-compute.md).
- **Durable provenance log** — Every tool call, compute job, artifact, and verification result lands in an append-only JSONL log per session. Cross-LLM verifiable — a fresh model from any provider can audit a session it didn't run. See [Provenance Log Schema](provenance_log_schema.md).
- **Task orchestration** — A unified registry for in-flight work (`task_index`) covering cloud jobs and background subagents alike. New tools: `task_list`, `task_get`, `task_wait`. See [Task Orchestration](task-orchestration.md).
- **Background subagents with checkpoint & resume** — `spawn(background=True)` returns a `task_id`; per-iteration checkpoints survive crashes; the parent gets a 3-way resume prompt on the next spawn. See [Task Orchestration → Checkpoint & resume](task-orchestration.md#checkpoint--resume).
- **Two new subagent kinds** — `compute` (cloud-job orchestration with token isolation) and `analyze` (post-job derivation: plots, statistics, light fits). The verifier subagent now reads the durable provenance log, so a different model can audit a session it didn't run. See [Architecture → Sub-agents](developers/architecture.md#sub-agents).
- **Updated default models** — Sonnet 4.6 / Opus 4.7 / Haiku 4.5 across the tier system. Provider-agnostic via [LiteLLM](https://github.com/BerriAI/litellm).
- **New scientific service** — `paraview` (multi-arch with EGL) for post-processing OpenFOAM and other simulation outputs. The OpenFOAM image now also ships with a SWAK4Foam-extended variant for field-processing, used internally when a case needs it.
- **First cloud-compute case study** — [Datacenter CFD with OpenFOAM](case-studies/datacenter-cfd.md) reproduces published results end-to-end on a SkyPilot cluster.

## Migrating from v1.0

There are no breaking API changes — v1.0 code that called `AgentLoop`, `SubAgentOrchestrator`, or the core tool registry continues to work. The v2.0 surface is additive:

- The default model has changed from `claude-opus-4-5` to `claude-sonnet-4-6` for the scientific tier. To pin to v1.0 behavior, pass `--model anthropic/claude-opus-4-5-20251101`.
- The `compute_*`, `materialize*`, `task_*`, `bg_*`, and `monitor*` tools are new — main agents pre-v2.0 didn't have them. They're registered automatically; nothing to opt into.
- `pip install '.[cloud]'` is required for SkyPilot. The base install does not pull it in.

If you have v1.0 sessions saved in `.agent_states/`, they remain loadable — the session schema is back-compat.

## Component changes at a glance

| Area | v1.0 | v2.0 |
|------|------|------|
| Compute backends | Local Docker only | Local Docker + SkyPilot (managed jobs + cluster mode) |
| Output handling | Local files | Cloud-agnostic URIs (S3/GCS/Azure/R2/OCI) + per-session workspace bucket |
| Long-running work | Foreground subagents | Background subagents + checkpoint/resume + `task_index` |
| Audit trail | Inline tool logs | Durable per-session JSONL log (`provenance.jsonl`) |
| Verification | Three-tier gates (data/exec/LLM) | Same gates + cross-LLM verifier reading the provenance log |
| Subagent kinds | explore, debug, research, plan, general, verifier | + compute, + analyze |
| Scientific services | openfoam, ... | openfoam (with SWAK4Foam-extended variant), paraview, ... |
| Default scientific model | claude-opus-4-5 | claude-sonnet-4-6 |

## Where to read next

- New to SciAgent? Start with [Getting Started](getting-started.md).
- Already using v1.0? Read [Cloud Compute](cloud-compute.md) and [Task Orchestration](task-orchestration.md) — those are the two surfaces you didn't have.
- Want the audit story? [Provenance Log Schema](provenance_log_schema.md) is the schema; [Architecture → Verification System](developers/architecture.md#verification-system) is the conceptual map.
- Building on the framework? [API Reference](developers/api-reference.md) covers the new Python surface.
