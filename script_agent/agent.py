from __future__ import annotations

import atexit
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TextIO

from .analysis_report import write_report_markdown, write_report_pdf
from .skill_library import (
    default_skills_root,
    format_loaded_skills_markdown,
    format_skills_index_markdown,
    iter_skill_records,
    skill_manage,
    skill_view,
    skills_list,
)

# =========================
# Runtime Config
# =========================
CHUNK_MAX_CHARS = 8000
# smart：先试场次/场景标记 → 段落 → 再按字数合并或重叠硬切；fixed：纯固定字数切片
DEFAULT_CHUNK_STRATEGY = "smart"
DEFAULT_CHUNK_OVERLAP = 200
# 操作日志：默认写入当前工作目录下 agent_operations.log；设为 off 关闭
DEFAULT_AGENT_LOG_FILE = "agent_operations.log"
# analyze 完成后自动写出人物关系图谱 HTML（相对路径基于进程 cwd）；设为 off 关闭
DEFAULT_CHARACTER_GRAPH_HTML = "character_graph.html"
# analyze 完成后是否自动写解析报告到 cwd：md（默认）| pdf | both | off
DEFAULT_REPORT_AFTER_ANALYZE = "pdf"

# 人物关系图谱：借鉴「本体约束 → 实例节点/边」思路（与 MiroFish 中 ontology + graph 节点/边结构对齐思想，不调用其代码）
ENTITY_RELATION_GRAPH_GUIDE = """
【人物关系图谱提取规范】（知识图谱式：先类型约束，再填实例；便于合并与可视化）

一、实体类型 entity_type（每条节点必选其一）
- Character：人物。role_tags 可用 protagonist/antagonist/supporting 等。
- FamilyUnit：家庭/家族集合。
- Organization：门派、公司、官府、团体等机构。
- Location：反复出现的关键场景（可选）。
- StoryElement：关键物件、身份符号等（可选）。

二、关系类型 relation_type（英文 UPPER_SNAKE_CASE，语义稳定）
常用：KINSHIP, ROMANTIC, HOSTILE, POWER_OVER, ALLIANCE, DEBT_GRATITUDE, MENTORSHIP, RIVALRY, KNOWS, OTHER。
- directed：权力、敌对、单向操控一般为 true；亲属、同盟可为 false 或 true（说明理由写在 fact）。
- fact：一句话说明「在剧中这条关系意味着什么」，勿空泛。
- evidence_quote：支撑该关系的原文短引（必须来自输入文本，勿编造）。

三、图结构 character_relation_graph（与图谱 API 中 nodes/edges 列表一致的思想）
- nodes[]：{"id","name","entity_type","role_tags":[],"summary":"","aliases":[]}
  · id 在全剧范围内稳定唯一（建议 char_xxx / org_xxx，合并时分块间可对齐同名）。
- edges[]：{"source_id","target_id","relation_type","directed","fact","evidence_quote"}

四、与叙事字段的关系
- 仍需填写 key_characters_and_relations（动机/冲突/主线作用）；若对应图中节点，请填可选字段 graph_node_id 与 nodes.id 一致。
"""


def _empty_character_relation_graph() -> Dict[str, Any]:
    return {"nodes": [], "edges": []}


def finalize_stage_1_graph(stage_1: Dict[str, Any]) -> Dict[str, Any]:
    """补全缺省字段并对图中节点、边做轻量去重（纯本地，不调用外部库）。"""
    graph = stage_1.get("character_relation_graph")
    if not isinstance(graph, dict):
        graph = _empty_character_relation_graph()
        stage_1["character_relation_graph"] = graph
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list):
        nodes = []
    if not isinstance(edges, list):
        edges = []

    seen_nid: set[str] = set()
    out_nodes: List[Dict[str, Any]] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id", "")).strip()
        if not nid or nid in seen_nid:
            continue
        seen_nid.add(nid)
        out_nodes.append(
            {
                "id": nid,
                "name": str(n.get("name", "")).strip() or nid,
                "entity_type": str(n.get("entity_type", "Character")).strip() or "Character",
                "role_tags": n.get("role_tags") if isinstance(n.get("role_tags"), list) else [],
                "summary": str(n.get("summary", "")).strip(),
                "aliases": n.get("aliases") if isinstance(n.get("aliases"), list) else [],
            }
        )

    seen_e: set[tuple[str, str, str, str]] = set()
    out_edges: List[Dict[str, Any]] = []
    for e in edges:
        if not isinstance(e, dict):
            continue
        sid = str(e.get("source_id", "")).strip()
        tid = str(e.get("target_id", "")).strip()
        rt = str(e.get("relation_type", "OTHER")).strip() or "OTHER"
        quote = str(e.get("evidence_quote", "")).strip()
        key = (sid, tid, rt, quote[:48])
        if not sid or not tid or key in seen_e:
            continue
        seen_e.add(key)
        directed = e.get("directed")
        if not isinstance(directed, bool):
            directed = True
        out_edges.append(
            {
                "source_id": sid,
                "target_id": tid,
                "relation_type": rt,
                "directed": directed,
                "fact": str(e.get("fact", "")).strip(),
                "evidence_quote": quote,
            }
        )

    graph["nodes"] = out_nodes
    graph["edges"] = out_edges
    return stage_1


# 多轮对话：保留最近几轮 ask/trace（重新 analyze 会清空）
DEFAULT_HISTORY_MAX_ROUNDS = 8
DEFAULT_HISTORY_BLOCK_CHARS = 12000
DEFAULT_SKILL_AUTOPICK_MAX = 4
DEFAULT_SKILL_AUTOPICK_SCRIPT_CHARS = 3500
DEFAULT_SKILL_AUTO_EVOLVE_MAX_PER_SESSION = 3
# CLI `skill evolve`：向模型注入可 patch 的 user SKILL.md 正文上限（自动演化不使用）
DEFAULT_SKILL_EVOLVE_PATCH_MD_TOTAL_CHARS = 32000
DEFAULT_SKILL_EVOLVE_PATCH_MD_PER_SKILL_CHARS = 14000


def _resolve_agent_log_file_path() -> Optional[str]:
    raw = os.getenv("SCRIPT_AGENT_LOG_FILE", DEFAULT_AGENT_LOG_FILE).strip()
    if raw.lower() in {"off", "none", "false", "0", "-"}:
        return None
    return raw or None

# =========================
# Prompts
# =========================
SYSTEM_PROMPT = """你是一个剧本解析助手，你的工作目标是帮助用户在短时间内判断一部剧本是否值得看。

你需要按“三阶段任务”执行分析：
阶段1【核心内容提取】
- 提取主线、关键冲突、人物关系、关键转折与卖点。
- 区分“核心信息”与“噪音信息”，避免流水账复述。

阶段2【观众视角多维评估：值不值得看】
- 从剧本对观众的吸引力的角度评估，至少覆盖以下维度：
  1) 开篇抓力：开场是否快速建立冲突/悬念；
  2) 角色吸引力：主角目标是否清晰、动机是否成立、人物是否有辨识度；
  3) 冲突与反转：矛盾强度、反转有效性、爽点/情绪峰值；
  4) 节奏与信息密度：推进是否连续，是否拖沓或重复；
  5) 情绪价值：代入感、情绪波动、共鸣点；
  6) 新鲜度与差异化：设定/关系/桥段是否有记忆点；
  7) 可读性与理解门槛：是否存在理解障碍或信息断层。
- 每个维度都要给出“结论 + 简短依据”。

阶段3【不足与改进方向】
- 识别最影响观感与留存的问题，按优先级输出。
- 改进建议要具体到“改哪里、怎么改、预期提升什么”，避免空泛建议。

输出要求：
- 结论要服务决策，表达简洁、结构化、可快速扫描。
- 关键判断尽量附原文依据（引用片段）。
- 证据不足时明确说明不确定性，不要臆断。
- 若系统提示中挂载了额外「Skill」全文，须在对应阶段分析中落实该视角，并与默认框架区分标注。
"""


SKILL_EVOLVE_SYSTEM = """你是剧本解析助手的 Skill 库管理员。
你只输出 JSON，不要输出其他文字。
"""


SKILL_EVOLVE_PROMPT = """【Skill 演化任务】
根据「用户指令」与当前 skill 目录（JSON），决定如何演化技能库。

输出 JSON Schema：
{
  "action": "patch" | "create" | "noop",
  "target_name": "skill 的 name 字段",
  "category": "仅 create：存放在 user/<category>/ 下，默认 general",
  "find_text": "patch：在 SKILL.md 或 file_path 指定文件中查找的原文片段（必须唯一匹配）",
  "replace_text": "patch：替换文本",
  "file_path": "patch：可选，相对 skill 目录；省略表示 SKILL.md",
  "full_skill_md": "create：完整 SKILL.md（含 YAML frontmatter）",
  "summary": "一句话说明改动"
}

规则：
1) **禁止** patch `agent_created=false` 的 bundled skill；若要基于 bundled 改版，请 **create** 新 skill（新 target_name），可复制并改写内容。
2) patch 仅用于 user 下 skill（agent_created=true）。
3) 用户指令若与 skill 无关，action=n noop。
4) patch 时 **find_text 必须与下方 patchable_skills 中对应 skill_md 原文逐字一致**（含换行）；禁止臆造未出现的片段。若有多处匹配应加长 find_text 上下文。
5) 若提供了「多轮对话摘要」，用户指令中的「刚才/之前说的」等可据此对齐；但仍不得编造 catalog 中不存在的 skill 名。
6) **不要**只靠改动 frontmatter 的 `description:` 来落实用户偏好；可操作口径应写在正文（如 `## 解析与报告偏好`），description 保持简短索引用语即可。
"""


SKILL_SELECT_SYSTEM = """你是剧本解析助手的 Skill 路由员。
你只输出 JSON，不要输出其他文字。
"""


SKILL_SELECT_PROMPT_HEAD = """【Skill 选择任务】
下面是 skills_catalog（仅 name / description，不含正文）。请根据 phase 与「上下文」判断：后续剧本解析应**额外**全文加载哪些 skill。

规则：
- **skills 数组里的 name 必须完全来自 skills_catalog**，禁止编造。
- 若无明确契合点，skills 取 []。
- 数量上限见 max_skills；优先选最相关的少数几个。
- already_loaded_skill_names 中的 skill **已经**会全文注入（用户环境变量钉住或本轮会话粘性加载），你**不要**把它们放进 skills（避免重复）。
- phase 取值含义：analyze_pipeline=整剧分析开始前；followup_ask=用户追问；evidence_trace=对某条判断做原文取证。

输出 JSON：
{"skills": ["name1"], "reason": "简短理由"}
"""


SKILL_AUTO_EVOLVE_SYSTEM = """你是剧本解析助手的 Skill 管理员（会话结束时自动演化）。只输出 JSON，不要输出其他文字。
"""


