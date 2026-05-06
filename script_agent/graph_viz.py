"""
人物关系图谱可视化：生成交互式 HTML（vis-network CDN），无需额外 pip 依赖。
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Dict, List


NODE_COLORS: Dict[str, Dict[str, str]] = {
    "Character": {"background": "#5B8FF9", "border": "#3D6BC7", "highlight": "#7BA3FA"},
    "FamilyUnit": {"background": "#61DDAA", "border": "#3BB884", "highlight": "#8AE8C4"},
    "Organization": {"background": "#F6BD16", "border": "#C99410", "highlight": "#FFD666"},
    "Location": {"background": "#9270CA", "border": "#6B4FA3", "highlight": "#B094E0"},
    "StoryElement": {"background": "#FF9845", "border": "#D97230", "highlight": "#FFB380"},
    "_default": {"background": "#8C8C8C", "border": "#595959", "highlight": "#BFBFBF"},
}


def extract_character_graph(analysis_raw: Dict[str, Any]) -> Dict[str, Any]:
    """从 analyze 完整结果中提取 character_relation_graph。"""
    g = analysis_raw.get("character_relation_graph")
    if isinstance(g, dict):
        return g
    s1 = analysis_raw.get("stage_1_core_extraction") or {}
    g2 = s1.get("character_relation_graph")
    if isinstance(g2, dict):
        return g2
    return {"nodes": [], "edges": []}


def _node_color(entity_type: str) -> Dict[str, str]:
    return NODE_COLORS.get(entity_type or "", NODE_COLORS["_default"])


def build_vis_payload(graph: Dict[str, Any]) -> Dict[str, Any]:
    """将 character_relation_graph 转为 vis-network 可用的 nodes / edges 列表。"""
    nodes_in = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    edges_in = graph.get("edges") if isinstance(graph.get("edges"), list) else []

    vis_nodes: List[Dict[str, Any]] = []
    for n in nodes_in:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id", "")).strip()
        if not nid:
            continue
        name = str(n.get("name", "")).strip() or nid
        et = str(n.get("entity_type", "Character")).strip() or "Character"
        summary = str(n.get("summary", "")).strip()
        aliases = n.get("aliases") if isinstance(n.get("aliases"), list) else []
        tags = n.get("role_tags") if isinstance(n.get("role_tags"), list) else []
        tooltip_lines = [f"<b>{html.escape(name)}</b>", f"类型: {html.escape(et)}"]
        if tags:
            tooltip_lines.append("标签: " + html.escape(", ".join(str(t) for t in tags)))
        if aliases:
            tooltip_lines.append("别名: " + html.escape(", ".join(str(a) for a in aliases)))
        if summary:
            tooltip_lines.append(html.escape(summary))
        colors = _node_color(et)
        vis_nodes.append(
            {
                "id": nid,
                "label": name[:28] + ("…" if len(name) > 28 else ""),
                "title": "<br/>".join(tooltip_lines),
                "color": colors,
                "font": {"color": "#ffffff" if et == "Character" else "#1f1f1f"},
            }
        )

    vis_edges: List[Dict[str, Any]] = []
    for i, e in enumerate(edges_in):
        if not isinstance(e, dict):
            continue
        sid = str(e.get("source_id", "")).strip()
        tid = str(e.get("target_id", "")).strip()
        if not sid or not tid:
            continue
        rt = str(e.get("relation_type", "")).strip() or "RELATED"
        fact = str(e.get("fact", "")).strip()
        quote = str(e.get("evidence_quote", "")).strip()
        directed = e.get("directed")
        if not isinstance(directed, bool):
            directed = True
        tip_parts = [f"<b>{html.escape(rt)}</b>"]
        if fact:
            tip_parts.append(html.escape(fact))
        if quote:
            tip_parts.append("原文: " + html.escape(quote[:500]))
        edge: Dict[str, Any] = {
            "id": f"e{i}",
            "from": sid,
            "to": tid,
            "label": rt[:18] + ("…" if len(rt) > 18 else ""),
            "title": "<br/>".join(tip_parts),
            "font": {"align": "middle", "size": 11},
            "color": {"color": "#848484", "highlight": "#333"},
        }
        if directed:
            edge["arrows"] = "to"
        vis_edges.append(edge)

    return {"nodes": vis_nodes, "edges": vis_edges}


def write_character_graph_html(
    graph: Dict[str, Any],
    output_path: str | Path,
    *,
    title: str = "人物关系图谱",
) -> Path:
    """
    写入独立 HTML，使用浏览器打开即可拖拽、缩放、点击查看 Tooltip。
    """
    path = Path(output_path)
    payload = build_vis_payload(graph)
    json_text = json.dumps(payload, ensure_ascii=False)
    json_text = json_text.replace("</script>", "<\\/script>")
    json_text = json_text.replace("</Script>", "<\\/Script>")

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(title)}</title>
  <script type="text/javascript" src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "PingFang SC", "Microsoft YaHei", sans-serif;
      background: #f5f5f5;
      color: #262626;
    }}
    header {{
      padding: 12px 16px;
      background: #fff;
      border-bottom: 1px solid #e8e8e8;
    }}
    header h1 {{ margin: 0; font-size: 18px; font-weight: 600; }}
    header p {{ margin: 6px 0 0; font-size: 13px; color: #8c8c8c; }}
    #network {{
      width: 100%;
      height: calc(100vh - 72px);
      background: #fafafa;
    }}
  </style>
</head>
<body>
  <header>
    <h1>{html.escape(title)}</h1>
    <p>拖拽画布平移 · 滚轮缩放 · 悬停节点/边查看详情 · 物理布局自动稳定后可按住节点拖动</p>
  </header>
  <div id="network"></div>
  <script type="application/json" id="graph-payload">{json_text}</script>
  <script type="text/javascript">
    (function () {{
      var el = document.getElementById("graph-payload");
      var payload = JSON.parse(el.textContent);
      var container = document.getElementById("network");
      var nodes = new vis.DataSet(payload.nodes);
      var edges = new vis.DataSet(payload.edges);
      var data = {{ nodes: nodes, edges: edges }};
      var options = {{
        nodes: {{
          shape: "dot",
          size: 22,
          font: {{ size: 14, face: "PingFang SC, Microsoft YaHei, sans-serif" }}
        }},
        edges: {{
          smooth: {{ type: "continuous" }},
          font: {{ strokeWidth: 0, background: "rgba(255,255,255,0.85)" }}
        }},
        physics: {{
          enabled: true,
          stabilization: {{ iterations: 200 }},
          barnesHut: {{
            gravitationalConstant: -2800,
            centralGravity: 0.35,
            springLength: 180,
            springConstant: 0.06
          }}
        }},
        interaction: {{ hover: true, tooltipDelay: 120 }}
      }};
      var network = new vis.Network(container, data, options);
      network.once("stabilizationIterationsDone", function () {{
        network.setOptions({{ physics: false }});
      }});
    }})();
  </script>
</body>
</html>
"""
    path.write_text(html_doc, encoding="utf-8")
    return path.resolve()
