# 通过 GitHub 部署到云（不把你自己的 API 写进仓库）

目标：代码在 **GitHub** 上，云主机 / PaaS 从仓库拉取或连接构建；**不在仓库和平台环境变量里放你的个人 OpenAI Key**，由**每位访问者**在网页里填自己的 Key（仅存浏览器，经你的服务器转发请求）。

## 1. 准备仓库

1. 在本地确认 **没有** 把密钥写进要提交的文件（尤其不要提交 `test.sh` 里带真 Key 的版本；可改用本机私有分支或只保留 `export` 模板）。
2. 使用仓库内 **`.gitignore`**，勿提交 `.env`、大日志、生成的 `*_解析报告.pdf` 等。
3. 推送到 GitHub：若仓库含 `hermes-agent-main` 等巨大目录且与当前 Web 无关，可另开薄仓库只放本应用所需文件，以加快拉取与构建。

## 2. 在云平台接 GitHub

常见方式（任选一类，**先不改业务代码**）：

| 方式 | 做法概要 |
|------|----------|
| **Render / Railway / Fly.io** | 新建 Web Service → Connect GitHub 选仓库 → 构建命令与启动命令见下；环境变量在控制台配置。 |
| **自有 VPS** | `git clone` → Docker 或 `pip install -r requirements.txt` → `uvicorn web.server:app --host 0.0.0.0 --port $PORT`。 |

**Docker**：仓库根目录已有 `Dockerfile`，多数平台选 Dockerfile 构建即可。

**启动命令**（非 Docker 时示例）：

```bash
uvicorn web.server:app --host 0.0.0.0 --port ${PORT:-8000}
```

## 3. 环境变量怎么配（对外服务且不暴露你的 Key）

在云平台 **只配置**（示例）：

| 变量 | 建议 |
|------|------|
| **`SCRIPT_AGENT_MODE`** | `real` |
| **`OPENAI_API_KEY`** | **留空**（让访客自备 Key） |
| **`PORT`** | 由平台注入时可不手写 |

**不要**把个人 `OPENAI_API_KEY` 配进公共服务；访客在页面折叠栏 **「你的模型 API」** 填写后，请求头会带上 **`X-OpenAI-API-Key`** 等，服务端按会话使用。

可选：**`OPENAI_BASE_URL`** / **`OPENAI_MODEL`** 仍可由服务端统一默认；若也让用户自定，可不配，由页面填写。

## 4. HTTPS 与信任说明

- 公网必须 **HTTPS**，否则密钥易被窃听。
- 用户的 Key 会到达你的服务器并由服务端发起模型请求，**运营方技术上有能力误用或记录**；仅适合「你或可信小圈」使用，或明确告知用户风险。

## 5. 本站访问控制（可选）

若需防止陌生人扫你的算力：在平台设置 **`SCRIPT_AGENT_API_KEY`**，并在网页顶部 **「本站密钥」** 填写同一值后保存，所有 `/api/*`（含报告下载）会带 `Authorization: Bearer ...`。

## 6. 限制（与 `docs/DEPLOY.md` 一致）

- 会话在**进程内存**；多实例无粘性会断 session，公共服务建议单实例或后续再改架构。

## 7. 相关代码位置

- 请求头解析与同步：`web/server.py` 中 `openai_headers`、`_sync_agent_openai_headers`。
- 前端发送头与报告下载（带 Bearer）：`static/index.html` 中 `mergeAuthHeaders` / `buildHeaders` / `downloadReportFile`。
