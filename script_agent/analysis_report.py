"""从 merge 后的分析结果生成面向阅读者的报告（Markdown / 可选 PDF）。

不包含 chunk_index 等实现细节，只保留决策与理解剧本所需信息。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple


def _s(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, (dict, list)):
        return ""
    return str(x).strip()


def _lines(blocks: List[str]) -> str:
    return "\n\n".join(b for b in blocks if b.strip())


def _fmt_quick_decision(raw: Dict[str, Any]) -> str:
    qd = raw.get("quick_decision") or {}
    lines = [
        f"- **是否值得继续看**：{_s(qd.get('is_worth_continue'))}",
        f"- **信心**：{_s(qd.get('confidence'))}",
        f"- **理由**：{_s(qd.get('reason'))}",
    ]
    ow = (raw.get("stage_2_audience_evaluation") or {}).get("overall_watch_worthiness")
    if isinstance(ow, dict) and (ow.get("reason") or ow.get("is_worth_continue")):
        lines.append(
            f"- **综合判断**（与上表可能一致）：{_s(ow.get('is_worth_continue'))}，"
            f"信心 {_s(ow.get('confidence'))} — {_s(ow.get('reason'))}"
        )
    return "\n".join(lines)


def _fmt_stage1_core(s1: Dict[str, Any]) -> str:
    parts: List[str] = []
    if _s(s1.get("core_storyline")):
        parts.append(s1["core_storyline"])
    kc = s1.get("key_conflicts")
    if isinstance(kc, list) and kc:
        parts.append("**关键冲突**\n" + "\n".join(f"- {_s(c)}" for c in kc if _s(c)))
    mtp = s1.get("major_turning_points")
    if isinstance(mtp, list) and mtp:
        rows = []
        for item in mtp:
            if not isinstance(item, dict):
                continue
            rows.append(
                f"- **{_s(item.get('turning_point'))}** — 影响：{_s(item.get('impact_on_story'))}"
            )
        if rows:
            parts.append("**关键转折**\n" + "\n".join(rows))
    csp = s1.get("core_selling_points")
    if isinstance(csp, list) and csp:
        parts.append("**核心卖点**\n" + "\n".join(f"- {_s(x)}" for x in csp if _s(x)))
    hhr = s1.get("highlights_hooks_reversals")
    if isinstance(hhr, list) and hhr:
        rows = []
        for item in hhr:
            if not isinstance(item, dict):
                continue
            rows.append(
                f"- 【{_s(item.get('type'))}】{_s(item.get('description'))} "
                f"（{_s(item.get('why_it_matters'))}）"
            )
        if rows:
            parts.append("**看点 / 钩子 / 反转**\n" + "\n".join(rows))
    return _lines(parts)


def _fmt_characters(s1: Dict[str, Any]) -> str:
    rows = s1.get("key_characters_and_relations")
    if not isinstance(rows, list) or not rows:
        return ""
    out: List[str] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        name = _s(item.get("name")) or "（未命名）"
        bits = [
            f"**{name}**",
            f"  - 动机：{_s(item.get('motivation'))}",
            f"  - 冲突：{_s(item.get('conflicts'))}",
            f"  - 与主线：{_s(item.get('relation_to_mainline'))}",
        ]
        out.append("\n".join(bits))
    return "\n\n".join(out)


def _fmt_dimensions(s2: Dict[str, Any]) -> str:
    ds = s2.get("dimension_scores")
    if not isinstance(ds, list) or not ds:
        return ""
    lines = []
    for item in ds:
        if not isinstance(item, dict):
            continue
        lines.append(
            f"- **{_s(item.get('dimension'))}**（{_s(item.get('score'))}/10）"
            f" {_s(item.get('judgement'))} — {_s(item.get('evidence_or_reason'))}"
        )
    return "\n".join(lines)


def _fmt_where_read(s2: Dict[str, Any]) -> str:
    w = s2.get("where_to_read_next")
    if not isinstance(w, list) or not w:
        return ""
    lines = []
    for item in w:
        if not isinstance(item, dict):
            continue
        lines.append(f"- **{_s(item.get('goal'))}**：{_s(item.get('suggested_focus'))}")
    return "\n".join(lines)


def _fmt_stage3(s3: Dict[str, Any]) -> str:
    parts: List[str] = []
    ti = s3.get("top_issues")
    if isinstance(ti, list) and ti:
        rows = []
        for item in ti:
            if not isinstance(item, dict):
                continue
            rows.append(
                f"- 【{_s(item.get('severity'))} / P{_s(item.get('priority'))}】{_s(item.get('issue'))} "
                f"— 影响：{_s(item.get('impact'))}"
            )
        if rows:
            parts.append("**主要问题**\n" + "\n".join(rows))
    im = s3.get("improvement_directions")
    if isinstance(im, list) and im:
        rows = []
        for item in im:
            if not isinstance(item, dict):
                continue
            rows.append(
                f"- 针对「{_s(item.get('for_issue'))}」\n"
                f"  - 改什么：{_s(item.get('what_to_change'))}\n"
                f"  - 怎么改：{_s(item.get('how_to_change'))}\n"
                f"  - 预期：{_s(item.get('expected_benefit'))}"
            )
        if rows:
            parts.append("**改进方向**\n" + "\n".join(rows))
    return _lines(parts)


def _fmt_evidence(raw: Dict[str, Any], max_items: int = 10) -> str:
    ev = raw.get("evidence")
    if not isinstance(ev, list) or not ev:
        return ""
    lines = []
    for item in ev[:max_items]:
        if not isinstance(item, dict):
            continue
        lines.append(
            f"- **结论**：{_s(item.get('claim'))}\n"
            f"  - 原文：{_s(item.get('quote'))}\n"
            f"  - 说明：{_s(item.get('note'))}"
        )
    return "\n".join(lines)


def _fmt_limitations(raw: Dict[str, Any]) -> str:
    lim = raw.get("limitations")
    if not isinstance(lim, list) or not lim:
        return ""
    return "\n".join(f"- {_s(x)}" for x in lim if _s(x))


def _fmt_graph_note(raw: Dict[str, Any], graph_html_path: Optional[str]) -> str:
    g = raw.get("character_relation_graph") or {}
    nodes = g.get("nodes") if isinstance(g, dict) else None
    edges = g.get("edges") if isinstance(g, dict) else None
    n_n = len(nodes) if isinstance(nodes, list) else 0
    n_e = len(edges) if isinstance(edges, list) else 0
    if n_n == 0 and not graph_html_path:
        return ""
    lines = [f"- 图谱统计：约 **{n_n}** 个节点，**{n_e}** 条关系。"]
    if graph_html_path:
        lines.append(f"- 交互式人物关系图请打开：`{graph_html_path}`（浏览器）。")
    return "\n".join(lines)


def iter_report_sections(
    raw: Dict[str, Any],
    *,
    source_path: Optional[str] = None,
    graph_html_path: Optional[str] = None,
    generated_at: Optional[str] = None,
) -> Iterator[Tuple[str, str]]:
    """(章节标题, Markdown 正文) — 正文可为空，调用方跳过。"""
    ts = generated_at or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    s1 = raw.get("stage_1_core_extraction") or {}
    s2 = raw.get("stage_2_audience_evaluation") or {}
    s3 = raw.get("stage_3_issues_and_improvements") or {}

    meta_lines = [f"- **生成时间**：{ts}"]
    if source_path:
        meta_lines.append(f"- **剧本文件**：`{source_path}`")
    yield ("报告说明", "\n".join(meta_lines))

    qd = _fmt_quick_decision(raw)
    if qd:
        yield ("快速决策（值不值得看）", qd)

    core = _fmt_stage1_core(s1)
    if core:
        yield ("主线、冲突与看点", core)

    ch = _fmt_characters(s1)
    if ch:
        yield ("关键人物与关系", ch)

    dim = _fmt_dimensions(s2)
    if dim:
        yield ("观众视角七维评估", dim)

    wr = _fmt_where_read(s2)
    if wr:
        yield ("建议重点阅读方向", wr)

    st3 = _fmt_stage3(s3)
    if st3:
        yield ("主要问题与改进方向", st3)

    ev = _fmt_evidence(raw)
    if ev:
        yield ("关键依据摘录", ev)

    lim = _fmt_limitations(raw)
    if lim:
        yield ("局限与不确定", lim)

    gn = _fmt_graph_note(raw, graph_html_path)
    if gn:
        yield ("人物关系图谱", gn)


def build_report_markdown(
    raw: Dict[str, Any],
    *,
    source_path: Optional[str] = None,
    graph_html_path: Optional[str] = None,
    title: Optional[str] = None,
) -> str:
    stem = Path(source_path).stem if source_path else "剧本"
    doc_title = title or f"{stem} — 剧本解析报告"
    parts: List[str] = [f"# {doc_title}", ""]
    for heading, body in iter_report_sections(
        raw,
        source_path=source_path,
        graph_html_path=graph_html_path,
    ):
        if not body.strip():
            continue
        parts.append(f"## {heading}")
        parts.append("")
        parts.append(body.strip())
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def write_report_markdown(
    out_path: Path,
    raw: Dict[str, Any],
    *,
    source_path: Optional[str] = None,
    graph_html_path: Optional[str] = None,
    title: Optional[str] = None,
) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = build_report_markdown(
        raw,
        source_path=source_path,
        graph_html_path=graph_html_path,
        title=title,
    )
    out_path.write_text(text, encoding="utf-8")
    return out_path.resolve()


def _split_paragraphs(text: str) -> List[str]:
    return [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]


def write_report_pdf(
    out_path: Path,
    raw: Dict[str, Any],
    *,
    source_path: Optional[str] = None,
    graph_html_path: Optional[str] = None,
    title: Optional[str] = None,
) -> Path:
    """依赖 reportlab；使用内置华文宋体 CID 字体以支持中文。"""
    try:
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
        from xml.sax.saxutils import escape
    except ImportError as exc:
        raise RuntimeError("导出 PDF 需要安装 reportlab：pip install reportlab") from exc

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    font_name = "STSong-Light"
    pdfmetrics.registerFont(UnicodeCIDFont(font_name))

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        name="RepTitle",
        fontName=font_name,
        fontSize=16,
        leading=22,
        alignment=TA_LEFT,
        spaceAfter=10,
    )
    h_style = ParagraphStyle(
        name="RepH2",
        fontName=font_name,
        fontSize=13,
        leading=18,
        alignment=TA_LEFT,
        spaceBefore=10,
        spaceAfter=6,
    )
    body_style = ParagraphStyle(
        name="RepBody",
        fontName=font_name,
        fontSize=10,
        leading=14,
        alignment=TA_LEFT,
        spaceAfter=4,
    )

    stem = Path(source_path).stem if source_path else "剧本"
    doc_title = title or f"{stem} — 剧本解析报告"

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
    )
    story: List[Any] = []
    story.append(Paragraph(escape(doc_title), title_style))
    story.append(Spacer(1, 4))

    for heading, body in iter_report_sections(
        raw,
        source_path=source_path,
        graph_html_path=graph_html_path,
    ):
        if not body.strip():
            continue
        story.append(Paragraph(escape(heading), h_style))
        for para in _split_paragraphs(body):
            safe = escape(para).replace("\n", "<br/>")
            story.append(Paragraph(safe, body_style))
        story.append(Spacer(1, 4))

    doc.build(story)
    return out_path.resolve()
