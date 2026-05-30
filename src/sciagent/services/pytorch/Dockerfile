# pytorch: generic GPU ML base (training + inference)
# CUDA 12.4 + PyTorch 2.4.1 runtime + HuggingFace stack + numpy/scipy/pandas
# Size target: ~5 GB compressed (budget: 6 GB)
# Domain extensions (pytorch-bio, pytorch-cv, pytorch-rl) compose on top via
# an explicit FROM ghcr.io/sciagent-ai/pytorch in their own Dockerfile.
FROM pytorch/pytorch:2.4.1-cuda12.4-cudnn9-runtime

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="PyTorch 2.x + CUDA 12 + transformers/accelerate/peft + numpy/scipy/pandas"
LABEL org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    git wget build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    transformers==4.* \
    accelerate \
    peft \
    numpy \
    scipy \
    pandas \
    matplotlib

WORKDIR /workspace

CMD ["python3"]
