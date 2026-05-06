"""Hermes-style skill library for the script analysis agent.

- Skills live under SCRIPT_AGENT_SKILLS_DIR (default: repo-root `script_agent_skills/`).
- Each skill is a directory containing SKILL.md with YAML frontmatter (name, description, …).
- Progressive disclosure: list metadata only; load full SKILL.md (or linked files) on demand.
- Agent-created skills live under user/ (bundled skills under bundled/).
- Usage sidecar: .usage.json (mirrors tools/skill_usage.py idea).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .runtime_paths import is_serverless_readonly_cwd

# --- layout ---
SKILL_MD = "SKILL.md"
BUNDLED_SEGMENT = "bundled"
USER_SEGMENT = "user"
ARCHIVE_DIR = ".archive"
EXCLUDED_TOP = frozenset({".git", ".github", ARCHIVE_DIR, ".hub", "node_modules"})

MAX_NAME_LEN = 64
MAX_DESC_LEN = 1024


def default_skills_root() -> Path:
    raw = os.getenv("SCRIPT_AGENT_SKILLS_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parent.parent / "script_agent_skills"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _usage_path(root: Path) -> Path:
    return root / ".usage.json"


def _load_usage(root: Path) -> Dict[str, Any]:
    p = _usage_path(root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_usage(root: Path, data: Dict[str, Any]) -> None:
    _atomic_write(_usage_path(root), json.dumps(data, ensure_ascii=False, indent=2))


def bump_usage(root: Path, skill_name: str, field: str) -> None:
    """field: view_count | use_count | patch_count"""
    if field not in {"view_count", "use_count", "patch_count"}:
        return
    usage = _load_usage(root)
    rec = usage.get(skill_name)
    if not isinstance(rec, dict):
        rec = {}
    try:
        rec[field] = int(rec.get(field) or 0) + 1
    except (TypeError, ValueError):
        rec[field] = 1
    ts_key = {
        "view_count": "last_viewed_at",
        "use_count": "last_used_at",
        "patch_count": "last_patched_at",
    }[field]
    rec[ts_key] = _now_iso()
    usage[skill_name] = rec
    try:
        _save_usage(root, usage)
    except OSError:
        pass


@dataclass(frozen=True)
class SkillRecord:
    name: str
    description: str
    skill_dir: Path
    relative_skill_md: str
    agent_created: bool

    @property
    def skill_md_path(self) -> Path:
        return self.skill_dir / SKILL_MD


def _simple_frontmatter_parse(block: str) -> Dict[str, str]:
    """Parse flat key: value lines from YAML frontmatter (no nested structures)."""
    out: Dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        val = rest.strip().strip("\"'")
        if key and val:
            out[key] = val
    return out


def split_skill_md(text: str) -> Tuple[Dict[str, str], str]:
    """Return (frontmatter dict, body markdown). Tolerates missing frontmatter."""
    text = text.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    if len(lines) < 2:
        return {}, text
    fm_lines: List[str] = []
    i = 1
    while i < len(lines):
        if lines[i].strip() == "---":
            break
        fm_lines.append(lines[i])
        i += 1
    if i >= len(lines):
        return {}, text
    body = "\n".join(lines[i + 1 :]).lstrip("\n")
    return _simple_frontmatter_parse("\n".join(fm_lines)), body


def validate_skill_md(content: str) -> Optional[str]:
    if not content.strip():
        return "SKILL.md 内容为空。"
    if not content.lstrip().startswith("---"):
        return "SKILL.md 必须以 YAML frontmatter（---）开头。"
    fm, body = split_skill_md(content)
    if not body.strip():
        return "frontmatter 之后需要正文说明。"
    name = fm.get("name", "").strip()
    desc = fm.get("description", "").strip()
    if not name:
        return "frontmatter 缺少 name。"
    if len(name) > MAX_NAME_LEN:
        return f"name 超过 {MAX_NAME_LEN} 字符。"
    if not desc:
        return "frontmatter 缺少 description。"
    if len(desc) > MAX_DESC_LEN:
        return f"description 超过 {MAX_DESC_LEN} 字符。"
    return None


def _is_under(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _safe_join_skill_file(skill_dir: Path, file_path: Optional[str]) -> Optional[Path]:
    if not file_path:
        return skill_dir / SKILL_MD
    rel = Path(file_path)
    if rel.is_absolute() or ".." in rel.parts:
        return None
    candidate = (skill_dir / rel).resolve()
    if not _is_under(skill_dir.resolve(), candidate):
        return None
    return candidate


def iter_skill_records(root: Path) -> List[SkillRecord]:
    root = root.resolve()
    if not root.is_dir():
        return []
    out: List[SkillRecord] = []
    for skill_md in root.rglob(SKILL_MD):
        try:
            rel = skill_md.relative_to(root)
        except ValueError:
            continue
        parts = rel.parts
        if not parts:
            continue
        if parts[0] in EXCLUDED_TOP or parts[0].startswith("."):
            continue
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm, _ = split_skill_md(text)
        name = fm.get("name", "").strip() or skill_md.parent.name
        desc = fm.get("description", "").strip() or ""
        skill_dir = skill_md.parent
        parts_lower = {p.lower() for p in skill_dir.parts}
        if USER_SEGMENT in parts_lower:
            agent_created = True
        elif BUNDLED_SEGMENT in parts_lower:
            agent_created = False
        else:
            agent_created = True
        out.append(
            SkillRecord(
                name=name,
                description=desc[:MAX_DESC_LEN],
                skill_dir=skill_dir,
                relative_skill_md=str(rel).replace("\\", "/"),
                agent_created=agent_created,
            )
        )
    # Dedupe by name (first wins — shallow paths preferred)
    seen: set[str] = set()
    deduped: List[SkillRecord] = []
    for rec in sorted(out, key=lambda r: len(r.skill_dir.parts)):
        if rec.name in seen:
            continue
        seen.add(rec.name)
        deduped.append(rec)
    return sorted(deduped, key=lambda r: r.name.lower())


def skills_list(root: Optional[Path] = None) -> List[Dict[str, Any]]:
    root = root or default_skills_root()
    root_r = root.resolve()
    rows: List[Dict[str, Any]] = []
    for r in iter_skill_records(root):
        try:
            rel_path = str(r.skill_dir.relative_to(root_r)).replace("\\", "/")
        except ValueError:
            rel_path = str(r.skill_dir)
        rows.append(
            {
                "name": r.name,
                "description": r.description,
                "path": rel_path,
                "agent_created": r.agent_created,
            }
        )
    return rows


def find_skill_record(root: Path, name: str) -> Optional[SkillRecord]:
    for r in iter_skill_records(root):
        if r.name == name:
            return r
    return None


def skill_view(
    name: str,
    file_path: Optional[str] = None,
    root: Optional[Path] = None,
    bump_view: bool = True,
) -> Dict[str, Any]:
    root = root or default_skills_root()
    rec = find_skill_record(root, name)
    if not rec:
        return {"ok": False, "error": f"未找到 skill: {name!r}"}
    target = _safe_join_skill_file(rec.skill_dir, file_path)
    if target is None:
        return {"ok": False, "error": "非法 file_path（禁止绝对路径或 ..）。"}
    if not target.is_file():
        return {"ok": False, "error": f"文件不存在: {file_path or SKILL_MD}"}
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    if bump_view:
        bump_usage(root.resolve(), rec.name, "view_count")
    linked_files = _list_linked_files(rec.skill_dir)
    return {
        "ok": True,
        "name": rec.name,
        "file": str(target.relative_to(rec.skill_dir)).replace("\\", "/")
        if target.is_relative_to(rec.skill_dir)
        else SKILL_MD,
        "content": content,
        "linked_files": linked_files,
    }


def _list_linked_files(skill_dir: Path) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {"references": [], "templates": [], "scripts": [], "assets": []}
    for key in out:
        d = skill_dir / key
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if p.is_file() and not p.name.startswith("."):
                try:
                    rel = p.relative_to(skill_dir)
                    out[key].append(str(rel).replace("\\", "/"))
                except ValueError:
                    continue
    return {k: v for k, v in out.items() if v}


def skill_manage(
    action: str,
    *,
    name: str = "",
    content: Optional[str] = None,
    category: str = "user",
    find_text: Optional[str] = None,
    replace_text: Optional[str] = None,
    file_path: Optional[str] = None,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """create | edit | patch — write under user/<category>/<name>/"""
    root = (root or default_skills_root()).resolve()
    action = action.strip().lower()
    if action in {"create", "edit", "patch"} and is_serverless_readonly_cwd():
        return {
            "ok": False,
            "error": "当前运行环境部署目录只读（如 Vercel），无法创建或修改 skill 文件。"
            "请在本地使用 CLI，或改用带持久磁盘的托管方式。",
        }
    if action == "create":
        if not name.strip():
            return {"ok": False, "error": "create 需要 name。"}
        if not content:
            return {"ok": False, "error": "create 需要 content（完整 SKILL.md）。"}
        err = validate_skill_md(content)
        if err:
            return {"ok": False, "error": err}
        fm, _ = split_skill_md(content)
        declared = fm.get("name", "").strip()
        if declared and declared != name.strip():
            return {
                "ok": False,
                "error": f"frontmatter name={declared!r} 与参数 name={name!r} 不一致。",
            }
        safe_cat = re.sub(r"[^a-zA-Z0-9_\-./]", "_", category.strip() or "user")[:80]
        skill_dir = root / USER_SEGMENT / safe_cat / name.strip()
        skill_dir.mkdir(parents=True, exist_ok=True)
        md_path = skill_dir / SKILL_MD
        if md_path.exists():
            return {"ok": False, "error": f"已存在同名 skill 目录: {skill_dir}"}
        _atomic_write(md_path, content)
        bump_usage(root, name.strip(), "patch_count")
        return {"ok": True, "message": f"已创建 skill {name!r}，路径 {skill_dir.relative_to(root)}。"}

    if action == "edit":
        if not name.strip():
            return {"ok": False, "error": "edit 需要 name。"}
        if not content:
            return {"ok": False, "error": "edit 需要 content（完整 SKILL.md）。"}
        err = validate_skill_md(content)
        if err:
            return {"ok": False, "error": err}
        rec = find_skill_record(root, name.strip())
        if not rec:
            return {"ok": False, "error": f"未找到 skill: {name!r}"}
        if not rec.agent_created:
            return {"ok": False, "error": "bundled skill 不可 edit，请用 create 新建 user skill。"}
        _atomic_write(rec.skill_md_path, content)
        bump_usage(root, rec.name, "patch_count")
        return {"ok": True, "message": f"已重写 {rec.name!r} 的 SKILL.md。"}

    if action == "patch":
        if not name.strip():
            return {"ok": False, "error": "patch 需要 name。"}
        if not find_text:
            return {"ok": False, "error": "patch 需要 find_text。"}
        if replace_text is None:
            return {"ok": False, "error": "patch 需要 replace_text（可为空字符串）。"}
        rec = find_skill_record(root, name.strip())
        if not rec:
            return {"ok": False, "error": f"未找到 skill: {name!r}"}
        if not rec.agent_created:
            return {"ok": False, "error": "bundled skill 不可 patch；请 skill_view 后复制到 user 新 skill。"}
        target = _safe_join_skill_file(rec.skill_dir, file_path)
        if target is None:
            return {"ok": False, "error": "非法 file_path。"}
        if not target.is_file():
            # 模型常给出不存在的子路径；若 SKILL.md 存在则回退到主文件再试
            requested = isinstance(file_path, str) and bool(file_path.strip())
            if requested and rec.skill_md_path.is_file():
                target = rec.skill_md_path
            else:
                return {"ok": False, "error": "目标文件不存在。"}
        try:
            original = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        if find_text not in original:
            return {"ok": False, "error": "find_text 未匹配到任何位置。"}
        new_text = original.replace(find_text, replace_text, 1)
        if not file_path or file_path.endswith(SKILL_MD) or target.name == SKILL_MD:
            err = validate_skill_md(new_text)
            if err:
                return {"ok": False, "error": f"patch 后校验失败: {err}"}
        _atomic_write(target, new_text)
        bump_usage(root, rec.name, "patch_count")
        return {
            "ok": True,
            "message": f"已在 {rec.name!r} 中替换 1 处（{target.name}）。",
        }

    return {"ok": False, "error": f"未知 action: {action!r}（支持 create/edit/patch）。"}


def format_skills_index_markdown(records: List[SkillRecord], max_items: int = 48) -> str:
    if not records:
        return ""
    lines = [
        "",
        "【可用 Skills（渐进式披露：需要某 skill 的完整指令时由会话加载或 CLI skill load）】",
    ]
    for r in records[:max_items]:
        lines.append(f"- `{r.name}`: {r.description or '（无描述）'}")
    if len(records) > max_items:
        lines.append(f"- … 另有 {len(records) - max_items} 个 skill 未列出")
    return "\n".join(lines)


def format_loaded_skills_markdown(
    root: Path, names: List[str], max_body_chars: int = 12000
) -> str:
    if not names:
        return ""
    parts: List[str] = ["", "【本会话已加载的 Skill 全文】"]
    for n in names:
        data = skill_view(n, root=root, bump_view=False)
        if not data.get("ok"):
            parts.append(f"### {n}\n（加载失败: {data.get('error')}）")
            continue
        body = data.get("content") or ""
        if len(body) > max_body_chars:
            body = body[:max_body_chars] + "\n…（已截断，可用 skill view 查看文件）"
        parts.append(f"### skill:{data.get('name')}\n{body}")
        bump_usage(root, str(data.get("name")), "use_count")
    return "\n".join(parts)
