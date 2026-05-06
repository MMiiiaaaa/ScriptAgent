"""剧本解析 Agent — HTTP API（便于云上部署与演示）。

模型密钥有两种用法（可同时支持）：
1. **服务端环境变量**：`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`，全员共用。
2. **访客自备密钥**：请求头 `X-OpenAI-API-Key`（及可选 `X-OpenAI-Base-URL`、`X-OpenAI-Model`、
   `X-Script-Agent-Mode`），写入对应会话；GitHub 部署对外演示时可不在服务端配置 Key，由网页填写。

启动示例：
  export SCRIPT_AGENT_MODE=real
  uvicorn web.server:app --host 0.0.0.0 --port 8000

可选鉴权：`SCRIPT_AGENT_API_KEY` — 所有 `/api/*` 需 `Authorization: Bearer <值>`（内置页提供输入框）。

会话：首次 analyze 可省略 session_id（服务端新建）；后续 ask/trace 需带上同一 session_id。
"""

from __future__ import annotations

import json
import os
import queue
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from script_agent.agent import LLMClient, ScriptAnalysisAgent

_llm = LLMClient()
_sessions: Dict[str, ScriptAnalysisAgent] = {}
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(
    title="剧本解析 Agent API",
    description="云上访问 analyze / ask / trace；与 script_agent 包共用核心逻辑。",
    version="1.0.0",
)

_origins_raw = os.getenv("SCRIPT_AGENT_CORS_ORIGINS", "*").strip()
_cors_origins = [o.strip() for o in _origins_raw.split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _existing_report_path(raw: Optional[str]) -> Optional[Path]:
    """校验 Agent 记录的报告路径仍存在且为文件。"""
    if not raw or not isinstance(raw, str):
        return None
    try:
        p = Path(raw).expanduser().resolve()
    except (OSError, ValueError):
        return None
    return p if p.is_file() else None


def _sync_agent_openai_headers(
    agent: ScriptAnalysisAgent,
    api_key: Optional[str],
    base_url: Optional[str],
    model: Optional[str],
    mode: Optional[str],
) -> None:
    """将请求头中的 OpenAI 兼容参数写入会话（未传的项不修改已有覆盖）。"""
    agent.configure_llm_provider(api_key=api_key, base_url=base_url, model=model, mode=mode)


def openai_headers(
    x_openai_api_key: Optional[str] = Header(None, alias="X-OpenAI-API-Key"),
    x_openai_base_url: Optional[str] = Header(None, alias="X-OpenAI-Base-URL"),
    x_openai_model: Optional[str] = Header(None, alias="X-OpenAI-Model"),
    x_script_agent_mode: Optional[str] = Header(None, alias="X-Script-Agent-Mode"),
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    return x_openai_api_key, x_openai_base_url, x_openai_model, x_script_agent_mode


def _require_api_key(authorization: Optional[str]) -> None:
    expected = os.getenv("SCRIPT_AGENT_API_KEY", "").strip()
    if not expected:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="需要 Header：Authorization: Bearer <token>")
    token = authorization[len("Bearer ") :].strip()
    if token != expected:
        raise HTTPException(status_code=403, detail="无效的 Bearer token")


def _get_or_create_agent(session_id: Optional[str]) -> tuple[str, ScriptAnalysisAgent]:
    if session_id:
        agent = _sessions.get(session_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="未知的 session_id，请先 POST /api/session 或携带上次返回的 session_id")
        return session_id, agent
    sid = str(uuid.uuid4())
    _sessions[sid] = ScriptAnalysisAgent(llm=_llm)
    return sid, _sessions[sid]


class SessionCreateResponse(BaseModel):
    session_id: str


class AnalyzeRequest(BaseModel):
    script_text: str = Field(..., description="完整剧本文本")
    source_path: Optional[str] = Field(None, description="可选：虚拟文件名，用于报告标题等")
    session_id: Optional[str] = Field(None, description="可选：已有会话；省略则新建会话")


class AnalyzeResponse(BaseModel):
    session_id: str
    analysis: Dict[str, Any]
    graph_export_path: Optional[str] = None
    report_pdf_ready: bool = False
    report_md_ready: bool = False


class AskRequest(BaseModel):
    session_id: str
    question: str


class AskResponse(BaseModel):
    reply: str


class TraceRequest(BaseModel):
    session_id: str
    claim: str


class TraceResponse(BaseModel):
    result: Dict[str, Any]


class SkillEvolveRequest(BaseModel):
    session_id: str
    instruction: str


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "llm_mode": _llm.mode,
        "auth_required": bool(os.getenv("SCRIPT_AGENT_API_KEY", "").strip()),
        "openai_env_configured": bool(os.getenv("OPENAI_API_KEY", "").strip()),
    }


