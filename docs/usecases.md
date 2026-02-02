---
layout: default
title: Use Cases
nav_order: 4
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

SciAgent integrates with containerised simulation environments via the `service` tool.  Use it for:

* **Numerical and symbolic computation** – run scripts in services like SciPy and SymPy to solve differential equations, perform optimisations or manipulate matrices.
* **Molecular modelling** – leverage the RDKit service to calculate molecular descriptors, generate fingerprints or perform similarity searches.
* **Optimisation and convex programming** – model and solve convex programs with CVXPY, then visualise the results.
* **Electromagnetic and fluid simulations** – use RCWA for photonic structures, MEEP for time‑domain electromagnetic simulations and OpenFOAM for computational fluid dynamics.  The agent can prepare input files, run the simulations and analyse the output.
* **Circuit analysis** – run NGSPICE simulations from Python code and interpret voltage and current results.

## Complex workflows

Many tasks require multiple stages that depend on each other.  SciAgent’s todo graph and sub‑agent system make it easy to orchestrate these workflows.  For example:

1. **Research** – instruct the `researcher` sub‑agent to gather background information and summarise requirements.
2. **Implementation** – generate code or mathematical models based on the research.
3. **Simulation** – run code inside a service (e.g. SciPy or RCWA) to verify the implementation.
4. **Testing** – delegate test generation to the `test_writer` sub‑agent.
5. **Review** – have the `reviewer` sub‑agent critique the code for style, potential errors and performance issues.
6. **Iteration** – integrate feedback, refine the solution and repeat until satisfied.

Because each sub‑agent operates in isolation with a customised tool set and system prompt, tasks remain focused and easier to manage.  The main agent orchestrates the flow of information and ensures that dependencies are respected.

## Domain‑specific extensions

If your project involves specialised domains—such as machine learning, bioinformatics or control theory—you can extend SciAgent with custom tools and services.  For example, create a tool that interfaces with a remote API (e.g. protein structure prediction) or add a new container image to the service registry for your favourite simulation engine.  Once registered, these extensions become first‑class citizens in the agent’s reasoning, enabling bespoke workflows tailored to your needs.
