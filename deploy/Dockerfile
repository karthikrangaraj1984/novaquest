# NovaQuest app image (web and worker share it). Not built in the test workspace (no Docker engine there).
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr tesseract-ocr-eng && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY novaquest ./novaquest
COPY tools ./tools
COPY data ./data
RUN useradd --create-home nq && chown -R nq /app
USER nq
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "novaquest.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]