@app.post("/api/session", response_model=SessionCreateResponse)
def create_session(
    authorization: Optional[str] = Header(None),
    oh: Tuple[Optional[str], Optional[str], Optional[str], Optional[str]] = Depends(openai_headers),
) -> SessionCreateResponse:
    _require_api_key(authorization)
    sid = str(uuid.uuid4())
    agent = ScriptAnalysisAgent(llm=_llm)
    _sync_agent_openai_headers(agent, *oh)
    _sessions[sid] = agent
    return SessionCreateResponse(session_id=sid)


@app.post("/api/analyze", response_model=AnalyzeResponse)
def api_analyze(
    body: AnalyzeRequest,
    authorization: Optional[str] = Header(None),
    oh: Tuple[Optional[str], Optional[str], Optional[str], Optional[str]] = Depends(openai_headers),
) -> AnalyzeResponse:
    _require_api_key(authorization)
    sid_in = (body.session_id or "").strip() or None
    if sid_in:
        sid, agent = _get_or_create_agent(sid_in)
    else:
        sid = str(uuid.uuid4())
        agent = ScriptAnalysisAgent(llm=_llm)
        _sessions[sid] = agent
    _sync_agent_openai_headers(agent, *oh)
    if not body.script_text.strip():
        raise HTTPException(status_code=400, detail="script_text 不能为空")
    try:
        result = agent.analyze_script(body.script_text.strip(), source_path=body.source_path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return AnalyzeResponse(
        session_id=sid,
        analysis=result.raw,
        graph_export_path=agent.last_graph_export_path,
        report_pdf_ready=bool(agent.last_auto_report_pdf_path),
        report_md_ready=bool(agent.last_auto_report_md_path),
    )


@app.post("/api/analyze/stream")
def api_analyze_stream(
    body: AnalyzeRequest,
    authorization: Optional[str] = Header(None),
    oh: Tuple[Optional[str], Optional[str], Optional[str], Optional[str]] = Depends(openai_headers),
) -> StreamingResponse:
    """NDJSON 流式解析：每行一条 JSON，`type` 为 progress | meta | complete | error。"""
    _require_api_key(authorization)
    sid_in = (body.session_id or "").strip() or None
    if not body.script_text.strip():
        raise HTTPException(status_code=400, detail="script_text 不能为空")
    script_text = body.script_text.strip()
    source_path = body.source_path

    if sid_in:
        sid_final, agent = _get_or_create_agent(sid_in)
    else:
        sid_final = str(uuid.uuid4())
        agent = ScriptAnalysisAgent(llm=_llm)
        _sessions[sid_final] = agent
    _sync_agent_openai_headers(agent, *oh)

    def ndjson_iter() -> Iterator[str]:
        yield json.dumps({"type": "meta", "session_id": sid_final}, ensure_ascii=False) + "\n"
        q: "queue.Queue[Optional[str]]" = queue.Queue()
        err_holder: list[Exception] = []

        def on_progress(phase: str, payload: Dict[str, Any]) -> None:
            q.put(
                json.dumps({"type": "progress", "phase": phase, "payload": payload}, ensure_ascii=False)
                + "\n"
            )

        def worker() -> None:
            try:
                agent.analyze_script(script_text, source_path=source_path, on_progress=on_progress)
            except Exception as exc:
                err_holder.append(exc)
            finally:
                q.put(None)

        th = threading.Thread(target=worker, daemon=True)
        th.start()
        while True:
            item = q.get()
            if item is None:
                break
            yield item
        th.join(timeout=7200)

        if err_holder:
            yield json.dumps({"type": "error", "message": str(err_holder[0])}, ensure_ascii=False) + "\n"
            return

        raw = agent.last_analysis.raw if agent.last_analysis else {}
        yield json.dumps(
            {
                "type": "complete",
                "session_id": sid_final,
                "analysis": raw,
                "graph_export_path": agent.last_graph_export_path,
                "report_pdf_ready": bool(agent.last_auto_report_pdf_path),
                "report_md_ready": bool(agent.last_auto_report_md_path),
            },
            ensure_ascii=False,
        ) + "\n"

    return StreamingResponse(
        ndjson_iter(),
        media_type="application/x-ndjson; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/report/pdf")
def api_report_pdf(
    session_id: str = Query(..., description="analyze 返回的会话 id"),
    download: bool = Query(
        False,
        description="为 true 时使用附件下载；默认 inline 供 iframe/标签页内嵌预览",
    ),
    authorization: Optional[str] = Header(None),
) -> FileResponse:
    """本会话最近一次自动导出的解析报告 PDF。默认 inline 展示；download=true 触发下载。"""
    _require_api_key(authorization)
    _, agent = _get_or_create_agent(session_id)
    path = _existing_report_path(getattr(agent, "last_auto_report_pdf_path", None))
    if path is None:
        raise HTTPException(
            status_code=404,
            detail="未找到 PDF（请设置 SCRIPT_AGENT_REPORT_AFTER_ANALYZE=pdf 或 both，并安装 reportlab）",
        )
    disposition = "attachment" if download else "inline"
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/pdf",
        content_disposition_type=disposition,
    )


