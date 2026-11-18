# One image for the services outside the sandbox: erp, broker, policy, auditlog, egress.
FROM python:3.11-slim
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.29" "httpx>=0.27" "pydantic>=2.6" "pyyaml>=6" "mcp>=2.3,<3"
COPY audit /app/audit
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
WORKDIR /app
