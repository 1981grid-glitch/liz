# Matches throne-mcp/Dockerfile's shape and base image so the two servers stay
# one thing to reason about. Two deliberate differences, both noted:
#   * PORT is 8000, not Throne's 8080 (the brief specifies 8000 for this server).
#   * a non-root user, which Throne does not currently set.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so the layer caches across source edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY onenote_mcp_server.py .

# Non-root. Nothing here writes to disk: unlike Throne, this server keeps no
# session store on a mounted volume, because there is no long-lived app
# credential to protect across restarts -- a restart just means the connector
# re-runs its sign-in.
RUN useradd --create-home --uid 10001 appuser
USER appuser

# Container App ingress target-port
ENV PORT=8000
EXPOSE 8000

CMD ["python", "onenote_mcp_server.py"]
