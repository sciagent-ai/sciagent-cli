---
layout: default
title: Comparison
nav_order: 6
---

# How SciAgent Fits the AI Agent Landscape

The AI agent landscape in 2026 spans three categories: general-purpose coding agents, multi-agent orchestration frameworks, and domain-specific scientific systems. SciAgent bridges these — combining software engineering capabilities with containerized scientific computing, cloud compute orchestration, durable provenance, and a fresh-context independent verifier.

This page positions SciAgent against the field on three lenses:

1. [Levels of Scientific Automation](#levels-of-scientific-automation) — where SciAgent sits in the Self-Driving Laboratory autonomy hierarchy.
2. [Capability Axes](#capability-axes) — the capability questions that differentiate systems in this space.
3. [Feature Comparison by Category](#feature-comparison-by-category) — concrete tool-by-tool tables for the three categories.

It closes with a [use-case decision table](#when-to-use-each-approach).

---

## Levels of Scientific Automation

The chemistry and materials community has formalised a Self-Driving Laboratory (SDL) autonomy framework analogous to SAE's autonomy levels for self-driving cars [14][15]. Systems are scored on two independent axes — software autonomy (planning, decisions, analysis) and hardware autonomy (the physical execution substrate) — each from category 0 (manual) to category 3 (fully unattended, diverse experiments). Levels 2-5 are derived from combinations of the two axes; **Level 2-3 is where the vast majority of demonstrated systems sit today, and a true Level 5 (cat 3 in both) remains unattained in the field.**

Where SciAgent fits:

| Axis | Cat 0 | Cat 1 | Cat 2 | Cat 3 | SciAgent |
|------|-------|-------|-------|-------|----------|
| **Software** — planning, dispatch, execution, analysis, verification | Human ideation | One-shot AI suggestion | AI plans + iterates | AI plans, executes, analyzes, verifies independently | **Cat 2-3** |
| **Compute substrate** — provisioning, cluster lifecycle, workspace, fault tolerance | Manual | Single-task script | Workflow config | Diverse jobs, unattended | **Cat 2-3** |

SciAgent automates the **design, computation, and optimization** half of scientific work — simulations, numerical experiments, data analysis, model fitting, design-space exploration. It sits in the AI-for-scientific-computing space, alongside emerging tools like [Dyad](https://juliahub.com/products/dyad) (JuliaHub) connecting simulation and CAE — automating and connecting that ecosystem from the AI side. For its substrate (compute), it covers the same end-to-end loop the SDL framework describes: plan → dispatch → run → observe → derive → verify.

The piece SciAgent adds on the software-autonomy axis is the closed audit loop: durable provenance + independent fresh-context verifier (see [§ Closed audit loop](#1-closed-audit-loop-durable-provenance--fresh-context-verifier) below). Most "AI for science" agents stop at *running* the work — they have no record an external party can audit, and any verification step shares context with the executor. SciAgent's verifier reads the log and the on-disk artifacts only, so a different LLM in a different process can re-audit a session it didn't run. This is what lets the loop scale as autonomy increases [16].

---

## Capability Axes

Tool-by-tool comparisons age fast — competitors ship updates, names change, services come and go. The more durable framing is to ask which **capabilities** a system actually offers. Eight axes that differentiate systems in the autonomous-computational-science space:

| Axis | Question |
|------|----------|
| **Cloud compute** | Can the agent provision and tear down cloud clusters as a normal tool, or does the user wire that up out-of-band? |
| **Workspace persistence** | Do outputs survive cluster teardown via cloud-backed storage, or are they lost when the cluster goes away? |
| **Containerized scientific services** | Are domain-specific environments (CFD, MD, photonics, EDA) bundled and registry-resolvable, or does the user wire up their own Docker images? |
| **Durable provenance** | Is there an append-only audit trail of every tool call, job, artifact, and verification — or do you just have agent transcripts? |
| **Independent verification** | Does an external/fresh-context verifier validate claims against the artifacts and the log, or is "verification" self-attestation by the same model? |
| **Cross-LLM auditability** | Can a different model in a different process re-audit a completed session? |
| **Background work + checkpointing** | Can long-running jobs survive crashes (per-iteration checkpoints, 3-way resume), or is failure terminal? |
| **Software engineering** | Beyond running simulations, can the agent navigate code, debug, do git ops, refactor? |

The matrix below scores broad categories — individual tools within each category vary, and a "✓" marks the typical case rather than a universal claim. The SciAgent column reflects v2.0 as released.

| Axis | Coding agents | Multi-agent frameworks | Scientific agents | SciAgent v2.0 |
|------|---------------|------------------------|-------------------|---------------|
| Cloud compute | ✗ | Build-it-yourself | ✗ | ✓ via SkyPilot |
| Workspace persistence | ✗ | Build-it-yourself | ✗ | ✓ per-session bucket |
| Containerized scientific services | ✗ | Build-it-yourself | Domain-specific | ✓ 25+ services |
| Durable provenance | ✗ | Build-it-yourself | ✗ | ✓ JSONL v1 |
| Independent verification | ✗ | Build-it-yourself | Varies | ✓ fresh context |
| Cross-LLM auditability | ✗ | ✗ | ✗ | ✓ |
| Background work + checkpointing | Partial | Build-it-yourself | ✗ | ✓ |
| Software engineering | ✓ | ✓ | ✗ | ✓ |

"Build-it-yourself" in the multi-agent column means the frameworks *can* implement any of these — they give you primitives, not pre-built scientific infrastructure. The gap is the months of integration work between picking up the framework and running a verified simulation.

---

## Feature Comparison by Category

### Coding Agents

Tools focused on software engineering tasks: code generation, debugging, refactoring, and repository management.

| Feature | Coding Agents | SciAgent |
|---------|---------------|----------|
| Code generation & editing | ✓ All tools | ✓ |
| Repository navigation | ✓ All tools | ✓ |
| Git operations | ✓ All tools | ✓ |
| Autonomous execution | Varies (high in OpenHands, Devin; lower in Cursor) | ✓ |
| Scientific computing | ✗ None | ✓ 25+ containers |
| Cloud compute orchestration | ✗ None | ✓ via SkyPilot (managed jobs + cluster mode) |
| Durable provenance log | ✗ None | ✓ JSONL v1, append-only |
| Fresh-context independent verifier | ✗ None | ✓ reads log + artifacts |

**Representative tools:** Claude Code [1], Cursor [2], Aider [3], OpenHands [4], SWE-Agent [5], Devin [6]

**Key insight:** Coding agents excel at software engineering but stop at the codebase boundary — no scientific environments, no cloud-cluster lifecycle, no durable audit trail. SciAgent retains full SWE capabilities while adding containerized services, cloud compute, and a verifiable record of every run.

---

### Multi-Agent Frameworks

Frameworks for building and orchestrating multiple AI agents working together.

| Feature | Multi-Agent Frameworks | SciAgent |
|---------|------------------------|----------|
| Agent orchestration | ✓ Core capability | ✓ Compute, analyze, verifier, plan, debug, research, explore subagents |
| Custom agent design | ✓ Flexible | Focused, opinionated |
| Provider-agnostic | ✓ Most tools | ✓ Via LiteLLM |
| Scientific computing | ✗ Requires custom setup | ✓ Built-in |
| Pre-built scientific services | ✗ None | ✓ 25+ services |
| Cloud compute orchestration | ✗ Requires custom setup | ✓ via SkyPilot |
| Durable provenance log | ✗ Requires custom setup | ✓ JSONL v1 |
| Background subagents + checkpoint/resume | Varies | ✓ task_index registry, 3-way resume |

**Representative tools:** AG2 [7], Microsoft AutoGen/Semantic Kernel [8], LangChain/LangGraph [9]

**Key insight:** Multi-agent frameworks give you primitives; SciAgent gives you a vertically-integrated scientific runtime. If you need bespoke agent topology, the frameworks win. If you want to run a verified CFD simulation tomorrow, SciAgent skips months of integration.

---

### Scientific AI Agents

Domain-specific agents designed for scientific research and discovery.

| Feature | Scientific Agents | SciAgent |
|---------|-------------------|----------|
| Domain expertise | Typically single domain (chemistry, materials) | Cross-domain (11 areas) |
| Tool count | 5-18 tools | 25+ containerized services |
| Cross-domain pipelines | ✗ Limited | ✓ Full support |
| Software engineering | ✗ Minimal | ✓ Full SWE agent |
| Cloud compute orchestration | ✗ Mostly local or institutional HPC scripts | ✓ via SkyPilot, multi-cloud |
| Durable provenance log | ✗ Mostly transcripts only | ✓ JSONL v1, cross-LLM verifiable |
| Independent fresh-context verifier | Varies; often shares context with executor | ✓ Reads log + artifacts only |
| Workflow scope | Wet-lab synthesis + analysis (Coscientist) | Design, computation, optimization |

**Representative tools:** ChemCrow [10], Coscientist [11], FORUM-AI [12], Google AI Co-Scientist [13]

**Key insight:** Domain-specific scientific agents bring deep expertise but rarely cross domains, rarely orchestrate cloud compute, and rarely persist a verifiable record. SciAgent's strengths are cross-domain breadth, cloud-native execution, and the closed audit loop. Its workflow scope is **design, computation, and optimization** — the AI-for-scientific-computing space, alongside tools like [Dyad](https://juliahub.com/products/dyad) (JuliaHub) connecting simulation and CAE.

---

## Key Differentiators

### 1. Closed audit loop: durable provenance + fresh-context verifier

The unique failure mode of LLM-driven scientific work is plausible-looking but fabricated results. SciAgent addresses this with a closed loop: every relevant event is appended to a durable per-session JSONL log, and an independent verifier with fresh context reads the log + artifacts to validate the claims.

```
Task Execution
      │
      ▼
DATA GATE    → Verify HTTP fetches, detect HTML/error pages, validate CSV structure
      │
      ▼
EXEC GATE    → Verify commands ran, check exit codes
      │
      ▼
LLM VERIFY   → Independent verifier subagent
              · fresh context (no prior reasoning)
              · reads provenance log (JSONL v1)
              · cross-LLM friendly (audit a session you didn't run)
              · adversarial default verdict: "insufficient"
      │
      ▼
[ Provenance log — append-only JSONL ]
  tool_call · tool_result · compute_job_launched · compute_job_status_changed
  artifact_produced · verification_result · correction
```

Two properties together:

- **Durable**. The log is an append-only event stream you can replay. A different model in a different process can read it and reach the same verdict. Per-line cap 16 KB; per-field cap 4 KB; thread-safe via `fcntl.flock`.
- **Cross-LLM**. The verifier reads only the log + artifacts, so the executor can run on Claude Sonnet and the verifier on GPT-4 (or vice versa). Verification doesn't share priors with execution.

See [Provenance Log Schema](provenance_log_schema.md) for the v1 schema; [Cloud Compute](cloud-compute.md) and [Task Orchestration](task-orchestration.md) for what gets logged.

### 2. Cloud-native compute via SkyPilot

`compute_run` provisions a cluster, runs the job, persists outputs to a per-session cloud bucket (`<cloud>://sciagent-workspace-<sid>/`), and cleans up. Multi-cloud (AWS, GCP, Azure, Lambda Labs, etc.) via [SkyPilot](https://skypilot.readthedocs.io/). The agent has tools for the full lifecycle:

- `compute_run(mode="job"|"cluster", backend="skypilot")` — managed jobs (one-shot) or persistent clusters (iterative)
- `compute_exec(cluster_name=...)` — follow-up commands on a warm cluster
- `compute_cluster(action="status"|"stop"|"start"|"down"|"autostop"|"refresh_mounts"|"wait_for_job"|...)` — full lifecycle
- `materialize`, `materialize_workspace` — pull outputs back to local

Defaults that matter:

- **Stop, not down.** End-of-task action is `stop` (preserves disk for fast restart in seconds), not `down` (destroys cluster). The agent's prompt enforces this.
- **Cost gate at $5.** When the optimizer's estimated total exceeds $5, the tool prompts the user with the Sky-optimizer menu before launching. Tool-layer gate; the LLM cannot bypass it. Override via `SCIAGENT_COMPUTE_COMMIT_THRESHOLD_USD` or `~/.sciagent/config.yaml`.
- **Workspace persistence.** The per-session bucket auto-mounts at `/workspace/` on every cluster job; outputs survive cluster teardown.

See [Cloud Compute](cloud-compute.md) for the full guide and the [Datacenter CFD case study](case-studies/datacenter-cfd.md) for an end-to-end example.

### 3. Task orchestration with checkpoint & resume

A unified registry (`task_index`) tracks long-running work — cloud jobs and background subagents alike — at `~/.sciagent/tasks/<task_id>.json`. Two kinds today (`compute_job`, `subagent`); future kinds (`watch`, `scheduled`) land additively. The state machine:

```
pending → running → {completed | failed | cancelled | blocked_produce_missing}
                  → {crashed | blocked_resume}      ← resumable, subagent-only
```

Per-iteration checkpoints persist agent state at `~/.sciagent/sessions/<id>/subagents/<task_id>/checkpoint.jsonl`. On crash before terminal state, a fresh spawn matched by description hash offers the parent a 3-way resume — `skip` · `use_prior` · `retry` — surfaced as an explicit `ask_user` so the user sees what crashed and decides.

Long-running scientific workflows (CFD reproducing a paper, GROMACS trajectory analysis, design-space exploration) survive transient failures (server disconnect, network drop, LLM hiccup) without restarting from zero. See [Task Orchestration](task-orchestration.md).

### 4. Cross-domain containerized services

25+ isolated Docker environments registered in `services/registry.yaml`, spanning eleven scientific areas:

| Domain | Services |
|--------|----------|
| Math & Optimization | scipy-base, sci-core, sympy, cvxpy, optuna |
| Chemistry & Materials | rdkit, ase, dwsim |
| Molecular Dynamics | gromacs |
| Photonics & Optics | rcwa, meep, pyoptools |
| CFD & FEM | openfoam, openfoam-swak4foam, gmsh, elmer |
| Post-processing & Visualisation | paraview |
| Circuits & EDA | ngspice, openroad, iic-osic-tools |
| Quantum Computing | qiskit |
| Bioinformatics | biopython, blast |
| Network Analysis | networkx |
| Scientific ML | sciml-julia |

Cross-domain pipelines are first-class — e.g., RDKit → GROMACS → SciPy for molecular design → simulation → analysis. Service inheritance is registry-resolved (`extends:` chain); adding a new domain means adding a registry entry, with subagent kinds staying generic. See [Architecture → Service Registry](developers/architecture.md#service-registry).

### 5. Research-first workflow

The `use-service` skill enforces documentation research before code generation:

1. **Discovery** – Find the right service in the registry (`service_search`)
2. **Research** – Read official docs and examples (`web`, `service_detail`)
3. **Code** – Write using verified API patterns (`file_ops`)
4. **Execute** – Run in isolated container (`compute_run`)
5. **Debug** – Search for error solutions if needed (`web`, `bash`)

This mirrors the Coscientist approach [11] but generalises across all the registered scientific domains rather than being chemistry-specific.

### 6. SWE + science combined

| Capability | Pure coding agents | Pure scientific agents | SciAgent |
|------------|-------------------|------------------------|----------|
| Navigate codebases | ✓ | ✗ | ✓ |
| Debug complex issues | ✓ | ✗ | ✓ |
| Git operations | ✓ | ✗ | ✓ |
| Run simulations | ✗ | ✓ | ✓ |
| Cloud compute lifecycle | ✗ | ✗ | ✓ |
| Cross-domain compute | ✗ | ✗ | ✓ |
| Validate results | ✗ | Varies | ✓ |
| Durable audit trail | ✗ | ✗ | ✓ |

---

## When to Use Each Approach

| Use Case | Recommended Approach |
|----------|---------------------|
| Pure software engineering (no scientific computing) | Coding agents (Claude Code, Cursor, Aider, etc.) |
| Custom multi-agent architectures, bespoke topology | Orchestration frameworks (AG2, LangChain) |
| Chemistry with wet-lab synthesis (real robots) | ChemCrow, Coscientist |
| Materials science with institutional HPC clusters | FORUM-AI (institutional) |
| Scientific computing + software engineering | SciAgent |
| Cross-domain scientific pipelines (e.g. design → simulate → analyze) | SciAgent |
| Cloud-scale simulations with auditable provenance | SciAgent |
| Long-running runs that must survive transient failures | SciAgent |
| Cross-LLM verification of computational claims | SciAgent |

---

## References

**Coding Agents**

[1] Anthropic. "Claude Code." https://claude.ai/code

[2] Cursor. "The AI Code Editor." https://cursor.sh

[3] P. Gauthier. "Aider: AI pair programming in your terminal." https://github.com/paul-gauthier/aider

[4] All-Hands-AI. "OpenHands: Platform for AI software developers." https://github.com/All-Hands-AI/OpenHands

[5] C. Yang et al. "SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering." *arXiv preprint* arXiv:2405.15793, 2024. https://github.com/SWE-agent/SWE-agent

[6] Cognition AI. "Devin: The first AI software engineer." https://devin.ai

**Multi-Agent Frameworks**

[7] C. Wang et al. "AG2: Community-driven AutoGen fork." https://github.com/ag2ai/ag2

[8] Microsoft. "AutoGen: Multi-agent conversation framework." https://github.com/microsoft/autogen

[9] LangChain. "LangGraph: Build stateful, multi-actor applications." https://github.com/langchain-ai/langgraph

**Scientific AI Agents**

[10] A. M. Bran et al. "ChemCrow: Augmenting large language models with chemistry tools." *Nature Machine Intelligence*, 6, 525–535, 2024. https://doi.org/10.1038/s42256-024-00832-8

[11] D. A. Boiko et al. "Autonomous chemical research with large language models." *Nature*, 624, 570–578, 2023. https://doi.org/10.1038/s41586-023-06792-0

[12] Berkeley Lab. "Berkeley Lab Leads Effort to Build AI Assistant for Energy Materials Discovery (FORUM-AI)." *Berkeley Lab News Center*, 2026. https://newscenter.lbl.gov/2026/02/03/berkeley-lab-leads-effort-to-build-ai-assistant-for-energy-materials-discovery/

[13] Google Research. "AI Co-Scientist: Accelerating scientific discovery." 2024.

**Levels of Scientific Automation**

[14] "Self-Driving Laboratories for Chemistry and Materials Science." *Chemical Reviews*, 2024. https://pubs.acs.org/doi/10.1021/acs.chemrev.4c00055

[15] "Autonomous 'self-driving' laboratories: a review of technology and policy implications." *Royal Society Open Science*, 2025. https://royalsocietypublishing.org/rsos/article/12/7/250646/235354/Autonomous-self-driving-laboratories-a-review-of

[16] "Steering towards safe self-driving laboratories." *Nature Reviews Chemistry*, 2025. https://www.nature.com/articles/s41570-025-00747-x

[17] "Performance metrics to unleash the power of self-driving labs in chemistry and materials science." *Nature Communications*, 2024. https://www.nature.com/articles/s41467-024-45569-5

[18] Argonne National Laboratory. "Autonomous Discovery." https://www.anl.gov/autonomous-discovery

**Additional Resources**

[19] J. M. Zhang et al. "Awesome AI for Science." https://github.com/ai-boost/awesome-ai-for-science
