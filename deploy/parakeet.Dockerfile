FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libsndfile1 && rm -rf /var/lib/apt/lists/*
# Parakeet v3 is validated on NeMo 2.7.x; pin first, fall back to >=2.0 if 2.7.3
# is not on PyPI.
RUN pip install --no-cache-dir 'nemo_toolkit[asr]==2.7.3' fastapi 'uvicorn[standard]' python-multipart huggingface_hub || pip install --no-cache-dir 'nemo_toolkit[asr]>=2.0' fastapi 'uvicorn[standard]' python-multipart huggingface_hub
COPY deploy/parakeet_server.py /app/parakeet_server.py
WORKDIR /app
ENV HF_HOME=/models HF_DATASETS_CACHE=/models/ds
VOLUME /models
CMD ["python", "parakeet_server.py"]
