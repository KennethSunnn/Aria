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
import json
import os
import sys
import time
import logging
from difflib import SequenceMatcher
from typing import Any, Callable

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


def _set_windows_clipboard_text(text: str) -> tuple[bool, str]:
    """写入系统剪贴板（Unicode 文本）。"""
    if os.name != "nt":
        return False, "clipboard_windows_only"
    try:
        GMEM_MOVEABLE = 0x0002
        CF_UNICODETEXT = 13
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32

        data = (text or "") + "\x00"
        raw = data.encode("utf-16-le")
        h_global = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(raw))
        if not h_global:
            return False, "clipboard_alloc_failed"
        ptr = kernel32.GlobalLock(h_global)
        if not ptr:
            kernel32.GlobalFree(h_global)
            return False, "clipboard_lock_failed"
        ctypes.memmove(ptr, raw, len(raw))
        kernel32.GlobalUnlock(h_global)

        if not user32.OpenClipboard(0):
            kernel32.GlobalFree(h_global)
            return False, "clipboard_open_failed"
        try:
            user32.EmptyClipboard()
            if not user32.SetClipboardData(CF_UNICODETEXT, h_global):
                kernel32.GlobalFree(h_global)
                return False, "clipboard_set_failed"
            # SetClipboardData 成功后，内存所有权转移到系统，不可再 Free
            h_global = None  # type: ignore[assignment]
        finally:
            user32.CloseClipboard()
        return True, ""
    except Exception as e:
        return False, f"clipboard_failed:{e}"


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