SKILL_AUTO_EVOLVE_PROMPT_HEAD = """【Skill 自动演化任务】
根据 trigger、「上下文」与「多轮对话摘要」，判断是否需要**演化** skill 库。

何时必须考虑 **patch / create**（不要 noop）：
- 用户表达**对未来多次**剧本解析、结构化输出或**导出解析报告**的稳定偏好或指令，例如含「以后」「之后」「下次」「默认」「记住」「生成报告时要」「解析时一律」等，且内容可写成**与具体某剧无关**的可复用规则。
- 助手在回复中明确承诺了「会记住 / 之后按某口径执行」类持久偏好，且该口径适合写入 skill。

何时应 **noop**：
- 仅讨论当前剧本剧情、单次结论、或一次性追问，无可沉淀的通用规则；
- 用户要求与 analyze / 报告流程无关。

演化含义：
- **patch**：在已有 skill（**仅 agent_created=true**）的 SKILL.md（或子文件）中做**唯一匹配**的片段替换。**find_text 必须是目标文件真实磁盘内容中的字面片段（逐字一致，含换行）**；不确定正文时不要猜，可改用 **create** 或 **noop**。
- **create**：无可安全 patch 的把握、或需求更适合单独一条 skill 时，新建 user skill（完整 SKILL.md）。
- **noop**：确实无可复用规则变更。

优先策略：
- 已有「剧本解析」「报告」类 user skill（如 **剧本解析助手**）→ **优先 patch** 追加「解析与报告偏好」等小节；若 patch 风险高则 **create** 专项 skill，避免无意义 noop。
- bundled（agent_created=false）**禁止 patch**；若需在其基础上扩展，应 **create** 新 target_name。

严格禁止：
- 把**某一特定剧本的剧情、角色结局、具体桥段细节**写入 skill；
- **仅用加长或改写 frontmatter 里的 `description:` 一行来承载解析/报告偏好**（description 只用于索引摘要，Analyze 阶段主要落实的是正文 Markdown；偏好必须写进正文，例如新增 `## 解析与报告偏好` 或对现有正文做小段落替换）；
- 使用无法在目标文件中字面匹配的 **find_text**（胡猜模板句）；
- create 使用 existing_names 已有同名；
- frontmatter 的 name 与 target_name 不一致。

trigger：
- after_analyze：刚完成整剧三阶段分析；
- after_ask：刚回答用户追问；
- after_trace：刚完成证据取证。

输出 JSON Schema：
{
  "action": "noop" | "patch" | "create",
  "target_name": "skill 名（patch/create 必填）",
  "category": "仅 create：user 子目录，默认 auto_evolve",
  "find_text": "patch：必填",
  "replace_text": "patch：必填（可为空字符串）",
  "file_path": "patch：一般省略（默认 SKILL.md）。仅当确有非 SKILL.md 的已存在文件时才填；乱填会导致替换失败。",
  "full_skill_md": "create：必填",
  "summary": "一句话说明演化动机"
}
"""


STEP_1_CORE_EXTRACTION_PROMPT = (
    """【阶段1：核心内容提取】
你将收到剧本文本，请只完成“核心提取”，并输出 JSON：
{
  "core_storyline": "主线梗概（非流水账）",
  "key_conflicts": ["关键冲突1", "关键冲突2"],
  "key_characters_and_relations": [
    {
      "name": "...",
      "motivation": "...",
      "conflicts": "...",
      "relation_to_mainline": "...",
      "graph_node_id": "可选，对应 character_relation_graph.nodes[].id"
    }
  ],
  "character_relation_graph": {
    "nodes": [
      {"id": "char_xxx", "name": "...", "entity_type": "Character", "role_tags": [], "summary": "", "aliases": []}
    ],
    "edges": [
      {
        "source_id": "...",
        "target_id": "...",
        "relation_type": "HOSTILE",
        "directed": true,
        "fact": "剧中语义说明",
        "evidence_quote": "原文短引"
      }
    ]
  },
  "major_turning_points": [
    {"turning_point": "...", "impact_on_story": "..."}
  ],
  "core_selling_points": ["观众可能被吸引的点"],
  "highlights_hooks_reversals": [
    {"type": "看点/钩子/反转/爽点", "description": "...", "why_it_matters": "..."}
  ],
  "evidence": [
    {"claim": "关键提取结论", "quote": "原文片段", "note": "为何支持该结论"}
  ],
  "limitations": ["信息缺失或不确定点"]
}

要求：
- 不要输出 JSON 以外内容；
- evidence 至少 3 条；
- 必须输出 character_relation_graph；若无足够依据确认某条关系，可将 edges 留空或减少，并在 limitations 说明；
- 每条边的 evidence_quote 必须来自输入文本。
"""
    + ENTITY_RELATION_GRAPH_GUIDE
)

STEP_1_CHUNK_EXTRACTION_PROMPT = (
    """【阶段1A：分块核心提取】
你将收到剧本的一段分块文本。请只基于该分块提取关键信息，并输出 JSON：
{
  "core_storyline": "该分块的核心剧情推进",
  "key_conflicts": ["该分块出现的关键冲突"],
  "key_characters_and_relations": [
    {
      "name": "...",
      "motivation": "...",
      "conflicts": "...",
      "relation_to_mainline": "...",
      "graph_node_id": "可选"
    }
  ],
  "character_relation_graph": {
    "nodes": [],
    "edges": []
  },
  "major_turning_points": [
    {"turning_point": "...", "impact_on_story": "..."}
  ],
  "core_selling_points": ["该分块的吸引点"],
  "highlights_hooks_reversals": [
    {"type": "看点/钩子/反转/爽点", "description": "...", "why_it_matters": "..."}
  ],
  "evidence": [
    {"claim": "分块内关键结论", "quote": "原文片段", "note": "为何支持该结论"}
  ],
  "limitations": ["该分块的信息局限"]
}

要求：
- 不要输出 JSON 以外内容；
- 结论仅基于当前分块，不要臆测其他分块内容；
- character_relation_graph 仅包含本分块可支持的节点与边；id 尽量稳定（便于后续分块合并）。
"""
    + ENTITY_RELATION_GRAPH_GUIDE
)

STEP_1_CHUNK_MERGE_PROMPT = (
    """【阶段1B：分块结果汇总】
你将收到多个“阶段1A 分块提取结果”（JSON 数组）。
请将它们合并为全剧本级别的“阶段1核心提取”结果，并输出 JSON：
{
  "core_storyline": "全局主线梗概（非流水账）",
  "key_conflicts": ["去重后的关键冲突"],
  "key_characters_and_relations": [
    {
      "name": "...",
      "motivation": "...",
      "conflicts": "...",
      "relation_to_mainline": "...",
      "graph_node_id": "合并后的节点 id"
    }
  ],
  "character_relation_graph": {
    "nodes": [],
    "edges": []
  },
  "major_turning_points": [
    {"turning_point": "...", "impact_on_story": "..."}
  ],
  "core_selling_points": ["全剧本吸引点"],
  "highlights_hooks_reversals": [
    {"type": "看点/钩子/反转/爽点", "description": "...", "why_it_matters": "..."}
  ],
  "evidence": [
    {"claim": "关键提取结论", "quote": "原文片段", "note": "为何支持该结论"}
  ],
  "limitations": ["全局层面不确定点"]
}

要求：
- 不要输出 JSON 以外内容；
- 合并时去重并消除矛盾表述；
- evidence 优先保留信息量更高、可验证性更强的引用；
- 合并 character_relation_graph：同一人物多种称呼合并为单一节点 id，补齐 aliases；边去重；冲突关系在 limitations 说明；
- 输出全剧级连通的人物关系图，勿遗漏主线关键人物节点。
"""
    + ENTITY_RELATION_GRAPH_GUIDE
)


STEP_2_AUDIENCE_EVAL_PROMPT = """【阶段2：观众视角多维评估】
你将得到阶段1的核心提取结果（JSON）。其中若包含 character_relation_graph（人物关系图谱），评估时可参考关系张力、对立结构是否清晰。
请从观众“值不值得继续看”视角做评估，输出 JSON：
{
  "overall_watch_worthiness": {
    "is_worth_continue": "是/否/不确定",
    "confidence": 0-100,
    "reason": "一句话总结"
  },
  "dimension_scores": [
    {"dimension": "开篇抓力", "score": 1-10, "judgement": "...", "evidence_or_reason": "..."},
    {"dimension": "角色吸引力", "score": 1-10, "judgement": "...", "evidence_or_reason": "..."},
    {"dimension": "冲突与反转", "score": 1-10, "judgement": "...", "evidence_or_reason": "..."},
    {"dimension": "节奏与信息密度", "score": 1-10, "judgement": "...", "evidence_or_reason": "..."},
    {"dimension": "情绪价值", "score": 1-10, "judgement": "...", "evidence_or_reason": "..."},
    {"dimension": "新鲜度与差异化", "score": 1-10, "judgement": "...", "evidence_or_reason": "..."},
    {"dimension": "可读性与理解门槛", "score": 1-10, "judgement": "...", "evidence_or_reason": "..."}
  ],
  "where_to_read_next": [
    {"goal": "为了判断X，建议重点看哪段", "suggested_focus": "..."}
  ],
  "quick_decision": {
    "is_worth_continue": "是/否/不确定",
    "confidence": 0-100,
    "reason": "一句话理由"
  }
}

要求：
- 不要输出 JSON 以外内容；
- dimension_scores 必须完整覆盖 7 个指定维度。
"""


STEP_3_IMPROVEMENT_PROMPT = """【阶段3：不足与改进方向】
你将得到阶段1核心提取 + 阶段2观众评估。请识别最影响观感的问题并提出改进，输出 JSON：
{
  "top_issues": [
    {"issue": "...", "impact": "...", "severity": "高/中/低", "priority": 1}
  ],
  "improvement_directions": [
    {"for_issue": "...", "what_to_change": "...", "how_to_change": "...", "expected_benefit": "..."}
  ]
}

要求：
- 不要输出 JSON 以外内容；
- 建议要具体到改哪里、怎么改、预期提升什么。
"""


FOLLOWUP_QA_PROMPT = """你将得到：
1) 一份剧本结构化分析结果（JSON）；
2) 此前的多轮对话摘要（若有）；
3) 当前用户追问。

请结合历史对话理解指代（如「刚才那点」「上一问」），并基于已有分析回答用户，遵循：
- 先给结论，再给依据；
- 若证据不足，明确说“现有文本无法充分支持”；
- 能定位到原文依据时，优先引用 evidence 中的 quote；
- 控制在 5-10 句，避免冗长。
"""


EVIDENCE_TRACE_PROMPT = """你将得到：
1) 一份剧本文本（或检索片段）；
2) 一个待验证判断（claim）；
3) 可选的历史对话摘要（若 claim 引用前文观点，可结合理解）。

请输出 JSON：
{
  "claim": "...",
  "supporting_quotes": [{"quote": "...", "reason": "..."}],
  "opposing_quotes": [{"quote": "...", "reason": "..."}],
  "verdict": "支持/部分支持/不支持/证据不足"
}

要求：
- 如果找不到明确证据，supporting_quotes 与 opposing_quotes 可为空数组；
- 不要输出 JSON 以外内容。
"""

