#!/usr/bin/env bash
# 本地启动 Agent 网页（FastAPI + Uvicorn）
# 密钥：复制 .env.example 为 .env 并填写；勿提交 .env
set -euo pipefail
cd "$(dirname "$0")"

if [[ -d .venv ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
else
  echo "提示: 未找到 .env，可复制 .env.example 为 .env。将使用当前 shell 已有环境变量。" >&2
fi

exec uvicorn web.server:app --host 0.0.0.0 --port "${PORT:-8000}"