@app.get("/api/report/md")
def api_report_md(
    session_id: str = Query(..., description="analyze 返回的会话 id"),
    authorization: Optional[str] = Header(None),
) -> FileResponse:
    """下载本会话最近一次自动导出的 Markdown 报告。"""
    _require_api_key(authorization)
    _, agent = _get_or_create_agent(session_id)
    path = _existing_report_path(getattr(agent, "last_auto_report_md_path", None))
    if path is None:
        raise HTTPException(
            status_code=404,
            detail="未找到 Markdown 报告（请设置 SCRIPT_AGENT_REPORT_AFTER_ANALYZE 含 md/markdown/both）",
        )
    return FileResponse(
        path,
        filename=path.name,
        media_type="text/markdown; charset=utf-8",
    )


@app.post("/api/ask", response_model=AskResponse)
def api_ask(
    body: AskRequest,
    authorization: Optional[str] = Header(None),
    oh: Tuple[Optional[str], Optional[str], Optional[str], Optional[str]] = Depends(openai_headers),
) -> AskResponse:
    _require_api_key(authorization)
    _, agent = _get_or_create_agent(body.session_id)
    _sync_agent_openai_headers(agent, *oh)
    if not agent.last_analysis:
        raise HTTPException(status_code=400, detail="该会话尚未执行 analyze，无法追问")
    try:
        reply = agent.answer_followup(body.question.strip())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return AskResponse(reply=reply)


@app.post("/api/trace", response_model=TraceResponse)
def api_trace(
    body: TraceRequest,
    authorization: Optional[str] = Header(None),
    oh: Tuple[Optional[str], Optional[str], Optional[str], Optional[str]] = Depends(openai_headers),
) -> TraceResponse:
    _require_api_key(authorization)
    _, agent = _get_or_create_agent(body.session_id)
    _sync_agent_openai_headers(agent, *oh)
    try:
        result = agent.trace_claim(body.claim.strip())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(status_code=400, detail=str(result.get("error")))
    return TraceResponse(result=result)


@app.post("/api/skill/evolve")
def api_skill_evolve(
    body: SkillEvolveRequest,
    authorization: Optional[str] = Header(None),
    oh: Tuple[Optional[str], Optional[str], Optional[str], Optional[str]] = Depends(openai_headers),
) -> Dict[str, Any]:
    _require_api_key(authorization)
    _, agent = _get_or_create_agent(body.session_id)
    _sync_agent_openai_headers(agent, *oh)
    try:
        return agent.evolve_skills_from_instruction(body.instruction.strip())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/")
def interactive_ui() -> FileResponse:
    """交互式网页（static/index.html）；可用 SCRIPT_AGENT_WEB_UI=0 关闭。"""
    if os.getenv("SCRIPT_AGENT_WEB_UI", "1").strip().lower() in {"0", "false", "no", "off"}:
        raise HTTPException(status_code=404, detail="Web UI 已关闭（SCRIPT_AGENT_WEB_UI）")
    index_path = _STATIC_DIR / "index.html"
    if not index_path.is_file():
        raise HTTPException(status_code=500, detail="缺少 static/index.html")
    return FileResponse(index_path, media_type="text/html; charset=utf-8")


def main() -> None:
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("web.server:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
