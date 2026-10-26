# MCP server ("brain") image: for Render's Docker runtime or the future always-on machine.
# Build from the repo root:   docker build -f deploy/mcp/Dockerfile -t news-brain-mcp .
# Run:                        docker run --env-file .env -p 8000:8000 news-brain-mcp
# Everything is configured by env (see .env.example); nothing assumes Render or a tunnel.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /app

COPY brain/requirements.txt brain/requirements.txt
RUN pip install --no-cache-dir -r brain/requirements.txt

COPY brain brain
COPY docs/DATA_CONTRACT.md docs/DATA_CONTRACT.md

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health')"
CMD ["python", "-m", "brain.mcp_server", "--host", "0.0.0.0"]
