# ── Production Multi-Stage Dockerfile for SpiceRoute Kitchen ─────────────────
# Base Image: Python 3.11 Slim Debian
FROM python:3.11-slim as base

# Prevents Python from writing pyc files and enables unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PORT=8501

WORKDIR /app

# Install system dependencies (build-essential, curl for healthchecks)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Python requirements
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . /app

# Create a non-root application user for container security
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app
USER appuser

# Expose Streamlit (8501) and FastAPI (8000) ports
EXPOSE 8501 8000

# Healthcheck targeting Streamlit or FastAPI
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:${PORT}/_stcore/health || curl -f http://localhost:8000/health || exit 1

# Default command: launch Streamlit UI
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]
