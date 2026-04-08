# SciAgent Market Analysis

## What SciAgent Does (Summary)

SciAgent is an **AI agent framework** that bridges software engineering and scientific computing. It provides:
- 27 containerized scientific services across 10 domains (CFD, molecular dynamics, quantum, photonics, etc.)
- AI-powered natural language orchestration of complex workflows
- Multi-model support (Claude, GPT, Gemini, etc.)
- Cloud compute auto-routing (local Docker → SkyPilot for large jobs)
- Built-in verification to prevent scientific hallucination

---

## TAM, SAM, SOM Analysis

### TAM (Total Addressable Market): **$70-80B**

| Segment | Size (2025) | Source |
|---------|-------------|--------|
| HPC Market | $57-59B | [Grand View Research](https://www.grandviewresearch.com/industry-analysis/high-performance-computing-market) |
| AI Coding Tools | $7-8B | [Mordor Intelligence](https://www.mordorintelligence.com/industry-reports/artificial-intelligence-code-tools-market) |
| AI Assistant Software | $8.5B | [Grand View Research](https://www.grandviewresearch.com/industry-analysis/ai-assistant-software-market-report) |

The total addressable market spans everyone doing scientific computing, research simulations, or software development who could benefit from AI automation.

---

### SAM (Serviceable Addressable Market): **$8-12B**

This is the realistic market SciAgent can actually serve:

| Segment | Size | Rationale |
|---------|------|-----------|
| Cloud HPC (research workflows) | $5-7B | Subset of $35B cloud HPC focused on research/academic |
| AI-assisted scientific tools | $2-3B | Growing "self-driving lab" segment |
| Developer tools for research orgs | $1-2B | R&D teams needing both coding + simulation |

**Key constraints:**
- Requires Docker (excludes some enterprise environments)
- English-language primarily
- API costs for LLM usage
- Competes with established vendors (ANSYS, SimScale)

---

### SOM (Serviceable Obtainable Market): **$50-200M** (3-5 year horizon)

Realistic capture based on competitive dynamics:

| Scenario | Market Capture | Revenue |
|----------|----------------|---------|
| Conservative | 0.5% of SAM | $50-60M |
| Moderate | 1-2% of SAM | $100-150M |
| Aggressive | 2-3% of SAM | $200M+ |

**Why realistic:**
- GitHub Copilot has ~$800M ARR with 42% share
- Cursor hit $500M ARR at 18% share
- Research-focused tools (Elicit) have smaller but growing user bases
- Market is fragmented in scientific computing

---

## Competitive Landscape

### Direct Competitors

| Category | Competitors | SciAgent Advantage |
|----------|-------------|-------------------|
| **AI Coding** | GitHub Copilot, Cursor, Amazon Q | SciAgent adds scientific simulation |
| **CFD/FEM** | ANSYS, Siemens Simcenter, SimScale | AI orchestration, open-source, multi-domain |
| **Molecular Dynamics** | GROMACS, LAMMPS, NAMD | Unified interface, no MD expertise needed |
| **AI Research** | Elicit, Scispot | Code generation + execution, not just literature |

### Competitive Moat

1. **Cross-domain**: 10 scientific domains in one tool (competitors are siloed)
2. **AI-first**: Natural language → simulation (competitors require expertise)
3. **Open source services**: Lower barrier vs. $50K+ ANSYS licenses
4. **Verification built-in**: Addresses scientific hallucination problem

---

## ICP (Ideal Customer Profile)

### Primary ICPs

#### 1. Academic Research Labs (Tier 1)
- **Who**: PhD students, postdocs, PIs in computational sciences
- **Size**: 5-50 person labs at R1 universities
- **Pain**: Limited HPC access, steep learning curves, time pressure
- **Budget**: $5K-50K/year (often grant-funded)
- **Why they buy**: Reproduce papers faster, run simulations without expertise
- **Domains**: Chemistry, materials science, bioinformatics, physics

#### 2. Biotech/Pharma R&D Teams (Tier 1)
- **Who**: Computational biology groups, drug discovery teams
- **Size**: 10-100 person groups at biotech startups to large pharma
- **Pain**: MD simulations take weeks, need specialized staff
- **Budget**: $50K-500K/year
- **Why they buy**: Accelerate drug discovery pipelines (79× efficiency gains possible)
- **Domains**: GROMACS, LAMMPS, RDKit, BioPython

#### 3. Engineering Simulation Teams (Tier 2)
- **Who**: Mechanical/aerospace engineers doing CFD, FEM
- **Size**: 5-20 person teams at hardware companies
- **Pain**: ANSYS licenses expensive ($25K-100K/seat), long setup times
- **Budget**: $20K-200K/year
- **Why they buy**: Open-source alternative with AI assistance
- **Domains**: OpenFOAM, Gmsh, Elmer

#### 4. Hardware Startups (Tier 2)
- **Who**: Photonics, chip design, quantum computing startups
- **Size**: 3-30 engineers
- **Pain**: Can't afford full EDA suites, need rapid prototyping
- **Budget**: $10K-100K/year
- **Why they buy**: RCWA, ngspice, OpenROAD access with AI guidance
- **Domains**: Photonics, EDA, quantum

#### 5. National Labs & Government Research (Tier 3)
- **Who**: DOE labs, NIST, defense contractors
- **Size**: Large, but long sales cycles
- **Pain**: Legacy workflows, reproducibility challenges
- **Budget**: $100K-1M+ (but slow procurement)
- **Why they buy**: Reproducibility, automated pipelines

---

## ICP Qualification Criteria

| Criteria | Must Have | Nice to Have |
|----------|-----------|--------------|
| **Technical** | Docker capability, Python environment | Cloud access (AWS/GCP) |
| **Use case** | Running simulations OR coding workflows | Both combined |
| **Pain level** | >10 hours/week on simulation setup | Reproducibility concerns |
| **Budget authority** | $5K+ discretionary spend | Grant funding secured |
| **Team size** | 2+ computational researchers | Dedicated DevOps |

---

## Go-to-Market Recommendations

### Beachhead Market

**Academic computational chemistry/materials science labs** at top 100 research universities

**Why:**
- High pain (complex simulations, limited resources)
- Word-of-mouth culture (papers, conferences)
- Budget available (grants)
- Open to open-source tools
- Domain covered well (GROMACS, LAMMPS, RDKit, ASE)

### Suggested Pricing Model

| Tier | Price | Target |
|------|-------|--------|
| Free | $0 | Individual researchers, students |
| Pro | $50-100/mo | Small labs, startups |
| Team | $500-1000/mo | Research groups (5-20 users) |
| Enterprise | Custom | Pharma, national labs |

---

## Key Risks

1. **LLM cost**: Heavy API usage could erode margins
2. **Incumbents**: ANSYS/Siemens could add AI features
3. **GitHub**: Copilot could expand to scientific domains
4. **Accuracy**: Scientific hallucination is reputational risk
5. **Support burden**: 27 services require maintenance

---

## Sources

- [Grand View Research - HPC Market](https://www.grandviewresearch.com/industry-analysis/high-performance-computing-market)
- [Mordor Intelligence - Cloud HPC](https://www.mordorintelligence.com/industry-reports/cloud-high-performance-computing-hpc-market)
- [Markets and Markets - Simulation Software](https://www.marketsandmarkets.com/Market-Reports/simulation-software-market-263646018.html)
- [CB Insights - Coding AI Market Share](https://www.cbinsights.com/research/report/coding-ai-market-share-2025/)
- [Second Talent - AI Coding Statistics](https://www.secondtalent.com/resources/ai-coding-assistant-statistics/)
- [Frontiers - AI & Lab Automation](https://www.frontiersin.org/journals/artificial-intelligence/articles/10.3389/frai.2025.1649155/full)
- [Berkeley Lab - AI and Automation](https://newscenter.lbl.gov/2025/09/04/how-berkeley-lab-is-using-ai-and-automation-to-speed-up-science-and-discovery/)
- [Scispot - Self-Driving Labs](https://www.scispot.com/blog/ai-powered-self-driving-labs-accelerating-life-science-r-d)

---

*Generated: March 2026*
