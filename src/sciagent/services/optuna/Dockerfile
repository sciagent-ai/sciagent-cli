# optuna: Hyperparameter optimization framework
# Extends scipy-base for numerical foundation
FROM ghcr.io/sciagent-ai/scipy-base:latest

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="Optuna hyperparameter optimization framework"
LABEL org.opencontainers.image.licenses="MIT"

# Install Optuna and commonly used integrations
RUN pip install --no-cache-dir \
    optuna>=4.0 \
    optuna-integration \
    scikit-learn>=1.4 \
    && rm -rf ~/.cache/pip

# Verify installation
RUN python -c "import optuna; study = optuna.create_study(); print(f'optuna {optuna.__version__} ready')"

CMD ["python3"]
