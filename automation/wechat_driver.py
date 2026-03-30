"""
微信自动化驱动 - 支持桌面客户端和网页版两种方式

桌面客户端：使用 pywinauto 模拟键盘输入和快捷键
网页版：使用 Playwright 控制浏览器操作微信网页版

合规声明：
- 本功能属于辅助输入工具，类似语音输入法
- 不破解微信协议，不绕过安全机制
- 需要用户主动确认执行
- 不存储用户聊天记录和账号信息

安全边界：
- 不自动添加好友
- 不批量群发（避免被判定为营销号）
- 不处理转账、红包等敏感操作
- 不支持自动回复（避免违反微信使用规范）
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _web_fallback_enabled() -> bool:
    """默认关闭：优先桌面时不打开网页版微信。设置 ARIA_WECHAT_WEB_FALLBACK=1 可启用网页回退。"""
    return os.getenv("ARIA_WECHAT_WEB_FALLBACK", "").strip().lower() in ("1", "true", "yes", "on")


def _hwnd_from_pywinauto_window(window) -> int | None:
    """从 pywinauto 控件解析 HWND（uia / win32 包装器均可尽量解析）。"""
    if not window:
        return None
    candidates: list[Any] = []
    try:
        candidates.append(getattr(window, "handle", None))
    except Exception:
        pass
    try:
        wo = window.wrapper_object()
        candidates.append(getattr(wo, "handle", None))
    except Exception:
        pass
    try:
        ei = getattr(window, "element_info", None)
        if ei is not None:
            candidates.append(getattr(ei, "handle", None))
    except Exception:
        pass
    try:
        props = window.get_properties()
        if isinstance(props, dict):
            candidates.append(props.get("handle"))
    except Exception:
        pass
    for h in candidates:
        if h:
            try:
                return int(h)
            except (TypeError, ValueError):
                continue
    return None


def _is_foreground_hwnd(hwnd: int) -> bool:
    if os.name != "nt" or not hwnd:
        return False
    try:
        return bool(ctypes.windll.user32.GetForegroundWindow() == hwnd)
    except Exception:
        return False


def _flush_keyboard_modifiers() -> None:
    """释放可能卡住的 Alt/Ctrl/Shift，避免紧接着的 send_keys 变成 Alt+组合键误触微信快捷键。"""
    if os.name != "nt":
        return
    user32 = ctypes.windll.user32
    KEYEVENTF_KEYUP = 0x0002
    for vk in (0x12, 0x11, 0x10):  # VK_MENU ALT, VK_CONTROL, VK_SHIFT
        try:
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        except Exception:
            pass


def _win32_activate_window(hwnd: int, *, allow_alt_trick: bool = True) -> bool:
    """
    Windows 前台限制下，仅用 pywinauto.set_focus 常失败。
    使用 ShowWindow + BringWindowToTop + AttachThreadInput + SetForegroundWindow。
    allow_alt_trick=False：不模拟 Alt（聊天输入前须关闭，否则可能与 send_keys 竞态导致误触）。
    """
    if os.name != "nt" or not hwnd:
        return False
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    SW_RESTORE = 9
    try:
        user32.ShowWindow(hwnd, SW_RESTORE)
    except Exception:
        pass
    try:
        user32.BringWindowToTop(hwnd)
    except Exception:
        pass
    foreground = user32.GetForegroundWindow()
    fg_tid = ctypes.c_ulong(0)
    if foreground:
        fg_tid = user32.GetWindowThreadProcessId(foreground, None)
    cur_tid = kernel32.GetCurrentThreadId()
    try:
        if foreground and fg_tid and fg_tid != cur_tid:
            user32.AttachThreadInput(cur_tid, fg_tid, True)
        ok = bool(user32.SetForegroundWindow(hwnd))
        if ok or _is_foreground_hwnd(hwnd):
            return True
    except Exception:
        pass
    finally:
        try:
            if foreground and fg_tid and fg_tid != cur_tid:
                user32.AttachThreadInput(cur_tid, fg_tid, False)
        except Exception:
            pass
    if _is_foreground_hwnd(hwnd):
        return True
    if not allow_alt_trick:
        return False
    # Alt 键 trick：部分环境下可解除前台限制（仅搜索等阶段使用）
    try:
        VK_MENU = 0x12
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.05)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    return _is_foreground_hwnd(hwnd)


def _relax_wechat_focus_check() -> bool:
    """为 True 时：前台未确认也继续发键（仅当已解析到 HWND 时）。"""
    return os.getenv("ARIA_WECHAT_RELAX_FOCUS_CHECK", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _esc_before_chat_typing() -> bool:
    """
    输入正文前是否先发 Esc 退出搜索框。
    默认 False：Esc 在部分微信版本会「返回/收起」当前会话，像窗口被关掉。
    若字仍打进搜索栏，可设 ARIA_WECHAT_ESC_BEFORE_TYPE=1 自行承担风险。
    """
    return os.getenv("ARIA_WECHAT_ESC_BEFORE_TYPE", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _send_esc_after_search() -> bool:
    """默认 False：搜索打开会话后不再发 Esc（Esc 在部分微信版本会退出会话/收面板，像「窗口关了」）。"""
    return os.getenv("ARIA_WECHAT_SEND_ESC_AFTER_SEARCH", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _wechat_pc_send_hotkey_string() -> str:
    """
    微信 PC 默认：Enter 发送单条、Ctrl+Enter 换行。
    若设置里改为「Ctrl+Enter 发送」，设环境变量 ARIA_WECHAT_SEND_HOTKEY=ctrl_enter。
    """
    v = (
        os.getenv("ARIA_WECHAT_SEND_HOTKEY", "")
        .strip()
        .lower()
        .replace("+", "_")
    )
    if v in ("ctrl_enter", "ctrlenter", "control_enter"):
        return "^{ENTER}"
    return "{ENTER}"


# ==================== 桌面客户端驱动 ====================

class WeChatDesktopDriver:
    """微信桌面客户端驱动 - 使用 pywinauto"""
    
    def __init__(self):
        self.app = None
        self.window = None
        self._import_error: str | None = None
        
    def _check_pywinauto(self) -> tuple[bool, str]:
        """检查 pywinauto 是否可用"""
        if os.name != "nt":
            return False, "pywinauto_requires_windows"
        try:
            import pywinauto
            return True, ""
        except ImportError as e:
            self._import_error = str(e)
            return False, f"pywinauto_not_installed:{e}"
    
    def _click_wechat_input_area_for_focus(self) -> None:
        """
        点击会话输入区附近：三栏布局下输入框在右侧，取偏右下避免点到中间列表或顶部搜索框。
        """
        self._click_wechat_chat_input_area()

    def _click_wechat_chat_input_area(self) -> None:
        """右侧会话区底部偏右，贴近输入框（避免点到中间列表）。"""
        try:
            r = self.window.rectangle()
            w = max(80, r.width())
            h = max(80, r.height())
            cx = int(w * 0.78)
            cy = h - max(32, min(100, h // 6))
            self.window.click_input(coords=(cx, cy))
        except Exception as e:
            logger.debug("click_wechat_chat_input_area: %s", e)

    def _ensure_focus_in_chat_input(self) -> None:
        """聚焦会话输入区：默认仅双击输入区（无 Esc）；可选 Esc 见 _esc_before_chat_typing。"""
        from pywinauto.keyboard import send_keys

        if _esc_before_chat_typing():
            try:
                send_keys("{ESC}", pause=0.06)
                time.sleep(0.14)
            except Exception as e:
                logger.debug("esc_before_type: %s", e)
        try:
            self._click_wechat_chat_input_area()
            time.sleep(0.12)
            # 二次点击：首击有时只激活侧栏，再击更可靠落到输入框
            self._click_wechat_chat_input_area()
            time.sleep(0.18)
        except Exception as e:
            logger.debug("click_chat_input: %s", e)
    
    def _assert_wechat_foreground_strict(
        self,
        max_rounds: int | None = None,
        *,
        allow_mouse_click: bool = False,
        use_alt_trick: bool = True,
    ) -> tuple[bool, str]:
        """
        确认微信主窗口 HWND 为当前系统前台窗口后再发键。
        全局 send_keys 会发到「前台窗口」；若焦点在 ARIA 浏览器，Ctrl+F 会变成浏览器页内查找。
        use_alt_trick=False：不模拟 Alt（聊天输入前须关闭，避免与 send_keys 竞态误触微信快捷键）。
        """
        if not self.window:
            return False, "wechat_not_connected:未连接微信"
        hwnd = _hwnd_from_pywinauto_window(self.window)
        if not hwnd:
            return (
                False,
                "wechat_hwnd_unresolved:无法解析微信窗口句柄，已拒绝发送快捷键（防止键入到浏览器等其它窗口）",
            )
        rounds = max_rounds if max_rounds is not None else (12 if _relax_wechat_focus_check() else 10)
        for attempt in range(rounds):
            _win32_activate_window(hwnd, allow_alt_trick=use_alt_trick)
            try:
                self.window.set_focus()
            except Exception as e:
                logger.debug("set_focus: %s", e)
            time.sleep(0.05)
            if _is_foreground_hwnd(hwnd):
                return True, ""
            # 仅在允许时、最后一轮再点一次输入区抢焦点（避免打字阶段反复误点侧栏）
            if (
                allow_mouse_click
                and attempt == rounds - 1
                and rounds > 1
            ):
                self._click_wechat_input_area_for_focus()
                time.sleep(0.1)
                _win32_activate_window(hwnd, allow_alt_trick=use_alt_trick)
                try:
                    self.window.set_focus()
                except Exception:
                    pass
                time.sleep(0.05)
                if _is_foreground_hwnd(hwnd):
                    return True, ""
            time.sleep(0.06)
        return (
            False,
            "wechat_window_not_focused:微信未处于系统前台，快捷键会落到其它窗口（请先手动点开微信再确认执行，或关闭抢占焦点的全屏窗口）",
        )
    
    def _prepare_typing_focus_once(self) -> tuple[bool, str]:
        """
        会话已打开后输入文字：只做一次 set_focus + 至多一次 SetForegroundWindow，不循环 ShowWindow。
        多轮激活在部分微信版本上会触发异常（表现为即将打字时窗口像被关掉）。
        """
        if not self.window:
            return False, "wechat_not_connected:未连接微信"
        hwnd = _hwnd_from_pywinauto_window(self.window)
        if not hwnd:
            return (
                False,
                "wechat_hwnd_unresolved:无法解析微信窗口句柄",
            )
        try:
            self.window.set_focus()
        except Exception as e:
            logger.debug("set_focus typing: %s", e)
        time.sleep(0.12)
        if _is_foreground_hwnd(hwnd):
            return True, ""
        _win32_activate_window(hwnd, allow_alt_trick=False)
        time.sleep(0.1)
        if _is_foreground_hwnd(hwnd):
            return True, ""
        return (
            False,
            "wechat_window_not_focused:请先手动点击微信聊天输入框区域，再确认执行",
        )
    
    def _send_keys_to_wechat(
        self,
        keys: str,
        *,
        with_spaces: bool = False,
        pause: float | None = None,
        allow_mouse_click: bool = False,
        use_alt_trick: bool = False,
    ) -> tuple[bool, str]:
        """仅在微信确为前台时发送按键。"""
        from pywinauto.keyboard import send_keys
        
        ok, err = self._assert_wechat_foreground_strict(
            allow_mouse_click=allow_mouse_click,
            use_alt_trick=use_alt_trick,
        )
        if not ok:
            return False, err
        _flush_keyboard_modifiers()
        time.sleep(0.04)
        try:
            p = 0.05 if pause is None else pause
            send_keys(keys, with_spaces=with_spaces, pause=p)
            return True, ""
        except Exception as e:
            return False, f"send_keys_failed:{e}"
    
    def _activate_wechat_window(
        self,
        max_attempts: int = 4,
        *,
        allow_mouse_click: bool = False,
        use_alt_trick: bool = True,
    ) -> tuple[bool, str]:
        """
        将微信主窗口置于前台并获得输入焦点。
        依赖 HWND 前台校验，避免向浏览器等窗口发送 Ctrl+F。
        输入/发送阶段请保持 allow_mouse_click=False，避免误点侧栏。
        """
        if not self.window:
            return False, "wechat_not_connected:未连接微信"
        try:
            if self.window.is_minimized():
                self.window.restore()
                time.sleep(0.12)
        except Exception as e:
            logger.debug("restore minimized: %s", e)
        # max_attempts 映射为前台校验轮数
        mr = max(6, min(max_attempts * 3, 14))
        return self._assert_wechat_foreground_strict(
            max_rounds=mr,
            allow_mouse_click=allow_mouse_click,
            use_alt_trick=use_alt_trick,
        )
    
    def connect(self) -> tuple[bool, str]:
        """
        连接到微信客户端
        
        Returns:
            (success, error_message)
        """
        ok, err = self._check_pywinauto()
        if not ok:
            return False, err
        
        try:
            from pywinauto import Application
            from pywinauto.findwindows import ElementNotFoundError
            
            # 尝试连接已运行的微信
            # 微信窗口标题通常是 "微信" 或包含 "微信"
            try:
                self.app = Application(backend="uia").connect(title_re=".*微信.*", timeout=5)
                windows = self.app.windows()
                if not windows:
                    # 尝试通过进程名连接
                    self.app = Application(backend="uia").connect(path="WeChat.exe", timeout=5)
            except ElementNotFoundError:
                # 尝试通过进程名连接
                try:
                    self.app = Application(backend="uia").connect(path="WeChat.exe", timeout=5)
                except Exception:
                    return False, "wechat_not_running:微信客户端未运行，请先打开微信"
            
            # 获取主窗口
            windows = self.app.windows()
            for win in windows:
                title = win.window_text()
                if "微信" in title or title == "微信":
                    self.window = win
                    break
            
            if not self.window:
                self.window = windows[0] if windows else None
            
            if not self.window:
                return False, "wechat_window_not_found:无法找到微信窗口"
            
            # 激活窗口
            try:
                self.window.set_focus()
                self.window.wait_ready(timeout=5)
                # 检查窗口是否最小化
                if self.window.is_minimized():
                    self.window.restore()
                    time.sleep(0.5)
            except Exception as e:
                logger.warning(f"激活微信窗口失败：{e}")
            
            return True, ""
            
        except Exception as e:
            error_msg = f"connect_wechat_failed:连接微信失败 - {str(e)}"
            logger.error(error_msg)
            return False, error_msg
    
    def search_contact(self, contact_name: str) -> tuple[bool, str]:
        """
        搜索联系人（使用 Ctrl+F 快捷键）
        
        Args:
            contact_name: 联系人名称
            
        Returns:
            (success, error_message)
        """
        if not self.window:
            ok, err = self.connect()
            if not ok:
                return False, err
        
        try:
            ok_focus, err_focus = self._activate_wechat_window(
                allow_mouse_click=True, use_alt_trick=True
            )
            if not ok_focus:
                return False, err_focus
            time.sleep(0.2)
            # 每一步均先校验微信为前台再发键；搜索阶段可 Alt trick + 底部点击
            ok, err = self._send_keys_to_wechat(
                "^f", pause=0.1, allow_mouse_click=True, use_alt_trick=True
            )
            if not ok:
                return False, err
            time.sleep(0.4)
            ok, err = self._send_keys_to_wechat(
                "^a", pause=0.06, allow_mouse_click=True, use_alt_trick=True
            )
            if not ok:
                return False, err
            time.sleep(0.06)
            safe_name = self._escape_special_chars(contact_name)
            ok, err = self._send_keys_to_wechat(
                safe_name,
                with_spaces=True,
                pause=0.05,
                allow_mouse_click=True,
                use_alt_trick=True,
            )
            if not ok:
                return False, err
            time.sleep(0.85)
            ok, err = self._send_keys_to_wechat(
                "{ENTER}", pause=0.1, allow_mouse_click=True, use_alt_trick=True
            )
            if not ok:
                return False, err
            time.sleep(0.45)
            if _send_esc_after_search():
                ok, err = self._send_keys_to_wechat(
                    "{ESC}", pause=0.08, allow_mouse_click=True, use_alt_trick=True
                )
                if not ok:
                    return False, err
                time.sleep(0.28)
            
            return True, ""
            
        except Exception as e:
            error_msg = f"search_contact_failed:搜索联系人失败 - {str(e)}"
            logger.error(error_msg)
            return False, error_msg
    
    def _escape_special_chars(self, text: str) -> str:
        """转义 pywinauto 特殊字符"""
        # pywinauto 中需要转义的字符：^ % + ~ ( ) { } [ ]
        result = text
        for ch in "^%+~(){}[]":
            result = result.replace(ch, "{" + ch + "}")
        return result
    
    def type_message(self, message: str) -> tuple[bool, str]:
        """
        在输入框中输入消息
        
        Args:
            message: 消息内容
            
        Returns:
            (success, error_message)
        """
        if not self.window:
            ok, err = self.connect()
            if not ok:
                return False, err
        
        try:
            from pywinauto.keyboard import send_keys
            
            ok, err = self._prepare_typing_focus_once()
            if not ok:
                return False, err
            # 搜索进会话后焦点常在顶部搜索框，必须先退出/点输入区，否则会打进「搜索联系人」
            self._ensure_focus_in_chat_input()
            _flush_keyboard_modifiers()
            time.sleep(0.06)
            safe_message = self._escape_special_chars(message)
            send_keys(safe_message, with_spaces=True, pause=0.02)
            return True, ""
            
        except Exception as e:
            error_msg = f"type_message_failed:输入消息失败 - {str(e)}"
            logger.error(error_msg)
            return False, error_msg
    
    def send_message(self) -> tuple[bool, str]:
        """
        发送消息：默认按 Enter（微信 PC 常见设置）；Ctrl+Enter 发送时设 ARIA_WECHAT_SEND_HOTKEY=ctrl_enter。
        
        Returns:
            (success, error_message)
        """
        if not self.window:
            return False, "wechat_not_connected:未连接微信"
        
        try:
            from pywinauto.keyboard import send_keys
            
            ok, err = self._prepare_typing_focus_once()
            if not ok:
                return False, err
            self._ensure_focus_in_chat_input()
            _flush_keyboard_modifiers()
            time.sleep(0.06)
            send_keys(_wechat_pc_send_hotkey_string(), pause=0.1)
            time.sleep(0.5)
            
            return True, ""
            
        except Exception as e:
            error_msg = f"send_message_failed:发送消息失败 - {str(e)}"
            logger.error(error_msg)
            return False, error_msg
    
    def send_to_contact(
        self, contact_name: str, message: str, *, skip_search: bool = False
    ) -> dict:
        """
        完整流程：搜索联系人 -> 输入消息 -> 发送
        
        Args:
            contact_name: 联系人名称（skip_search 为 True 时仅作占位，可不填）
            message: 消息内容
            skip_search: 已为对端打开会话时设为 True，仅输入并发送（避免再次 Ctrl+F 搜两次）
            
        Returns:
            dict: {success: bool, error: str|None, method: str, warning: str|None}
        """
        method = "wechat_desktop"
        warnings = []
        
        if not skip_search:
            ok, err = self.search_contact(contact_name)
            if not ok:
                return {"success": False, "error": err, "method": method, "warning": None}
        else:
            ok_focus, err_focus = self._activate_wechat_window(
                max_attempts=3, allow_mouse_click=False, use_alt_trick=False
            )
            if not ok_focus:
                return {"success": False, "error": err_focus, "method": method, "warning": None}
            time.sleep(0.2)
        time.sleep(0.25)
        
        # 2. 输入消息
        ok, err = self.type_message(message)
        if not ok:
            return {"success": False, "error": err, "method": method, "warning": None}
        
        # 3. 发送消息
        ok, err = self.send_message()
        if not ok:
            return {"success": False, "error": err, "method": method, "warning": None}
        
        # 4. 添加警告：桌面自动化无法验证消息是否真的发送成功
        warnings.append(
            "桌面自动化已执行键盘操作，但无法验证消息是否真正发送。"
            "请检查微信窗口确认消息已送达。"
            "如窗口被遮挡/最小化/失去焦点，可能导致发送失败。"
        )
        
        return {"success": True, "error": None, "method": method, "warning": "; ".join(warnings)}
    
    def check_login(self) -> tuple[bool, str]:
        """
        检查微信是否已登录
        
        Returns:
            (is_logged_in, status_message)
        """
        ok, err = self.connect()
        if not ok:
            return False, "wechat_not_running:微信未运行"
        
        # 如果能成功连接，说明微信已登录
        # 更精确的检查需要分析窗口内容，这里简化处理
        return True, "wechat_logged_in:微信已登录"
    
    def open_chat(self, contact_name: str) -> tuple[bool, str]:
        """
        打开与指定联系人的聊天窗口
        
        Args:
            contact_name: 联系人名称
            
        Returns:
            (success, error_message)
        """
        ok, err = self.search_contact(contact_name)
        return ok, err


# ==================== 网页版驱动 ====================

class WeChatWebDriver:
    """微信网页版驱动 - 使用 Playwright"""
    
    def __init__(self):
        self.page = None
        self._logged_in = False
        
    def _check_playwright(self) -> tuple[bool, str]:
        """检查 Playwright 是否可用"""
        try:
            from playwright.sync_api import sync_playwright
            return True, ""
        except ImportError as e:
            return False, f"playwright_not_installed:{e}"
    
    def _get_page(self) -> tuple[bool, str]:
        """获取或创建浏览器页面"""
        from automation.browser_driver import ensure_session
        ok, err = ensure_session()
        if not ok:
            return False, err
        
        # 从 browser_driver 导入全局 page
        from automation import browser_driver
        if browser_driver._page is None:
            return False, "browser_session_not_initialized:浏览器会话未初始化"
        
        self.page = browser_driver._page
        return True, ""
    
    def navigate_to_web_wechat(self) -> tuple[bool, str]:
        """
        打开微信网页版
        
        Returns:
            (success, error_message)
        """
        ok, err = self._check_playwright()
        if not ok:
            return False, err
        
        ok, err = self._get_page()
        if not ok:
            return False, err
        
        try:
            # 导航到微信网页版
            self.page.goto("https://web.wechat.com/", wait_until="domcontentloaded", timeout=60000)
            return True, ""
        except Exception as e:
            error_msg = f"navigate_failed:导航到微信网页版失败 - {str(e)}"
            logger.error(error_msg)
            return False, error_msg
    
    def wait_login(self, timeout_seconds: int = 60) -> tuple[bool, str]:
        """
        等待用户扫码登录
        
        Args:
            timeout_seconds: 超时时间（秒）
            
        Returns:
            (is_logged_in, error_message)
        """
        if not self.page:
            ok, err = self.navigate_to_web_wechat()
            if not ok:
                return False, err
        
        start_time = time.time()
        
        # 轮询检查登录状态
        # 微信网页版登录后会出现聊天列表或主界面
        while time.time() - start_time < timeout_seconds:
            try:
                # 检查是否出现聊天列表（登录后的标志）
                # 微信网页版的聊天列表通常有特定的选择器
                chat_list_selectors = [
                    ".chat_list",
                    "[class*='chat-list']",
                    "[data-testid='chat-list']",
                    ".contact_list",
                    "[class*='contact-list']",
                ]
                
                for selector in chat_list_selectors:
                    try:
                        element = self.page.query_selector(selector)
                        if element:
                            self._logged_in = True
                            return True, ""
                    except Exception:
                        continue
                
                # 检查是否出现二维码（未登录状态）
                qr_selectors = [
                    "canvas.qrcode",
                    "[class*='qrcode']",
                    "[data-testid='qrcode']",
                ]
                
                has_qr = False
                for selector in qr_selectors:
                    try:
                        element = self.page.query_selector(selector)
                        if element:
                            has_qr = True
                            break
                    except Exception:
                        continue
                
                # 如果没有二维码且页面已加载，可能已登录
                if not has_qr:
                    # 额外检查：看是否有登录按钮或提示
                    try:
                        page_content = self.page.content()
                        if "登录" in page_content or "login" in page_content.lower():
                            # 仍在登录页面
                            pass
                        else:
                            # 可能已登录
                            self._logged_in = True
                            return True, ""
                    except Exception:
                        pass
                
                # 等待后重试
                time.sleep(2)
                
            except Exception as e:
                logger.warning(f"检查登录状态失败：{e}")
                time.sleep(2)
        
        return False, "login_timeout:登录超时，请在浏览器中完成扫码登录"
    
    def search_contact(self, contact_name: str) -> tuple[bool, str]:
        """
        搜索联系人
        
        Args:
            contact_name: 联系人名称
            
        Returns:
            (success, error_message)
        """
        if not self.page:
            return False, "browser_not_initialized:浏览器未初始化"
        
        if not self._logged_in:
            ok, err = self.wait_login()
            if not ok:
                return False, err
        
        try:
            # 微信网页版的搜索框选择器
            search_selectors = [
                "input[placeholder='搜索']",
                "input[placeholder*='搜索']",
                "[class*='search'] input",
                "input[type='search']",
            ]
            
            search_box = None
            for selector in search_selectors:
                try:
                    search_box = self.page.query_selector(selector)
                    if search_box:
                        break
                except Exception:
                    continue
            
            if not search_box:
                # 尝试点击搜索按钮（如果有）
                button_selectors = [
                    "[class*='search-btn']",
                    "[data-testid='search-btn']",
                    "button:has-text('搜索')",
                ]
                for selector in button_selectors:
                    try:
                        btn = self.page.query_selector(selector)
                        if btn:
                            btn.click(timeout=5000)
                            time.sleep(0.5)
                            # 再次尝试找输入框
                            for sel in search_selectors:
                                search_box = self.page.query_selector(sel)
                                if search_box:
                                    break
                            if search_box:
                                break
                    except Exception:
                        continue
            
            if not search_box:
                return False, "search_box_not_found:未找到搜索框"
            
            # 输入联系人名称
            search_box.fill(contact_name, timeout=5000)
            time.sleep(1.0)  # 等待搜索结果
            
            # 点击第一个搜索结果
            result_selectors = [
                ".contact-card",
                "[class*='contact-card']",
                "[class*='search-result']:first-child",
                ".contact-list-item:first-child",
            ]
            
            for selector in result_selectors:
                try:
                    result = self.page.query_selector(selector)
                    if result:
                        result.click(timeout=5000)
                        time.sleep(0.5)
                        return True, ""
                except Exception:
                    continue
            
            # 如果没找到特定选择器，尝试直接按 Enter
            search_box.press("Enter", timeout=3000)
            time.sleep(0.5)
            
            return True, ""
            
        except Exception as e:
            error_msg = f"search_contact_failed:搜索联系人失败 - {str(e)}"
            logger.error(error_msg)
            return False, error_msg
    
    def type_message(self, message: str) -> tuple[bool, str]:
        """
        在输入框中输入消息
        
        Args:
            message: 消息内容
            
        Returns:
            (success, error_message)
        """
        if not self.page:
            return False, "browser_not_initialized:浏览器未初始化"
        
        try:
            # 微信网页版的输入框选择器
            input_selectors = [
                "div[contenteditable='true']",
                "[class*='message-editor']",
                "[class*='input-box']",
                "textarea[placeholder*='发送消息']",
            ]
            
            input_box = None
            for selector in input_selectors:
                try:
                    input_box = self.page.query_selector(selector)
                    if input_box:
                        break
                except Exception:
                    continue
            
            if not input_box:
                return False, "input_box_not_found:未找到输入框"
            
            # 填充消息
            input_box.fill(message, timeout=5000)
            
            return True, ""
            
        except Exception as e:
            error_msg = f"type_message_failed:输入消息失败 - {str(e)}"
            logger.error(error_msg)
            return False, error_msg
    
    def click_send(self) -> tuple[bool, str]:
        """
        点击发送按钮
        
        Returns:
            (success, error_message)
        """
        if not self.page:
            return False, "browser_not_initialized:浏览器未初始化"
        
        try:
            # 发送按钮选择器
            send_selectors = [
                "button:has-text('发送')",
                "[class*='send-btn']",
                "[data-testid='send-btn']",
                "button[class*='send']",
            ]
            
            send_btn = None
            for selector in send_selectors:
                try:
                    send_btn = self.page.query_selector(selector)
                    if send_btn:
                        break
                except Exception:
                    continue
            
            if send_btn:
                send_btn.click(timeout=5000)
                time.sleep(0.5)
                return True, ""
            else:
                # 尝试按 Ctrl+Enter 发送
                self.page.keyboard.press("Control+Enter")
                time.sleep(0.5)
                return True, ""
            
        except Exception as e:
            error_msg = f"click_send_failed:点击发送失败 - {str(e)}"
            logger.error(error_msg)
            return False, error_msg
    
    def send_to_contact(self, contact_name: str, message: str) -> dict:
        """
        完整流程：搜索联系人 -> 输入消息 -> 发送
        
        Args:
            contact_name: 联系人名称
            message: 消息内容
            
        Returns:
            dict: {success: bool, error: str|None, method: str}
        """
        method = "wechat_web"
        
        # 1. 导航到网页版
        ok, err = self.navigate_to_web_wechat()
        if not ok:
            return {"success": False, "error": err, "method": method}
        
        # 2. 等待登录
        ok, err = self.wait_login()
        if not ok:
            return {"success": False, "error": err, "method": method}
        
        # 3. 搜索联系人
        ok, err = self.search_contact(contact_name)
        if not ok:
            return {"success": False, "error": err, "method": method}
        
        # 4. 输入消息
        ok, err = self.type_message(message)
        if not ok:
            return {"success": False, "error": err, "method": method}
        
        # 5. 发送消息
        ok, err = self.click_send()
        if not ok:
            return {"success": False, "error": err, "method": method}
        
        return {"success": True, "error": None, "method": method}
    
    def check_login(self) -> tuple[bool, str]:
        """
        检查微信网页版是否已登录
        
        Returns:
            (is_logged_in, status_message)
        """
        if not self.page:
            ok, err = self._get_page()
            if not ok:
                return False, err
        
        ok, err = self.navigate_to_web_wechat()
        if not ok:
            return False, err
        
        ok, err = self.wait_login(timeout_seconds=5)
        if ok:
            return True, "wechat_web_logged_in:微信网页版已登录"
        else:
            return False, "wechat_web_not_logged_in:微信网页版未登录"
    
    def open_chat(self, contact_name: str) -> tuple[bool, str]:
        """
        打开与指定联系人的聊天窗口
        
        Args:
            contact_name: 联系人名称
            
        Returns:
            (success, error_message)
        """
        ok, err = self.search_contact(contact_name)
        return ok, err


# ==================== 智能路由器 ====================

class WeChatRouter:
    """微信自动化智能路由器 - 自动选择最优方案"""
    
    def __init__(self, prefer_desktop: bool = True):
        """
        初始化路由器
        
        Args:
            prefer_desktop: 是否优先使用桌面客户端
        """
        self.prefer_desktop = prefer_desktop
        self.desktop = WeChatDesktopDriver()
        self.web = WeChatWebDriver()
    
    def send_message(
        self, contact_name: str, message: str, *, skip_search: bool = False
    ) -> dict:
        """
        发送微信消息（智能路由）
        
        策略：
        1. 优先使用桌面客户端（更稳定、快速）
        2. 桌面失败时，仅当环境变量 ARIA_WECHAT_WEB_FALLBACK=1 时才回退网页版（默认不回退）
        3. 记录使用的方案和错误信息
        
        Args:
            contact_name: 联系人名称
            message: 消息内容
            skip_search: 桌面端已打开对应对话时设为 True，不再 Ctrl+F 搜索（可与 wechat_open_chat 衔接）
            
        Returns:
            dict: {
                success: bool,
                error: str|None,
                method: str,
                fallback_used: bool,
                desktop_error: str|None,
                web_error: str|None
            }
        """
        fallback_used = False
        desktop_error = None
        web_error = None
        
        # 尝试桌面客户端
        if self.prefer_desktop:
            logger.info(f"尝试使用桌面客户端发送消息给 {contact_name}")
            result = self.desktop.send_to_contact(
                contact_name, message, skip_search=skip_search
            )
            
            if result.get("success"):
                logger.info("桌面客户端发送成功")
                return {
                    **result,
                    "fallback_used": False,
                    "desktop_error": None,
                    "web_error": None
                }
            
            # 桌面失败：默认不打开网页版，除非显式启用回退
            desktop_error = result.get("error", "unknown")
            logger.warning(f"桌面客户端失败：{desktop_error}")
            if not _web_fallback_enabled():
                return {
                    "success": False,
                    "error": desktop_error,
                    "method": result.get("method", "wechat_desktop"),
                    "fallback_used": False,
                    "desktop_error": desktop_error,
                    "web_error": None,
                }
            logger.warning("已设置 ARIA_WECHAT_WEB_FALLBACK=1，尝试网页版")
            fallback_used = True
        
        # 尝试网页版（未优先桌面、或桌面失败且允许回退）
        logger.info(f"尝试使用网页版发送消息给 {contact_name}")
        result = self.web.send_to_contact(contact_name, message)
        web_error = result.get("error", "unknown")
        
        if result.get("success"):
            logger.info("网页版发送成功")
            return {
                **result,
                "fallback_used": fallback_used,
                "desktop_error": desktop_error,
                "web_error": None
            }
        else:
            logger.error(f"网页版失败：{web_error}")
        
        # 都失败了，优先返回桌面版错误（因为是首选方案）
        return {
            "success": False,
            "error": desktop_error or web_error,
            "method": "wechat_web",
            "fallback_used": fallback_used,
            "desktop_error": desktop_error,
            "web_error": web_error
        }
    
    def check_login(self) -> dict:
        """
        检查登录状态
        
        Returns:
            dict: {
                desktop_logged_in: bool,
                web_logged_in: bool,
                desktop_status: str,
                web_status: str
            }
        """
        desktop_ok, desktop_status = self.desktop.check_login()
        if _web_fallback_enabled():
            web_ok, web_status = self.web.check_login()
        else:
            web_ok = False
            web_status = (
                "wechat_web_skipped:未检查网页版（默认不打开浏览器；"
                "设置 ARIA_WECHAT_WEB_FALLBACK=1 可检查网页版登录）"
            )
        
        return {
            "desktop_logged_in": desktop_ok,
            "web_logged_in": web_ok,
            "desktop_status": desktop_status,
            "web_status": web_status
        }
    
    def open_chat(self, contact_name: str) -> dict:
        """
        打开聊天窗口
        
        Args:
            contact_name: 联系人名称
            
        Returns:
            dict: {success: bool, error: str|None, method: str, desktop_error: str|None}
        """
        desktop_error = None
        
        if self.prefer_desktop:
            result = self.desktop.open_chat(contact_name)
            if result[0]:
                return {"success": True, "error": None, "method": "wechat_desktop"}
            else:
                # 保存桌面版错误信息
                desktop_error = result[1]
                logger.warning(f"桌面版打开聊天失败：{desktop_error}")
                if not _web_fallback_enabled():
                    return {
                        "success": False,
                        "error": desktop_error,
                        "method": "wechat_desktop",
                        "desktop_error": desktop_error,
                        "web_error": None,
                    }
        
        # 尝试网页版（未优先桌面、或桌面失败且允许回退）
        result = self.web.open_chat(contact_name)
        if result[0]:
            return {"success": True, "error": None, "method": "wechat_web", "fallback_used": True}
        
        # 都失败了，返回桌面版的错误（如果有的话）
        return {
            "success": False,
            "error": desktop_error or result[1],
            "method": "wechat_web",
            "desktop_error": desktop_error,
            "web_error": result[1]
        }


# ==================== 企业微信支持 ====================

class EnterpriseWeChatDesktopDriver(WeChatDesktopDriver):
    """企业微信桌面客户端驱动"""
    
    def connect(self) -> tuple[bool, str]:
        """连接到企业微信客户端"""
        ok, err = self._check_pywinauto()
        if not ok:
            return False, err
        
        try:
            from pywinauto import Application
            from pywinauto.findwindows import ElementNotFoundError
            
            # 企业微信窗口标题通常包含 "企业微信"
            try:
                self.app = Application(backend="uia").connect(title_re=".*企业微信.*", timeout=5)
            except ElementNotFoundError:
                try:
                    self.app = Application(backend="uia").connect(path="WXWork.exe", timeout=5)
                except Exception:
                    return False, "wxwork_not_running:企业微信客户端未运行"
            
            windows = self.app.windows()
            for win in windows:
                title = win.window_text()
                if "企业微信" in title:
                    self.window = win
                    break
            
            if not self.window:
                self.window = windows[0] if windows else None
            
            if not self.window:
                return False, "wxwork_window_not_found:无法找到企业微信窗口"
            
            try:
                self.window.set_focus()
                self.window.wait_ready(timeout=5)
            except Exception as e:
                logger.warning(f"激活企业微信窗口失败：{e}")
            
            return True, ""
            
        except Exception as e:
            error_msg = f"connect_wxwork_failed:连接企业微信失败 - {str(e)}"
            logger.error(error_msg)
            return False, error_msg


class EnterpriseWeChatWebDriver(WeChatWebDriver):
    """企业微信网页版驱动"""
    
    def navigate_to_web_wechat(self) -> tuple[bool, str]:
        """打开企业微信网页版"""
        ok, err = self._check_playwright()
        if not ok:
            return False, err
        
        ok, err = self._get_page()
        if not ok:
            return False, err
        
        try:
            # 企业微信网页版
            self.page.goto("https://work.weixin.qq.com/", wait_until="domcontentloaded", timeout=60000)
            return True, ""
        except Exception as e:
            error_msg = f"navigate_wxwork_failed:导航到企业微信网页版失败 - {str(e)}"
            logger.error(error_msg)
            return False, error_msg


class EnterpriseWeChatRouter:
    """企业微信智能路由器"""
    
    def __init__(self, prefer_desktop: bool = True):
        self.prefer_desktop = prefer_desktop
        self.desktop = EnterpriseWeChatDesktopDriver()
        self.web = EnterpriseWeChatWebDriver()
    
    def send_message(
        self, contact_name: str, message: str, *, skip_search: bool = False
    ) -> dict:
        """发送企业微信消息"""
        fallback_used = False
        
        if self.prefer_desktop:
            result = self.desktop.send_to_contact(
                contact_name, message, skip_search=skip_search
            )
            if result["success"]:
                return {**result, "fallback_used": False}
            if not _web_fallback_enabled():
                return {**result, "fallback_used": False}
            fallback_used = True
        
        result = self.web.send_to_contact(contact_name, message)
        return {**result, "fallback_used": fallback_used}


# ==================== 工具函数 ====================

def is_desktop_available() -> bool:
    """检查桌面客户端是否可用"""
    if os.name != "nt":
        return False
    try:
        import pywinauto  # noqa: F401
        from pywinauto.keyboard import send_keys  # noqa: F401
        return True
    except ImportError:
        return False


def is_web_available() -> bool:
    """检查网页版是否可用"""
    try:
        from playwright.sync_api import sync_playwright
        return True
    except ImportError:
        return False


def driver_install_hint() -> str:
    """当前环境缺什么依赖时的可执行安装说明（给 stderr / 日志）。"""
    exe = getattr(sys, "executable", "") or "python"
    parts: list[str] = [f"当前 Python：{exe}"]
    if os.name == "nt":
        try:
            import pywinauto  # noqa: F401
            from pywinauto.keyboard import send_keys  # noqa: F401
        except ImportError:
            parts.append(
                "桌面微信：请用**同一解释器**安装："
                f'"{exe}" -m pip install pywinauto'
            )
    else:
        parts.append("桌面微信：仅 Windows + pywinauto；当前系统请用网页版")
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        parts.append(
            "网页版："
            f'"{exe}" -m pip install playwright'
            "，再执行 playwright install chromium"
        )
    if len(parts) <= 1:
        return ""
    return "；".join(parts) + "。装好后请重启 ARIA/Web 服务进程。"


def get_capability_summary() -> str:
    """获取微信自动化能力描述（用于系统提示词）"""
    desktop_available = is_desktop_available()
    web_available = is_web_available()
    
    summary = "【微信自动化】"
    
    if not desktop_available and not web_available:
        summary += "未配置（未安装 pywinauto 和 playwright）。"
    elif not desktop_available:
        summary += "网页版可用（Playwright 已安装）；桌面客户端需要 pywinauto（仅 Windows）。"
    elif not web_available:
        summary += "桌面客户端可用（pywinauto 已安装）；网页版需要 playwright。"
    else:
        summary += "桌面客户端和网页版均可用，优先使用桌面客户端。"
    
    summary += "执行前必须用户确认；不自动添加好友、不群发、不处理转账/红包。"
    
    return summary


def create_router(prefer_desktop: bool = True, is_enterprise: bool = False):
    """
    创建微信路由器
    
    Args:
        prefer_desktop: 是否优先使用桌面客户端
        is_enterprise: 是否为企业微信
        
    Returns:
        WeChatRouter or EnterpriseWeChatRouter
    """
    if is_enterprise:
        return EnterpriseWeChatRouter(prefer_desktop=prefer_desktop)
    else:
        return WeChatRouter(prefer_desktop=prefer_desktop)
