FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 mcp
RUN mkdir -p /app/data && chown mcp:mcp /app/data

USER mcp
CMD ["python", "-m", "hazard_expert_mcp.app"]
