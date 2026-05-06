# 剧本解析 Agent：云上部署说明

当前仓库在 **`web/server.py`** 提供 **FastAPI + Uvicorn** HTTP 服务，与 **`script_agent/agent.py`** 共用同一套解析逻辑（analyze / ask / trace / skill evolve）。

**从 GitHub 部署且不让访客用你的 Key**：服务端不要配置 `OPENAI_API_KEY`（由用户在网页填写自备 Key）；不要把密钥写进仓库或镜像。

## 1. 本地试跑 Web

```bash
cd /path/to/剧本
pip install -r requirements.txt
export SCRIPT_AGENT_MODE=real
export OPENAI_API_KEY=sk-...
# 若主要使用内置网页：请勿设置 SCRIPT_AGENT_API_KEY（静态页不会带 Bearer）。
# 仅调用 REST API 且需鉴权时再设置，并在请求头 Authorization: Bearer …
uvicorn web.server:app --host 0.0.0.0 --port 8000
```

浏览器打开 `http://127.0.0.1:8000/` 使用**交互式网页**（左侧实时进度 + 右侧拖拽上传 `.txt`，解析完成后进入追问对话）；接口文档见 `http://127.0.0.1:8000/docs`。流式进度接口：`POST /api/analyze/stream`（NDJSON）。关闭页面时可设置 `SCRIPT_AGENT_WEB_UI=off`。

## 2. 必配环境变量（真实调用模型）

| 变量 | 说明 |
|------|------|
| **`SCRIPT_AGENT_MODE=real`** | 云上不要用 mock |
| **`OPENAI_API_KEY`** | 必填（或你对接的兼容网关密钥） |
| **`OPENAI_BASE_URL`** | 可选：自建网关 / Azure OpenAI 等 |
| **`SCRIPT_AGENT_API_KEY`** | 可选：设置后 `/api/*` 需 `Authorization: Bearer <值>`。**内置网页不会发送该头**，请unset或用网关鉴权 |

其它变量与 **`docs/ENVIRONMENT.md`** 一致（日志路径、Skill 目录、报告导出等）。

## 3. 会话与扩容限制

- **`session_id`** 存在服务**进程内存**中；多副本（Kubernetes 多 Pod、Serverless 多实例）时，同一用户若不 sticky 到同一实例，会 **404 session**。
- 对外正式产品建议后续改为：**Redis / 数据库** 存会话，或 **无状态 API**（每次请求带上完整 `analysis` JSON，不再依赖服务端记忆）。

## 4. 产物路径（图谱 / 报告）

`analyze` 可能按环境变量向**进程当前工作目录**写 HTML / PDF。容器内建议：

- 将 **`SCRIPT_AGENT_GRAPH_HTML`**、报告相关开关指向 **`/tmp`** 或挂载卷；或
- 关闭自动报告（见 `docs/ENVIRONMENT.md` 中 `SCRIPT_AGENT_REPORT_AFTER_ANALYZE`）。

API 响应里的 **`graph_export_path`** 在云上多为容器内路径，前端若需展示图谱，需后续扩展为 **返回文件 URL 或正文**。

## 5. Docker 构建与运行

```bash
docker build -t script-agent .
docker run --rm -p 8000:8000 \
  -e SCRIPT_AGENT_MODE=real \
  -e OPENAI_API_KEY=sk-... \
  -e SCRIPT_AGENT_API_KEY=你的密钥 \
  script-agent
```

## 6. 常见托管平台（思路）

- **Railway / Render / Fly.io**：连接 Git 仓库或推送镜像，设置上述环境变量，绑定域名与 HTTPS。
- **Google Cloud Run / AWS App Runner / Azure Container Apps**：托管容器，按并发自动扩缩；注意会话粘性或多副本会话方案。
- **国内云**：同类「容器服务 / 函数计算 + 自定义镜像」流程一致，需备案与域名策略按厂商要求。

生产环境请补充：**HTTPS、鉴权、限流、审计日志、密钥托管（不要写进镜像）**。