RETRIEVAL_DECISION_PROMPT = """你是一个工具调用决策器。你可以决定是否调用工具：
tool_name: original_text_retrieval
tool_use_case: 当问题需要原文证据、细节核对、桥段定位、上下文确认时检索原文分块。

你将依次收到：
1) 历史对话摘要（可能为空）；
2) 当前这一次的用户追问或待验证判断；
3) 已有结构化分析结果（可能很长、不完整）。

请输出 JSON：
{
  "need_retrieval": true/false,
  "query": "用于检索的关键词或短句",
  "reason": "简短理由"
}

要求：
- 只输出 JSON（**不要**使用 Markdown 代码围栏如 ```json）；
- 必须先结合「历史对话」理解当前输入：有无省略主语、是否追问/反驳前文、是否承接上一轮的「证据/原文」话题；
- 构造 query 时应融合「历史中的实体/冲突点/桥段」与「当前句」中的检索词，避免只根据当前一句抽关键词；
- 若当前句依赖上文才能判断要不要查原文，必须在 reason 中简要说明依据了哪段历史；
- 若问题偏观点总结且历史+已有分析已足够，可设 need_retrieval=false；
- 若用户明确要求证据、原文、桥段定位、核对真假，或历史里承诺「用原文说明」类意图，优先 need_retrieval=true。
"""


# =========================
# LLM Adapter
# =========================
class LLMClient:
    """Minimal LLM adapter.

    Default mode is real (calls `_call_real_model` when Key/base/model are available).
    Set SCRIPT_AGENT_MODE=mock for offline scaffolding without an API.
    """

    def __init__(self) -> None:
        self.mode = os.getenv("SCRIPT_AGENT_MODE", "real").strip().lower()
        # 每线程复用一个 OpenAI 客户端，避免每次 complete 新建连接（TLS + HTTP 池预热很慢）。
        self._openai_local = threading.local()

    @staticmethod
    def _message_content_to_str(raw: Any) -> Optional[str]:
        """OpenAI message.content：str 或多模态 list[{type,text}, ...]。"""
        if raw is None:
            return None
        if isinstance(raw, str):
            s = raw.strip()
            return s or None
        if isinstance(raw, list):
            parts: List[str] = []
            for item in raw:
                if isinstance(item, dict):
                    t = item.get("text")
                    if isinstance(t, str):
                        parts.append(t)
                    elif item.get("type") == "text" and isinstance(item.get("content"), str):
                        parts.append(item["content"])
                elif isinstance(item, str):
                    parts.append(item)
            joined = "".join(parts).strip()
            return joined or None
        text = str(raw).strip()
        return text or None

    @staticmethod
    def _extract_completion_text(resp: Any) -> str:
        """兼容官方 SDK 对象、裸 dict、以及部分网关返回的 JSON 字符串。"""
        if resp is None:
            raise RuntimeError("LLM 返回空响应。")
        if isinstance(resp, str):
            s = resp.strip()
            if s.startswith("{") or s.startswith("["):
                try:
                    return LLMClient._extract_completion_text(json.loads(s))
                except json.JSONDecodeError:
                    pass
            if s:
                return s
            raise RuntimeError("LLM 返回空字符串。")
        if isinstance(resp, dict):
            choices = resp.get("choices")
            if isinstance(choices, list) and choices:
                ch0 = choices[0]
                if isinstance(ch0, dict):
                    msg = ch0.get("message")
                    if isinstance(msg, dict):
                        c = LLMClient._message_content_to_str(msg.get("content"))
                        if c:
                            return c
                    c = LLMClient._message_content_to_str(ch0.get("content") or ch0.get("text"))
                    if c:
                        return c
            raise RuntimeError(
                "兼容网关返回 dict，但无法解析 choices[0].message.content；顶层键："
                + ",".join(sorted(resp.keys()))
            )
        choices = getattr(resp, "choices", None)
        if choices is not None:
            if not choices:
                raise RuntimeError("LLM 返回 choices 为空。")
            first = choices[0]
            msg = getattr(first, "message", None)
            if msg is None and isinstance(first, dict):
                msg = first.get("message")
            if msg is None:
                raise RuntimeError("LLM 返回缺少 message。")
            raw_c = getattr(msg, "content", None)
            if raw_c is None and isinstance(msg, dict):
                raw_c = msg.get("content")
            text = LLMClient._message_content_to_str(raw_c)
            if text:
                return text
            raise RuntimeError("LLM 返回 message.content 为空。")
        raise RuntimeError(
            f"无法解析 LLM 响应（期望含 choices），实际类型：{type(resp).__name__}。"
        )

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        openai_api_key: Optional[str] = None,
        openai_base_url: Optional[str] = None,
        openai_model: Optional[str] = None,
        mode_override: Optional[str] = None,
    ) -> str:
        eff_mode = (
            str(mode_override).strip().lower()
            if mode_override is not None and str(mode_override).strip()
            else self.mode
        )
        if eff_mode == "real":
            return self._call_real_model(
                system_prompt,
                user_prompt,
                api_key=openai_api_key,
                base_url=openai_base_url,
                model=openai_model,
            )
        return self._mock_response(user_prompt)

    def _call_real_model(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ) -> str:
        api_key_f = (api_key.strip() if api_key else "") or os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key_f:
            raise RuntimeError(
                "缺少模型 API Key：请在运行服务端配置 OPENAI_API_KEY，或通过网页/API 请求头传入 X-OpenAI-API-Key。"
            )

        model_f = (model.strip() if model else "") or os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
        base_raw = (base_url.strip() if base_url else "") or os.getenv("OPENAI_BASE_URL", "").strip()
        timeout_s = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "120"))

        client = self._thread_local_openai_client(api_key_f, base_raw, timeout_s)
        resp = client.chat.completions.create(
            model=model_f,
            temperature=0.2,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        content = self._extract_completion_text(resp)
        self._reject_if_html_api_response(content)
        return content

    @staticmethod
    def _reject_if_html_api_response(content: str) -> None:
        """兼容网关若把 Base URL 填成网站首页，会返回 SPA 的 HTML，易被误判为模型正文。"""
        head = content.lstrip()[:800].lower()
        if "<!doctype html" in head or head.startswith("<html"):
            raise RuntimeError(
                "模型接口返回了网页 HTML，而不是 Chat Completions 的 JSON。"
                "请检查 OPENAI_BASE_URL（或网页里的 Base URL）：应为 OpenAI 兼容的 API 根路径，"
                "通常以 /v1 结尾，例如 https://你的网关域名/v1 。"
                "不要填控制台首页、缺少 /v1 的根地址，或会 302 到网页的路径。"
            )

    def _thread_local_openai_client(self, api_key_f: str, base_raw: str, timeout_s: float):
        """同一线程内复用客户端；配置变更时在该线程重建。"""
        try:
            connect_s = float(os.getenv("OPENAI_CONNECT_TIMEOUT_SECONDS", "25"))
        except ValueError:
            connect_s = 25.0
        try:
            max_retries = int(os.getenv("OPENAI_MAX_RETRIES", "2"))
        except ValueError:
            max_retries = 2
        max_retries = max(0, min(max_retries, 10))

        sig = (api_key_f, base_raw, timeout_s, connect_s, max_retries)
        prev_sig = getattr(self._openai_local, "sig", None)
        client = getattr(self._openai_local, "client", None)
        if client is not None and prev_sig == sig:
            return client
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                "缺少 openai 依赖，请先执行: pip3 install openai"
            ) from exc

        base_f = base_raw.strip() or None
        # 单独限制「建连」时间，避免网关/DNS 异常时长时间假死（原先只用 float 时 connect 可能过久）
        try:
            import httpx

            timeout = httpx.Timeout(
                connect=connect_s,
                read=timeout_s,
                write=timeout_s,
                pool=connect_s,
            )
        except Exception:
            timeout = timeout_s

        client = OpenAI(
            api_key=api_key_f,
            base_url=base_f,
            timeout=timeout,
            max_retries=max_retries,
        )
        self._openai_local.client = client
        self._openai_local.sig = sig
        return client

    def _mock_response(self, user_prompt: str) -> str:
        if "【阶段1B：分块结果汇总】" in user_prompt:
            return json.dumps(self._mock_stage_1_data(), ensure_ascii=False)

        if "【阶段1A：分块核心提取】" in user_prompt or "【阶段1：核心内容提取】" in user_prompt:
            return json.dumps(self._mock_stage_1_data(), ensure_ascii=False)

        if "【阶段2：观众视角多维评估】" in user_prompt:
            return json.dumps(self._mock_stage_2_data(), ensure_ascii=False)

        if "【阶段3：不足与改进方向】" in user_prompt:
            return json.dumps(self._mock_stage_3_data(), ensure_ascii=False)

        if "待验证判断" in user_prompt:
            return json.dumps(self._mock_trace_data(), ensure_ascii=False)

        if "工具调用决策器" in user_prompt:
            return json.dumps(self._mock_retrieval_decision(user_prompt), ensure_ascii=False)

        if "【Skill 演化任务】" in user_prompt:
            return json.dumps(
                {"action": "noop", "target_name": "", "summary": "mock：未修改 skill。"},
                ensure_ascii=False,
            )

        if "【Skill 选择任务】" in user_prompt:
            return json.dumps({"skills": [], "reason": "mock：不额外加载 skill。"}, ensure_ascii=False)

        if "【Skill 自动演化任务】" in user_prompt:
            return json.dumps({"action": "noop", "summary": "mock：不自动演化 skill。"}, ensure_ascii=False)

        return "基于当前分析，我倾向于先关注关键冲突段落并核实动机链路；现有证据有限。"

    @staticmethod
    def _mock_stage_1_data() -> Dict[str, Any]:
        return {
            "core_storyline": "围绕主角在复杂关系中的处境变化展开，冲突核心在身份与利益博弈。",
            "key_conflicts": ["身份与利益冲突", "情感与生存选择冲突"],
            "key_characters_and_relations": [
                {
                    "name": "主角",
                    "motivation": "在压力中争取主动权",
                    "conflicts": "与关键对手存在持续对抗",
                    "relation_to_mainline": "推动主线冲突升级",
                    "graph_node_id": "char_protagonist",
                }
            ],
            "major_turning_points": [
                {
                    "turning_point": "主角在高压处境中做出关键选择",
                    "impact_on_story": "推动关系格局变化并升级主线冲突",
                }
            ],
            "core_selling_points": ["关系张力强", "冲突密度高"],
            "highlights_hooks_reversals": [
                {
                    "type": "钩子",
                    "description": "开场抛出关系冲突",
                    "why_it_matters": "快速建立阅读驱动力",
                }
            ],
            "evidence": [
                {
                    "claim": "开场存在冲突钩子",
                    "quote": "（示例）角色A与角色B在第一场出现尖锐对立。",
                    "note": "直接提供冲突入口",
                },
                {
                    "claim": "中段节奏可能拖沓",
                    "quote": "（示例）多段对白重复表达同一态度。",
                    "note": "信息增量有限",
                },
                {
                    "claim": "角色动机有断层",
                    "quote": "（示例）关键决定前缺少明确心理铺垫。",
                    "note": "决策因果链偏弱",
                },
            ],
            "limitations": ["当前为框架演示，需接入真实模型与完整剧本文本获取高质量结果。"],
            "character_relation_graph": {
                "nodes": [
                    {
                        "id": "char_protagonist",
                        "name": "主角",
                        "entity_type": "Character",
                        "role_tags": ["protagonist"],
                        "summary": "在复杂关系中争取主动权的核心人物",
                        "aliases": [],
                    },
                    {
                        "id": "char_antagonist",
                        "name": "关键对手",
                        "entity_type": "Character",
                        "role_tags": ["antagonist"],
                        "summary": "与主角形成持续对立的角色",
                        "aliases": [],
                    },
                ],
                "edges": [
                    {
                        "source_id": "char_protagonist",
                        "target_id": "char_antagonist",
                        "relation_type": "HOSTILE",
                        "directed": True,
                        "fact": "双方在利益与身份层面持续对抗，驱动主线冲突升级",
                        "evidence_quote": "（示例）第一场出现尖锐对立与施压。",
                    }
                ],
            },
        }

    @staticmethod
    def _mock_stage_2_data() -> Dict[str, Any]:
        return {
            "overall_watch_worthiness": {
                "is_worth_continue": "不确定",
                "confidence": 58,
                "reason": "冲突张力尚可，但角色动机铺垫影响代入感。",
            },
            "dimension_scores": [
                {"dimension": "开篇抓力", "score": 7, "judgement": "冲突进入较快", "evidence_or_reason": "开场即出现对立关系"},
                {"dimension": "角色吸引力", "score": 6, "judgement": "设定有潜力", "evidence_or_reason": "动机阐释略有断层"},
                {"dimension": "冲突与反转", "score": 7, "judgement": "冲突持续", "evidence_or_reason": "关键节点有关系变化"},
                {"dimension": "节奏与信息密度", "score": 6, "judgement": "局部拖沓", "evidence_or_reason": "部分对白重复"},
                {"dimension": "情绪价值", "score": 6, "judgement": "情绪起伏存在", "evidence_or_reason": "高压情境有代入点"},
                {"dimension": "新鲜度与差异化", "score": 6, "judgement": "题材常见", "evidence_or_reason": "需更强记忆点"},
                {"dimension": "可读性与理解门槛", "score": 6, "judgement": "整体可读", "evidence_or_reason": "个别信息衔接弱"},
            ],
            "where_to_read_next": [
                {"goal": "验证角色弧光是否成立", "suggested_focus": "主角做出代价性选择的前后段落"}
            ],
            "quick_decision": {
                "is_worth_continue": "不确定",
                "confidence": 58,
                "reason": "建议继续阅读关键冲突节点后再判断。",
            },
        }

    @staticmethod
    def _mock_stage_3_data() -> Dict[str, Any]:
        return {
            "top_issues": [
                {"issue": "关键动机铺垫不足", "impact": "观众对行为合理性产生疑问", "severity": "高", "priority": 1},
                {"issue": "中段对白重复", "impact": "节奏感下降，留存风险上升", "severity": "中", "priority": 2},
            ],
            "improvement_directions": [
                {
                    "for_issue": "关键动机铺垫不足",
                    "what_to_change": "在关键选择前补充心理与外部压力线索",
                    "how_to_change": "增加1-2个触发事件和内心矛盾表达，形成因果链",
                    "expected_benefit": "提升角色决策说服力和情感代入",
                },
                {
                    "for_issue": "中段对白重复",
                    "what_to_change": "压缩重复态度表达段落",
                    "how_to_change": "将重复对白合并为动作或冲突升级事件",
                    "expected_benefit": "提升推进效率并增强紧张感",
                },
            ],
        }

    @staticmethod
    def _mock_trace_data() -> Dict[str, Any]:
        return {
            "claim": "示例判断",
            "supporting_quotes": [],
            "opposing_quotes": [],
            "verdict": "证据不足",
        }

    @staticmethod
    def _mock_retrieval_decision(user_prompt: str) -> Dict[str, Any]:
        evidence_keywords = ["证据", "原文", "桥段", "片段", "依据", "证明", "核实", "真假"]
        if any(keyword in user_prompt for keyword in evidence_keywords):
            return {
                "need_retrieval": True,
                "query": "关键冲突 角色动机 节奏 拖沓",
                "reason": "问题指向证据与原文定位，需要检索支持。",
            }
        return {
            "need_retrieval": False,
            "query": "",
            "reason": "问题可基于已有结构化分析直接回答。",
        }


