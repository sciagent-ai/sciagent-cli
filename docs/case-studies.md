---
layout: default
title: Case Studies
nav_order: 6
---

# Case Studies

Real-world examples of SciAgent supporting scientific research and engineering workflows.

---

## AR Waveguide Metasurface Design

*Published in Optical Materials Express, Dec 2025*

### Problem

Design a multi-zone metasurface in-coupler for AR waveguides with efficiency approaching theoretical limits.

### SciAgent Workflow

**1. Research** - Agent searched RCWA documentation and metasurface design papers

**2. Simulation** - Ran RCWA simulations to optimize nano-beam geometries:
```bash
sciagent "Design a three-zone metasurface in-coupler for AR waveguide
         with 453nm grating period, optimizing diffraction efficiencies"
```

**3. Evaluation** - Ray-tracing to assess full system performance

### Results

| Metric | Simulated | Measured |
|--------|-----------|----------|
| Average coupling efficiency | 31% | 30% |
| Minimum field efficiency | 25.3% | 17% |
| Theoretical limit | 29% | - |

Close agreement validated the design approach. The research-first workflow ensured correct API usage and reproducible results.

**Services used:** rcwa, scipy-base

---

## Case Study 2

*Coming soon*

---

## Case Study 3

*Coming soon*
