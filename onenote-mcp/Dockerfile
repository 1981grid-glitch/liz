# Slim, non-root, no build toolchain in the final image.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so the layer caches across source edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY onenote_mcp_server.py .

# Non-root. Nothing here writes to disk: unlike Throne, this server keeps no session
# store on a mounted volume (OAuthProxy's default in-process store is enough because
# there is no long-lived app credential to protect across restarts -- a restart just
# means the connector re-runs its sign-in).
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000
CMD ["python", "onenote_mcp_server.py"]
