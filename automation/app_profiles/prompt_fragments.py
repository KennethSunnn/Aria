"""从 static/prompts 加载规划器片段，避免在 aria_manager 内嵌过长字符串。"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent


def load_planner_fragment(filename: str) -> str:
    """filename 如 planner_desktop_apps_fragment.txt（含后缀）。"""
    p = _ROOT / "static" / "prompts" / filename
    if p.is_file():
        try:
            return p.read_text(encoding="utf-8").strip()
        except Exception:
            return ""
    return ""
