# Case Studies Progress Tracker

## Objective
Find 2025 papers where:
- Methods are described in detail
- **Code does NOT exist publicly** (or not yet released)
- Results can be reproduced computationally
- SciAgent creates the implementation from scratch

This demonstrates that SciAgent can create reproducible artifacts even when they don't exist.

---

## Selection Criteria Refined

| Criterion | Requirement |
|-----------|-------------|
| Year | 2025 publication |
| Code | NOT available on GitHub |
| Methods | Clear parameters in paper |
| Results | Quantitative, reproducible |
| Services | Matches our stack (blast, gromacs, rcwa, meep, openroad) |

---

## Domain 1: Bioinformatics
**Services**: biopython, blast, gromacs

### Best Candidate: Antimicrobial Peptide Prediction

**Paper**: "Predicting Antimicrobial Peptide Activity: A Machine Learning-Based QSAR Approach"
**Source**: MDPI Pharmaceutics, July 2025
**URL**: https://www.mdpi.com/1999-4893/17/8/993

**Why this works**:
- Describes BLAST + PSSM methodology for homology search
- Uses CD-HIT at 70% threshold for redundancy removal
- Training set: 9731 sequences (870 AMPs, 8661 non-AMPs)
- Clear methodology but code not in a single repo

**Reproduction approach**:
1. Fetch AMP sequences from dbAMP 3.0 database
2. Run BLAST homology search with specified parameters
3. Use CD-HIT for redundancy removal at 70% threshold
4. Generate PSSM profiles
5. Compare sequence clustering results

**Alternative**: Protein-protein interaction prediction using BLAST pairwise alignment

### Backup Option: Multi-Scale MD Simulation Protocols

**Paper**: "Protocols for Multi-Scale Molecular Dynamics Simulations"
**Source**: bioRxiv (methodology paper)

**Parameters available**:
- Force field: Amber ff19sb for proteins
- Water model: OPC (four-point)
- Salt: ~0.15M NaCl
- CG models: SIRAH (Amber), Martini 3 (Gromacs)

---

## Domain 2: EDA / Digital Design
**Services**: openroad

### Best Candidate: ASIC Neural Network Accelerator

**Paper**: arXiv:2505.11252v1 (May 2025)
**Topic**: Neural network accelerator design using OpenROAD + ASAP7 PDK

**Why this works**:
- Uses open-source flow (OpenROAD + Yosys + KLayout)
- ASAP7 PDK (7nm predictive)
- Reports accuracy and resource metrics
- Design-specific, not a general benchmark

**Reproduction approach**:
1. Implement RTL for similar neural network accelerator
2. Run through OpenROAD synthesis flow
3. Extract PPA metrics (power, performance, area)
4. Compare with paper results

### Alternative: Custom Digital Design

Since most benchmark papers provide code, consider:
- Taking a published algorithm and implementing RTL from scratch
- Running through OpenROAD with sky130/asap7/nangate45
- Comparing PPA with any available baselines

---

## Domain 3: Photonics / Nanophotonics
**Services**: rcwa, meep

### Best Candidate: Nano-3D Metasurface

**Paper**: "Nano-3D: Metasurface-Based Neural Depth Imaging"
**Source**: arXiv:2503.15770v1 (March 2025)
**Authors**: NYU + Columbia

**Why this works**:
- Code will be "open-sourced upon acceptance" (NOT YET AVAILABLE)
- Clear RCWA simulation parameters:
  - Material: TiO2 (titanium dioxide)
  - Structure: Cross-shaped pillars
  - Height: 700 nm
  - Pitch: 400 nm (subwavelength)
  - Wavelength: 590 nm
  - Substrate: 500 μm fused silica
- Result to reproduce: Near-unity transmission, 2π phase coverage

**Reproduction approach**:
1. Set up RCWA simulation with S4
2. Model TiO2 cross-shaped pillars (sweep geometry)
3. Calculate transmission and phase response
4. Verify 2π phase coverage with high efficiency