def _relax_title_after_search() -> bool:
    """搜索后顶栏校验：默认严格（必须读到且匹配目标名）；设为 1 则沿用宽松逻辑（易误发）。"""
    return os.getenv("ARIA_WECHAT_RELAX_TITLE_AFTER_SEARCH", "0").strip().lower() in (
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


def _wechat_pc_alternate_send_hotkey(primary: str) -> str:
    """在 Enter / Ctrl+Enter 之间切换，用于发送兜底。"""
    return "^{ENTER}" if str(primary or "").strip() != "^{ENTER}" else "{ENTER}"


# ==================== 桌面客户端驱动 ====================

class WeChatDesktopDriver:
    """微信桌面客户端驱动 - 使用 pywinauto"""
    
    def __init__(self):
        self.app = None
        self.window = None
        self._import_error: str | None = None
        self._last_dropdown_row_hit_confirmed = False
        
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
        # 默认更快失败，避免长时间“像卡住”；需要更稳可通过 ARIA_WECHAT_FOCUS_ROUNDS 调高。
        if max_rounds is None:
            env_rounds = os.getenv("ARIA_WECHAT_FOCUS_ROUNDS", "").strip()
            if env_rounds.isdigit():
                rounds = max(2, min(int(env_rounds), 20))
            else:
                rounds = 6 if _relax_wechat_focus_check() else 4
        else:
            rounds = max_rounds
        for attempt in range(rounds):
            _win32_activate_window(hwnd, allow_alt_trick=use_alt_trick)
            try:
                self.window.set_focus()
            except Exception as e:
                logger.debug("set_focus: %s", e)
            time.sleep(0.03)
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
                time.sleep(0.03)
                if _is_foreground_hwnd(hwnd):
                    return True, ""
            time.sleep(0.03)
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

    def _input_search_text(self, text: str) -> tuple[bool, str]:
        """
        搜索框输入文本：中文/混合文本优先走剪贴板粘贴，降低 IME 与 send_keys 丢字风险。
        """
        raw = str(text or "")
        if not raw:
            return False, "empty_search_text"
        has_non_ascii = any(ord(ch) > 127 for ch in raw)
        if has_non_ascii:
            ok_clip, err_clip = _set_windows_clipboard_text(raw)
            if ok_clip:
                ok_paste, err_paste = self._send_keys_to_wechat(
                    "^v",
                    pause=0.05,
                    allow_mouse_click=True,
                    use_alt_trick=True,
                )
                if ok_paste:
                    return True, ""
                return False, err_paste
            logger.warning("clipboard paste unavailable, fallback send_keys: %s", err_clip)
        safe_text = self._escape_special_chars(raw)
        return self._send_keys_to_wechat(
            safe_text,
            with_spaces=True,
            pause=0.05,
            allow_mouse_click=True,
            use_alt_trick=True,
        )
    
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
        mr = max(3, min(max_attempts * 2, 8))
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
            from pywinauto import Application, Desktop
            from pywinauto.findwindows import ElementNotFoundError
            connect_timeout_s = float(os.getenv("ARIA_WECHAT_DESKTOP_CONNECT_TIMEOUT_S", "2").strip() or "2")
            connect_timeout_s = max(0.8, min(connect_timeout_s, 10.0))

            def _choose_main_window(app_obj) -> Any | None:
                # 先拿所有顶层窗口（包含隐藏/后台），避免“后台打开但 windows() 取不到”。
                wins: list[Any] = []
                for visible_only in (False, True):
                    try:
                        cur = app_obj.windows(visible_only=visible_only)
                    except Exception:
                        cur = []
                    if cur:
                        wins = cur
                        break
                if not wins:
                    try:
                        tw = app_obj.top_window()
                        if tw:
                            wins = [tw]
                    except Exception:
                        wins = []
                if not wins:
                    # 兜底：从全局 UIA 顶层窗口按进程筛选（部分版本 connect 成功但 app.windows() 为空）
                    try:
                        proc_id = int(getattr(app_obj, "process", 0) or 0)
                    except Exception:
                        proc_id = 0
                    try:
                        desktop_wins = Desktop(backend="uia").windows(visible_only=False)
                    except Exception:
                        desktop_wins = []
                    if proc_id and desktop_wins:
                        scoped: list[Any] = []
                        for w in desktop_wins:
                            try:
                                pid = int(getattr(getattr(w, "element_info", None), "process_id", 0) or 0)
                            except Exception:
                                pid = 0
                            if pid == proc_id:
                                scoped.append(w)
                        if scoped:
                            wins = scoped
                if not wins:
                    return None

                # 优先标题含“微信/WeChat/Weixin”的窗口。
                for win in wins:
                    try:
                        title = (win.window_text() or "").strip()
                    except Exception:
                        title = ""
                    if ("微信" in title) or ("WeChat" in title) or ("Weixin" in title):
                        return win

                # 次选：面积较大的窗口（通常是主窗口）。
                best = None
                best_area = -1
                for win in wins:
                    try:
                        rect = win.rectangle()
                        area = max(0, int(rect.width()) * int(rect.height()))
                    except Exception:
                        area = 0
                    if area > best_area:
                        best_area = area
                        best = win
                return best

            # 背景启动时偶发瞬态失败：做短重试。
            last_err = ""
            self.app = None
            for _ in range(2):
                try:
                    self.app = Application(backend="uia").connect(
                        title_re=".*(微信|WeChat|Weixin).*",
                        timeout=connect_timeout_s,
                    )
                except ElementNotFoundError as e:
                    last_err = str(e)
                    self.app = None
                    for exe_name in ("WeChat.exe", "Weixin.exe", "Weixin"):
                        try:
                            self.app = Application(backend="uia").connect(path=exe_name, timeout=connect_timeout_s)
                            break
                        except Exception as e2:
                            last_err = str(e2)
                            self.app = None
                except Exception as e:
                    last_err = str(e)
                    self.app = None
                    for exe_name in ("WeChat.exe", "Weixin.exe", "Weixin"):
                        try:
                            self.app = Application(backend="uia").connect(path=exe_name, timeout=connect_timeout_s)
                            break
                        except Exception as e2:
                            last_err = str(e2)
                            self.app = None
                if self.app:
                    break
                time.sleep(0.2)

            if not self.app:
                return False, f"wechat_not_running:微信客户端未运行或连接超时，请先打开微信（detail={last_err}）"

            self.window = _choose_main_window(self.app)
            if not self.window:
                return False, "wechat_window_not_found:微信已运行，但无法定位主窗口（可能被系统隐藏/托盘最小化）"

            # 激活窗口（后台/最小化时尽量恢复）
            try:
                try:
                    if self.window.is_minimized():
                        self.window.restore()
                        time.sleep(0.2)
                except Exception:
                    pass
                self.window.set_focus()
                self.window.wait_ready(timeout=5)
            except Exception as e:
                logger.warning(f"激活微信窗口失败（将继续尝试后续前台激活）：{e}")

            return True, ""

        except Exception as e:
            error_msg = f"connect_wechat_failed:连接微信失败 - {str(e)}"
            logger.error(error_msg)
            return False, error_msg
    
    def search_contact(
        self,
        contact_name: str,
        *,
        contact_hint: Any | None = None,
    ) -> tuple[bool, str]:
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
        self._last_dropdown_row_hit_confirmed = False
        
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
            ok, err = self._input_search_text(contact_name)
            if not ok:
                return False, err
            time.sleep(0.85)
            # 尝试在“搜索结果列表”中选中唯一匹配项，避免首条命中到微搜/非联系人。
            candidates = self._collect_desktop_search_candidate_controls(contact_name)
            hint = str(contact_hint or "").strip()
            if len(candidates) > 1:
                if hint:
                    chosen = [c for c in candidates if hint in str(c.get("text") or "")]
                    if len(chosen) == 1:
                        chosen_ctrl = chosen[0].get("ctrl")
                        try:
                            if chosen_ctrl:
                                chosen_ctrl.click_input()
                                time.sleep(0.4)
                            else:
                                raise RuntimeError("empty_ctrl")
                        except Exception:
                            return False, "wechat_search_candidate_click_failed:无法点击已匹配候选，已阻断发送以避免误发"
                    else:
                        payload = {"contact_name": contact_name, "candidates": [c.get("text") for c in candidates if c.get("text")][:6]}
                        return False, f"wechat_need_disambiguation:{json.dumps(payload, ensure_ascii=False)}"
                else:
                    payload = {"contact_name": contact_name, "candidates": [c.get("text") for c in candidates if c.get("text")][:6]}
                    return False, f"wechat_need_disambiguation:{json.dumps(payload, ensure_ascii=False)}"
            elif len(candidates) == 1:
                chosen_ctrl = candidates[0].get("ctrl")
                try:
                    if chosen_ctrl:
                        chosen_ctrl.click_input()
                        time.sleep(0.4)
                    else:
                        raise RuntimeError("empty_ctrl")
                except Exception:
                    return False, "wechat_search_candidate_click_failed:无法点击唯一候选，已阻断发送以避免误发"
            else:
                # 若 UIA 仅能读到窗口标题（如 Weixin）而读不到列表项，先用键盘打开首个搜索结果（与网页版 Enter 兜底一致）。
                ok_kb, err_kb = self._try_activate_search_result_by_keyboard(contact_name)
                if ok_kb:
                    pass
                else:
                    logger.debug("wechat keyboard search fallback skipped/failed: %s", err_kb)
                if not ok_kb:
                    # 第二阶段：尝试从搜索面板中直接定位并点击真实联系人（包括在微搜条目下方的群聊/联系人）。
                    ok_click, err_click = self._try_click_entry_from_search_panel(
                        contact_name,
                        contact_hint=hint,
                    )
                else:
                    ok_click, err_click = True, ""
                if not ok_click:
                    panel_texts = self._read_desktop_search_panel_texts()
                    # 微搜劫持分支若已在会话列表或 OCR 中点到目标，则不再重复走下方兜底（否则会误报失败且 ocr=[]）。
                    resolved_secondary = False
                    if err_click == "wechat_search_hijacked_by_non_contact":
                        # 第三阶段：尝试从左侧会话列表直接命中联系人（很多场景下真实联系人在列表已存在）
                        ok_list, err_list = self._try_click_entry_from_chat_list(
                            contact_name,
                            contact_hint=hint,
                        )
                        if ok_list:
                            resolved_secondary = True
                        else:
                            if str(err_list).startswith("wechat_need_disambiguation:"):
                                return False, err_list
                            # 第四阶段兜底：UIA 空场景改用 OCR 识别点击
                            ok_ocr, err_ocr = self._try_click_entry_by_ocr(
                                contact_name,
                                contact_hint=hint,
                            )
                            if ok_ocr:
                                resolved_secondary = True
                            else:
                                if str(err_ocr).startswith("wechat_need_disambiguation:"):
                                    return False, err_ocr
                                preview = ", ".join(panel_texts[:8]) or "none"
                                chat_preview = ", ".join(
                                    [str(x.get("text") or "") for x in self._collect_desktop_chat_list_entries()[:8]]
                                ) or "none"
                                return False, (
                                    "wechat_search_hijacked_by_non_contact:搜索结果疑似被微搜/内容结果置顶"
                                    f"（search_panel=[{preview}] chat_list=[{chat_preview}] ocr=[{err_ocr}]）"
                                )
                    if str(err_click).startswith("wechat_need_disambiguation:"):
                        return False, err_click
                    # 第三阶段兜底：从左侧会话列表查找目标（微搜分支已成功则跳过）
                    if not resolved_secondary:
                        ok_list, err_list = self._try_click_entry_from_chat_list(
                            contact_name,
                            contact_hint=hint,
                        )
                        if ok_list:
                            pass
                        else:
                            if str(err_list).startswith("wechat_need_disambiguation:"):
                                return False, err_list
                            # 优先尝试“搜索下拉面板 OCR 点击”（针对微搜置顶但真实候选在下拉中）
                            ok_drop, err_drop = self._try_click_search_dropdown_by_ocr(contact_name)
                            if ok_drop:
                                pass
                            else:
                                # 下拉面板行位探测（命中前先做行内 OCR 确认）
                                ok_drop_probe, err_drop_probe = self._try_click_search_dropdown_rows_probe(contact_name)
                                if ok_drop_probe:
                                    pass
                                else:
                                    ok_ocr, err_ocr = self._try_click_entry_by_ocr(
                                        contact_name,
                                        contact_hint=hint,
                                    )
                                    if ok_ocr:
                                        pass
                                    else:
                                        if str(err_ocr).startswith("wechat_need_disambiguation:"):
                                            return False, err_ocr
                                        # OCR 依赖缺失时禁止进入行位盲探，避免搜索后乱点。
                                        if self._wechat_ocr_error_is_dependency_missing(err_ocr) or self._wechat_ocr_error_is_dependency_missing(err_drop):
                                            ok_probe, err_probe = False, "wechat_probe_skipped:ocr_dependency_missing"
                                        else:
                                            # 末级兜底：微搜场景下按左侧结果行位点击探测
                                            ok_probe, err_probe = self._try_click_left_result_rows_probe(contact_name)
                                        if ok_probe:
                                            pass
                                        else:
                                            panel_preview = ", ".join(panel_texts[:8]) or "none"
                                            chat_preview = ", ".join(
                                                [str(x.get("text") or "") for x in self._collect_desktop_chat_list_entries()[:8]]
                                            ) or "none"
                                            return False, (
                                                "wechat_contact_result_unresolved:未能确认联系人候选，已阻断发送以避免误发;"
                                                f" search_panel=[{panel_preview}]"
                                                f"; chat_list=[{chat_preview}]"
                                                f"; dropdown=[{err_drop}]"
                                                f"; dropdown_probe=[{err_drop_probe}]"
                                                f"; ocr=[{err_ocr}]"
                                                f"; probe=[{err_probe}]"
                                            )

            time.sleep(0.45)

            if _relax_title_after_search():
                ok_title, err_title = self._validate_desktop_chat_title(contact_name)
            else:
                ok_title, err_title = self._validate_desktop_chat_title_after_search(contact_name)
            if not ok_title:
                return False, err_title
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

    def _normalize_for_match(self, text: str) -> str:
        """用于 UI 文本匹配的轻量归一化。"""
        return (text or "").strip().replace("\u3000", " ")

    def _extract_candidate_name(self, text: str) -> str:
        """
        从搜索条目文本里提取“可用于匹配的名称部分”。
        微信条目常含多行（第1行联系人名，第2行消息预览），这里只取首行降噪。
        """
        t = self._normalize_for_match(text)
        if not t:
            return ""
        first_line = t.splitlines()[0].strip()
        return first_line or t

    def _control_text(self, ctrl: Any) -> str:
        """稳健读取控件文本：window_text 优先，失败时回退 element_info.name。"""
        try:
            txt = (ctrl.window_text() or "").strip()
        except Exception:
            txt = ""
        if txt:
            return txt
        try:
            ei = getattr(ctrl, "element_info", None)
            name = getattr(ei, "name", "") if ei is not None else ""
            return str(name or "").strip()
        except Exception:
            return ""

    def _is_probably_non_contact_result(self, text: str) -> bool:
        """
        过滤搜索结果中明显非联系人条目，避免 Enter/点击打开「微搜/小程序/公众号」等。
        该规则是启发式：即便误判，也会在后续“会话标题校验”兜底阻断发送。
        """
        t = self._normalize_for_match(text)
        if not t:
            return True
        non_contact_keywords = (
            "微搜",
            "小程序",
            "公众号",
            "文章",
            "视频号",
            "商品",
            "发现",
        )
        return any(k in t for k in non_contact_keywords)

    def _collect_desktop_search_candidate_controls(self, contact_name: str) -> list[dict[str, Any]]:
        """
        从“搜索结果列表”区域收集候选项（启发式）。
        返回结构：[{text: str, ctrl: pywinauto_element, rect: {..}}...]
        """
        if not self.window:
            return []
        target = self._normalize_for_match(contact_name)
        target_l = target.lower()
        if not target:
            return []

        wnd_rect = self.window.rectangle()
        top_cut = wnd_rect.top + int(wnd_rect.height() * 0.07)
        bottom_cut = wnd_rect.top + int(wnd_rect.height() * 0.88)

        candidate_types = ("Text", "ListItem", "DataItem", "Button", "Pane", "TreeItem", "Custom", "List")
        out: list[dict[str, Any]] = []
        seen_text: set[str] = set()

        for ct in candidate_types:
            try:
                controls = self.window.descendants(control_type=ct)
            except Exception:
                continue
            for ctrl in controls:
                try:
                    if not ctrl.is_visible():
                        continue
                except Exception:
                    pass
                try:
                    rect = ctrl.rectangle()
                except Exception:
                    continue
                if rect.top < top_cut or rect.bottom > bottom_cut:
                    continue
                txt = self._control_text(ctrl)
                if not txt or len(txt) > 200:
                    continue
                name_txt = self._extract_candidate_name(txt)
                if not name_txt:
                    continue
                if self._is_probably_non_contact_result(name_txt):
                    continue

                norm_txt = self._normalize_for_match(name_txt)
                norm_txt_l = norm_txt.lower()
                if not (norm_txt == target or target in norm_txt or norm_txt_l == target_l or target_l in norm_txt_l):
                    continue
                if norm_txt in seen_text:
                    continue
                seen_text.add(norm_txt)
                out.append({"text": norm_txt, "ctrl": ctrl, "rect": rect})
        return out

    def _read_desktop_search_panel_texts(self) -> list[str]:
        """读取搜索面板首屏文本（用于识别是否被微搜/内容搜索置顶劫持）。"""
        entries = self._collect_desktop_search_panel_entries()
        return [str(e.get("text") or "") for e in entries if str(e.get("text") or "").strip()]

    def _collect_desktop_search_panel_entries(self) -> list[dict[str, Any]]:
        """读取搜索面板首屏条目（含文本与矩形），用于第二阶段精确点击。"""
        if not self.window:
            return []
        wnd_rect = self.window.rectangle()
        top_cut = wnd_rect.top + int(wnd_rect.height() * 0.08)
        bottom_cut = wnd_rect.top + int(wnd_rect.height() * 0.90)

        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for ct in ("Text", "ListItem", "DataItem", "Button", "Pane", "TreeItem", "Custom"):
            try:
                controls = self.window.descendants(control_type=ct)
            except Exception:
                continue
            for ctrl in controls:
                try:
                    if not ctrl.is_visible():
                        continue
                    rect = ctrl.rectangle()
                except Exception:
                    continue
                if rect.top < top_cut or rect.bottom > bottom_cut:
                    continue
                txt = self._control_text(ctrl)
                if not txt or len(txt) > 200:
                    continue
                name_txt = self._extract_candidate_name(txt)
                if not name_txt:
                    continue
                if txt in ("微信", "企业微信") or name_txt in ("Weixin", "WeChat", "微信", "企业微信"):
                    continue
                if name_txt not in seen:
                    seen.add(name_txt)
                    entries.append({"text": name_txt, "rect": rect, "ctrl": ctrl})
        if entries:
            return entries

        # 宽松重试：部分微信版本 UIA 可见性/区域信息异常，放宽约束再抓一次
        for ct in ("Text", "ListItem", "DataItem", "Button", "Pane", "TreeItem", "Custom", "Edit", "List"):
            try:
                controls = self.window.descendants(control_type=ct)
            except Exception:
                continue
            for ctrl in controls:
                try:
                    rect = ctrl.rectangle()
                except Exception:
                    continue
                if rect.bottom < wnd_rect.top or rect.top > wnd_rect.bottom:
                    continue
                txt = self._control_text(ctrl)
                if not txt or len(txt) > 220:
                    continue
                name_txt = self._extract_candidate_name(txt)
                if not name_txt:
                    continue
                if name_txt in ("微信", "企业微信", "Weixin", "WeChat"):
                    continue
                if name_txt not in seen:
                    seen.add(name_txt)
                    entries.append({"text": name_txt, "rect": rect, "ctrl": ctrl})
        return entries

    def _collect_desktop_chat_list_entries(self) -> list[dict[str, Any]]:
        """从左侧会话列表抓取候选条目（搜索面板失败时第三层兜底）。"""
        if not self.window:
            return []
        wnd_rect = self.window.rectangle()
        left_min = wnd_rect.left + int(wnd_rect.width() * 0.01)
        # 仅允许左侧列表区域，避免把右侧聊天区文本当作会话候选
        left_max = wnd_rect.left + int(wnd_rect.width() * 0.42)
        top_cut = wnd_rect.top + int(wnd_rect.height() * 0.16)
        bottom_cut = wnd_rect.top + int(wnd_rect.height() * 0.97)

        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for ct in ("ListItem", "DataItem", "TreeItem", "Pane", "Text", "Custom", "Button"):
            try:
                controls = self.window.descendants(control_type=ct)
            except Exception:
                continue
            for ctrl in controls:
                try:
                    if not ctrl.is_visible():
                        continue
                    rect = ctrl.rectangle()
                except Exception:
                    continue
                if rect.left < left_min or rect.right > left_max:
                    continue
                if rect.top < top_cut or rect.bottom > bottom_cut:
                    continue
                txt = self._control_text(ctrl)
                if not txt or len(txt) > 220:
                    continue
                name_txt = self._extract_candidate_name(txt)
                if not name_txt:
                    continue
                if self._is_probably_non_contact_result(name_txt):
                    continue
                if name_txt in ("Weixin", "WeChat"):
                    continue
                norm = self._normalize_for_match(name_txt)
                if norm in seen:
                    continue
                seen.add(norm)
                entries.append({"text": norm, "rect": rect, "ctrl": ctrl})
        if entries:
            return entries

        # 宽松重试：不再限制可见性与左右区域，尽量拿到可匹配文本
        for ct in ("ListItem", "DataItem", "TreeItem", "Pane", "Text", "Custom", "Button", "List"):
            try:
                controls = self.window.descendants(control_type=ct)
            except Exception:
                continue
            for ctrl in controls:
                txt = self._control_text(ctrl)
                if not txt or len(txt) > 220:
                    continue
                name_txt = self._extract_candidate_name(txt)
                if not name_txt:
                    continue
                if self._is_probably_non_contact_result(name_txt):
                    continue
                norm = self._normalize_for_match(name_txt)
                if norm in seen:
                    continue
                seen.add(norm)
                entries.append({"text": norm, "rect": None, "ctrl": ctrl})
        return entries

    def _try_click_entry_from_search_panel(
        self,
        contact_name: str,
        *,
        contact_hint: str = "",
    ) -> tuple[bool, str]:
        """
        第二阶段：当常规候选控件抓取失败时，直接在搜索面板首屏条目中定位并点击目标。
        """
        target = self._normalize_for_match(contact_name)
        if not target:
            return False, "wechat_contact_result_unresolved"

        entries = self._collect_desktop_search_panel_entries()
        if not entries:
            return False, "wechat_contact_result_unresolved"

        # 先过滤掉明显非联系人项
        filtered = [e for e in entries if not self._is_probably_non_contact_result(str(e.get("text") or ""))]
        if not filtered:
            return False, "wechat_search_hijacked_by_non_contact"

        # 优先级：hint 全等/包含 > target 全等 > target 包含
        hint = self._normalize_for_match(contact_hint or "")
        target_l = target.lower()
        hint_l = hint.lower()
        scored: list[tuple[int, dict[str, Any]]] = []
        for e in filtered:
            txt = self._normalize_for_match(str(e.get("text") or ""))
            txt_l = txt.lower()
            score = -1
            if hint and (hint == txt or hint in txt or hint_l == txt_l or hint_l in txt_l):
                score = 300
            elif txt == target or txt_l == target_l:
                score = 200
            elif target in txt or target_l in txt_l:
                score = 100
            if score >= 0:
                scored.append((score, e))
        if not scored:
            return False, "wechat_contact_result_unresolved"

        scored.sort(key=lambda x: x[0], reverse=True)
        top_score = scored[0][0]
        top_items = [x[1] for x in scored if x[0] == top_score]
        if len(top_items) > 1:
            payload = {"contact_name": contact_name, "candidates": [str(x.get("text") or "") for x in top_items[:6]]}
            return False, f"wechat_need_disambiguation:{json.dumps(payload, ensure_ascii=False)}"

        chosen = top_items[0]
        ctrl = chosen.get("ctrl")
        rect = chosen.get("rect")
        try:
            if ctrl is not None:
                ctrl.click_input()
            elif rect is not None:
                cx = int((rect.left + rect.right) / 2)
                cy = int((rect.top + rect.bottom) / 2)
                self.window.click_input(coords=(cx - self.window.rectangle().left, cy - self.window.rectangle().top))
            else:
                return False, "wechat_search_candidate_click_failed"
            time.sleep(0.4)
            return True, ""
        except Exception:
            return False, "wechat_search_candidate_click_failed"

    def _try_click_entry_from_chat_list(
        self,
        contact_name: str,
        *,
        contact_hint: str = "",
    ) -> tuple[bool, str]:
        """第三阶段：搜索面板失败后，直接从左侧会话列表定位并点击目标。"""
        target = self._normalize_for_match(contact_name)
        target_l = target.lower()
        if not target:
            return False, "wechat_contact_result_unresolved"
        hint = self._normalize_for_match(contact_hint or "")
        hint_l = hint.lower()

        entries = self._collect_desktop_chat_list_entries()
        if not entries:
            return False, "wechat_contact_result_unresolved"

        scored: list[tuple[int, dict[str, Any]]] = []
        for e in entries:
            txt = self._normalize_for_match(str(e.get("text") or ""))
            txt_l = txt.lower()
            score = -1
            if hint and (hint == txt or hint in txt or hint_l == txt_l or hint_l in txt_l):
                score = 280
            elif txt == target or txt_l == target_l:
                score = 220
            elif target in txt or target_l in txt_l:
                score = 120
            if score >= 0:
                scored.append((score, e))
        if not scored:
            return False, "wechat_contact_result_unresolved"

        scored.sort(key=lambda x: x[0], reverse=True)
        top_score = scored[0][0]
        top_items = [x[1] for x in scored if x[0] == top_score]
        if len(top_items) > 1:
            payload = {"contact_name": contact_name, "candidates": [str(x.get("text") or "") for x in top_items[:6]]}
            return False, f"wechat_need_disambiguation:{json.dumps(payload, ensure_ascii=False)}"

        chosen = top_items[0]
        try:
            ctrl = chosen.get("ctrl")
            if ctrl is not None:
                ctrl.click_input()
                time.sleep(0.35)
                return True, ""
        except Exception:
            pass
        return False, "wechat_search_candidate_click_failed"

    @staticmethod
    def _wechat_ocr_error_is_tesseract_missing(err: str | None) -> bool:
        if not err:
            return False
        s = err.lower()
        if "tesseract_not_found" in s:
            return True
        if "tesseract" not in s:
            return False
        return (
            "not installed" in s
            or "not in your path" in s
            or "not in your" in s
            or "could not be found" in s
        )

    @staticmethod
    def _wechat_ocr_error_is_dependency_missing(err: str | None) -> bool:
        if not err:
            return False
        s = str(err).lower()
        return (
            "missing_dependency" in s
            or "no module named" in s
            or "wechat_ocr_unavailable" in s
        )

    def _wechat_contact_name_is_latin_letters(self, name: str) -> bool:
        """纯拉丁字母名（如 Kenneth）：chi_sim+eng 常识别差，需叠加 eng OCR。"""
        t = self._normalize_for_match(name)
        if not t:
            return False
        letters = [c for c in t if c.isalpha()]
        if not letters:
            return False
        return all(c.isascii() for c in letters)

    def _wechat_ocr_merged_blocks(
        self,
        region: tuple[int, int, int, int],
        *,
        contact_name: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """
        合并多语言、低阈值 OCR 词块；英文名片段常被 Tesseract 标成低置信度，默认 >60 会整段丢失。
        """
        try:
            from automation import screen_ocr
        except Exception as e:
            return [], str(e)

        langs = ["chi_sim+eng"]
        if self._wechat_contact_name_is_latin_letters(contact_name):
            langs.append("eng")

        merged: list[dict[str, Any]] = []
        seen: set[tuple[str, int, int]] = set()
        last_err: str | None = None
        tess_cfg = "--psm 6"

        for lang in langs:
            r = screen_ocr.ocr_screen(
                region,
                lang=lang,
                min_confidence=12,
                scale=2.0,
                tesseract_config=tess_cfg,
            )
            if not r.get("success", False):
                last_err = str(r.get("error") or "ocr_failed")
                continue
            for b in r.get("blocks", []) or []:
                txt = self._normalize_for_match(str(b.get("text") or ""))
                if not txt:
                    continue
                bb = b.get("bbox") or [0, 0, 0, 0]
                if len(bb) != 4:
                    continue
                lx, ly = int(bb[0]), int(bb[1])
                key = (txt, lx // 8, ly // 8)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(
                    {
                        "text": txt,
                        "bbox": bb,
                        "confidence": int(b.get("confidence") or 0),
                        "lang": lang,
                    }
                )
        return merged, last_err

    def _try_click_entry_by_ocr(
        self,
        contact_name: str,
        *,
        contact_hint: str = "",
    ) -> tuple[bool, str]:
        """
        第四阶段兜底：UIA 抓取不到候选时，基于窗口区域 OCR 识别并点击联系人文本。
        """
        if not self.window:
            return False, "wechat_not_connected:未连接微信"
        target = self._normalize_for_match(contact_name)
        if not target:
            return False, "wechat_contact_result_unresolved"
        hint = self._normalize_for_match(contact_hint or "")

        try:
            from automation import screen_ocr
        except Exception as e:
            return False, f"wechat_ocr_unavailable:{e}"

        try:
            wr = self.window.rectangle()
            left = int(wr.left + wr.width() * 0.01)
            # OCR 点击候选严格限制在左侧联系人/搜索列表区域
            top = int(wr.top + wr.height() * 0.14)
            width = int(wr.width() * 0.40)
            height = int(wr.height() * 0.80)
            region = (left, top, width, height)
        except Exception as e:
            return False, f"wechat_ocr_region_failed:{e}"

        queries: list[str] = []
        if hint:
            queries.append(hint)
        if target and target not in queries:
            queries.append(target)

        blocks, ocr_merge_err = self._wechat_ocr_merged_blocks(region, contact_name=contact_name)
        if not blocks and self._wechat_ocr_error_is_tesseract_missing(ocr_merge_err):
            return (
                False,
                "wechat_ocr_tesseract_missing:未检测到 Tesseract OCR，无法使用屏幕识别兜底。"
                "请从 https://github.com/UB-Mannheim/tesseract/wiki 安装 Windows 版，"
                "安装时勾选将 Tesseract 加入 PATH，并建议勾选中文语言包（chi_sim）；"
                "安装完成后重启 ARIA 再试。",
            )

        def _block_to_candidate(raw_text: str, bbox: list[int], base_score: int) -> dict[str, Any] | None:
            if len(bbox) != 4:
                return None
            bx, by, bw, bh = [int(x) for x in bbox]
            abs_x = left + bx + max(1, bw // 2)
            abs_y = top + by + max(1, bh // 2)
            rel_x = abs_x - wr.left
            rel_y = abs_y - wr.top
            if rel_x < 0 or rel_y < 0:
                return None
            # 双重保险：仅允许左侧会话列表区域点击，避免点击到右侧聊天记录
            if rel_x > int(wr.width() * 0.45):
                return None
            if rel_y < int(wr.height() * 0.12) or rel_y > int(wr.height() * 0.97):
                return None
            return {
                "text": raw_text,
                "score": base_score,
                "rel_x": rel_x,
                "rel_y": rel_y,
            }

        candidates: list[dict[str, Any]] = []
        noise_tokens = {"weixin", "wechat", "微信", "weix", "wx"}

        # 1) 子串命中（合并后的词块，含低置信度英文）
        for q in queries:
            if not q:
                continue
            q_l = q.lower()
            for b in blocks:
                raw_text = self._normalize_for_match(str(b.get("text") or ""))
                if not raw_text:
                    continue
                if self._is_probably_non_contact_result(raw_text) or ("搜一搜" in raw_text):
                    continue
                tl = raw_text.lower()
                if tl.strip() in noise_tokens or raw_text.strip() in ("Weixin", "微信"):
                    continue
                if q_l not in tl and tl not in q_l:
                    continue
                conf = int(b.get("confidence") or 0)
                score = conf + 120
                if raw_text.lower() == q_l:
                    score += 80
                cnd = _block_to_candidate(raw_text, b.get("bbox") or [], score)
                if cnd:
                    candidates.append(cnd)

        # 2) 模糊匹配（Kenneth -> Kerneth 等）
        if not candidates:
            target_l = target.lower()
            hint_l = hint.lower()
            for b in blocks:
                raw_text = self._normalize_for_match(str(b.get("text") or ""))
                if not raw_text:
                    continue
                if self._is_probably_non_contact_result(raw_text) or ("搜一搜" in raw_text):
                    continue
                text_l = raw_text.lower()
                if text_l.strip() in noise_tokens:
                    continue
                sim_target = SequenceMatcher(None, text_l, target_l).ratio() if target_l else 0.0
                sim_hint = SequenceMatcher(None, text_l, hint_l).ratio() if hint_l else 0.0
                token_hit = (target_l and (target_l in text_l or text_l in target_l)) or (
                    hint_l and (hint_l in text_l or text_l in hint_l)
                )
                if not token_hit and max(sim_target, sim_hint) < 0.52:
                    continue

                bbox = b.get("bbox") or [0, 0, 0, 0]
                conf = int(b.get("confidence") or 0)
                score = conf + int(max(sim_target, sim_hint) * 100)
                if token_hit:
                    score += 40
                cnd = _block_to_candidate(raw_text, bbox, score)
                if cnd:
                    candidates.append(cnd)

        if not candidates:
            preview = ""
            try:
                r0 = screen_ocr.ocr_screen(
                    region,
                    lang="eng" if self._wechat_contact_name_is_latin_letters(contact_name) else "chi_sim+eng",
                    min_confidence=12,
                    scale=2.0,
                    tesseract_config="--psm 6",
                )
                if r0.get("success"):
                    preview = (r0.get("text") or "")[:120].replace("\n", " ").strip()
            except Exception:
                preview = ""
            nblk = len(blocks)
            return False, f"wechat_ocr_no_match:blocks={nblk}|preview={preview or 'empty'}|err={ocr_merge_err or 'none'}"

        candidates.sort(key=lambda x: int(x.get("score") or 0), reverse=True)
        top_score = int(candidates[0].get("score") or 0)
        top_items = [c for c in candidates if int(c.get("score") or 0) == top_score][:6]
        unique_names = []
        for c in top_items:
            t = str(c.get("text") or "")
            if t and t not in unique_names:
                unique_names.append(t)
        if len(unique_names) > 1:
            payload = {"contact_name": contact_name, "candidates": unique_names}
            return False, f"wechat_need_disambiguation:{json.dumps(payload, ensure_ascii=False)}"

        chosen = candidates[0]
        try:
            self.window.click_input(coords=(int(chosen["rel_x"]), int(chosen["rel_y"])))
            time.sleep(0.45)
            return True, ""
        except Exception as e:
            return False, f"wechat_ocr_click_failed:{e}"

    def _try_click_search_dropdown_by_ocr(self, contact_name: str) -> tuple[bool, str]:
        """
        专门针对搜索下拉面板的 OCR 点击：
        微搜置顶时，真实联系人/群聊常出现在搜索框下拉区域，而非左侧常规会话列表。
        """
        if not self.window:
            return False, "wechat_not_connected:未连接微信"
        target = self._normalize_for_match(contact_name)
        if not target:
            return False, "wechat_contact_result_unresolved"
        try:
            from automation import screen_ocr
        except Exception as e:
            return False, f"wechat_ocr_unavailable:{e}"
        try:
            wr = self.window.rectangle()
            # 搜索框下拉区域（左侧上半区域）
            left = int(wr.left + wr.width() * 0.01)
            top = int(wr.top + wr.height() * 0.08)
            width = int(wr.width() * 0.44)
            height = int(wr.height() * 0.58)
            region = (left, top, width, height)
        except Exception as e:
            return False, f"wechat_ocr_region_failed:{e}"

        queries = [target]
        if self._wechat_contact_name_is_latin_letters(target):
            langs = ("eng", "chi_sim+eng")
        else:
            langs = ("chi_sim+eng", "eng")

        for q in queries:
            for lang in langs:
                r = screen_ocr.find_text_on_screen(q, region=region, lang=lang)
                if not r.get("success", False):
                    continue
                ms = r.get("matches", []) or []
                if not ms:
                    continue
                # 优先点最上方匹配，通常为下拉候选中的第一命中
                ms.sort(key=lambda m: int((m.get("center") or [0, 10**9])[1]))
                m = ms[0]
                center = m.get("center") or [0, 0]
                if len(center) != 2:
                    continue
                rel_x = int(left + int(center[0]) - wr.left)
                rel_y = int(top + int(center[1]) - wr.top)
                # 限制点击在左侧区域，避免误触右侧会话区
                if rel_x < 0 or rel_y < 0 or rel_x > int(wr.width() * 0.46):
                    continue
                try:
                    self.window.click_input(coords=(rel_x, rel_y))
                    time.sleep(0.42)
                    return True, ""
                except Exception:
                    continue
        return False, "wechat_ocr_dropdown_no_match"

    def _try_click_search_dropdown_rows_probe(self, contact_name: str) -> tuple[bool, str]:
        """
        搜索下拉面板的行位探测（不依赖完整 OCR 文本）：
        优先探测“群聊”分组附近行位，并在点击前做行内 OCR 命中确认。
        """
        if not self.window:
            return False, "wechat_not_connected:未连接微信"
        target = self._normalize_for_match(contact_name)
        if not target:
            return False, "wechat_contact_result_unresolved"
        try:
            wr = self.window.rectangle()
            w = max(100, wr.width())
            h = max(100, wr.height())
            # 左侧搜索下拉面板内部横坐标（名称文本列）
            x = int(w * 0.18)
            # 从“群聊”分组常见起始位到下方逐行探测
            y_candidates = [
                int(h * 0.48),
                int(h * 0.54),
                int(h * 0.60),
                int(h * 0.66),
                int(h * 0.72),
            ]
            for y in y_candidates:
                if y < int(h * 0.12) or y > int(h * 0.90):
                    continue
                # 先确认该行 OCR 中包含目标名，再点击，减少“乱点”
                if not self._probe_row_label_matches_expected(x, y, target):
                    continue
                try:
                    self.window.click_input(coords=(x, y))
                except Exception:
                    continue
                time.sleep(0.42)
                self._last_dropdown_row_hit_confirmed = True
                return True, ""
            return False, "wechat_dropdown_rows_probe_no_match"
        except Exception as e:
            return False, f"wechat_dropdown_rows_probe_failed:{e}"

    def _try_click_left_result_rows_probe(self, contact_name: str) -> tuple[bool, str]:
        """
        微搜置顶时的末级兜底：
        不依赖文本识别，直接在左侧搜索结果区按行位点击探测，并做严格会话校验。
        """
        if not self.window:
            return False, "wechat_not_connected:未连接微信"
        try:
            wr = self.window.rectangle()
            w = max(100, wr.width())
            h = max(100, wr.height())
            # 左侧列表中轴附近，避开最左图标和右侧聊天区
            x = int(w * 0.22)
            # 搜索结果常从顶部搜索框下方开始，逐行下探
            y0 = int(h * 0.23)
            step = max(28, int(h * 0.055))
            max_rows = 6
            # 第1行常被微搜/内容入口占据，默认跳过以降低劫持概率
            start_row = 1
            last_err = "wechat_contact_result_unresolved"
            for i in range(start_row, max_rows):
                y = y0 + i * step
                if y >= int(h * 0.92):
                    break
                try:
                    self.window.click_input(coords=(x, y))
                except Exception:
                    continue
                time.sleep(0.38)
                ok_title, err_title = self._validate_desktop_chat_title_after_search(contact_name)
                if ok_title:
                    return True, ""
                # 一旦判定为非联系人页面（微搜/内容），先回退再继续探测下一行
                if "non_contact" in str(err_title):
                    try:
                        self._send_keys_to_wechat("{ESC}", pause=0.05, allow_mouse_click=True, use_alt_trick=True)
                        time.sleep(0.2)
                    except Exception:
                        pass
                    ok_rs, err_rs = self._reset_search_for_probe(contact_name)
                    if not ok_rs:
                        return False, f"wechat_probe_rows_failed:{err_rs}"
                    last_err = err_title or last_err
                    continue
                # 群聊场景下，标题可能不可读；补充“点击行本地 OCR”校验
                if self._probe_row_label_matches_expected(x, y, contact_name):
                    return True, ""
                # 未命中时重置搜索上下文，防止后续点击漂移到非搜索列表
                ok_rs, err_rs = self._reset_search_for_probe(contact_name)
                if not ok_rs:
                    return False, f"wechat_probe_rows_failed:{err_rs}"
                last_err = err_title or last_err
            return False, f"wechat_probe_rows_failed:{last_err}"
        except Exception as e:
            return False, f"wechat_probe_rows_failed:{e}"

    def _reset_search_for_probe(self, contact_name: str) -> tuple[bool, str]:
        """probe 每轮失败后重置到稳定搜索态（Ctrl+F -> 全选 -> 重填关键词）。"""
        ok, err = self._send_keys_to_wechat(
            "^f",
            pause=0.08,
            allow_mouse_click=True,
            use_alt_trick=True,
        )
        if not ok:
            return False, err
        time.sleep(0.2)
        ok, err = self._send_keys_to_wechat(
            "^a",
            pause=0.05,
            allow_mouse_click=True,
            use_alt_trick=True,
        )
        if not ok:
            return False, err
        time.sleep(0.05)
        ok, err = self._input_search_text(contact_name)
        if not ok:
            return False, err
        time.sleep(0.35)
        return True, ""

    def _try_activate_search_result_by_keyboard(self, contact_name: str) -> tuple[bool, str]:
        """
        部分微信版本搜索列表对 UIA 不可见（只读到窗口标题 Weixin 等），OCR 又未安装时无法点选。
        此时用方向键进入结果列表并 Enter 打开首条，再用顶栏校验防误发；失败则重置搜索后试下一套按键。
        """
        expected = self._normalize_for_match(contact_name)
        if not expected:
            return False, "wechat_keyboard_search_missing_contact"

        sequences = ("{DOWN}{ENTER}", "{ENTER}", "{DOWN}{DOWN}{ENTER}")
        last_err = "wechat_keyboard_search_no_match"
        for i, seq in enumerate(sequences):
            if i > 0:
                ok_rs, err_rs = self._reset_search_for_probe(contact_name)
                if not ok_rs:
                    return False, f"wechat_keyboard_search_reset_failed:{err_rs}"
            ok, err = self._send_keys_to_wechat(
                seq,
                pause=0.06,
                allow_mouse_click=True,
                use_alt_trick=True,
            )
            if not ok:
                return False, f"wechat_keyboard_search_send_failed:{err}"
            time.sleep(0.55)
            ok_title, err_title = self._validate_desktop_chat_title_after_search(expected)
            if ok_title:
                return True, ""
            last_err = err_title or last_err
            if err_title and "non_contact" in str(err_title):
                try:
                    self._send_keys_to_wechat(
                        "{ESC}",
                        pause=0.06,
                        allow_mouse_click=True,
                        use_alt_trick=True,
                    )
                    time.sleep(0.22)
                except Exception:
                    pass
        return False, str(last_err)

    def _probe_row_label_matches_expected(self, rel_x: int, rel_y: int, expected_name: str) -> bool:
        """
        在左侧已点击行附近做小区域 OCR，判断是否包含目标名称。
        目的：补足“群聊标题不稳定/不可读”时的确认能力。
        """
        if not self.window:
            return False
        expected = self._normalize_for_match(expected_name)
        if not expected:
            return False
        try:
            from automation import screen_ocr
        except Exception:
            return False
        try:
            wr = self.window.rectangle()
            abs_x = wr.left + int(rel_x)
            abs_y = wr.top + int(rel_y)
            left = max(wr.left + int(wr.width() * 0.01), abs_x - int(wr.width() * 0.16))
            top = max(wr.top + int(wr.height() * 0.14), abs_y - max(18, int(wr.height() * 0.02)))
            width = int(wr.width() * 0.34)
            height = max(34, int(wr.height() * 0.05))
            if left + width > wr.left + int(wr.width() * 0.45):
                width = max(40, wr.left + int(wr.width() * 0.45) - left)
            region = (int(left), int(top), int(width), int(height))

            langs = ["chi_sim+eng"]
            if self._wechat_contact_name_is_latin_letters(expected):
                langs.append("eng")
            for lang in langs:
                r = screen_ocr.ocr_screen(
                    region,
                    lang=lang,
                    min_confidence=12,
                    scale=2.0,
                    tesseract_config="--psm 6",
                )
                if not r.get("success", False):
                    continue
                blob = self._normalize_for_match(str(r.get("text") or ""))
                if blob and (expected in blob or expected.lower() in blob.lower()):
                    return True
                for b in r.get("blocks", []) or []:
                    t = self._normalize_for_match(str(b.get("text") or ""))
                    if not t:
                        continue
                    if expected in t or expected.lower() in t.lower():
                        return True
            return False
        except Exception:
            return False

    def _read_desktop_chat_header_texts(self) -> list[str]:
        """尽量读取聊天顶栏文本（启发式）。"""
        if not self.window:
            return []
        wnd_rect = self.window.rectangle()
        header_bottom = wnd_rect.top + int(wnd_rect.height() * 0.22)
        texts: list[str] = []
        seen: set[str] = set()
        for ct in ("Text", "Pane", "Group", "Custom"):
            try:
                controls = self.window.descendants(control_type=ct)
            except Exception:
                continue
            for ctrl in controls:
                try:
                    if not ctrl.is_visible():
                        continue
                    rect = ctrl.rectangle()
                except Exception:
                    continue
                if rect.bottom > header_bottom:
                    continue
                txt = self._control_text(ctrl)
                if not txt or len(txt) > 80:
                    continue
                if txt in ("微信", "企业微信", "Weixin", "WeChat"):
                    continue
                if txt not in seen:
                    seen.add(txt)
                    texts.append(txt)
        return texts

    def _read_desktop_chat_header_texts_ocr(self, contact_name: str = "") -> list[str]:
        """
        UIA 读不到顶栏时，对右侧会话区顶部做 OCR（与联系人校验共用 Tesseract 配置）。
        多区域 + 中英：降低「顶栏有字但 UIA 无 Name」导致的误杀。
        """
        if not self.window:
            return []
        try:
            from automation import screen_ocr
        except Exception:
            return []

        wr = self.window.rectangle()
        regions: list[tuple[int, int, int, int]] = []
        # 右侧会话标题常见区域（避开最左侧导航/列表）
        regions.append(
            (
                int(wr.left + wr.width() * 0.26),
                int(wr.top + wr.height() * 0.06),
                int(wr.width() * 0.70),
                int(wr.height() * 0.16),
            )
        )
        # 兜底：稍宽的顶条（部分 DPI/布局下标题略偏）
        regions.append(
            (
                int(wr.left + wr.width() * 0.18),
                int(wr.top + wr.height() * 0.05),
                int(wr.width() * 0.78),
                int(wr.height() * 0.18),
            )
        )

        langs = ["chi_sim+eng"]
        if self._wechat_contact_name_is_latin_letters(contact_name):
            langs.append("eng")

        out: list[str] = []
        seen: set[str] = set()

        def _push(s: str) -> None:
            t = self._normalize_for_match(s)
            if not t or len(t) < 2:
                return
            if t in ("微信", "企业微信", "Weixin", "WeChat"):
                return
            if t not in seen:
                seen.add(t)
                out.append(t)

        for region in regions:
            for lang in langs:
                r = screen_ocr.ocr_screen(
                    region,
                    lang=lang,
                    min_confidence=12,
                    scale=2.0,
                    tesseract_config="--psm 6",
                )
                if not r.get("success", False):
                    continue
                for b in r.get("blocks", []) or []:
                    _push(str(b.get("text") or ""))
                blob = (r.get("text") or "").strip()
                if blob:
                    for line in blob.replace("\r", "\n").split("\n"):
                        _push(line)
            if out:
                break
        return out

    def _header_text_matches_expected(self, texts: list[str], expected_contact_name: str) -> bool:
        """顶栏片段是否与目标联系人名一致（含大小写不敏感子串）。"""
        exp = self._normalize_for_match(expected_contact_name)
        if not exp:
            return False
        exp_l = exp.lower()
        for t in texts:
            nt = self._normalize_for_match(t)
            if not nt:
                continue
            nl = nt.lower()
            if nt == exp or exp == nt:
                return True
            if exp in nt or nt in exp:
                return True
            if exp_l in nl or nl in exp_l:
                return True
        # 整段拼接（OCR 常把标题拆成多词块）
        blob = self._normalize_for_match(" ".join(texts))
        blob_l = blob.lower()
        if blob and (exp in blob or exp_l in blob_l):
            return True
        return False

    def _validate_desktop_chat_title_after_search(self, expected_contact_name: str) -> tuple[bool, str]:
        """
        搜索选联系人之后的顶栏校验：默认严格，防止「仍在上一会话却判定成功」导致误发。

        - 读不到顶栏：失败（不再放行）。
        - 顶栏无目标名称：失败（不再仅警告）。
        - 仍可通过 ARIA_WECHAT_RELAX_TITLE_AFTER_SEARCH=1 恢复旧行为。
        """
        expected = self._normalize_for_match(expected_contact_name)
        if not expected:
            return False, "wechat_chat_title_validation_skipped:missing_expected_contact_name"

        texts = self._read_desktop_chat_header_texts()
        if not texts:
            texts = self._read_desktop_chat_header_texts_ocr(expected)
        elif not self._header_text_matches_expected(texts, expected):
            extra = self._read_desktop_chat_header_texts_ocr(expected)
            if extra:
                seen = {self._normalize_for_match(t) for t in texts if self._normalize_for_match(t)}
                for t in extra:
                    nt = self._normalize_for_match(t)
                    if nt and nt not in seen:
                        seen.add(nt)
                        texts.append(t)
        if not texts:
            return (
                False,
                "wechat_chat_title_unreadable_after_search:无法从顶栏(UIA/OCR)确认当前会话是否已切换到目标联系人，已阻断发送",
            )

        if self._header_text_matches_expected(texts, expected):
            return True, ""

        non_contact_keywords = (
            "微搜",
            "小程序",
            "公众号",
            "文章",
            "视频号",
            "商品",
            "发现",
        )
        norm_texts = [self._normalize_for_match(x) for x in texts]
        if any(any(k in x for k in non_contact_keywords) for x in norm_texts):
            preview = ", ".join(texts[:6])
            return False, f"wechat_chat_title_mismatch_non_contact:expected={expected}; actual=[{preview}]"

        if self._last_dropdown_row_hit_confirmed:
            preview = ", ".join(texts[:6])
            logger.warning(
                "wechat_chat_title_mismatch_but_dropdown_row_confirmed: expected=%s actual=[%s]",
                expected,
                preview,
            )
            return True, f"wechat_chat_title_mismatch_warn:expected={expected}; actual=[{preview}]"

        preview = ", ".join(texts[:6])
        return False, f"wechat_chat_title_mismatch_after_search:expected={expected}; actual=[{preview}]"

    def _validate_desktop_chat_title(self, expected_contact_name: str) -> tuple[bool, str]:
        """
        尽量校验当前聊天顶栏是否与目标一致。

        说明：部分群聊/UI 布局下，目标名称不一定出现在我可读的“顶栏区域”，
        这会导致误杀。为兼容：仅当顶栏明确出现“微搜/公众号/小程序”等非联系人类型关键字时才失败，
        其余不匹配场景降级为 warning（允许继续发送）。
        """
        expected = self._normalize_for_match(expected_contact_name)
        if not expected:
            return False, "wechat_chat_title_validation_skipped:missing_expected_contact_name"

        texts = self._read_desktop_chat_header_texts()
        if not texts:
            logger.warning("wechat_chat_title_unreadable: 无法读取聊天顶栏名称，跳过严格校验")
            return True, ""

        for t in texts:
            norm_t = self._normalize_for_match(t)
            if norm_t == expected or expected in norm_t:
                return True, ""

        non_contact_keywords = (
            "微搜",
            "小程序",
            "公众号",
            "文章",
            "视频号",
            "商品",
            "发现",
        )
        norm_texts = [self._normalize_for_match(x) for x in texts]
        if any(any(k in x for k in non_contact_keywords) for x in norm_texts):
            preview = ", ".join(texts[:6])
            return False, f"wechat_chat_title_mismatch_non_contact:expected={expected}; actual=[{preview}]"

        preview = ", ".join(texts[:6])
        logger.warning("wechat_chat_title_mismatch:降级为警告，不阻断发送: expected=%s actual=[%s]", expected, preview)
        return True, f"wechat_chat_title_mismatch_warn:expected={expected}; actual=[{preview}]"
    
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
            primary_hotkey = _wechat_pc_send_hotkey_string()
            send_keys(primary_hotkey, pause=0.1)
            # 兜底：若用户微信设置与环境变量不一致，自动补发另一种发送快捷键。
            # 这样可覆盖「Enter 发送」与「Ctrl+Enter 发送」两种设置，减少“看似执行但未发出”。
            if os.getenv("ARIA_WECHAT_SEND_HOTKEY_AUTO_FALLBACK", "1").strip().lower() in ("1", "true", "yes", "on"):
                time.sleep(0.12)
                _flush_keyboard_modifiers()
                send_keys(_wechat_pc_alternate_send_hotkey(primary_hotkey), pause=0.1)
            time.sleep(0.5)
            
            return True, ""
            
        except Exception as e:
            error_msg = f"send_message_failed:发送消息失败 - {str(e)}"
            logger.error(error_msg)
            return False, error_msg
    
    def send_to_contact(
        self,
        contact_name: str,
        message: str,
        *,
        skip_search: bool = False,
        contact_hint: Any | None = None,
        cancel_checker: Callable[[], bool] | None = None,
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
        if cancel_checker and cancel_checker():
            return {"success": False, "error": "request_cancelled_by_user", "method": method, "warning": None}
        
        if not skip_search:
            ok, err = self.search_contact(contact_name, contact_hint=contact_hint)
            if not ok:
                if str(err).startswith("wechat_need_disambiguation:"):
                    payload = str(err).split(":", 1)[1].strip()
                    try:
                        candidates = json.loads(payload).get("candidates", [])
                    except Exception:
                        candidates = []
                    return {
                        "success": False,
                        "error": "wechat_need_disambiguation",
                        "method": method,
                        "candidates": candidates,
                        "warning": None,
                    }
                return {"success": False, "error": err, "method": method, "warning": None}
        else:
            ok_focus, err_focus = self._activate_wechat_window(
                max_attempts=3, allow_mouse_click=False, use_alt_trick=False
            )
            if not ok_focus:
                return {"success": False, "error": err_focus, "method": method, "warning": None}
            time.sleep(0.2)
            # skip_search 时也做顶栏名称校验：避免消息发到当前聊天窗口
            if contact_name:
                ok_title, err_title = self._validate_desktop_chat_title(contact_name)
                if not ok_title:
                    return {
                        "success": False,
                        "error": err_title,
                        "method": method,
                        "warning": None,
                    }
        if cancel_checker and cancel_checker():
            return {"success": False, "error": "request_cancelled_by_user", "method": method, "warning": None}
        time.sleep(0.25)
        
        # 2. 输入消息
        ok, err = self.type_message(message)
        if not ok:
            return {"success": False, "error": err, "method": method, "warning": None}
        if cancel_checker and cancel_checker():
            return {"success": False, "error": "request_cancelled_by_user", "method": method, "warning": None}
        
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
    
    def open_chat(self, contact_name: str, *, contact_hint: Any | None = None) -> tuple[bool, str]:
        """
        打开与指定联系人的聊天窗口
        
        Args:
            contact_name: 联系人名称
            
        Returns:
            (success, error_message)
        """
        ok, err = self.search_contact(contact_name, contact_hint=contact_hint)
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
            nav_timeout_ms = int(os.getenv("ARIA_WECHAT_WEB_NAV_TIMEOUT_MS", "20000").strip() or "20000")
            self.page.goto(
                "https://web.wechat.com/",
                wait_until="domcontentloaded",
                timeout=max(3000, nav_timeout_ms),
            )
            return True, ""
        except Exception as e:
            error_msg = f"navigate_failed:导航到微信网页版失败 - {str(e)}"
            logger.error(error_msg)
            return False, error_msg
    
    def wait_login(self, timeout_seconds: int = 20) -> tuple[bool, str]:
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
                poll_interval_s = float(os.getenv("ARIA_WECHAT_WEB_LOGIN_POLL_INTERVAL_S", "1").strip() or "1")
                time.sleep(max(0.2, poll_interval_s))
                
            except Exception as e:
                logger.warning(f"检查登录状态失败：{e}")
                poll_interval_s = float(os.getenv("ARIA_WECHAT_WEB_LOGIN_POLL_INTERVAL_S", "1").strip() or "1")
                time.sleep(max(0.2, poll_interval_s))
        
        return False, "login_timeout:登录超时，请在浏览器中完成扫码登录"

    def _validate_web_chat_title(self, expected_contact_name: str) -> tuple[bool, str]:
        """
        尽量校验当前聊天顶栏是否为目标联系人（启发式）。
        若找不到可用的“标题元素”，则跳过校验返回 True（避免误拒绝）。
        """
        expected = (expected_contact_name or "").strip()
        if not expected:
            return False, "wechat_web_chat_title_validation_skipped:missing_expected_contact_name"
        if not self.page:
            return False, "wechat_web_chat_title_validation_failed:no_page"

        selectors = [
            ".chat-title",
            "[class*='chat-title']",
            "[class*='chat-name']",
            "[class*='contact-name']",
            "[data-testid*='chat']",
            "div[role='heading']",
            "h1",
        ]
        try:
            for sel in selectors:
                try:
                    el = self.page.query_selector(sel)
                except Exception:
                    el = None
                if not el:
                    continue
                try:
                    txt = (el.inner_text(timeout=3000) or "").strip()
                except Exception:
                    try:
                        txt = (el.text_content() or "").strip()
                    except Exception:
                        txt = ""
                if not txt:
                    continue
                # 明确读到了标题：若不匹配则失败，若匹配则成功。
                if expected in txt or expected.lower() in txt.lower():
                    return True, ""
                return False, f"wechat_web_chat_title_mismatch:expected={expected}; actual={txt}"
        except Exception:
            return True, ""
        # 没读到标题元素：跳过校验
        return True, ""
    
    def search_contact(
        self,
        contact_name: str,
        *,
        contact_hint: Any | None = None,
    ) -> tuple[bool, str]:
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
            
            # 获取搜索结果候选，避免首条命中到“微搜链接/非联系人”。
            result_selectors = [
                ".contact-card",
                "[class*='contact-card']",
                ".contact-list-item",
                "[class*='contact-list-item']",
                "[class*='search-result']",
            ]

            non_contact_keywords = ("微搜", "小程序", "公众号", "文章", "视频号", "商品", "发现")
            target = (contact_name or "").strip()
            target_lower = target.lower()
            candidates: list[str] = []
            seen: set[str] = set()

            for selector in result_selectors:
                try:
                    elems = self.page.query_selector_all(selector)
                except Exception:
                    elems = []
                for elem in elems:
                    if not elem:
                        continue
                    try:
                        txt = (elem.inner_text(timeout=3000) or "").strip()
                    except Exception:
                        try:
                            txt = (elem.text_content() or "").strip()
                        except Exception:
                            txt = ""
                    if not txt or len(txt) > 64:
                        continue
                    if any(k in txt for k in non_contact_keywords):
                        continue
                    if target and (target in txt or target_lower in txt.lower()):
                        if txt not in seen:
                            seen.add(txt)
                            candidates.append(txt)

            hint = str(contact_hint or "").strip()

            if not candidates:
                # 收集不到候选：兜底按 Enter（后续会在发消息前做顶栏校验或拒绝发送）
                search_box.press("Enter", timeout=3000)
                time.sleep(0.5)
                ok_title, err_title = self._validate_web_chat_title(contact_name)
                if not ok_title:
                    return False, err_title
                return True, ""

            if len(candidates) > 1 and hint:
                chosen = None
                for c in candidates:
                    if hint in c:
                        chosen = c
                        break
                if chosen:
                    candidates = [chosen]

            if len(candidates) > 1:
                payload = {"contact_name": contact_name, "candidates": candidates[:6]}
                return False, f"wechat_need_disambiguation:{json.dumps(payload, ensure_ascii=False)}"

            # 单候选：点击
            chosen = candidates[0]
            clicked = False
            for selector in result_selectors:
                try:
                    elems = self.page.query_selector_all(selector)
                except Exception:
                    elems = []
                for elem in elems:
                    if not elem:
                        continue
                    try:
                        txt2 = (elem.inner_text(timeout=3000) or "").strip()
                    except Exception:
                        try:
                            txt2 = (elem.text_content() or "").strip()
                        except Exception:
                            txt2 = ""
                    if txt2 == chosen:
                        try:
                            elem.click(timeout=5000)
                            time.sleep(0.5)
                            clicked = True
                            break
                        except Exception:
                            continue
                if clicked:
                    break

            if not clicked:
                search_box.press("Enter", timeout=3000)
                time.sleep(0.5)

            ok_title, err_title = self._validate_web_chat_title(contact_name)
            if not ok_title:
                return False, err_title

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
    
    def send_to_contact(
        self,
        contact_name: str,
        message: str,
        *,
        contact_hint: Any | None = None,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> dict:
        """
        完整流程：搜索联系人 -> 输入消息 -> 发送
        
        Args:
            contact_name: 联系人名称
            message: 消息内容
            
        Returns:
            dict: {success: bool, error: str|None, method: str}
        """
        method = "wechat_web"
        if cancel_checker and cancel_checker():
            return {"success": False, "error": "request_cancelled_by_user", "method": method}
        
        # 1. 导航到网页版
        ok, err = self.navigate_to_web_wechat()
        if not ok:
            return {"success": False, "error": err, "method": method}
        if cancel_checker and cancel_checker():
            return {"success": False, "error": "request_cancelled_by_user", "method": method}
        
        # 2. 等待登录
        send_login_timeout_s = int(os.getenv("ARIA_WECHAT_WEB_LOGIN_TIMEOUT_S_SEND", "15").strip() or "15")
        ok, err = self.wait_login(timeout_seconds=send_login_timeout_s)
        if not ok:
            return {"success": False, "error": err, "method": method}
        if cancel_checker and cancel_checker():
            return {"success": False, "error": "request_cancelled_by_user", "method": method}
        
        # 3. 搜索联系人
        ok, err = self.search_contact(contact_name, contact_hint=contact_hint)
        if not ok:
            if str(err).startswith("wechat_need_disambiguation:"):
                payload = str(err).split(":", 1)[1].strip()
                try:
                    candidates = json.loads(payload).get("candidates", [])
                except Exception:
                    candidates = []
                return {
                    "success": False,
                    "error": "wechat_need_disambiguation",
                    "method": method,
                    "candidates": candidates,
                }
            return {"success": False, "error": err, "method": method}
        if cancel_checker and cancel_checker():
            return {"success": False, "error": "request_cancelled_by_user", "method": method}
        
        # 4. 输入消息
        ok, err = self.type_message(message)
        if not ok:
            return {"success": False, "error": err, "method": method}
        if cancel_checker and cancel_checker():
            return {"success": False, "error": "request_cancelled_by_user", "method": method}
        
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
    
    def open_chat(self, contact_name: str, *, contact_hint: Any | None = None) -> tuple[bool, str]:
        """
        打开与指定联系人的聊天窗口
        
        Args:
            contact_name: 联系人名称
            
        Returns:
            (success, error_message)
        """
        ok, err = self.search_contact(contact_name, contact_hint=contact_hint)
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
        self,
        contact_name: str,
        message: str,
        *,
        skip_search: bool = False,
        contact_hint: Any | None = None,
        cancel_checker: Callable[[], bool] | None = None,
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
                contact_name,
                message,
                skip_search=skip_search,
                contact_hint=contact_hint,
                cancel_checker=cancel_checker,
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
        result = self.web.send_to_contact(
            contact_name,
            message,
            contact_hint=contact_hint,
            cancel_checker=cancel_checker,
        )
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
    
    def open_chat(self, contact_name: str, *, contact_hint: Any | None = None) -> dict:
        """
        打开聊天窗口
        
        Args:
            contact_name: 联系人名称
            
        Returns:
            dict: {success: bool, error: str|None, method: str, desktop_error: str|None}
        """
        desktop_error = None
        
        if self.prefer_desktop:
            result = self.desktop.open_chat(contact_name, contact_hint=contact_hint)
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
        result = self.web.open_chat(contact_name, contact_hint=contact_hint)
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

            connect_timeout_s = float(os.getenv("ARIA_WECHAT_DESKTOP_CONNECT_TIMEOUT_S", "2").strip() or "2")
            connect_timeout_s = max(0.8, min(connect_timeout_s, 10.0))

            def _choose_main_window(app_obj) -> Any | None:
                wins: list[Any] = []
                for visible_only in (False, True):
                    try:
                        cur = app_obj.windows(visible_only=visible_only)
                    except Exception:
                        cur = []
                    if cur:
                        wins = cur
                        break
                if not wins:
                    try:
                        tw = app_obj.top_window()
                        if tw:
                            wins = [tw]
                    except Exception:
                        wins = []
                if not wins:
                    return None
                for win in wins:
                    try:
                        title = (win.window_text() or "").strip()
                    except Exception:
                        title = ""
                    if "企业微信" in title:
                        return win
                best = None
                best_area = -1
                for win in wins:
                    try:
                        rect = win.rectangle()
                        area = max(0, int(rect.width()) * int(rect.height()))
                    except Exception:
                        area = 0
                    if area > best_area:
                        best_area = area
                        best = win
                return best

            self.app = None
            for _ in range(2):
                try:
                    self.app = Application(backend="uia").connect(title_re=".*企业微信.*", timeout=connect_timeout_s)
                except ElementNotFoundError:
                    try:
                        self.app = Application(backend="uia").connect(path="WXWork.exe", timeout=connect_timeout_s)
                    except Exception:
                        self.app = None
                except Exception:
                    try:
                        self.app = Application(backend="uia").connect(path="WXWork.exe", timeout=connect_timeout_s)
                    except Exception:
                        self.app = None
                if self.app:
                    break
                time.sleep(0.2)

            if not self.app:
                return False, "wxwork_not_running:企业微信客户端未运行或连接超时"

            self.window = _choose_main_window(self.app)
            if not self.window:
                return False, "wxwork_window_not_found:企业微信已运行，但无法定位主窗口（可能被系统隐藏/托盘最小化）"

            try:
                try:
                    if self.window.is_minimized():
                        self.window.restore()
                        time.sleep(0.2)
                except Exception:
                    pass
                self.window.set_focus()
                self.window.wait_ready(timeout=5)
            except Exception as e:
                logger.warning(f"激活企业微信窗口失败（将继续尝试后续前台激活）：{e}")

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
        self,
        contact_name: str,
        message: str,
        *,
        skip_search: bool = False,
        contact_hint: Any | None = None,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> dict:
        """发送企业微信消息"""
        fallback_used = False
        
        if self.prefer_desktop:
            result = self.desktop.send_to_contact(
                contact_name,
                message,
                skip_search=skip_search,
                contact_hint=contact_hint,
                cancel_checker=cancel_checker,
            )
            if result["success"]:
                return {**result, "fallback_used": False}
            if not _web_fallback_enabled():
                return {**result, "fallback_used": False}
            fallback_used = True
        
        result = self.web.send_to_contact(
            contact_name,
            message,
            contact_hint=contact_hint,
            cancel_checker=cancel_checker,
        )
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
