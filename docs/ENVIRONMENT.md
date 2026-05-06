# 剧本解析 Agent：环境变量说明

本文档列出当前 **`script_agent/agent.py`**、**`script_agent/skill_library.py`** 中读取的环境变量及其含义。未设置的变量使用代码内默认值（文中标注）。

布尔类变量多数支持：`0`、`false`、`no`、`off`（大小写不敏感）表示关闭；其它取值通常视为开启（具体见各节）。

---

## 一、LLM 接入（`LLMClient`）

| 变量 | 默认 | 含义 |
|------|------|------|
| **`SCRIPT_AGENT_MODE`** | `real` | `mock`：不走真实 API；`real`：调用 `_call_real_model()`（OpenAI 兼容接口）。 |
| **`OPENAI_API_KEY`** | 空 | 真实模式必填：API Key。 |
| **`OPENAI_MODEL`** | `gpt-4o-mini` | 模型名称。 |
| **`OPENAI_BASE_URL`** | 空 | 可选：兼容网关的 **API 根路径**（一般为 `https://域名/v1`）；勿填网站控制台首页，否则会返回 HTML 而非模型 JSON。 |
| **`OPENAI_TIMEOUT_SECONDS`** | `120` | 单次请求的 **读/写** 超时（秒）；建连单独见下项。 |
| **`OPENAI_CONNECT_TIMEOUT_SECONDS`** | `25` | 与模型服务 **建立 TCP/TLS 连接** 的最长等待（秒）；过小会在弱网下易失败，过大则网关宕机时界面会像「一直卡住」。 |
| **`OPENAI_MAX_RETRIES`** | `2` | SDK 对可重试错误的自动重试次数（`0` 表示不重试，失败更快暴露）。 |

**Web 服务（`web.server`）**：

- **公网 / 多用户场景（推荐）**：服务端**不要**设置 `OPENAI_API_KEY`（避免泄露你的 Key）。使用者在网页「你的模型 API」中填写自己的 Key，随请求以请求头传给服务端，经 **`_sync_agent_openai_headers` → `configure_llm_provider`** 写入该 `session_id` 对应会话。请求头与 **`openai_headers` 依赖**一致：  
  **`X-OpenAI-API-Key`**、**`X-OpenAI-Base-URL`**、**`X-OpenAI-Model`**、**`X-Script-Agent-Mode`**（`real` / `mock`）。  
  注意：流量仍经你的服务器转发，**必须上 HTTPS**；用户需信任你方不滥用其 Key。

- **自建单用户**：可继续只配环境变量 `OPENAI_API_KEY` 等，页面可不填 Key。

- **本站鉴权**：**`SCRIPT_AGENT_API_KEY`** 与 **`Authorization: Bearer <值>`**；内置页顶部可填「本站密钥」并随 API 请求发送。

---

## 二、日志与终端输出

| 变量 | 默认 | 含义 |
|------|------|------|
| **`SCRIPT_AGENT_LOG_FILE`** | `agent_operations.log` | 操作日志文件路径；设为 `off` / `none` / `false` / `0` / `-` 关闭文件日志。**Serverless（`VERCEL` 或 `AWS_LAMBDA_FUNCTION_NAME`）**：此处为**相对路径**时自动写到 **`/tmp/` 同名文件**；亦可显式设为 `/tmp/agent.log`。 |
| **`SCRIPT_AGENT_VERBOSE`** | `1` | `0` / `false` / `no`：不在终端打印 `[agent]` 调试行；否则打印。 |
| **`SCRIPT_AGENT_LOG_LLM_RESPONSES`** | `1` | `0` / `false` / `no` / `off`：**不向日志文件写入**各次模型调用的**原始返回正文**（仍会照常调用模型）。仅在 **`SCRIPT_AGENT_LOG_FILE` 未关闭**时生效。 |
| **`SCRIPT_AGENT_LOG_LLM_RESPONSE_MAX_CHARS`** | `200000` | 单次模型返回写入日志的最大字符数；超出截断并标注全文长度。设为 **`0`** 表示不限制（日志可能非常大）。 |

---

## 三、Skill 目录与总开关

| 变量 | 默认 | 含义 |
|------|------|------|
| **`SCRIPT_AGENT_SKILLS_DIR`** | （空） | Skill 根目录；**未设置**时为仓库内 `script_agent_skills`（与 `script_agent/skill_library.py` 的默认解析约定一致）。设置后为绝对或相对路径，按路径解析。 |
| **`SCRIPT_AGENT_SKILLS`** | `1` | `0` / `false` / `no` / `off`：关闭 Skill——不在 system 里拼接索引与全文，选课/自动演化等依赖 Skill 的逻辑也会提前退出。 |
| **`SCRIPT_AGENT_SESSION_SKILLS`** | 空 | 逗号分隔的 skill **`name`**：始终在会话中 **全文注入**（钉住列表），不受自动选课影响。 |

---

## 四、自动选课（analyze / ask / trace 前）

| 变量 | 默认 | 含义 |
|------|------|------|
| **`SCRIPT_AGENT_SKILL_AUTOPICK`** | `1` | `0` / `false` / `no` / `off`：关闭模型自动选课（`_agent_selected_skill_names` 始终为空）。 |
| **`SCRIPT_AGENT_SKILL_AUTOPICK_MAX`** | `4` | 单次最多选几个 skill 注入全文（上限代码内钳制为 12）。设为 `0` 等价于不选额外 skill。 |
| **`SCRIPT_AGENT_SKILL_AUTOPICK_SCRIPT_CHARS`** | `3500` | **仅 analyze**：提供给选课模型的剧本文本「开头摘录」最大字符数（下限 500）。 |

