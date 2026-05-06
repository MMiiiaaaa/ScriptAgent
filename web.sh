#!/usr/bin/env bash
# 与 test.sh 中相同的 export 环境变量，然后启动网页（不启动 CLI）。
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
eval "$(grep '^export ' test.sh)"
exec uvicorn web.server:app --host 0.0.0.0 --port "${PORT:-8000}"



# http://127.0.0.1:8000