### Alternative: Multi-Zone Metasurface In-Coupler

**Paper**: "Design and Experimental Validation of a High-Efficiency Multi-Zone Metasurface Waveguide In-Coupler"
**Source**: Opt. Mater. Express 15, 3129-3140 (2025)
**Authors**: Xiong et al.

**Experimental results to validate**:
- Measured efficiency: 30%
- Simulated efficiency: 31%
- Edge of field: 17% measured vs 25.3% simulated

---

## Recording-Friendly Run Plan

### Pre-Run Preparation
1. **Paper summary card** - 1-pager with citation, key params, expected results
2. **Prompt script** - Pre-written, tested prompt for SciAgent
3. **Terminal setup** - Clean terminal, proper font size, dark theme

### During Recording
1. Show paper card briefly
2. Display the prompt
3. Run SciAgent (subagents enabled)
4. Let it execute fully
5. Show results comparison at end

### Post-Recording
1. Extract trajectory from logs
2. Document in case_studies.md
3. Add novel directions suggested by agent

---

## Prompt Templates (Draft)

### Bioinformatics Prompt
```
Reproduce the antimicrobial peptide homology analysis from the 2025 MDPI paper:
1. Fetch a sample of 100 AMP sequences from the APD database
2. Use BLAST to perform pairwise alignment and calculate sequence similarity
3. Apply CD-HIT at 70% identity threshold to remove redundant sequences
4. Report the number of unique clusters and representative sequences
5. Suggest a novel analysis direction based on the results
```

### EDA Prompt
```
Design and synthesize a simple 8-bit counter using the OpenROAD flow:
1. Write Verilog RTL for an 8-bit synchronous counter with enable
2. Synthesize using Yosys targeting the nangate45 library
3. Run placement and routing with OpenROAD
4. Extract PPA metrics (area, timing, power estimate)
5. Suggest optimization strategies for better timing closure
```

### Photonics Prompt
```
Reproduce the TiO2 metasurface unit cell simulation from the Nano-3D paper:
1. Set up an RCWA simulation for TiO2 cross-shaped pillars
2. Parameters: height=700nm, pitch=400nm, wavelength=590nm
3. Sweep the pillar arm widths from 50nm to 350nm
4. Calculate transmission amplitude and phase for each geometry
5. Verify 2π phase coverage and identify high-efficiency designs
6. Suggest a novel metasurface application based on these results
```

---

## Status Summary

| Domain | Paper Found | Code Status | Prompt Ready | Recording Ready |
|--------|-------------|-------------|--------------|-----------------|
| Bioinformatics | ✅ AMP prediction | ❌ No full code | 🔄 Draft | ⏳ Pending |
| EDA | ✅ Neural accelerator | ⚠️ Partial only | 🔄 Draft | ⏳ Pending |
| Photonics | ✅ Nano-3D metasurface | ❌ Not yet released | 🔄 Draft | ⏳ Pending |

---

## Next Steps

1. [ ] Finalize paper selections (confirm no code exists)
2. [ ] Test prompts with SciAgent locally
3. [ ] Refine prompts based on test runs
4. [ ] Set up recording environment
5. [ ] Execute and record each case study
6. [ ] Write up results in case_studies.md

---

## Reference Links

### Bioinformatics
- dbAMP 3.0: https://awi.cuhk.edu.cn/dbAMP/
- APD6: https://aps.unmc.edu/
- CD-HIT: https://sites.google.com/view/cd-hit

### EDA
- OpenROAD: https://github.com/The-OpenROAD-Project/OpenROAD
- ASAP7 PDK: https://github.com/The-OpenROAD-Project/asap7
- NanGate45: included in OpenROAD

### Photonics
- S4 RCWA: https://github.com/victorliu/S4
- MEEP: https://github.com/NanoComp/meep

---

*Last updated: 2025-02-04*
