FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libsndfile1 && rm -rf /var/lib/apt/lists/*
# faster-whisper (ctranslate2 runtime) - drop-in STT replacement for the Parakeet service.
# ctranslate2 + nvidia-cu12 pull in the CUDA runtime deps. faster-whisper pulls av/onnxruntime/tokenizers.
RUN pip install --no-cache-dir 'faster-whisper>=1.0,<2' 'ctranslate2>=4.5' \
    'nvidia-cudnn-cu12>=9' 'nvidia-cublas-cu12>=12' \
    fastapi 'uvicorn[standard]' python-multipart
COPY deploy/whisper_server.py /app/whisper_server.py
WORKDIR /app
ENV HF_HOME=/models HF_DATASETS_CACHE=/models/ds \
    CUDA_MODULE_LOADING=LAZY \
    LD_LIBRARY_PATH=/usr/local/lib/python3.11/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.11/site-packages/nvidia/cuda_nvrtc/lib:/usr/local/lib/python3.11/site-packages/nvidia/cudnn/lib
VOLUME /models
CMD ["python", "whisper_server.py"]
