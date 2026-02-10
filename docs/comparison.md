---
layout: default
title: Comparison
nav_order: 6
---

# Comparison with Other Frameworks

## Quick Overview

| Feature | SciAgent | AutoGen | LangChain | OpenHands | Aider |
|---------|----------|---------|-----------|-----------|-------|
| Primary focus | Scientific computing | Multi-agent workflows | General agents | Autonomous coding | CLI code editing |
| Containerized simulations | 18+ services | No | No | Sandboxed | No |
| Research-first workflow | Built-in | No | No | No | No |
| Sub-agents | Yes | Yes | Yes | Single | Single |

## When to Use SciAgent

**Choose SciAgent if you need:**
- Scientific simulations (CFD, photonics, chemistry, quantum)
- Research-first approach that checks documentation before coding
- Multi-service pipelines (e.g., rdkit -> gromacs -> scipy)
- Task orchestration with parallel execution

## Alternatives

### Microsoft AutoGen
Best for enterprise multi-agent workflows with Azure integration.

**Strengths:** Enterprise features, graph-based workflows, Azure integration

**Use AutoGen when:** Building production systems with enterprise requirements

### LangChain / DeepAgents
Best for building custom agents with extensive ecosystem integrations.

**Strengths:** Large ecosystem, pluggable backends, provider-agnostic

**Use LangChain when:** Need extensive third-party integrations

### OpenHands
Best for autonomous software development with minimal human input.

**Strengths:** High autonomy, strong benchmarks (SWE-bench), sandboxed execution

**Use OpenHands when:** Want maximum coding autonomy

### Aider
Best for lightweight, fast code editing via chat.

**Strengths:** Lightweight, excellent Git integration, focused interface

**Use Aider when:** Quick code edits in existing projects

## What Makes SciAgent Different

### 1. Scientific Service Registry
18+ containerized environments for domain-specific computing:
- Electromagnetics (MEEP, RCWA)
- Chemistry (RDKit, GROMACS)
- CFD (OpenFOAM)
- Quantum (Qiskit)

### 2. Research-First Skills
The `sci-compute` skill enforces documentation research before code generation:
1. Discovery - Find the right service
2. Research - Search official docs
3. Code - Write using verified patterns
4. Execute - Run in containers
5. Debug - Search for error solutions

### 3. Reproducible Execution
Docker containers ensure identical results across machines and isolate complex dependencies from your local environment.

## Links

- [Microsoft AutoGen](https://github.com/microsoft/autogen)
- [LangChain DeepAgents](https://github.com/langchain-ai/deepagents)
- [OpenHands](https://github.com/All-Hands-AI/OpenHands)
- [Aider](https://github.com/paul-gauthier/aider)
