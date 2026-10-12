FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg for audio; tesseract (eng + spa) and poppler for free document OCR
# (/v1/extract, 2026-10-12). No paid AI reads a document.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tesseract-ocr tesseract-ocr-eng tesseract-ocr-spa poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Model weights are NOT baked in: they download on first boot into MODEL_CACHE_DIR
# (a Railway volume at /models), so the image stays small and redeploys reuse them.
ENV MODEL_CACHE_DIR=/models

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
