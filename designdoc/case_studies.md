# Design Doc: Case Studies for SciAgent

## Objective

Create verified case studies by reproducing results from 2025 publications using SciAgent's services, then suggesting novel future directions.

## Why This Matters

Current docs describe capabilities abstractly. Case studies prove them:
- **Verifiable**: readers can check source papers
- **Credible**: reproduction > hypotheticals
- **Creative**: novel directions show SciAgent thinks, not just executes

## Selected Domains (3)

### 1. Bioinformatics
**Services**: biopython, blast, gromacs
**Paper sources**: Nature Methods, Bioinformatics, PLOS Computational Biology
**Why**: High reproducibility, multi-service chaining, broad audience

**Target paper criteria**:
- Sequence analysis or protein structure work
- Computational results that can be verified
- Methods section with clear parameters

### 2. EDA / Digital Design
**Services**: openroad
**Paper sources**: ICCAD, DAC, IEEE TCAD
**Why**: Unique differentiator, underserved by other tools, growing open-source EDA ecosystem

**Target paper criteria**:
- RTL-to-GDS flow results
- Uses open PDKs (sky130, asap7, nangate45)
- Reports PPA metrics (power, performance, area)

### 3. Photonics / Nanophotonics
**Services**: rcwa, meep
**Paper sources**: Optica, ACS Photonics, Nanophotonics
**Why**: Strong simulation verification, optimization-friendly, visual results

**Target paper criteria**:
- Metasurface or photonic crystal design
- RCWA or FDTD simulation results
- Efficiency/phase/transmission data to verify

## Case Study Template

Each case study should include:

```markdown
## [Paper Title] - [Domain]

**Paper**: [Full citation with DOI]
**Services used**: [list]

### Original Results
- Key findings from the paper
- Specific figures/tables we're reproducing

### Reproduction with SciAgent
- The prompt we used
- What happened step by step
- Our results vs paper results

### Novel Directions
- What SciAgent suggested as extensions
- Why these are scientifically interesting
- Potential for new discoveries

### Key Patterns
- What made this prompt effective
- Service chaining demonstrated
```

## Execution Plan

1. **Paper search**: Find 1 suitable 2025 paper per domain
2. **Feasibility check**: Verify we have the data/parameters to reproduce
3. **Reproduction**: Run through SciAgent, capture the workflow
4. **Validation**: Compare our results to published results
5. **Novel directions**: Ask SciAgent for extensions, evaluate scientific merit
6. **Documentation**: Write up in case_studies.md

## Success Criteria

- [ ] 3 papers identified (one per domain)
- [ ] Results reproduced within 10% of published values
- [ ] At least 1 novel direction per case study that is scientifically plausible
- [ ] case_studies.md written and linked from main docs

## Status

- [ ] Bioinformatics paper: Not started
- [ ] EDA paper: Not started
- [ ] Photonics paper: Not started
