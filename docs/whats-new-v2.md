---
layout: default
title: What's New in v2
nav_order: 1.5
---

# What's New in v2

{: .note }
Looking for v1.0 docs? They live on the [`release/v1.0` branch on GitHub](https://github.com/sciagent-ai/sciagent-cli/tree/release/v1.0/docs). This page describes what changed since.

## v2.1 (2026-07)

A minor release. 44 commits on top of v2.0.0 — no breaking API changes, additive behavior across verification, LLM I/O, compute routing, and cost accounting.

### Headlines

- **Verification gates walk child sessions and retry once** — The DATA/EXEC/LLM gates on `TaskOrchestrator` now traverse subagent sessions when scoring a task, so evidence produced inside a spawned subagent counts toward the parent's gate. A subagent that returns success but fails the `produces_uris` gate gets one continuation turn seeded with the gate's complaint before the failure bubbles up. See [Task Orchestration → Verification](task-orchestration.md#verification).
- **Trajectory-aware verification + `session_end` event** — The verification gate now reads the full session trajectory (tool calls, artifacts, timing) via a new `session_end` event in the provenance log, rather than only the final result. See [Provenance Log Schema](provenance_log_schema.md).
- **Multimodal attachments ship exactly once per session** — Each PDF/image gets a UUID `artifact_id` at the agent-loop boundary; `LLMClient` sends the full base64 block on first pass and a text-fallback marker on every subsequent pass. Anthropic prompt-cache layout stays byte-stable. Subagents get their own isolated set. See [Tools → PDF & image inputs](tools.md#pdf--image-inputs).
- **Anthropic native block format for images/documents** — Instead of a provider-neutral shape, image/document blocks now go over the wire in Anthropic's native format when the provider is Anthropic. Other providers unaffected (LiteLLM continues to normalize).
- **LLM-agnostic caching and reasoning in the agent** — Cache-control emission and reasoning-token accounting are now handled at the agent layer, not per-provider. Gemini implicit cache still hits automatically on 2.5+; Anthropic explicit cache_control still emitted for Anthropic.
- **Compute router looks at the local daemon before going to cloud** — When a workload fits the current environment, the router keeps it local instead of provisioning a SkyPilot cluster. New env-composition and compute-threshold heuristics decide the split. See [Cloud Compute → When SkyPilot vs local](cloud-compute.md#when-skypilot-vs-local).
- **Aggregate cost rollup across LLM, compute, and storage** — Costs are tracked on separate axes and rolled up per run and per `CellResult`. `sky.cost_report` powers the compute axis. See [Configuration → Cost caps](configuration.md#cost-caps).
- **Layered config surface (`--config`, `--set`)** — CLI now accepts a layered `--config <path>` plus `--set key=value` overrides, with kill-switch caps that hard-limit tokens/cost regardless of config. Precedence: `--set` > `--config` > env > yaml > default. See [Configuration → Configuration layers](configuration.md#configuration-layers).
- **`ask_user` replaces `pause_for_user`** — What was an out-of-band gate is now an agent tool the model can call itself when it needs input. See [Tools → ask_user](tools.md#ask_user).
- **Scientific web search tools** — `web_search_openalex`, `web_search_crossref`, and companions surface as first-class tools alongside the existing web search. Research subagent now ingests artifacts (PDFs, figures) rather than URLs only.
- **`service_search` token-fallback** — When the exact-substring pass returns no matches, a second pass tokenizes and singularizes both query and haystack. Rescues multi-word queries whose tokens span fields (`molecular dynamics`) and plural queries against singular haystacks (`simulations` vs `simulation`). Result rows carry `match_mode` and `matched_tokens`.
- **Session-scoped exec/fetch logger** — Log lines from `exec_shell` / `fetch_url` are attributed to the session that spawned them, so parallel sessions no longer interleave in one flat log.
- **Registry adds** — PyTorch dockerfile; RCWA image ships `S4.so` in site-packages with an import verification step.

### Migrating from v2.0

No breaking changes to the Python API. Two surface renames worth knowing:

- `pause_for_user` gate → `ask_user` tool. Existing sessions saved before the switch replay fine; new sessions call the tool directly.
- `BaseTool` schema now emits `parameters` instead of `input_schema` when serialized (aligns with the Anthropic SDK's tool-use format). If you consumed the schema JSON externally, update the key.

Everything else is additive. `--config` and `--set` are new CLI flags; existing env vars and `~/.sciagent/config.yaml` continue to work with the same precedence order.

### Component changes at a glance

| Area | v2.0 | v2.1 |
|------|------|------|
| Verification scope | Final result of the parent session | Full trajectory + walks child sessions + one corrective retry on gate failure |
| Multimodal I/O | Re-sent every turn | Sent once per session via `artifact_id`; text-fallback marker on replay |
| Anthropic image/PDF wire format | Provider-neutral block | Native Anthropic block format |
| Compute routing | Threshold → SkyPilot | Local daemon first if it fits; env-composition heuristic decides |
| Cost tracking | Per-tool | Aggregate rollup across LLM / compute / storage axes on runs and `CellResult` |
| CLI config | Env + yaml | Env + yaml + `--config` + `--set` + kill-switch caps |
| User-in-the-loop | `pause_for_user` gate | `ask_user` agent tool |
| Web search | Generic web search | + `openalex`, `crossref`, and companions |
| `service_search` | Exact substring only | + token-fallback with plural handling; `match_mode` per row |

## v2.0 (2026-05)

v2.0 is a major release. The codebase grew from a single-process agent with file I/O into a **multi-substrate runtime** with cloud compute, durable provenance, and registry-backed orchestration. v1.0 stays available as a separate marketing-tag artifact (`tag v1.0`, branch `release/v1.0`); semver starts at v2.0.0.

### Headlines

- **Cloud compute via SkyPilot** — Run scientific simulations on cloud clusters without leaving the agent. New tools: `compute_run`, `compute_exec`, `compute_cluster`, `materialize`, `materialize_workspace`. See [Cloud Compute](cloud-compute.md).
- **Durable provenance log** — Every tool call, compute job, artifact, and verification result lands in an append-only JSONL log per session. Cross-LLM verifiable — a fresh model from any provider can audit a session it didn't run. See [Provenance Log Schema](provenance_log_schema.md).
- **Task orchestration** — A unified registry for in-flight work (`task_index`) covering cloud jobs and background subagents alike. New tools: `task_list`, `task_get`, `task_wait`. See [Task Orchestration](task-orchestration.md).
- **Background subagents with checkpoint & resume** — `spawn(background=True)` returns a `task_id`; per-iteration checkpoints survive crashes; the parent gets a 3-way resume prompt on the next spawn. See [Task Orchestration → Checkpoint & resume](task-orchestration.md#checkpoint--resume).
- **Two new subagent kinds** — `compute` (cloud-job orchestration with token isolation) and `analyze` (post-job derivation: plots, statistics, light fits). The verifier subagent now reads the durable provenance log, so a different model can audit a session it didn't run. See [Architecture → Sub-agents](developers/architecture.md#sub-agents).
- **Updated default models** — Sonnet 4.6 / Opus 4.7 / Haiku 4.5 across the tier system. Provider-agnostic via [LiteLLM](https://github.com/BerriAI/litellm).
- **Two-layer Python config** — `AgentConfig` (agent-loop concerns: model, tokens, iterations, compaction) is now joined by a separate `CloudConfig` (cloud-side concerns: cost gate, workspace store, autostop default, subagent warm-resume). `AgentLoop` accepts both as independent kwargs. Existing env vars and `~/.sciagent/config.yaml` keys still work; precedence is env > config dataclass > yaml > built-in default. See [Configuration](configuration.md#configuration-layers).
- **New scientific service** — `paraview` (multi-arch with EGL) for post-processing OpenFOAM and other simulation outputs. The OpenFOAM image now also ships with a SWAK4Foam-extended variant for field-processing, used internally when a case needs it.
- **First cloud-compute case study** — [Datacenter CFD with OpenFOAM](case-studies/datacenter-cfd.md) reproduces published results end-to-end on a SkyPilot cluster.

### Migrating from v1.0

There are no breaking API changes — v1.0 code that called `AgentLoop`, `SubAgentOrchestrator`, or the core tool registry continues to work. The v2.0 surface is additive:

- The default model has changed from `claude-opus-4-5` to `claude-sonnet-4-6` for the scientific tier. To pin to v1.0 behavior, pass `--model anthropic/claude-opus-4-5-20251101`.
- The `compute_*`, `materialize*`, `task_*`, `bg_*`, and `monitor*` tools are new — main agents pre-v2.0 didn't have them. They're registered automatically; nothing to opt into.
- `pip install '.[cloud]'` is required for SkyPilot. The base install does not pull it in.

If you have v1.0 sessions saved in `.agent_states/`, they remain loadable — the session schema is back-compat.

### Component changes at a glance

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
