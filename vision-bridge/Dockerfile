# Same base and shape as onenote-mcp/Dockerfile so the three servers stay one
# thing to reason about. Differences, both deliberate:
#   * PORT 8080, matching throne-mcp (onenote-mcp uses 8000 per its own brief).
#   * COPYs a directory (client/) as well as the server, because the bridge
#     serves its own web client from GET / -- one URL is the whole install.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so the layer caches across source edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vision_bridge.py .
COPY client/ ./client/

# Non-root, as in onenote-mcp. Nothing here writes to disk: session context is
# in-process and deliberately lost on restart, which is the privacy-preserving
# default for a camera feed -- there is no store to protect across restarts.
RUN useradd --create-home --uid 10001 appuser
USER appuser

ENV PORT=8080
EXPOSE 8080

CMD ["python", "vision_bridge.py"]
