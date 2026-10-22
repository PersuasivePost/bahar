FROM python:3.11-slim

# libgomp1: OpenMP runtime needed by the llama.cpp CPU wheel
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

# Hugging Face Spaces run as uid 1000
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    UV_PROJECT_ENVIRONMENT=/home/user/venv \
    UV_LINK_MODE=copy \
    PATH=/home/user/venv/bin:$PATH \
    HF_HOME=/home/user/hf \
    CACHE_DIR=/home/user/cache
WORKDIR /home/user/app

# Dependencies first (cached layer). llama-cpp-python comes from a prebuilt wheel URL in
# pyproject.toml, so nothing is compiled.
COPY --chown=user pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

# Bake the model into the image so cold starts never download it.
ARG MODEL_REPO=Qwen/Qwen2.5-1.5B-Instruct-GGUF
ARG MODEL_FILE=qwen2.5-1.5b-instruct-q4_k_m.gguf
ENV MODEL_REPO=${MODEL_REPO} MODEL_FILE=${MODEL_FILE}
RUN python -c "from huggingface_hub import hf_hub_download; hf_hub_download('${MODEL_REPO}', '${MODEL_FILE}')"
ENV HF_HUB_OFFLINE=1

COPY --chown=user bahar ./bahar
RUN uv sync --frozen --no-dev

EXPOSE 7860
CMD ["uvicorn", "bahar.api.main:app", "--host", "0.0.0.0", "--port", "7860"]