@dataclass
class AnalysisResult:
    raw: Dict[str, Any]


@dataclass
class ChunkRecord:
    index: int
    text: str
    stage_1_extraction: Dict[str, Any]


# =========================
# Agent Workflow
# =========================
class ScriptAnalysisAgent:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm
        self.skills_root: Path = default_skills_root()
        raw_session = os.getenv("SCRIPT_AGENT_SESSION_SKILLS", "").strip()
        self._pinned_skill_names: List[str] = []
        if raw_session:
            seen_ns: set[str] = set()
            for part in raw_session.split(","):
                n = part.strip()
                if n and n not in seen_ns:
                    seen_ns.add(n)
                    self._pinned_skill_names.append(n)
        # skill evolve create 成功后会追加；analyze/ask/trace 前模型自动选课写入此处
        self._sticky_skill_names: List[str] = []
        self._agent_selected_skill_names: List[str] = []
        self._skill_auto_evolve_session_count: int = 0
        self.last_script: str = ""
        self.last_analysis: AnalysisResult | None = None
        self.last_chunk_records: List[ChunkRecord] = []
        # 多轮会话：每项为 {"channel": "ask"|"trace", "user": str, "assistant": str}
        self.turn_history: List[Dict[str, str]] = []
        self.last_graph_export_path: Optional[str] = None
        # 自动报告导出路径（供 Web 下载/内嵌）；analyze 开始时清空
        self.last_auto_report_pdf_path: Optional[str] = None
        self.last_auto_report_md_path: Optional[str] = None
        self.last_analyzed_source_path: Optional[str] = None
        self.verbose = os.getenv("SCRIPT_AGENT_VERBOSE", "1").strip().lower() not in {"0", "false", "no"}
        self._log_file_path: Optional[str] = _resolve_agent_log_file_path()
        self._log_file: Optional[TextIO] = None
        if self._log_file_path:
            self._log_file = open(self._log_file_path, "a", encoding="utf-8", buffering=1)
            self._write_log_file(f"===== session start pid={os.getpid()} path={self._log_file_path!r} =====")
            atexit.register(self._atexit_close_log)
        self.log_event(f"Agent 初始化完成 llm_mode={self.llm.mode!r} verbose={self.verbose}")
        # Web/API：可按会话覆盖 OpenAI 兼容参数（优先于环境变量）
        self._llm_api_key_override: Optional[str] = None
        self._llm_base_url_override: Optional[str] = None
        self._llm_model_override: Optional[str] = None
        self._llm_mode_override: Optional[str] = None

    def configure_llm_provider(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> None:
        """更新会话级 LLM 配置。传入非 None 的值才会覆盖对应项；空字符串表示清除该项覆盖。"""
        if api_key is not None:
            self._llm_api_key_override = api_key.strip() or None
        if base_url is not None:
            self._llm_base_url_override = base_url.strip() or None
        if model is not None:
            self._llm_model_override = model.strip() or None
        if mode is not None:
            self._llm_mode_override = mode.strip().lower() or None

    def _notify_progress(
        self,
        cb: Optional[Callable[[str, Dict[str, Any]], None]],
        phase: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """供 Web 流式解析等场景回调；回调异常不影响分析主流程。"""
        if cb is None:
            return
        try:
            cb(phase, dict(payload or {}))
        except Exception:
            pass

    def _skills_enabled(self) -> bool:
        return os.getenv("SCRIPT_AGENT_SKILLS", "1").strip().lower() not in {"0", "false", "no", "off"}

    def _skill_autopick_enabled(self) -> bool:
        return os.getenv("SCRIPT_AGENT_SKILL_AUTOPICK", "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }

    def _skill_autopick_max(self) -> int:
        try:
            return max(
                0,
                min(
                    12,
                    int(os.getenv("SCRIPT_AGENT_SKILL_AUTOPICK_MAX", str(DEFAULT_SKILL_AUTOPICK_MAX))),
                ),
            )
        except ValueError:
            return DEFAULT_SKILL_AUTOPICK_MAX

    def _merged_skill_names_for_prompt(self) -> List[str]:
        """顺序：用户钉住 → evolve 粘性加载 → 本轮模型选课（去重）。"""
        out: List[str] = []
        seen: set[str] = set()
        for bucket in (
            self._pinned_skill_names,
            self._sticky_skill_names,
            self._agent_selected_skill_names,
        ):
            for n in bucket:
                if n not in seen:
                    seen.add(n)
                    out.append(n)
        return out

    def _autopick_skills(self, phase: str, context_block: str) -> None:
        """每轮 analyze / ask / trace 前由模型从目录元数据中选择要注入全文的 skill。"""
        self._agent_selected_skill_names = []
        if not self._skills_enabled() or not self._skill_autopick_enabled():
            return
        max_n = self._skill_autopick_max()
        if max_n <= 0:
            return
        catalog = self.skills_catalog()
        if not catalog:
            return
        valid = {str(row["name"]).strip() for row in catalog if row.get("name")}
        already = set(self._pinned_skill_names) | set(self._sticky_skill_names)
        user_prompt = (
            f"{SKILL_SELECT_PROMPT_HEAD}\n\n=== skills_catalog ===\n"
            f"{json.dumps(catalog, ensure_ascii=False)}\n\n=== phase ===\n{phase}\n\n"
            f"=== max_skills ===\n{max_n}\n\n=== already_loaded_skill_names ===\n"
            f"{json.dumps(sorted(already), ensure_ascii=False)}\n\n=== 上下文 ===\n"
            f"{context_block.strip()}"
        )
        self._log(f"Skill 自动选择：请求模型 phase={phase!r}")
        raw = self._llm_complete("skill_autopick", SKILL_SELECT_SYSTEM, user_prompt)
        parsed = self._safe_parse_json(raw, fallback={"skills": [], "reason": "JSON 解析失败"})
        skills_raw = parsed.get("skills")
        if skills_raw is None:
            skills_raw = parsed.get("skill_names")
        picked: List[str] = []
        if isinstance(skills_raw, list):
            for item in skills_raw:
                n = str(item).strip()
                if n in valid and n not in already and n not in picked:
                    picked.append(n)
                if len(picked) >= max_n:
                    break
        self._agent_selected_skill_names = picked
        reason = str(parsed.get("reason") or "").strip()
        self._log(f"Skill 自动选择结果 picked={picked!r} reason={reason[:240]!r}")

    def _skill_auto_evolve_enabled(self) -> bool:
        raw = os.getenv(
            "SCRIPT_AGENT_SKILL_AUTO_EVOLVE",
            os.getenv("SCRIPT_AGENT_SKILL_AUTOCREATE", "1"),
        ).strip()
        return raw.lower() not in {"0", "false", "no", "off"}

    def _skill_auto_evolve_max_per_session(self) -> int:
        try:
            mx = os.getenv(
                "SCRIPT_AGENT_SKILL_AUTO_EVOLVE_MAX",
                os.getenv(
                    "SCRIPT_AGENT_SKILL_AUTOCREATE_MAX",
                    str(DEFAULT_SKILL_AUTO_EVOLVE_MAX_PER_SESSION),
                ),
            )
            return max(0, int(mx))
        except ValueError:
            return DEFAULT_SKILL_AUTO_EVOLVE_MAX_PER_SESSION

    def _skill_evolution_history_block(self) -> str:
        """供 skill evolve / 自动演化使用的多轮对话摘要（可截断，避免撑爆上下文）。"""
        try:
            cap = max(
                500,
                int(os.getenv("SCRIPT_AGENT_SKILL_HISTORY_CHARS", str(DEFAULT_HISTORY_BLOCK_CHARS))),
            )
        except ValueError:
            cap = DEFAULT_HISTORY_BLOCK_CHARS
        block = self._format_turn_history_block()
        if len(block) > cap:
            return "…（对话摘要过长，仅保留末尾）\n" + block[-cap:]
        return block

    def _patchable_skills_md_payload_for_evolve(self) -> List[Dict[str, str]]:
        """供 CLI `skill evolve` 构造 patch：注入 user SKILL.md 正文（自动演化不使用）。"""
        try:
            total_cap = max(
                4000,
                int(
                    os.getenv(
                        "SCRIPT_AGENT_SKILL_AUTO_EVOLVE_PATCH_MD_CHARS",
                        str(DEFAULT_SKILL_EVOLVE_PATCH_MD_TOTAL_CHARS),
                    )
                ),
            )
        except ValueError:
            total_cap = DEFAULT_SKILL_EVOLVE_PATCH_MD_TOTAL_CHARS
        try:
            per_cap = max(
                2000,
                int(
                    os.getenv(
                        "SCRIPT_AGENT_SKILL_AUTO_EVOLVE_PATCH_MD_PER_SKILL",
                        str(DEFAULT_SKILL_EVOLVE_PATCH_MD_PER_SKILL_CHARS),
                    )
                ),
            )
        except ValueError:
            per_cap = DEFAULT_SKILL_EVOLVE_PATCH_MD_PER_SKILL_CHARS
        budget = total_cap
        rows: List[Dict[str, str]] = []
        try:
            recs = [
                r
                for r in iter_skill_records(self.skills_root)
                if r.agent_created and r.skill_md_path.is_file()
            ]
        except OSError:
            return []
        for r in sorted(recs, key=lambda x: x.name.lower()):
            if budget <= 0:
                break
            try:
                full = r.skill_md_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            take = min(len(full), per_cap, budget)
            body = full[:take]
            if take < len(full):
                body += (
                    "\n…（正文过长已截断；patch 时 find_text 只能使用以上可见范围内的字面文本；"
                    "若需改动截断之后的内容请 noop 并提示用户使用 CLI：`skill evolve <指令>`）"
                )
            rows.append(
                {
                    "name": r.name,
                    "relative_skill_md": r.relative_skill_md,
                    "skill_md": body,
                }
            )
            budget -= take
        return rows

    def _maybe_auto_evolve_skill(self, trigger: str, context_block: str) -> None:
        """analyze / ask / trace 结束后：模型可 noop / patch / create（本会话成功写盘次数受限）。"""
        if not self._skills_enabled() or not self._skill_auto_evolve_enabled():
            return
        max_c = self._skill_auto_evolve_max_per_session()
        if max_c <= 0:
            return
        if self._skill_auto_evolve_session_count >= max_c:
            self._log("Skill 自动演化：已达本会话上限，跳过")
            return
        catalog = self.skills_catalog()
        existing = {str(r["name"]).strip() for r in catalog if r.get("name")}
        hist = self._skill_evolution_history_block()
        user_prompt = (
            f"{SKILL_AUTO_EVOLVE_PROMPT_HEAD}\n\n=== trigger ===\n{trigger}\n\n"
            f"=== skills_catalog ===\n{json.dumps(catalog, ensure_ascii=False)}\n\n"
            f"=== existing_names ===\n{json.dumps(sorted(existing), ensure_ascii=False)}\n\n"
            f"=== 上下文 ===\n{context_block.strip()}\n\n"
            f"=== 多轮对话摘要 ===\n{hist}"
        )
        self._log(f"Skill 自动演化：请求模型 trigger={trigger!r}")
        raw = self._llm_complete("skill_auto_evolve", SKILL_AUTO_EVOLVE_SYSTEM, user_prompt)
        plan = self._safe_parse_json(raw, fallback={"action": "noop"})
        action = str(plan.get("action") or "noop").strip().lower()
        summary = str(plan.get("summary") or "").strip()

        if action == "noop":
            self._log(f"Skill 自动演化：noop summary={summary[:160]!r}")
            return

        if action == "patch":
            name = str(plan.get("target_name") or "").strip()
            find_text = plan.get("find_text")
            replace_text = plan.get("replace_text")
            if not name or not isinstance(find_text, str) or not find_text:
                self._log("Skill 自动演化：patch 计划不完整，跳过")
                return
            if replace_text is None:
                self._log("Skill 自动演化：patch 缺少 replace_text，跳过")
                return
            fp_raw = plan.get("file_path")
            fp = str(fp_raw).strip() if isinstance(fp_raw, str) and fp_raw.strip() else None
            result = skill_manage(
                "patch",
                name=name,
                find_text=find_text,
                replace_text=replace_text if isinstance(replace_text, str) else str(replace_text),
                file_path=fp,
                root=self.skills_root,
            )
            if not result.get("ok"):
                self._log(f"Skill 自动演化 patch 失败 {result.get('error')!r}")
                return
            self._skill_auto_evolve_session_count += 1
            self.skill_load_session(name)
            self.log_event(f"Skill 自动演化：已 patch {name!r} ({summary[:120]})")
            return

        if action == "create":
            name = str(plan.get("target_name") or "").strip()
            content = plan.get("full_skill_md")
            if not name or not isinstance(content, str) or not content.strip():
                self._log("Skill 自动演化：create 计划不完整，跳过")
                return
            if name in existing:
                self._log(f"Skill 自动演化：名称已存在 {name!r}，跳过（可改用 patch）")
                return
            cat = str(plan.get("category") or "auto_evolve").strip() or "auto_evolve"
            result = skill_manage(
                "create",
                name=name,
                content=content,
                category=cat,
                root=self.skills_root,
            )
            if not result.get("ok"):
                self._log(f"Skill 自动演化 create 失败 {result.get('error')!r}")
                return
            self._skill_auto_evolve_session_count += 1
            self.skill_load_session(name)
            self.log_event(f"Skill 自动演化：已新建 {name!r} ({summary[:120]})")
            return

        self._log(f"Skill 自动演化：未知 action={action!r} summary={summary[:120]!r}")

    def _effective_system_prompt(self) -> str:
        if not self._skills_enabled():
            return SYSTEM_PROMPT
        try:
            records = iter_skill_records(self.skills_root)
        except OSError:
            records = []
        idx = format_skills_index_markdown(records)
        loaded = format_loaded_skills_markdown(
            self.skills_root, self._merged_skill_names_for_prompt()
        )
        return SYSTEM_PROMPT + idx + loaded

    def skills_catalog(self) -> List[Dict[str, Any]]:
        try:
            return skills_list(self.skills_root)
        except OSError:
            return []

    def skill_load_session(self, name: str) -> str:
        name = name.strip()
        if not name:
            return "skill 名称不能为空。"
        data = skill_view(name, root=self.skills_root, bump_view=True)
        if not data.get("ok"):
            return data.get("error") or "加载失败。"
        if name not in self._sticky_skill_names:
            self._sticky_skill_names.append(name)
        self.log_event(f"粘性会话已加载 skill（evolve 等）: {name}")
        return f"已加载 skill `{name}`（后续 analyze / ask / trace 将注入全文）。"

    def skill_unload_session(self, name: str) -> str:
        name = name.strip()
        if name in self._sticky_skill_names:
            self._sticky_skill_names = [x for x in self._sticky_skill_names if x != name]
            self.log_event(f"已移除粘性 skill: {name}")
            return f"已从粘性列表移除 `{name}`（环境变量钉住的 skill 需改启动参数）。"
        return f"粘性列表未包含 `{name}`。"

    def skill_show_loaded(self) -> str:
        merged = self._merged_skill_names_for_prompt()
        if not merged:
            return "（当前无注入全文的 skill）"
        return ", ".join(f"`{x}`" for x in merged)

    def export_analysis_report(self, output_path: str, fmt: str = "md") -> Path:
        """将当前 last_analysis 导出为 Markdown 或 PDF（不含 chunk_index 等内部字段）。"""
        if not self.last_analysis:
            raise ValueError("暂无分析结果，请先执行 analyze。")
        fmt_l = fmt.strip().lower()
        path = Path(output_path).expanduser().resolve()
        raw = self.last_analysis.raw
        src = self.last_analyzed_source_path
        graph = self.last_graph_export_path
        if fmt_l == "md":
            return write_report_markdown(path, raw, source_path=src, graph_html_path=graph)
        if fmt_l == "pdf":
            return write_report_pdf(path, raw, source_path=src, graph_html_path=graph)
        raise ValueError(f"不支持格式 {fmt!r}，请使用 md 或 pdf。")

    def evolve_skills_from_instruction(self, instruction: str) -> Dict[str, Any]:
        """LLM 规划 + skill_manage 执行（Hermes skill_manage 思路的精简版）。"""
        instruction = instruction.strip()
        if not instruction:
            return {"ok": False, "error": "演化指令为空。"}
        catalog = self.skills_catalog()
        patchable = self._patchable_skills_md_payload_for_evolve()
        hist = self._skill_evolution_history_block()
        user_prompt = (
            f"{SKILL_EVOLVE_PROMPT}\n\n=== skills 目录 JSON ===\n"
            f"{json.dumps(catalog, ensure_ascii=False)}\n\n"
            f"=== patchable_skills（SKILL.md 原文，patch 据此构造 find_text）===\n"
            f"{json.dumps(patchable, ensure_ascii=False)}\n\n"
            f"=== 用户指令 ===\n{instruction}\n\n"
            f"=== 多轮对话摘要 ===\n{hist}"
        )
        self._log("Skill 演化：请求模型生成变更计划")
        raw = self._llm_complete("skill_evolve_cli", SKILL_EVOLVE_SYSTEM, user_prompt)
        plan = self._safe_parse_json(
            raw,
            fallback={"action": "noop", "summary": "无法解析模型输出 JSON"},
        )
        action = str(plan.get("action") or "noop").strip().lower()
        summary = str(plan.get("summary") or "").strip()
        if action == "noop":
            return {"ok": True, "applied": False, "plan": plan, "message": summary or "无需变更。"}

        if action == "create":
            name = str(plan.get("target_name") or "").strip()
            content = plan.get("full_skill_md")
            if not isinstance(content, str) or not content.strip():
                return {"ok": False, "error": "create 需要有效的 full_skill_md。", "plan": plan}
            cat = str(plan.get("category") or "general").strip() or "general"
            result = skill_manage(
                "create",
                name=name,
                content=content,
                category=cat,
                root=self.skills_root,
            )
            self.log_event(f"skill evolve create name={name!r} ok={result.get('ok')}")
            out = {"ok": bool(result.get("ok")), "applied": bool(result.get("ok")), "plan": plan, "manage": result}
            if result.get("ok") and name:
                self.skill_load_session(name)
                out["message"] = result.get("message") + " 已自动会话加载。"
            return out

        if action == "patch":
            name = str(plan.get("target_name") or "").strip()
            find_text = plan.get("find_text")
            replace_text = plan.get("replace_text")
            if not isinstance(find_text, str) or not find_text:
                return {"ok": False, "error": "patch 需要 find_text。", "plan": plan}
            if replace_text is None:
                return {"ok": False, "error": "patch 需要 replace_text（可为空字符串）。", "plan": plan}
            file_path = plan.get("file_path")
            fp = str(file_path).strip() if isinstance(file_path, str) and file_path.strip() else None
            result = skill_manage(
                "patch",
                name=name,
                find_text=find_text,
                replace_text=replace_text if isinstance(replace_text, str) else str(replace_text),
                file_path=fp,
                root=self.skills_root,
            )
            self.log_event(f"skill evolve patch name={name!r} ok={result.get('ok')}")
            return {
                "ok": bool(result.get("ok")),
                "applied": bool(result.get("ok")),
                "plan": plan,
                "manage": result,
                "message": result.get("message") or result.get("error"),
            }

        return {"ok": False, "error": f"未知 action: {action!r}", "plan": plan}

    def analyze_script(
        self,
        script_text: str,
        *,
        source_path: Optional[str] = None,
        on_progress: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> AnalysisResult:
        self._log("开始分析剧本")
        self._notify_progress(on_progress, "start", {"script_chars": len(script_text)})
        self.turn_history = []
        self.last_graph_export_path = None
        self.last_auto_report_pdf_path = None
        self.last_auto_report_md_path = None
        self.last_analyzed_source_path = source_path.strip() if source_path else None
        self._log("对话历史已清空（新剧本 analyze）")
        self.last_script = script_text
        try:
            cap = max(500, int(os.getenv("SCRIPT_AGENT_SKILL_AUTOPICK_SCRIPT_CHARS", str(DEFAULT_SKILL_AUTOPICK_SCRIPT_CHARS))))
        except ValueError:
            cap = DEFAULT_SKILL_AUTOPICK_SCRIPT_CHARS
        preview = script_text[:cap]
        ctx = f"剧本总字数约 {len(script_text)}。\n\n开头摘录：\n{preview}"
        self._autopick_skills("analyze_pipeline", ctx)
        self._notify_progress(on_progress, "skills_ready", {})
        self._log("阶段1：核心提取")
        stage_1 = self._run_stage_1(script_text, on_progress=on_progress)
        self._notify_progress(on_progress, "stage_2_start", {})
        self._log("阶段2：观众视角评估")
        stage_2 = self._run_stage_2(stage_1)
        self._notify_progress(on_progress, "stage_2_done", {})
        self._notify_progress(on_progress, "stage_3_start", {})
        self._log("阶段3：不足与改进建议")
        stage_3 = self._run_stage_3(stage_1, stage_2)
        self._notify_progress(on_progress, "stage_3_done", {})
        result = AnalysisResult(raw=self._merge_stage_results(stage_1, stage_2, stage_3))
        self.last_analysis = result
        self._try_export_character_graph_html(result.raw)
        if self.last_graph_export_path:
            self._notify_progress(on_progress, "graph_export", {"path": self.last_graph_export_path})
        self._log("分析完成")
        merged_json = json.dumps(result.raw, ensure_ascii=False)
        try:
            cap_ac = int(
                os.getenv(
                    "SCRIPT_AGENT_SKILL_AUTO_EVOLVE_CONTEXT_CHARS",
                    os.getenv("SCRIPT_AGENT_SKILL_AUTOCREATE_CONTEXT_CHARS", "14000"),
                )
            )
        except ValueError:
            cap_ac = 14000
        cap_ac = max(2000, cap_ac)
        if len(merged_json) > cap_ac:
            merged_json = merged_json[:cap_ac] + "\n…（截断）"
        self._notify_progress(on_progress, "post_analysis_hooks", {})
        self._maybe_auto_evolve_skill(
            "after_analyze",
            f"本轮完整分析结果 JSON：\n{merged_json}",
        )
        self._maybe_auto_report_after_analyze()
        self._notify_progress(on_progress, "complete", {})
        return result

    def _maybe_auto_report_after_analyze(self) -> None:
        """analyze 成功后默认写入 Markdown 报告（当前工作目录）；PDF 需 reportlab。"""
        mode = os.getenv("SCRIPT_AGENT_REPORT_AFTER_ANALYZE", DEFAULT_REPORT_AFTER_ANALYZE).strip().lower()
        if mode in {"off", "0", "false", "no", "none", "-"}:
            return
        stem = (
            Path(self.last_analyzed_source_path).stem
            if self.last_analyzed_source_path
            else "剧本解析报告"
        )
        cwd = Path.cwd()
        try:
            if mode in {"md", "markdown", "1", "true", "yes"}:
                path = cwd / f"{stem}_解析报告.md"
                self.export_analysis_report(str(path), "md")
                self.last_auto_report_md_path = str(path.resolve())
                print(f"解析报告已保存：{path.resolve()}")
                self._log(f"自动解析报告 Markdown {path.resolve()}")
            elif mode == "pdf":
                path_pdf = cwd / f"{stem}_解析报告.pdf"
                path_md = cwd / f"{stem}_解析报告.md"
                try:
                    self.export_analysis_report(str(path_pdf), "pdf")
                    self.last_auto_report_pdf_path = str(path_pdf.resolve())
                    print(f"解析报告已保存：{path_pdf.resolve()}")
                    self._log(f"自动解析报告 PDF {path_pdf.resolve()}")
                except (RuntimeError, ImportError) as exc:
                    err = str(exc).lower()
                    if "reportlab" in err:
                        self.export_analysis_report(str(path_md), "md")
                        self.last_auto_report_md_path = str(path_md.resolve())
                        print(
                            f"未安装 reportlab，无法生成 PDF，已改为 Markdown：{path_md.resolve()}\n"
                            "生成 PDF 请先执行：pip install reportlab"
                        )
                        self._log(f"PDF 不可用，回退 Markdown {path_md.resolve()} ({exc!r})")
                    else:
                        raise
            elif mode == "both":
                p_md = cwd / f"{stem}_解析报告.md"
                p_pdf = cwd / f"{stem}_解析报告.pdf"
                self.export_analysis_report(str(p_md), "md")
                self.last_auto_report_md_path = str(p_md.resolve())
                print(f"解析报告已保存：{p_md.resolve()}")
                try:
                    self.export_analysis_report(str(p_pdf), "pdf")
                    self.last_auto_report_pdf_path = str(p_pdf.resolve())
                    print(f"解析报告已保存：{p_pdf.resolve()}")
                except Exception as pdf_exc:
                    self._log(f"自动 PDF 报告失败（Markdown 已写入）：{pdf_exc!r}")
                    print(f"[警告] PDF 未生成（可 pip install reportlab）：{pdf_exc}")
                self._log(f"自动解析报告 both md={p_md.resolve()}")
            else:
                self._log(f"SCRIPT_AGENT_REPORT_AFTER_ANALYZE 未知取值 {mode!r}，跳过自动报告")
        except Exception as exc:
            self._log(f"自动解析报告失败: {exc!r}")
            print(f"[警告] 自动解析报告失败: {exc}")

    def _try_export_character_graph_html(self, raw: Dict[str, Any]) -> None:
        """analyze 成功后自动导出交互式图谱页面（不新增 CLI 命令）。"""
        configured = os.getenv("SCRIPT_AGENT_GRAPH_HTML", DEFAULT_CHARACTER_GRAPH_HTML).strip()
        if not configured or configured.lower() in {"off", "none", "false", "0", "-"}:
            return
        try:
            from .graph_viz import extract_character_graph, write_character_graph_html
        except ImportError as exc:
            self._log(f"人物关系图谱导出跳过（缺少 graph_viz）: {exc}")
            return
        try:
            graph = extract_character_graph(raw)
            out = write_character_graph_html(graph, configured, title="剧本人物关系图谱")
            self.last_graph_export_path = str(out)
            n_nodes = len(graph.get("nodes") or [])
            n_edges = len(graph.get("edges") or [])
            self._log(f"人物关系图谱已保存 {self.last_graph_export_path}（节点{n_nodes}，边{n_edges}）")
        except Exception as exc:
            self.last_graph_export_path = None
            self._log(f"人物关系图谱导出失败: {exc!r}")

    def _run_stage_1(
        self,
        script_text: str,
        *,
        on_progress: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        strategy = os.getenv("SCRIPT_CHUNK_STRATEGY", DEFAULT_CHUNK_STRATEGY).strip().lower()
        overlap = int(os.getenv("CHUNK_OVERLAP_CHARS", str(DEFAULT_CHUNK_OVERLAP)))
        if strategy == "fixed":
            chunks = chunk_text_fixed(script_text, max_chars=CHUNK_MAX_CHARS)
        else:
            chunks = chunk_script_smart(script_text, max_chars=CHUNK_MAX_CHARS, overlap=overlap)
        self._log(f"阶段1分块完成（strategy={strategy}），共 {len(chunks)} 块")
        self._notify_progress(
            on_progress,
            "stage_1_chunks",
            {"chunk_count": len(chunks), "strategy": strategy},
        )
        if len(chunks) == 1:
            self._notify_progress(on_progress, "stage_1_extract", {"mode": "full"})
            stage_1_full = self._run_stage_1_full(script_text)
            self.last_chunk_records = [
                ChunkRecord(index=1, text=script_text, stage_1_extraction=stage_1_full)
            ]
            self._notify_progress(on_progress, "stage_1_done", {})
            return stage_1_full

        chunk_results: List[Dict[str, Any]] = []
        total = len(chunks)
        self.last_chunk_records = []
        for idx, chunk in enumerate(chunks, start=1):
            self._log(f"阶段1A：处理分块 {idx}/{total}")
            self._notify_progress(on_progress, "stage_1_chunk", {"index": idx, "total": total})
            chunk_result = self._run_stage_1_chunk(chunk, idx, total)
            chunk_results.append(chunk_result)
            self.last_chunk_records.append(
                ChunkRecord(index=idx, text=chunk, stage_1_extraction=chunk_result)
            )
        self._log("阶段1B：汇总所有分块结果")
        self._notify_progress(on_progress, "stage_1_merge", {"chunk_count": total})
        merged = self._merge_stage_1_chunks(chunk_results)
        self._notify_progress(on_progress, "stage_1_done", {})
        return merged

    def _run_stage_1_full(self, script_text: str) -> Dict[str, Any]:
        user_prompt = (
            f"{STEP_1_CORE_EXTRACTION_PROMPT}\n\n=== 剧本文本开始 ===\n{script_text}\n=== 剧本文本结束 ==="
        )
        content = self._llm_complete("stage_1_full", self._effective_system_prompt(), user_prompt)
        parsed = self._safe_parse_json(content, fallback=self._stage_1_fallback("阶段1解析失败"))
        return finalize_stage_1_graph(parsed)

    def _run_stage_1_chunk(self, chunk_text_value: str, chunk_index: int, total_chunks: int) -> Dict[str, Any]:
        user_prompt = (
            f"{STEP_1_CHUNK_EXTRACTION_PROMPT}\n\n=== 当前分块 ===\n"
            f"chunk_index={chunk_index}, total_chunks={total_chunks}\n"
            f"{chunk_text_value}\n=== 分块结束 ==="
        )
        content = self._llm_complete(
            f"stage_1_chunk_{chunk_index}_of_{total_chunks}",
            self._effective_system_prompt(),
            user_prompt,
        )
        parsed = self._safe_parse_json(content, fallback=self._stage_1_fallback(f"阶段1分块{chunk_index}解析失败"))
        return finalize_stage_1_graph(parsed)

    def _merge_stage_1_chunks(self, chunk_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        chunk_results_json = json.dumps(chunk_results, ensure_ascii=False)
        user_prompt = (
            f"{STEP_1_CHUNK_MERGE_PROMPT}\n\n=== 分块提取结果(JSON数组) ===\n{chunk_results_json}"
        )
        content = self._llm_complete("stage_1_merge_chunks", self._effective_system_prompt(), user_prompt)
        parsed = self._safe_parse_json(content, fallback=self._stage_1_fallback("阶段1分块汇总失败"))
        return finalize_stage_1_graph(parsed)

    @staticmethod
    def _stage_1_fallback(reason: str) -> Dict[str, Any]:
        return {
            "core_storyline": "",
            "key_conflicts": [],
            "key_characters_and_relations": [],
            "character_relation_graph": _empty_character_relation_graph(),
            "major_turning_points": [],
            "core_selling_points": [],
            "highlights_hooks_reversals": [],
            "evidence": [],
            "limitations": [reason],
        }

    def _run_stage_2(self, stage_1_result: Dict[str, Any]) -> Dict[str, Any]:
        stage_1_json = json.dumps(stage_1_result, ensure_ascii=False)
        user_prompt = f"{STEP_2_AUDIENCE_EVAL_PROMPT}\n\n=== 阶段1结果 ===\n{stage_1_json}"
        content = self._llm_complete("stage_2_audience_eval", self._effective_system_prompt(), user_prompt)
        return self._safe_parse_json(
            content,
            fallback={
                "overall_watch_worthiness": {
                    "is_worth_continue": "不确定",
                    "confidence": 0,
                    "reason": "阶段2评估失败",
                },
                "dimension_scores": [],
                "where_to_read_next": [],
                "quick_decision": {
                    "is_worth_continue": "不确定",
                    "confidence": 0,
                    "reason": "阶段2评估失败",
                },
            },
        )

    def _run_stage_3(self, stage_1_result: Dict[str, Any], stage_2_result: Dict[str, Any]) -> Dict[str, Any]:
        stage_1_json = json.dumps(stage_1_result, ensure_ascii=False)
        stage_2_json = json.dumps(stage_2_result, ensure_ascii=False)
        user_prompt = (
            f"{STEP_3_IMPROVEMENT_PROMPT}\n\n=== 阶段1结果 ===\n{stage_1_json}\n\n=== 阶段2结果 ===\n{stage_2_json}"
        )
        content = self._llm_complete("stage_3_improvement", self._effective_system_prompt(), user_prompt)
        return self._safe_parse_json(
            content,
            fallback={"top_issues": [{"issue": "阶段3生成失败", "impact": "无法提供改进建议", "severity": "中", "priority": 1}], "improvement_directions": []},
        )

    def _merge_stage_results(
        self,
        stage_1_result: Dict[str, Any],
        stage_2_result: Dict[str, Any],
        stage_3_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "stage_1_core_extraction": stage_1_result,
            "stage_2_audience_evaluation": stage_2_result,
            "stage_3_issues_and_improvements": stage_3_result,
            "quick_decision": stage_2_result.get(
                "quick_decision",
                {
                    "is_worth_continue": "不确定",
                    "confidence": 0,
                    "reason": "暂无有效决策信息",
                },
            ),
            "highlights_hooks_reversals": stage_1_result.get("highlights_hooks_reversals", []),
            "evidence": stage_1_result.get("evidence", []),
            "limitations": stage_1_result.get("limitations", []),
            "character_relation_graph": stage_1_result.get(
                "character_relation_graph", _empty_character_relation_graph()
            ),
            "chunk_index": [
                {
                    "chunk_index": record.index,
                    "preview": record.text[:120],
                    "stage_1_core_storyline": record.stage_1_extraction.get("core_storyline", ""),
                }
                for record in self.last_chunk_records
            ],
        }

    def answer_followup(self, question: str) -> str:
        if not self.last_analysis:
            return "请先执行 analyze，再进行追问。"
        self._log("开始处理追问")
        analysis_json = json.dumps(self.last_analysis.raw, ensure_ascii=False)
        history_block = self._format_turn_history_block()
        pick_ctx = (
            f"用户追问：\n{question}\n\n历史对话摘要：\n{history_block}\n\n"
            f"已有分析 JSON 长度：{len(analysis_json)} 字符（此处不重复全文）。"
        )
        self._autopick_skills("followup_ask", pick_ctx)
        decision = self._decide_retrieval(question, analysis_json, history_block)
        self._log(f"工具决策详情 ask {json.dumps(decision, ensure_ascii=False)}")
        chunk_context = "本轮未调用 original_text_retrieval 工具。"
        if decision.get("need_retrieval"):
            retrieval_query = str(decision.get("query") or question)
            self._log(f"调用工具 original_text_retrieval top_k=3 query={retrieval_query!r}")
            retrieved_chunks = self._tool_original_text_retrieval(retrieval_query, top_k=3)
            idx_list = [r.index for r in retrieved_chunks]
            self._log(f"工具 original_text_retrieval 结果：命中 {len(retrieved_chunks)} 块 chunk_index={idx_list}")
            chunk_context = self._format_chunk_context(retrieved_chunks)
        else:
            self._log("工具 original_text_retrieval：未调用（need_retrieval=false）")
        user_prompt = (
            f"{FOLLOWUP_QA_PROMPT}\n\n=== 已有分析 ===\n{analysis_json}\n\n"
            f"=== 历史对话 ===\n{history_block}\n\n"
            f"=== 工具决策 ===\n{json.dumps(decision, ensure_ascii=False)}\n\n"
            f"=== 检索命中的原文分块 ===\n{chunk_context}\n\n"
            f"=== 用户追问 ===\n{question}"
        )
        reply = self._llm_complete("ask_followup", self._effective_system_prompt(), user_prompt)
        self._record_turn("ask", question, reply)
        self._maybe_auto_evolve_skill(
            "after_ask",
            f"用户追问：\n{question}\n\n助手回复：\n{reply}\n",
        )
        return reply

    def trace_claim(self, claim: str) -> Dict[str, Any]:
        if not self.last_script:
            return {"error": "请先提供剧本并完成一次分析。"}
        self._log(f"开始证据验证：{claim}")
        analysis_json = json.dumps(self.last_analysis.raw if self.last_analysis else {}, ensure_ascii=False)
        history_block = self._format_turn_history_block()
        pick_ctx = (
            f"待验证判断：\n{claim}\n\n历史对话摘要：\n{history_block}\n\n"
            f"已有分析 JSON 长度：{len(analysis_json)} 字符。"
        )
        self._autopick_skills("evidence_trace", pick_ctx)
        decision = self._decide_retrieval(claim, analysis_json, history_block)
        self._log(f"工具决策详情 trace {json.dumps(decision, ensure_ascii=False)}")
        script_context = self.last_script
        if decision.get("need_retrieval"):
            retrieval_query = str(decision.get("query") or claim)
            self._log(f"调用工具 original_text_retrieval top_k=4 query={retrieval_query!r}")
            retrieved_chunks = self._tool_original_text_retrieval(retrieval_query, top_k=4)
            if retrieved_chunks:
                idx_list = [r.index for r in retrieved_chunks]
                self._log(f"工具 original_text_retrieval 结果：命中 {len(retrieved_chunks)} 块 chunk_index={idx_list}")
                script_context = self._format_chunk_context(retrieved_chunks)
            else:
                self._log("工具 original_text_retrieval：无命中分块，证据上下文回退为全文")
        else:
            self._log("工具 original_text_retrieval：未调用（need_retrieval=false），证据上下文为全文")
        user_prompt = (
            f"{EVIDENCE_TRACE_PROMPT}\n\n=== 工具决策 ===\n{json.dumps(decision, ensure_ascii=False)}\n\n"
            f"=== 历史对话 ===\n{history_block}\n\n"
            f"=== 剧本文本（检索片段） ===\n{script_context}\n\n=== 待验证判断 ===\n{claim}"
        )
        content = self._llm_complete("trace_claim", self._effective_system_prompt(), user_prompt)
        result = self._safe_parse_json(content, fallback={"claim": claim, "verdict": "证据不足"})
        self._record_turn("trace", claim, json.dumps(result, ensure_ascii=False))
        self._maybe_auto_evolve_skill(
            "after_trace",
            f"待验证判断：\n{claim}\n\n取证结果：\n{json.dumps(result, ensure_ascii=False)}\n",
        )
        return result

    def _history_max_rounds(self) -> int:
        try:
            return max(0, int(os.getenv("SCRIPT_AGENT_HISTORY_MAX_ROUNDS", str(DEFAULT_HISTORY_MAX_ROUNDS))))
        except ValueError:
            return DEFAULT_HISTORY_MAX_ROUNDS

    def _history_max_block_chars(self) -> int:
        try:
            return max(500, int(os.getenv("SCRIPT_AGENT_HISTORY_BLOCK_CHARS", str(DEFAULT_HISTORY_BLOCK_CHARS))))
        except ValueError:
            return DEFAULT_HISTORY_BLOCK_CHARS

    def _record_turn(self, channel: str, user_text: str, assistant_text: str) -> None:
        self.turn_history.append({"channel": channel, "user": user_text, "assistant": assistant_text})
        self._trim_turn_history()
        self._log(f"会话历史已记录 {channel}，当前轮数={len(self.turn_history)}")

    def _trim_turn_history(self) -> None:
        max_r = self._history_max_rounds()
        if max_r <= 0:
            self.turn_history = []
            return
        while len(self.turn_history) > max_r:
            self.turn_history.pop(0)

    def _format_turn_history_block(self) -> str:
        if not self.turn_history:
            return "（尚无历史对话）"
        try:
            per_reply_cap = max(200, int(os.getenv("SCRIPT_AGENT_HISTORY_REPLY_CHARS", "3500")))
        except ValueError:
            per_reply_cap = 3500
        lines: List[str] = []
        for i, turn in enumerate(self.turn_history, start=1):
            ans = turn["assistant"]
            if len(ans) > per_reply_cap:
                ans = ans[:per_reply_cap] + "…（已截断）"
            lines.append(f"[第{i}轮 {turn['channel']}] 用户：{turn['user']}")
            lines.append(f"助手：{ans}")
        text = "\n".join(lines)
        max_block = self._history_max_block_chars()
        if len(text) > max_block:
            return "…（历史较长，仅保留末尾）\n" + text[-max_block:]
        return text

    def _decide_retrieval(self, user_query: str, analysis_json: str, history_block: str = "") -> Dict[str, Any]:
        # 历史前置，避免超长 analysis 淹没对话上下文
        user_prompt = (
            f"{RETRIEVAL_DECISION_PROMPT}\n\n"
            f"=== 历史对话摘要 ===\n{history_block or '（无）'}\n\n"
            f"=== 当前用户问题/判断 ===\n{user_query}\n\n"
            f"=== 已有结构化分析 ===\n{analysis_json}"
        )
        content = self._llm_complete("retrieval_decision", self._effective_system_prompt(), user_prompt)
        return self._safe_parse_json(
            content,
            fallback={
                "need_retrieval": True,
                "query": user_query,
                "reason": "决策解析失败，默认调用检索工具以保证证据充分性。",
            },
        )

    def _tool_original_text_retrieval(self, query: str, top_k: int = 3) -> List[ChunkRecord]:
        """Tool: original_text_retrieval."""
        return self._retrieve_relevant_chunks(query, top_k=top_k)

    def _retrieve_relevant_chunks(self, query: str, top_k: int = 3) -> List[ChunkRecord]:
        if not self.last_chunk_records:
            return []
        terms = self._extract_query_terms(query)
        scored: List[tuple[int, ChunkRecord]] = []
        for record in self.last_chunk_records:
            text = record.text
            stage_1_text = json.dumps(record.stage_1_extraction, ensure_ascii=False)
            score = 0
            for term in terms:
                score += text.count(term) * 3
                score += stage_1_text.count(term)
            if score > 0:
                scored.append((score, record))
        if not scored:
            return self.last_chunk_records[:top_k]
        scored.sort(key=lambda item: item[0], reverse=True)
        return [record for _, record in scored[:top_k]]

    @staticmethod
    def _extract_query_terms(query: str) -> List[str]:
        terms = re.findall(r"[A-Za-z0-9_\u4e00-\u9fff]{2,}", query)
        # 去重同时保持顺序
        return list(dict.fromkeys(terms))

    @staticmethod
    def _format_chunk_context(records: List[ChunkRecord], max_chars_each: int = 1500) -> str:
        if not records:
            return "未检索到分块。"
        parts: List[str] = []
        for record in records:
            excerpt = record.text[:max_chars_each]
            parts.append(
                f"[chunk_{record.index}]\n{excerpt}\n"
            )
        return "\n".join(parts)

    def log_event(self, message: str) -> None:
        """记录一条操作日志（写入日志文件；是否在终端打印由 SCRIPT_AGENT_VERBOSE 决定）。"""
        self._log(message)

    def _log(self, message: str) -> None:
        self._write_log_file(message)
        if self.verbose:
            print(f"[agent] {message}")

    def _write_log_file(self, message: str) -> None:
        if not self._log_file:
            return
        ts = datetime.now().isoformat(timespec="seconds")
        self._log_file.write(f"[{ts}] {message}\n")
        self._log_file.flush()

    def _write_log_file_block(self, headline: str, body: str) -> None:
        """写入一块多行内容（仅首行带时间戳，便于阅读完整模型输出）。"""
        if not self._log_file:
            return
        ts = datetime.now().isoformat(timespec="seconds")
        self._log_file.write(f"[{ts}] {headline}\n")
        self._log_file.write(body)
        if not body.endswith("\n"):
            self._log_file.write("\n")
        self._log_file.flush()

    def _llm_complete(self, label: str, system_prompt: str, user_prompt: str) -> str:
        """调用 LLM，并把本轮原始返回写入日志文件（由环境变量控制开关与长度上限）。"""
        self._log(f"LLM 调用开始 label={label!r}")
        raw = self.llm.complete(
            system_prompt,
            user_prompt,
            openai_api_key=self._llm_api_key_override,
            openai_base_url=self._llm_base_url_override,
            openai_model=self._llm_model_override,
            mode_override=self._llm_mode_override,
        )
        self._log_llm_raw_response(label, raw)
        return raw

    def _log_llm_raw_response(self, label: str, raw: str) -> None:
        if not self._log_file:
            return
        if os.getenv("SCRIPT_AGENT_LOG_LLM_RESPONSES", "1").strip().lower() in {
            "0",
            "false",
            "no",
            "off",
        }:
            return
        try:
            max_chars = int(os.getenv("SCRIPT_AGENT_LOG_LLM_RESPONSE_MAX_CHARS", "200000"))
        except ValueError:
            max_chars = 200000
        note = ""
        body = raw
        if max_chars > 0 and len(raw) > max_chars:
            body = raw[:max_chars]
            note = (
                f" truncated_full_len={len(raw)} "
                f"SCRIPT_AGENT_LOG_LLM_RESPONSE_MAX_CHARS={max_chars}"
            )
        headline = f"LLM 原始返回 [{label}] response_chars={len(raw)}{note}"
        self._write_log_file_block(headline, f"---BEGIN---\n{body}\n---END---")

    def _atexit_close_log(self) -> None:
        if not self._log_file:
            return
        try:
            ts = datetime.now().isoformat(timespec="seconds")
            self._log_file.write(f"[{ts}] ===== session end =====\n")
            self._log_file.close()
        except Exception:
            pass
        finally:
            self._log_file = None

    @staticmethod
    def _extract_outer_json_object(s: str) -> Optional[str]:
        """从带前言/后记的文本中取出第一个平衡的 JSON 对象文本（忽略双引号字符串内的花括号）。"""
        start = s.find("{")
        if start < 0:
            return None
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(s)):
            ch = s[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[start : i + 1]
        return None

    @staticmethod
    def _llm_json_candidate_strings(text: str) -> List[str]:
        """模型常输出 ```json ... ``` 或前后多余说明；依次尝试多种剥离后的子串。"""
        raw = (text or "").strip()
        out: List[str] = []
        seen: set[str] = set()

        def push(x: str) -> None:
            x = x.strip()
            if x and x not in seen:
                seen.add(x)
                out.append(x)

        push(raw)
        m = re.search(r"```(?:json|JSON)?\s*\n?([\s\S]*?)```", raw)
        if m:
            push(m.group(1))
        for base in list(out):
            sliced = ScriptAnalysisAgent._extract_outer_json_object(base)
            if sliced:
                push(sliced)
        return out

    @staticmethod
    def _safe_parse_json(text: str, fallback: Dict[str, Any]) -> Dict[str, Any]:
        for blob in ScriptAnalysisAgent._llm_json_candidate_strings(text):
            try:
                parsed = json.loads(blob)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue
        return fallback

    @staticmethod
    def pretty_json(data: Dict[str, Any]) -> str:
        return json.dumps(data, ensure_ascii=False, indent=2)


def chunk_text_fixed(text: str, max_chars: int = 12000) -> List[str]:
    """按固定字数顺序切块（可能在句中截断）。"""
    if len(text) <= max_chars:
        return [text]
    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        chunks.append(text[start:end])
        start = end
    return chunks


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _split_by_scene_markers(text: str) -> List[str]:
    """按常见中英剧本场次/场景头切分；若无足够标记则返回空列表交由段落路径处理。"""
    pat = re.compile(
        r"^[\s　]*(?:"
        r"第[一二三四五六七八九十百千万0-9]{1,8}场"
        r"|第\d{1,6}场"
        r"|场次\s*[：:]"
        r"|【[^】\n]{0,40}场[^】\n]{0,40}】"
        r"|第[一二三四五六七八九十0-9]{1,6}幕"
        r"|(?:INT\.|EXT\.)\s"
        r"|SCENE\s+\d+"
        r"|场\s*\d+\s*[：:]"
        r")",
        re.MULTILINE,
    )
    starts = [m.start() for m in pat.finditer(text)]
    if len(starts) < 2:
        return []
    segments: List[str] = []
    if starts[0] > 0:
        head = text[: starts[0]].strip()
        if head:
            segments.append(head)
    for i, s in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        piece = text[s:end].strip()
        if piece:
            segments.append(piece)
    return segments


def _split_paragraphs(text: str) -> List[str]:
    parts = re.split(r"\n\s*\n+", text)
    return [p.strip() for p in parts if p.strip()]


def _merge_units_to_max(units: List[str], max_chars: int) -> List[str]:
    """把较小单元合并到不超过 max_chars。"""
    if not units:
        return []
    out: List[str] = []
    buf = ""
    sep = "\n\n"
    for u in units:
        if not buf:
            buf = u
        elif len(buf) + len(sep) + len(u) <= max_chars:
            buf = buf + sep + u
        else:
            out.append(buf)
            buf = u
    if buf:
        out.append(buf)
    return out


def _split_oversized_with_overlap(segment: str, max_chars: int, overlap: int) -> List[str]:
    if len(segment) <= max_chars:
        return [segment]
    chunks: List[str] = []
    start = 0
    while start < len(segment):
        end = min(start + max_chars, len(segment))
        chunks.append(segment[start:end])
        if end >= len(segment):
            break
        start = max(end - overlap, start + 1)
    return chunks


def chunk_script_smart(text: str, max_chars: int, overlap: int = DEFAULT_CHUNK_OVERLAP) -> List[str]:
    """
    格式不一致时的自适应分块：
    1) 先试场次/场景类标题切分；
    2) 否则按空行段落切分；
    3) 合并过小段落直到接近 max_chars；
    4) 仍超长的块再按字数重叠切开。
    """
    text = _normalize_newlines(text).strip()
    if len(text) <= max_chars:
        return [text]

    units = _split_by_scene_markers(text)
    if not units:
        units = _split_paragraphs(text)
    if not units:
        units = [text]

    merged = _merge_units_to_max(units, max_chars)
    final: List[str] = []
    for m in merged:
        final.extend(_split_oversized_with_overlap(m, max_chars, max(0, overlap)))
    return final if final else [text]


# 兼容旧名：固定字数切块
def chunk_text(text: str, max_chars: int = 12000) -> List[str]:
    return chunk_text_fixed(text, max_chars=max_chars)
