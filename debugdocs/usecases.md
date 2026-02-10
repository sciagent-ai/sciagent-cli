---
layout: default
title: Use Cases
nav_order: 5
---

# Use Cases

SciAgent is designed to support researchers, developers and scientists working on complex tasks that combine coding, documentation, data processing and simulation.  This page highlights common scenarios where SciAgent can accelerate your workflow.

## Software engineering and coding

SciAgent excels at automating repetitive parts of the software development process:

* **Code generation** – provide a high‑level description of a function or script and let the agent plan and implement it using the appropriate language and libraries.
* **Bug fixing and refactoring** – ask SciAgent to identify and fix errors, update deprecated API calls or reorganise code into modular components.
* **Unit test writing** – instruct the agent to read existing functions and write comprehensive tests using frameworks such as `pytest`.  The built‑in `test_writer` sub‑agent specialises in this.
* **Documentation and comments** – generate docstrings, README files and code comments based on the implementation.
* **Code search and analysis** – use the `search` tool to perform regex or glob searches across large repositories and summarise findings.

## Research and learning

When exploring unfamiliar domains or libraries, SciAgent can act as a research assistant:

* **Literature review** – have the agent search the web for peer‑reviewed papers, preprints and tutorials on a given topic using the `web` tool.  The results are categorised by quality so you can focus on authoritative sources.
* **API usage examples** – query how to use specific functions or frameworks and generate example code snippets.
* **Data extraction and summarisation** – fetch content from websites, parse tables or JSON data, and produce concise summaries.
* **Comparative analysis** – compare multiple algorithms, libraries or design patterns by gathering and synthesising information from diverse sources.

## Scientific computing and simulation

SciAgent integrates with 18 containerised simulation environments via the `service` tool, organised by domain:

### Bioinformatics and Computational Biology
* **biopython** – DNA/RNA/protein sequence manipulation, file parsing (FASTA, GenBank), phylogenetics
* **blast** – NCBI BLAST+ for sequence similarity searching (blastn, blastp, blastx)
* **gromacs** – molecular dynamics for biomolecular systems, free energy calculations

### Computational Chemistry and Materials
* **rdkit** – cheminformatics: molecular descriptors, fingerprints, similarity searches, SMILES/SDF parsing
* **ase** – Atomic Simulation Environment for atomistic simulations, DFT interfaces, trajectory analysis
* **gromacs** – force field simulations for proteins, membranes, and small molecules

### Photonics and Electromagnetics
* **rcwa** – Rigorous Coupled Wave Analysis (S4) for photonic crystals, gratings, multilayer optics
* **meep** – FDTD electromagnetic simulation for waveguides, resonators, near-to-far-field transforms

### CFD and Multiphysics
* **openfoam** – incompressible/compressible flow, turbulence modeling (RANS, LES), multiphase
* **gmsh** – 2D/3D mesh generation with boundary layer refinement, CAD integration
* **elmer** – multiphysics FEM: heat transfer, structural mechanics, electromagnetics, acoustics

### EDA and Digital Design
* **openroad** – complete RTL-to-GDS flow: synthesis (Yosys), placement, CTS, routing, timing analysis

### Circuit Simulation
* **ngspice** – SPICE circuit simulation with PySpice bindings: transient, AC, DC analysis

### Quantum Computing
* **qiskit** – quantum circuit construction, gate operations, simulation, algorithm implementation (Grover, VQE, QAOA)

### Mathematical Computing
* **scipy-base** – NumPy, SciPy, Matplotlib, Pandas for numerical computing and visualization
* **sympy** – symbolic algebra, calculus, equation solving, LaTeX output
* **cvxpy** – convex optimization: LP, QP, SDP with multiple solver backends

### Network and Graph Analysis
* **networkx** – graph algorithms, centrality measures, community detection, visualization

### Scientific Machine Learning
* **sciml-julia** – Julia ecosystem: DifferentialEquations.jl, ModelingToolkit.jl, neural ODEs

## Complex workflows

Many tasks require multiple stages that depend on each other.  SciAgent's todo graph and sub‑agent system make it easy to orchestrate these workflows.

### Example: Multi-service pipelines

