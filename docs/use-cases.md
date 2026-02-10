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
# Photonics & Electromagnetics
sciagent "Simulate electromagnetic wave propagation through a metasurface using RCWA"
sciagent "Design a photonic crystal waveguide with MEEP"
sciagent "Trace rays through a lens system using pyoptools"

# Chemistry & Materials
sciagent "Analyze molecular properties of this compound from SMILES"
sciagent "Run a molecular dynamics simulation of a protein-ligand complex"
sciagent "Simulate a Lennard-Jones fluid with LAMMPS"

# Math & Optimization
sciagent "Solve this optimization problem using CVXPY"
sciagent "Derive the symbolic integral of this expression with SymPy"
sciagent "Tune hyperparameters for my ML model using Optuna"

# Circuit & IC Design
sciagent "Simulate this RC circuit with ngspice"
sciagent "Run RTL-to-GDS flow for this Verilog design using OpenROAD"

# Quantum Computing
sciagent "Implement Grover's algorithm and simulate it with Qiskit"

# Bioinformatics
sciagent "Analyze this DNA sequence and find ORFs using Biopython"
sciagent "Run BLAST search against a local database"

# Network & Graph Analysis
sciagent "Find communities in this social network using NetworkX"

# Chemical Process Engineering
sciagent "Simulate a distillation column using DWSIM"

# Differential Equations (Julia)
sciagent "Solve this system of ODEs using Julia's DifferentialEquations.jl"
```

### Available Services

| Domain | Services | Capabilities |
|--------|----------|--------------|
| **Math & Optimization** | scipy-base, sympy, cvxpy, optuna | Numerical computing, symbolic math, convex optimization, hyperparameter tuning |
| **Chemistry & Materials** | rdkit, ase, lammps, dwsim | Molecular analysis, atomistic simulations, MD, chemical process simulation |
| **Molecular Dynamics** | gromacs, lammps | Biomolecular simulations, soft matter, solid-state materials |
| **Photonics & Optics** | rcwa, meep, pyoptools | RCWA for gratings, FDTD electromagnetics, optical ray tracing |
| **CFD & FEM** | openfoam, gmsh, elmer | Fluid dynamics, mesh generation, multiphysics FEM |
| **Circuits & EDA** | ngspice, openroad, iic-osic-tools | SPICE simulation, RTL-to-GDS flow, 80+ IC design tools |
| **Quantum Computing** | qiskit | Quantum circuits, gates, algorithms (Grover, VQE, QAOA) |
| **Bioinformatics** | biopython, blast | Sequence analysis, BLAST searching, phylogenetics |
| **Network Analysis** | networkx | Graph algorithms, centrality, community detection |
| **Scientific ML** | sciml-julia | Julia ODE/SDE solving, symbolic modeling, neural DEs |

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

# Protein structure pipeline
sciagent "Find similar proteins to this sequence and run MD simulation"
# Uses: blast -> biopython -> gromacs

# Optical system design
sciagent "Design a lens system and optimize for minimum aberration"
# Uses: pyoptools (ray tracing) -> optuna (optimization) -> scipy-base (analysis)

# Materials simulation pipeline
sciagent "Build a crystal structure and run molecular dynamics"
# Uses: ase (structure) -> lammps (MD) -> scipy-base (analysis)

# Network-based drug discovery
sciagent "Build protein interaction network and identify key drug targets"
# Uses: biopython (sequences) -> networkx (graph analysis) -> scipy-base (statistics)

# Chemical process optimization
sciagent "Optimize reactor conditions for maximum yield"
# Uses: dwsim (process sim) -> optuna (optimization)

# Quantum chemistry workflow
sciagent "Calculate ground state energy using VQE algorithm"
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
| Materials modeling | ase → lammps → scipy-base | Thermodynamic properties |
| Process engineering | dwsim → optuna | Optimal reactor conditions |

See [Case Studies](case-studies.md) for real-world examples of SciAgent in published research.
