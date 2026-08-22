FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY throne_mcp_server.py .

# Container App ingress target-port
ENV PORT=8080
EXPOSE 8080

CMD ["python", "throne_mcp_server.py"]
