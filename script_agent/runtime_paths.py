"""部署环境可写路径：Vercel / Lambda 等进程 cwd 只读，产物需落到 /tmp。"""

from __future__ import annotations

import os
from pathlib import Path


def is_serverless_readonly_cwd() -> bool:
    """常见 Serverless：部署目录不可写（日志、报告、图谱等需换目录）。"""
    return bool(os.getenv("VERCEL")) or bool(os.getenv("AWS_LAMBDA_FUNCTION_NAME"))


def writable_output_dir() -> Path:
    if is_serverless_readonly_cwd():
        return Path("/tmp")
    return Path.cwd()


def resolve_writable_output_path(path_str: str | Path) -> Path:
    """绝对路径沿用；相对路径落在 writable_output_dir() 下（避免只读 cwd）。"""
    p = Path(path_str).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (writable_output_dir() / p).resolve()
