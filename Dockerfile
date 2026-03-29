FROM node:22-slim

# Install Python, pip, and system deps
RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-venv \
    curl git \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Install Python bridge dependencies
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --break-system-packages -r requirements.txt

COPY app.py .

# Workspace for Claude Code to operate in
RUN mkdir -p /workspace

EXPOSE 8765

HEALTHCHECK --interval=15s --timeout=5s --retries=3 \
    CMD curl -sf http://127.0.0.1:8765/healthz || exit 1

CMD ["python3", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8765"]
