"""
可选：实验性桌面快捷键/向前台窗口输入（pywinauto keyboard）。

启用：.env 中 ARIA_DESKTOP_UIA=1；Windows 且 pip install pywinauto

合规与稳定性：依赖前台焦点、DPI 与目标应用；私聊自动化可能违反应用条款，风险自负。
"""

from __future__ import annotations

import os


def is_uia_enabled() -> bool:
    return os.getenv("ARIA_DESKTOP_UIA", "").strip().lower() in ("1", "true", "yes", "on")


def pywinauto_package_installed() -> bool:
    try:
        import pywinauto  # noqa: F401
        return True
    except ImportError:
        return False


def _modifiers_and_main(hotkey: str) -> tuple[str, str] | tuple[None, str]:
    """解析 'ctrl+shift+t' -> ('^+', 't')；失败返回 (None, error)."""
    raw = (hotkey or "").strip()
    if not raw:
        return None, "empty_hotkey"
    parts = [p.strip().lower() for p in raw.replace(" ", "").split("+")]
    parts = [p for p in parts if p]
    if not parts:
        return None, "empty_hotkey"
    mod_map = {
        "ctrl": "^",
        "control": "^",
        "ctl": "^",
        "shift": "+",
        "alt": "%",
    }
    i = 0
    prefix_parts: list[str] = []
    while i < len(parts) and parts[i] in mod_map:
        prefix_parts.append(mod_map[parts[i]])
        i += 1
    if i >= len(parts):
        return None, "missing_main_key"
    main_tokens = parts[i:]
    main_raw = "+".join(main_tokens)
    special: dict[str, str] = {
        "enter": "{ENTER}",
        "return": "{ENTER}",
        "tab": "{TAB}",
        "esc": "{ESC}",
        "escape": "{ESC}",
        "backspace": "{BS}",
        "delete": "{DEL}",
        "del": "{DEL}",
        "space": " ",
        "up": "{UP}",
        "down": "{DOWN}",
        "left": "{LEFT}",
        "right": "{RIGHT}",
    }
    if main_raw in special:
        main = special[main_raw]
    elif len(main_raw) == 1 and main_raw.isascii():
        main = main_raw
    elif len(main_raw) == 1:
        main = main_raw
    elif main_raw.startswith("f") and main_raw[1:].isdigit() and 1 <= int(main_raw[1:]) <= 24:
        main = "{" + main_raw.upper() + "}"
    else:
        return None, f"unsupported_main_key:{main_raw}"
    return "".join(prefix_parts), main


def send_hotkey(hotkey: str) -> tuple[bool, str]:
    if os.name != "nt":
        return False, "desktop_uia_windows_only"
    spec_m, main_or_err = _modifiers_and_main(hotkey)
    if spec_m is None:
        return False, main_or_err
    try:
        from pywinauto.keyboard import send_keys
    except ImportError as e:
        return False, f"pywinauto_not_installed:{e}"
    spec = spec_m + main_or_err
    try:
        send_keys(spec, pause=0.02)
        return True, ""
    except Exception as e:
        return False, str(e)


def type_text(text: str) -> tuple[bool, str]:
    """将字符串发送到当前前台焦点（逐字符转义 send_keys 特殊符号）。"""
    if os.name != "nt":
        return False, "desktop_uia_windows_only"
    try:
        from pywinauto.keyboard import send_keys
    except ImportError as e:
        return False, f"pywinauto_not_installed:{e}"
    t = text or ""
    if not t:
        return False, "empty_text"
    # 文档：^%+~{} 等为特殊字符，用大括号转义
    out: list[str] = []
    for ch in t:
        if ch in "^%+~(){}[]":
            out.append("{" + ch + "}")
        elif ch == "\n":
            out.append("{ENTER}")
        elif ch == "\t":
            out.append("{TAB}")
        else:
            out.append(ch)
    try:
        send_keys("".join(out), with_spaces=True, pause=0.01)
        return True, ""
    except Exception as e:
        return False, str(e)


def capability_summary_for_planner() -> str:
    if not is_uia_enabled():
        return (
            "【桌面 UI 自动化】未启用（未设置 ARIA_DESKTOP_UIA=1）。desktop_hotkey / desktop_type 为模拟占位，不向系统注入快捷键或文本。"
            "用户要通过微信/企业微信发消息时，应使用 wechat_send_message 等专用动作（由用户确认后执行），勿用 desktop_type/desktop_hotkey 冒充已发送；"
            "勿承诺未启用的 desktop_* 真实注入。"
        )
    if os.name != "nt":
        return (
            "【桌面 UI 自动化】已设置 ARIA_DESKTOP_UIA=1，但当前非 Windows；desktop_hotkey / desktop_type 仍为模拟。"
        )
    if not pywinauto_package_installed():
        return (
            "【桌面 UI 自动化】已启用 ARIA_DESKTOP_UIA=1，但未安装 pywinauto。请执行: pip install pywinauto。"
            "在此之前勿承诺真实快捷键或前台输入。"
        )
    return (
        "【桌面 UI 自动化】实验功能已配置：desktop_hotkey 使用 pywinauto 发组合键（params.hotkey，如 ctrl+v）；"
        "desktop_type 向前台窗口发送 params.text（字符级）；desktop_sequence 按 params.steps 顺序执行 sleep/hotkey/type。"
        "高度依赖焦点与目标 UI，易失败；私聊/刷单等可能违规，须用户已确认执行。"
    )
