FROM python:3.12-slim

WORKDIR /app
ENV CONTAINER_UP_BASE_URL=http://192.168.199.168:30081 \
    CONTAINER_UP_FRONTEND_ID=web-test
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 mcp
COPY --chown=mcp:mcp hazard_expert_mcp ./hazard_expert_mcp
RUN mkdir -p /app/data && chown mcp:mcp /app/data

USER mcp
CMD ["python", "-m", "hazard_expert_mcp.app"]
