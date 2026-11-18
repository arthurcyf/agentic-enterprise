# One image for the services outside the sandbox: erp, broker, policy, auditlog, egress.
#
# How this file fits in
# ---------------------
# Built by:  `python -m audit up` (audit/cli.py) as audit-services:latest.
# Used by:   docker-compose.yml. Every service runs this image with a different command
#            (audit.erp.app, audit.broker.server, audit.policy.service, audit.auditlog.service,
#            audit.egress.proxy). No Agent SDK here: the agents only run in the sandbox image.
FROM python:3.11-slim
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.29" "httpx>=0.27" "pydantic>=2.6" "pyyaml>=6" "mcp>=2.3,<3"
COPY audit /app/audit
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
WORKDIR /app
