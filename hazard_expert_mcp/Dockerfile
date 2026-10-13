FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 mcp
WORKDIR /mnt/source/hazard_expert_mcp
# Source is mounted at runtime; the image contains only the Python environment.

USER mcp
CMD ["python", "-m", "hazard_expert_mcp.app"]
