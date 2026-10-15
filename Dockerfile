# Backend Dockerfile (Python 3.14 + uv + FFmpeg Subtitle Fonts)
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

# Install fontconfig & DejaVu fonts required by FFmpeg libass subtitle burn-in
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fontconfig \
    fonts-dejavu-core \
    ca-certificates \
    && fc-cache -f -v \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Enable bytecode compilation and copy from uv cache
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PORT=8080

# Install dependencies first (cached layer)
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --no-install-project

# Copy application source code
COPY . .

EXPOSE 8080

# Start FastAPI with WebSocket keep-alive ping settings for Cloud Run
CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--ws-ping-interval", "20", "--ws-ping-timeout", "60"]