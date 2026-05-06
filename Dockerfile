# 剧本解析 Agent — 容器镜像（API + 极简演示页）
FROM python:3.12-slim-bookworm

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    SCRIPT_AGENT_MODE=real

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY script_agent ./script_agent
COPY web ./web
COPY cli ./cli
COPY main.py ./
COPY script_agent_skills ./script_agent_skills
COPY static ./static

EXPOSE 8000

# 云平台常注入 PORT；本地默认 8000
CMD ["sh", "-c", "exec uvicorn web.server:app --host 0.0.0.0 --port ${PORT:-8000}"]
