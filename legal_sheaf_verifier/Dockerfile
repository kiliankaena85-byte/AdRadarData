FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ADRADAR_DATA_DIR=/data \
    HF_HOME=/data/models \
    SENTENCE_TRANSFORMERS_HOME=/data/models

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency files and install
COPY pyproject.toml .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# Copy project files
COPY src/ src/
COPY artifacts/ artifacts/
COPY config/ config/
COPY legal_kb_seed/ legal_kb_seed/

# Re-install package in editable/local mode for scripts entrypoint
RUN pip install --no-cache-dir -e .

RUN mkdir -p /data/models /data/db /data/reports

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD adradar doctor || exit 1

ENTRYPOINT ["adradar"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]
