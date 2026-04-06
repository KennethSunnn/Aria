"""
高风险 shell 命令检测（单一真源）。

供 `aria_manager._sanitize_shell_command` 等调用，避免规则分叉。
"""
from __future__ import annotations

import re
from typing import Final

# 子串黑名单（与历史 `shell_blocklist` 行为一致：整段命令小写后包含即拦截）
_BLOCKLIST_SUBSTRINGS: Final[tuple[str, ...]] = (
    "rm -rf /",
    "del /f /s /q",
    "format ",
    "shutdown",
    "net user",
    "reg delete",
)

_DANGER_REGEX_PATTERNS: Final[tuple[str, ...]] = (
    r"rm\s+-[rRfF]*\s*/",
    r"rm\s+-[rRfF]+",
    r"del\s+/[fFsS]",
    r"format\s+[a-zA-Z]:",
    r"shutdown\s+",
    r"net\s+user\s+",
    r"reg\s+delete\s+",
    r"DROP\s+TABLE",
    r"DROP\s+DATABASE",
    r":\(\)\{.*\}",
    r"mkfs\.",
    r"dd\s+if=.*of=/dev/",
)

_COMPILED = tuple(re.compile(p, re.IGNORECASE) for p in _DANGER_REGEX_PATTERNS)


def shell_command_blocked_reason(command: str) -> str | None:
    """
    若命令应拦截，返回简短原因标识；否则返回 None。

    原因字符串用于 ValueError / hook JSON，保持稳定即可。
    """
    cmd = (command or "").strip()
    if not cmd:
        return "empty_command"
    lowered = cmd.lower()
    for bad in _BLOCKLIST_SUBSTRINGS:
        if bad in lowered:
            return bad.strip()
    for pat in _COMPILED:
        if pat.search(cmd):
            return pat.pattern
    return None
