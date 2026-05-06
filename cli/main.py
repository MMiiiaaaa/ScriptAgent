from __future__ import annotations

from pathlib import Path

from script_agent.agent import LLMClient, ScriptAnalysisAgent
from script_agent.runtime_paths import resolve_writable_output_path, writable_output_dir


def _default_report_stem(agent: ScriptAnalysisAgent) -> str:
    if agent.last_analyzed_source_path:
        return Path(agent.last_analyzed_source_path).stem + "_解析报告"
    return "剧本解析报告"


def load_script(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def run_cli() -> None:
    print("=== 剧本解析助手 (CLI) ===")
    print(
        "命令: analyze <path> | ask <问题> | trace <判断> | show | "
        "report md [输出路径] | report pdf [输出路径] | skill evolve <指令...> | exit"
    )

    llm = LLMClient()
    agent = ScriptAnalysisAgent(llm=llm)

    while True:
        raw = input("\n> ").strip()
        if not raw:
            continue
        if raw == "exit":
            agent.log_event("CLI exit")
            print("bye.")
            break

        if raw.startswith("analyze "):
            path = raw[len("analyze ") :].strip()
            agent.log_event(f"CLI analyze path={path!r}")
            try:
                script = load_script(path)
                agent.log_event(f"CLI analyze 已读入剧本 chars={len(script)}")
                result = agent.analyze_script(script, source_path=path)
                print(agent.pretty_json(result.raw))
                if agent.last_graph_export_path:
                    print(f"人物关系图谱已保存：{agent.last_graph_export_path}（用浏览器打开）")
            except FileNotFoundError:
                agent.log_event(f"CLI analyze 失败: 文件不存在 path={path!r}")
                print(f"文件不存在: {path}")
            except Exception as exc:
                agent.log_event(f"CLI analyze 异常: {exc!r}")
                print(f"分析失败: {exc}")
            continue

        if raw.startswith("ask "):
            question = raw[len("ask ") :].strip()
            agent.log_event(f"CLI ask question={question!r}")
            print(agent.answer_followup(question))
            continue

        if raw.startswith("trace "):
            claim = raw[len("trace ") :].strip()
            agent.log_event(f"CLI trace claim={claim!r}")
            trace_result = agent.trace_claim(claim)
            print(agent.pretty_json(trace_result))
            continue

        if raw == "show":
            agent.log_event("CLI show")
            if not agent.last_analysis:
                print("暂无分析结果。")
            else:
                print(agent.pretty_json(agent.last_analysis.raw))
            continue

        if raw == "report md" or raw.startswith("report md "):
            rest = raw[len("report md") :].strip()
            if not agent.last_analysis:
                print("请先 analyze，再导出报告。")
                continue
            out = (
                resolve_writable_output_path(rest)
                if rest
                else writable_output_dir() / f"{_default_report_stem(agent)}.md"
            )
            try:
                saved = agent.export_analysis_report(str(out), "md")
                agent.log_event(f"CLI report md -> {saved}")
                print(f"解析报告（Markdown）已保存：{saved}")
            except Exception as exc:
                agent.log_event(f"CLI report md 失败: {exc!r}")
                print(f"导出失败: {exc}")
            continue

        if raw == "report pdf" or raw.startswith("report pdf "):
            rest = raw[len("report pdf") :].strip()
            if not agent.last_analysis:
                print("请先 analyze，再导出报告。")
                continue
            out = (
                resolve_writable_output_path(rest)
                if rest
                else writable_output_dir() / f"{_default_report_stem(agent)}.pdf"
            )
            try:
                saved = agent.export_analysis_report(str(out), "pdf")
                agent.log_event(f"CLI report pdf -> {saved}")
                print(f"解析报告（PDF）已保存：{saved}")
            except (RuntimeError, ImportError) as exc:
                err = str(exc).lower()
                if "reportlab" in err:
                    fallback = out.with_suffix(".md") if out.suffix.lower() == ".pdf" else out
                    try:
                        saved = agent.export_analysis_report(str(fallback), "md")
                        agent.log_event(f"CLI report pdf 回退 md -> {saved}")
                        print(
                            f"未安装 reportlab，已改为 Markdown：{saved}\n"
                            "安装依赖后可导出 PDF：pip install reportlab"
                        )
                    except Exception as exc2:
                        agent.log_event(f"CLI report pdf 回退失败: {exc2!r}")
                        print(f"导出失败: {exc2}")
                else:
                    agent.log_event(f"CLI report pdf 失败: {exc!r}")
                    print(f"导出失败: {exc}")
            except Exception as exc:
                agent.log_event(f"CLI report pdf 失败: {exc!r}")
                print(f"导出失败: {exc}")
            continue

        if raw.startswith("skill evolve "):
            instr = raw[len("skill evolve ") :].strip()
            agent.log_event(f"CLI skill evolve instr={instr!r}")
            out = agent.evolve_skills_from_instruction(instr)
            print(agent.pretty_json(out))
            continue

        agent.log_event(f"CLI 未知命令 raw={raw!r}")
        print(
            "未知命令。可用: analyze <path> | ask <问题> | trace <判断> | show | "
            "report md [路径] | report pdf [路径] | skill evolve <指令...> | exit"
        )


if __name__ == "__main__":
    run_cli()
