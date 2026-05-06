# 剧本解析助手 Agent 框架

这个仓库提供一个可运行的 CLI Agent 骨架，目标是解决“长剧本理解效率”问题，而不只是摘要。

## 当前能力

1. 输入剧本并输出结构化分析（`analyze`）
2. 围绕已有分析继续追问（`ask`）
3. 对关键判断做依据追溯（`trace`）
4. 保存会话态（最近一次剧本与分析结果）
5. **解析报告**：`analyze` 完成后默认在当前工作目录自动生成 **`{剧本文件名}_解析报告.md`**（可用环境变量关闭或改成 PDF）；也可手动执行 `report md` / `report pdf`


## 目录

- `main.py`：CLI 入口（转发到 `cli.main`）
- `cli/`：命令行实现
- `script_agent/`：核心流程、LLM 适配与 prompts（`agent.py`、`skill_library.py`、`analysis_report.py`、`graph_viz.py`）
- `web/`：FastAPI 服务（`uvicorn web.server:app`）
- `static/`：Web 演示页静态资源
- `script_agent_skills/`：Skill 库
- `docs/`：环境与部署说明

## 运行

```bash
python3 main.py
# 或：python -m cli.main
```

命令：

- `analyze <剧本路径>`
- `ask <你的追问>`
- `trace <待验证判断>`
- `show`
- `report md [输出路径]` — 默认当前目录下 `{剧本文件名}_解析报告.md`
- `report pdf [输出路径]` — 同上，扩展名 `.pdf`（需 `pip install reportlab`）
- `skill evolve <指令>` — 手动演化 skill 库（可选）
- `exit`

示例：

```bash
analyze 小妾(1).txt
ask 这部剧最影响转化率的风险是什么？
trace 角色动机在中段不成立
```

## 报告文件在哪

- 默认在 **启动 CLI 时的当前工作目录**（一般为你在终端里 `cd` 到的文件夹），不是剧本文件所在目录。
- 关闭自动生成：`SCRIPT_AGENT_REPORT_AFTER_ANALYZE=off`
- 仅 PDF / 双份：`SCRIPT_AGENT_REPORT_AFTER_ANALYZE=pdf` 或 `both`。**PDF 必须先安装**：`pip install reportlab`；未安装时自动报告会回退为只写 Markdown，并在终端说明原因。

## LLM 接入说明

当前默认是 mock 模式（方便本地先跑通框架）。  
如需接入真实模型：

1. 设置环境变量：`SCRIPT_AGENT_MODE=real`
2. 在 `script_agent/agent.py` 的 `LLMClient._call_real_model()` 中填入你使用的模型 SDK 调用。

## 设计取舍

- 先保证核心闭环：输入 → 结构化理解 → 追问 → 依据追溯 → **导出报告**
- 终端里的 JSON 面向调试；报告面向阅读（决策、主线、评估、问题与改进、依据摘录等）
- PDF 使用 reportlab 内置 CID 字体（`STSong-Light`）；若缺依赖请只用 `report md` 或用 Markdown 工具自行转 PDF
