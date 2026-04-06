"""
微信桌面客户端自动化执行端

执行策略（双层回退）：
1. pywinauto 路径（ARIA_DESKTOP_UIA=1 且已安装 pywinauto）：UIAutomation 精确控制
2. computer_use 回退路径：window_activate + screen_find_text + computer_click/type

支持的操作：
- wechat_check_login  检查微信是否已登录
- wechat_open_chat    打开与指定联系人的聊天窗口
- wechat_send_message 向指定联系人发送消息
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

_WECHAT_WINDOW_TITLES = ["微信", "WeChat"]
_ENTERPRISE_WINDOW_TITLES = ["企业微信", "WeCom"]


def _is_uia_enabled() -> bool:
    return os.getenv("ARIA_DESKTOP_UIA", "").strip().lower() in ("1", "true", "yes", "on")


def _find_wechat_window(is_enterprise: bool = False):
    """用 pywinauto 查找微信主窗口，返回 Application 对象或 None。"""
    try:
        from pywinauto import Application, findwindows  # type: ignore

        titles = _ENTERPRISE_WINDOW_TITLES if is_enterprise else _WECHAT_WINDOW_TITLES
        for title in titles:
            try:
                handles = findwindows.find_windows(title_re=f".*{title}.*")
                if handles:
                    app = Application(backend="uia").connect(handle=handles[0])
                    return app
            except Exception:
                continue
    except ImportError:
        pass
    return None


def _activate_wechat_window(is_enterprise: bool = False) -> bool:
    """激活微信窗口，返回是否成功。"""
    try:
        import subprocess
        titles = _ENTERPRISE_WINDOW_TITLES if is_enterprise else _WECHAT_WINDOW_TITLES
        # 尝试通过 window_activate 逻辑激活
        try:
            import pyautogui  # type: ignore
            import pygetwindow as gw  # type: ignore

            for title in titles:
                wins = gw.getWindowsWithTitle(title)
                if wins:
                    wins[0].activate()
                    time.sleep(0.5)
                    return True
        except Exception:
            pass
        # 回退：用 pywinauto
        app = _find_wechat_window(is_enterprise)
        if app:
            try:
                win = app.top_window()
                win.set_focus()
                time.sleep(0.5)
                return True
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"activate_wechat_window failed: {e}")
    return False


# ──────────────────────────────────────────────
# 公开接口
# ──────────────────────────────────────────────

def wechat_check_login(is_enterprise: bool = False) -> dict[str, Any]:
    """检查微信是否已登录（窗口是否存在且可见）。"""
    titles = _ENTERPRISE_WINDOW_TITLES if is_enterprise else _WECHAT_WINDOW_TITLES
    app_name = "企业微信" if is_enterprise else "微信"

    # pywinauto 路径
    if _is_uia_enabled():
        app = _find_wechat_window(is_enterprise)
        if app:
            try:
                win = app.top_window()
                title = win.window_text()
                # 登录界面通常包含"登录"字样
                is_login_screen = "登录" in title or "Login" in title.lower()
                return {
                    "success": True,
                    "logged_in": not is_login_screen,
                    "window_title": title,
                    "message": f"{app_name}已{'登录' if not is_login_screen else '显示登录界面'}",
                }
            except Exception as e:
                logger.debug(f"wechat_check_login uia error: {e}")

    # computer_use 回退：检查进程
    try:
        import psutil  # type: ignore

        proc_names = ["WeCom.exe", "企业微信.exe"] if is_enterprise else ["WeChat.exe", "微信.exe"]
        for proc in psutil.process_iter(["name"]):
            if proc.info["name"] in proc_names:
                return {
                    "success": True,
                    "logged_in": True,
                    "message": f"{app_name}进程正在运行（假定已登录）",
                }
        return {
            "success": True,
            "logged_in": False,
            "message": f"未检测到{app_name}进程，可能未启动或未登录",
        }
    except Exception as e:
        return {"success": False, "message": f"wechat_check_login_failed:{e}", "logged_in": False}


def wechat_open_chat(contact_name: str, is_enterprise: bool = False) -> dict[str, Any]:
    """打开与指定联系人的聊天窗口。"""
    if not contact_name.strip():
        return {"success": False, "message": "contact_name_empty"}

    app_name = "企业微信" if is_enterprise else "微信"

    # ── pywinauto 路径 ──
    if _is_uia_enabled():
        try:
            from pywinauto import keyboard  # type: ignore

            app = _find_wechat_window(is_enterprise)
            if app:
                win = app.top_window()
                win.set_focus()
                time.sleep(0.3)
                # Ctrl+F 打开搜索
                keyboard.send_keys("^f")
                time.sleep(0.5)
                keyboard.send_keys(contact_name, with_spaces=True)
                time.sleep(1.0)
                keyboard.send_keys("{ENTER}")
                time.sleep(0.8)
                return {
                    "success": True,
                    "message": f"已通过 UIA 打开与「{contact_name}」的{app_name}聊天",
                }
        except Exception as e:
            logger.debug(f"wechat_open_chat uia failed, falling back: {e}")

    # ── computer_use 回退路径 ──
    activated = _activate_wechat_window(is_enterprise)
    if not activated:
        return {
            "success": False,
            "message": f"wechat_window_not_found：请先启动{app_name}",
            "error_code": "window_not_found",
        }

    try:
        import pyautogui  # type: ignore

        # Ctrl+F 搜索联系人
        pyautogui.hotkey("ctrl", "f")
        time.sleep(0.5)
        pyautogui.typewrite(contact_name, interval=0.05)
        time.sleep(1.0)
        pyautogui.press("enter")
        time.sleep(0.8)
        return {
            "success": True,
            "message": f"已通过 computer_use 打开与「{contact_name}」的{app_name}聊天",
        }
    except Exception as e:
        return {"success": False, "message": f"wechat_open_chat_failed:{e}", "error_code": "automation_error"}


def wechat_send_message(contact_name: str, message: str, is_enterprise: bool = False) -> dict[str, Any]:
    """向指定联系人发送微信消息。"""
    if not contact_name.strip():
        return {"success": False, "message": "contact_name_empty"}
    if not message.strip():
        return {"success": False, "message": "message_empty"}

    app_name = "企业微信" if is_enterprise else "微信"

    # 先打开聊天窗口
    open_result = wechat_open_chat(contact_name, is_enterprise)
    if not open_result.get("success"):
        return open_result

    time.sleep(0.5)

    # ── pywinauto 路径 ──
    if _is_uia_enabled():
        try:
            from pywinauto import keyboard  # type: ignore

            app = _find_wechat_window(is_enterprise)
            if app:
                win = app.top_window()
                win.set_focus()
                time.sleep(0.3)
                # 输入消息（中文需要剪贴板方式）
                _type_message_via_clipboard(message)
                time.sleep(0.3)
                keyboard.send_keys("{ENTER}")
                time.sleep(0.5)
                return {
                    "success": True,
                    "message": f"已通过 UIA 向「{contact_name}」发送{app_name}消息",
                    "contact": contact_name,
                    "message_sent": message,
                }
        except Exception as e:
            logger.debug(f"wechat_send_message uia failed, falling back: {e}")

    # ── computer_use 回退路径 ──
    try:
        _type_message_via_clipboard(message)
        time.sleep(0.3)
        import pyautogui  # type: ignore
        pyautogui.press("enter")
        time.sleep(0.5)
        return {
            "success": True,
            "message": f"已通过 computer_use 向「{contact_name}」发送{app_name}消息",
            "contact": contact_name,
            "message_sent": message,
        }
    except Exception as e:
        return {"success": False, "message": f"wechat_send_message_failed:{e}", "error_code": "automation_error"}


def _type_message_via_clipboard(text: str) -> None:
    """通过剪贴板粘贴方式输入文本（支持中文）。"""
    try:
        import pyperclip  # type: ignore
        import pyautogui  # type: ignore

        pyperclip.copy(text)
        time.sleep(0.1)
        pyautogui.hotkey("ctrl", "v")
    except ImportError:
        # 回退到直接 typewrite（仅 ASCII 可靠）
        import pyautogui  # type: ignore
        pyautogui.typewrite(text, interval=0.03)
