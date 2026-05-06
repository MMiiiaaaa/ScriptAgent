"""Vercel FastAPI 入口（CLI 占用根目录 main.py，故在此转发 web.server.app）。"""
from web.server import app

__all__ = ["app"]
