---
layout: default
title: Use Cases
nav_order: 7
---

# Use Cases

All examples below use the current CLI form:

```bash
sciagent run --task "..."
```

## Software Engineering

```bash
# Generate code from description
sciagent run --task "Create a REST API endpoint for user authentication"

# Fix bugs and refactor
sciagent run --task "Fix the memory leak in process_data.py"

# Write tests
sciagent run --task "Write pytest tests for the User class"

# Search and analyze
sciagent run --task "Find all TODO comments and summarize what needs work"
```

## Research

```bash
# Literature search
sciagent run --task "Find recent papers on transformer architectures"

# API exploration
sciagent run --task "Show me how to use the pandas groupby function with examples"

# Comparative analysis
sciagent run --task "Compare React and Vue for building dashboards"
```

## Scientific Computing

SciAgent runs simulations in isolated Docker containers. Ask naturally:

```bash
# Photonics & Electromagnetics
sciagent run --task "Simulate electromagnetic wave propagation through a metasurface using RCWA"
sciagent run --task "Design a photonic crystal waveguide with MEEP"
sciagent run --task "Trace rays through a lens system using pyoptools"

# Chemistry & Materials
sciagent run --task "Analyze molecular properties of this compound from SMILES"
sciagent run --task "Run a molecular dynamics simulation of a protein-ligand complex"
sciagent run --task "Build a crystal structure with ASE and compute its lattice properties"

# Math & Optimization
sciagent run --task "Solve this optimization problem using CVXPY"
sciagent run --task "Derive the symbolic integral of this expression with SymPy"
sciagent run --task "Tune hyperparameters for my ML model using Optuna"

# Circuit & IC Design
sciagent run --task "Simulate this RC circuit with ngspice"
sciagent run --task "Run RTL-to-GDS flow for this Verilog design using OpenROAD"

# Quantum Computing
sciagent run --task "Implement Grover's algorithm and simulate it with Qiskit"

# Bioinformatics
sciagent run --task "Analyze this DNA sequence and find ORFs using Biopython"
sciagent run --task "Run BLAST search against a local database"

# Network & Graph Analysis
sciagent run --task "Find communities in this social network using NetworkX"

# Chemical Process Engineering
sciagent run --task "Simulate a distillation column using DWSIM"

# Differential Equations (Julia)
sciagent run --task "Solve this system of ODEs using Julia's DifferentialEquations.jl"
```

### Available Services

| Domain | Services | Capabilities |
|--------|----------|--------------|
| **Math & Optimization** | scipy-base, sympy, cvxpy, optuna | Numerical computing, symbolic math, convex optimization, hyperparameter tuning |
| **Chemistry & Materials** | rdkit, ase, dwsim | Molecular analysis, atomistic simulations, chemical process simulation |
| **Molecular Dynamics** | gromacs | Biomolecular simulations, soft matter |
| **Photonics & Optics** | rcwa, meep, pyoptools | RCWA for gratings, FDTD electromagnetics, optical ray tracing |
| **CFD & FEM** | openfoam, gmsh, elmer | Fluid dynamics, mesh generation, multiphysics FEM |
| **Post-processing & Visualisation** | paraview | Multi-arch (with EGL) — pairs with the OpenFOAM services |
| **Circuits & EDA** | ngspice, openroad, iic-osic-tools | SPICE simulation, RTL-to-GDS flow, 80+ IC design tools |
| **Quantum Computing** | qiskit | Quantum circuits, gates, algorithms (Grover, VQE, QAOA) |
| **Bioinformatics** | biopython, blast | Sequence analysis, BLAST searching, phylogenetics |
| **Network Analysis** | networkx | Graph algorithms, centrality, community detection |
| **Scientific ML** | sciml-julia | Julia ODE/SDE solving, symbolic modeling, neural DEs |

## Multi-Step Workflows

Combine services for complex pipelines:

```bash
# Drug screening pipeline
sciagent run --task "Screen molecules from compounds.sdf for binding affinity to target protein"
# Uses: rdkit -> gromacs -> scipy-base

# Photonic optimization
sciagent run --task "Optimize a metasurface unit cell for maximum transmission at 1550nm"
# Uses: scipy-base (optimization) -> rcwa (simulation)

# Chip analysis
sciagent run --task "Analyze power/performance/area for this RTL design"
# Uses: openroad -> scipy-base

# Protein structure pipeline
sciagent run --task "Find similar proteins to this sequence and run MD simulation"
# Uses: blast -> biopython -> gromacs

# Optical system design
sciagent run --task "Design a lens system and optimize for minimum aberration"
# Uses: pyoptools (ray tracing) -> optuna (optimization) -> scipy-base (analysis)

# Crystal structure + analysis pipeline
sciagent run --task "Build a crystal structure and analyze its lattice properties"
# Uses: ase (structure) -> scipy-base (analysis)

# Network-based drug discovery
sciagent run --task "Build protein interaction network and identify key drug targets"
# Uses: biopython (sequences) -> networkx (graph analysis) -> scipy-base (statistics)

# Chemical process optimization
sciagent run --task "Optimize reactor conditions for maximum yield"
# Uses: dwsim (process sim) -> optuna (optimization)

# Quantum chemistry workflow
sciagent run --task "Calculate ground state energy using VQE algorithm"
# Uses: qiskit (quantum simulation) -> scipy-base (classical optimization)
```

### Example Pipeline: Multi-Service Workflows

| Workflow | Services | Output |
|----------|----------|--------|
| Drug screening | rdkit → gromacs → scipy-base | Ranked molecules by binding affinity |
| Metasurface design | scipy-base → rcwa | Optimized nano-structure geometry |
| IC design flow | openroad → scipy-base | Power/performance/area report |
| Protein pipeline | blast → biopython → gromacs | MD trajectory from sequence |
| Optical design | pyoptools → optuna → scipy-base | Optimized lens parameters |
| Crystal structure analysis | ase → scipy-base | Lattice properties |
| Process engineering | dwsim → optuna | Optimal reactor conditions |

See [Case Studies](case-studies/) for real-world examples of SciAgent in published research.
