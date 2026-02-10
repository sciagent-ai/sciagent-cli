---
layout: default
title: Use Cases
nav_order: 5
---

# Use Cases

## Software Engineering

```bash
# Generate code from description
sciagent "Create a REST API endpoint for user authentication"

# Fix bugs and refactor
sciagent "Fix the memory leak in process_data.py"

# Write tests
sciagent "Write pytest tests for the User class"

# Search and analyze
sciagent "Find all TODO comments and summarize what needs work"
```

## Research

```bash
# Literature search
sciagent "Find recent papers on transformer architectures"

# API exploration
sciagent "Show me how to use the pandas groupby function with examples"

# Comparative analysis
sciagent "Compare React and Vue for building dashboards"
```

## Scientific Computing

SciAgent runs simulations in isolated Docker containers. Ask naturally:

```bash
sciagent "Simulate electromagnetic wave propagation through a metasurface using RCWA"
sciagent "Run a molecular dynamics simulation of a protein-ligand complex"
sciagent "Solve this optimization problem using CVXPY"
```

### Available Services

| Domain | Services | Capabilities |
|--------|----------|--------------|
| **Math** | scipy-base, sympy, cvxpy | Numerical computing, symbolic math, optimization |
| **Chemistry** | rdkit, gromacs, ase | Molecular analysis, MD simulations, atomistic modeling |
| **Photonics** | rcwa, meep | RCWA for gratings, FDTD for electromagnetics |
| **CFD** | openfoam, gmsh, elmer | Fluid dynamics, meshing, multiphysics FEM |
| **Circuits** | ngspice | SPICE simulation |
| **Quantum** | qiskit | Quantum circuit simulation |
| **Bio** | biopython, blast | Sequence analysis, similarity search |

## Multi-Step Workflows

Combine services for complex pipelines:

```bash
# Drug screening pipeline
sciagent "Screen molecules from compounds.sdf for binding affinity to target protein"
# Uses: rdkit -> gromacs -> scipy-base

# Photonic optimization
sciagent "Optimize a metasurface unit cell for maximum transmission at 1550nm"
# Uses: scipy-base (optimization) -> rcwa (simulation)

# Chip analysis
sciagent "Analyze power/performance/area for this RTL design"
# Uses: openroad -> scipy-base
```

## Case Study: AR Waveguide Design

This example shows SciAgent supporting photonics research from simulation to publication (*Optical Materials Express*, Dec 2025).

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
