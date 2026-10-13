FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 mcp
COPY --chown=mcp:mcp hazard_expert_mcp ./hazard_expert_mcp

USER mcp
CMD ["python", "-m", "hazard_expert_mcp.app"]