---

## 五、自动演化 Skill（analyze / ask / trace 后）

会话结束时由模型决定 **`noop`**（不改）、**`patch`**（修订已有 **user** skill，优先）或 **`create`**（新建）；每次 **`patch` / `create` 成功写盘** 计一次，达到上限后本会话内不再触发。

| 变量 | 默认 | 含义 |
|------|------|------|
| **`SCRIPT_AGENT_SKILL_AUTO_EVOLVE`** | `1` | `0` / `false` / `no` / `off`：关闭自动演化。 |
| **`SCRIPT_AGENT_SKILL_AUTO_EVOLVE_MAX`** | `3` | 当前进程内 **成功 patch/create** 的次数上限；`0` 表示关闭自动演化。 |
| **`SCRIPT_AGENT_SKILL_AUTO_EVOLVE_CONTEXT_CHARS`** | `14000` | **仅 after_analyze**：塞进演化模型的「完整分析 JSON」最大长度（下限 2000），超出截断。 |
| **`SCRIPT_AGENT_SKILL_AUTO_EVOLVE_PATCH_MD_CHARS`** | `32000` | **仅 CLI `skill evolve`**：注入模型的「可 patch user skill」**SKILL.md 正文**总字符上限。 |
| **`SCRIPT_AGENT_SKILL_AUTO_EVOLVE_PATCH_MD_PER_SKILL`** | `14000` | **仅 `skill evolve`**：单个 skill 的 SKILL.md 最多注入字符数。 |

为兼容旧配置，若未设置新变量，代码会回退读取：**`SCRIPT_AGENT_SKILL_AUTOCREATE`**、**`SCRIPT_AGENT_SKILL_AUTOCREATE_MAX`**、**`SCRIPT_AGENT_SKILL_AUTOCREATE_CONTEXT_CHARS`**（含义分别对应上表前三行）。

---

## 六、Skill 演化 / 沉淀用的对话摘要长度

| 变量 | 默认 | 含义 |
|------|------|------|
| **`SCRIPT_AGENT_SKILL_HISTORY_CHARS`** | 与下栏相同 | 提供给 **`skill evolve`**、**自动演化** 的多轮对话摘要最大字符数；超出只保留 **末尾**一段。 |
| **`SCRIPT_AGENT_HISTORY_BLOCK_CHARS`** | `12000` | 对话摘要默认值来源：`SCRIPT_AGENT_SKILL_HISTORY_CHARS` 未用时与此同步逻辑相关（见代码）。 |

（追问/检索里用到的历史块默认上限也是 **`SCRIPT_AGENT_HISTORY_BLOCK_CHARS`**。）

---

## 七、剧本分块（阶段 1）

| 变量 | 默认 | 含义 |
|------|------|------|
| **`SCRIPT_CHUNK_STRATEGY`** | `smart` | `smart`：场次/段落启发式分块；`fixed`：纯固定字数切分。 |
| **`CHUNK_OVERLAP_CHARS`** | `200` | `smart` 时超长段再切片的重叠字数（与常量 **`CHUNK_MAX_CHARS`**（代码内 8000）配合使用；**`CHUNK_MAX_CHARS` 仅代码常量，非环境变量**。） |

---

## 八、多轮对话历史（ask / trace / 检索决策）

| 变量 | 默认 | 含义 |
|------|------|------|
| **`SCRIPT_AGENT_HISTORY_MAX_ROUNDS`** | `8` | 最多保留几轮 ask/trace；`0` 清空历史。 |
| **`SCRIPT_AGENT_HISTORY_BLOCK_CHARS`** | `12000` | 拼进 prompt 的「历史对话」块最大字符数；超长保留末尾。 |
| **`SCRIPT_AGENT_HISTORY_REPLY_CHARS`** | `3500` | 每一轮里助手回复写入历史前的最大长度（超出截断）；下限 200。 |

---

## 九、人物关系图谱 HTML

| 变量 | 默认 | 含义 |
|------|------|------|
| **`SCRIPT_AGENT_GRAPH_HTML`** | `character_graph.html` | analyze 成功后写入的 HTML 路径（本地：相对 **cwd**）；**Serverless** 下相对路径会落到 **`/tmp/`**。`off` / `none` / `false` / `0` / `-` 关闭导出。 |

---

## 十、解析报告（Markdown / PDF）

| 变量 | 默认 | 含义 |
|------|------|------|
| **`SCRIPT_AGENT_REPORT_AFTER_ANALYZE`** | `md` | analyze 结束后自动写报告：**本地**为 **cwd**；**Serverless** 为 **`/tmp`**。取值：`md` — 仅 Markdown；`pdf` — 仅 PDF（无 **reportlab** 时回退 MD）；`both` — 两份；`off` / `0` / `false` / `no` / `none` / `-` — 关闭。（未知取值会跳过并写日志。） |

PDF 另需安装：**`pip install reportlab`**。

---

## 十一、相关代码位置

- **`script_agent/agent.py`**：`LLMClient`、`ScriptAnalysisAgent` 内几乎所有 `os.getenv`。
- **`script_agent/runtime_paths.py`**：`VERCEL` / `AWS_LAMBDA_FUNCTION_NAME` 下将日志、报告、图谱等相对路径统一到 **`/tmp`**。
- **`script_agent/skill_library.py`**：`SCRIPT_AGENT_SKILLS_DIR`。

修改默认值时请以源码为准；本文档随实现变更可能滞后，以 **`grep os.getenv`** 为准。
