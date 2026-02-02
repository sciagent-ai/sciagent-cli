# sciml-julia: Julia SciML ecosystem for scientific machine learning
# Includes: DifferentialEquations, Optimization, ModelingToolkit, and more
FROM julia:1.11-bookworm

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="Julia SciML ecosystem for differential equations, optimization, and scientific machine learning"
LABEL org.opencontainers.image.licenses="MIT"

ENV JULIA_DEPOT_PATH=/opt/julia
ENV JULIA_NUM_THREADS=auto

# Create package depot directory
RUN mkdir -p /opt/julia

# Install core SciML packages in separate layers to avoid timeout
# First: DifferentialEquations (the largest dependency)
RUN julia -e 'using Pkg; Pkg.add("DifferentialEquations"); using DifferentialEquations; println("DifferentialEquations installed")'

# Second: ModelingToolkit and Catalyst
RUN julia -e 'using Pkg; Pkg.add(["ModelingToolkit", "Catalyst"]); using ModelingToolkit; using Catalyst; println("ModelingToolkit and Catalyst installed")'

# Third: Optimization and NonlinearSolve
RUN julia -e 'using Pkg; Pkg.add(["Optimization", "NonlinearSolve"]); using Optimization; using NonlinearSolve; println("Optimization and NonlinearSolve installed")'

# Fourth: Visualization and data handling
RUN julia -e 'using Pkg; Pkg.add(["Plots", "CSV", "DataFrames", "StaticArrays"]); using Plots; using CSV; using DataFrames; println("Plots, CSV, DataFrames installed")'

# Create workspace
WORKDIR /workspace

# Verify installation
RUN julia -e 'using DifferentialEquations; f(u, p, t) = -u; prob = ODEProblem(f, 1.0, (0.0, 1.0)); sol = solve(prob); println("sciml-julia ready: ODE solved, final value = ", sol[end])'

CMD ["julia"]