| Workflow | Services chained | Output |
|----------|------------------|--------|
| Drug candidate screening | rdkit → gromacs → scipy-base | Ranked molecules by binding affinity |
| Metasurface optimization | web (literature) → scipy-base (BO) → rcwa → file_ops | Novel unit-cell design |
| Digital chip PPA analysis | openroad → scipy-base (analysis) | Power/performance/area report |
| Protein structure pipeline | blast → biopython → gromacs | MD trajectory from sequence |
| Photonic device design | meep → scipy-base (optimization) → gmsh | Optimized waveguide geometry |

### Workflow stages

1. **Research** – instruct the `researcher` sub‑agent to gather background information and summarise requirements.
2. **Implementation** – generate code or mathematical models based on the research.
3. **Simulation** – run code inside a service (e.g. SciPy or RCWA) to verify the implementation.
4. **Testing** – delegate test generation to the `test_writer` sub‑agent.
5. **Review** – have the `reviewer` sub‑agent critique the code for style, potential errors and performance issues.
6. **Iteration** – integrate feedback, refine the solution and repeat until satisfied.

Because each sub‑agent operates in isolation with a customised tool set and system prompt, tasks remain focused and easier to manage.  The main agent orchestrates the flow of information and ensures that dependencies are respected.

## Domain‑specific extensions

If your project involves specialised domains—such as machine learning, bioinformatics or control theory—you can extend SciAgent with custom tools and services.  For example, create a tool that interfaces with a remote API (e.g. protein structure prediction) or add a new container image to the service registry for your favourite simulation engine.  Once registered, these extensions become first‑class citizens in the agent's reasoning, enabling bespoke workflows tailored to your needs.

## Case study: AR Waveguide Metasurface Design

This case study demonstrates how SciAgent supports a complete photonics research workflow, from simulation to experimental validation. The work was published in *Optical Materials Express* (Vol. 15, No. 12, Dec 2025).

### The Problem

Augmented reality (AR) waveguide displays suffer from low efficiency due to multiple interactions of incoming light with the in-coupler. This fundamental limitation affects system brightness and causes field-dependent losses. The challenge: design a multi-zone metasurface in-coupler that approaches theoretical efficiency limits.

### SciAgent Workflow

**Phase 1: Discovery & Research**

The `sci-compute` skill guided the research process:
- Searched official RCWA documentation for rigorous coupled-wave analysis methods
- Retrieved tutorials on metasurface design and TiO2 meta-atom optimization
- Found relevant papers on waveguide combiner architectures

**Phase 2: Simulation**

Using the `rcwa` service for electromagnetic simulation:
```bash
sciagent "Design a three-zone metasurface in-coupler for AR waveguide with 453nm grating period, optimizing diffraction and reflection efficiencies"
```

SciAgent executed RCWA simulations to:
- Optimize nano-beam widths and nano-pillar geometries for each zone
- Calculate first-order diffraction and zeroth-order reflection efficiencies
- Iterate on designs using a custom feedback loop with realistic efficiency targets

**Phase 3: System Evaluation**

Ray-tracing simulations evaluated the complete AR system:
- Modeled display engine, in-coupler, expander, and out-coupler
- Assessed coupling efficiency across the horizontal field of view
- Achieved simulated MFE (Minimum Field Efficiency) of 25.3%

### Results

The three-zone metasurface design achieved:

| Metric | Simulated | Measured |
|--------|-----------|----------|
| Average coupling efficiency | 31% | 30% |
| MFE at -10° field | 25.3% | 17% |
| Theoretical limit | 29% | - |

The close agreement between simulation and measurement validated the multi-zone optimization strategy. Minor discrepancies at edge fields were attributed to fabrication tolerances and angular sensitivity.

### Key Takeaways

1. **Research-first approach** - The `sci-compute` skill ensured correct API usage by researching documentation before generating simulation code
2. **Iterative optimization** - SciAgent's feedback loop incorporated realistic efficiency values, accounting for material loss and non-ideal sums
3. **Multi-service pipelines** - Combined RCWA simulations with ray-tracing evaluation for end-to-end validation
4. **Reproducibility** - Docker-based service isolation ensured consistent results across different machines

This case study demonstrates SciAgent's ability to support cutting-edge photonics research from initial design through experimental validation.
