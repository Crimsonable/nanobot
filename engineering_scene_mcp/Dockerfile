FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml /app/pyproject.toml

RUN python -m pip install --upgrade pip && \
    python - <<'PY'
import subprocess
import tomllib
from pathlib import Path

data = tomllib.loads(Path("/app/pyproject.toml").read_text(encoding="utf-8"))
deps = data["project"]["dependencies"]
subprocess.run(["pip", "install", *deps], check=True)
PY

WORKDIR /mnt/source/engineering_scene_mcp

EXPOSE 8081

CMD ["python", "-m", "engineering_scene_mcp.app"]
