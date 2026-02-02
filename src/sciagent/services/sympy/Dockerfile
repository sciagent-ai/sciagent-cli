# sympy: Symbolic mathematics for Python
# Extends scipy-base for numerical integration
# Size target: ~600MB (adds ~100MB to scipy-base)
FROM ghcr.io/sciagent-ai/scipy-base:latest

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="SymPy symbolic mathematics environment"
LABEL org.opencontainers.image.licenses="MIT"

# Install SymPy and useful extensions
RUN pip install --no-cache-dir \
    sympy>=1.13 \
    mpmath>=1.3 \
    && rm -rf ~/.cache/pip

# Verify installation
RUN python -c "import sympy; from sympy import symbols, solve, diff, integrate; print(f'sympy {sympy.__version__} ready')"

CMD ["python3"]
