"""
可选：Playwright 驱动的受控 Chromium，用于 browser_open / browser_click / browser_type。

启用：.env 中 ARIA_PLAYWRIGHT=1，并执行 pip install playwright && playwright install chromium
"""

from __future__ import annotations

import os
import threading
from typing import Any

_lock = threading.RLock()
_pw: Any = None
_browser: Any = None
_context: Any = None
_page: Any = None
_import_error: str | None = None


def is_playwright_enabled() -> bool:
    return os.getenv("ARIA_PLAYWRIGHT", "").strip().lower() in ("1", "true", "yes", "on")


def _headless() -> bool:
    return os.getenv("ARIA_PLAYWRIGHT_HEADLESS", "").strip().lower() in ("1", "true", "yes", "on")


def ensure_session() -> tuple[bool, str]:
    """启动 Playwright Chromium（若尚未启动）。返回 (ok, error_message)。"""
    global _pw, _browser, _context, _page, _import_error
    with _lock:
        if _page is not None:
            return True, ""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            _import_error = str(e)
            return False, f"playwright_not_installed:{e}"
        try:
            # 正确初始化：sync_playwright() 返回上下文管理器，需要调用 start()
            _pw_instance = sync_playwright()
            _pw = _pw_instance.start()
            _browser = _pw.chromium.launch(headless=_headless())
            _context = _browser.new_context()
            _page = _context.new_page()
            _import_error = None
            return True, ""
        except Exception as e:
            # 清理失败的状态
            _pw = None
            _browser = None
            _context = None
            _page = None
            return False, f"playwright_init_failed:{str(e)}"


def navigate(url: str, timeout_ms: int = 60_000) -> tuple[bool, str]:
    u = (url or "").strip()
    if not u.startswith(("http://", "https://")):
        u = "https://" + u.lstrip("/")
    ok, err = ensure_session()
    if not ok:
        return False, err
    with _lock:
        try:
            assert _page is not None
            _page.goto(u, wait_until="domcontentloaded", timeout=timeout_ms)
            return True, ""
        except Exception as e:
            return False, str(e)


def click(selector: str, timeout_ms: int = 30_000, navigate_url: str | None = None) -> tuple[bool, str]:
    sel = (selector or "").strip()
    if not sel:
        return False, "missing_selector"
    ok, err = ensure_session()
    if not ok:
        return False, err
    with _lock:
        try:
            assert _page is not None
            if navigate_url:
                u = navigate_url.strip()
                if u and not u.startswith(("http://", "https://")):
                    u = "https://" + u.lstrip("/")
                if u:
                    _page.goto(u, wait_until="domcontentloaded", timeout=60_000)
            _page.click(sel, timeout=timeout_ms)
            return True, ""
        except Exception as e:
            return False, str(e)


def fill(selector: str, text: str, timeout_ms: int = 30_000, navigate_url: str | None = None) -> tuple[bool, str]:
    sel = (selector or "").strip()
    if not sel:
        return False, "missing_selector"
    ok, err = ensure_session()
    if not ok:
        return False, err
    with _lock:
        try:
            assert _page is not None
            if navigate_url:
                u = navigate_url.strip()
                if u and not u.startswith(("http://", "https://")):
                    u = "https://" + u.lstrip("/")
                if u:
                    _page.goto(u, wait_until="domcontentloaded", timeout=60_000)
            _page.fill(sel, text, timeout=timeout_ms)
            return True, ""
        except Exception as e:
            return False, str(e)


def playwright_package_installed() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def capability_summary_for_planner() -> str:
    """规划阶段不启动浏览器，仅根据环境变量与是否已安装包描述能力边界。"""
    if not is_playwright_enabled():
        return (
            "【浏览器自动化】当前未启用 Playwright（未设置 ARIA_PLAYWRIGHT=1）。"
            "browser_open 仅用系统默认浏览器打开链接；browser_click/browser_type 为模拟占位，无法在页面内真实点击或输入。"
            "勿向用户承诺已完成淘宝/微信等客户端内操作。"
        )
    if not playwright_package_installed():
        return (
            "【浏览器自动化】已设置 ARIA_PLAYWRIGHT=1，但未安装 playwright 包。"
            "请执行: pip install playwright && playwright install chromium。"
            "在此之前仍勿承诺真实页面内点击/输入。"
        )
    return (
        "【浏览器自动化】Playwright 已配置：执行时 browser_open 将用受控 Chromium 导航；"
        "browser_click / browser_type 使用 CSS 选择器 params.selector，可选 params.url 在操作前先打开页面。"
        "强 JS/登录/验证码站点仍可能失败；勿承诺绕过风控或代用户完成微信私聊等。"
    )

