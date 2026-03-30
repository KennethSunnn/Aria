"""
微信相关关键词启发式（可配置）。核心规划仍以 LLM 为主；本模块为可选兜底。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

_TRIGGERS: dict[str, Any] | None = None


def _defaults() -> dict[str, Any]:
    return {
        "product_markers": ["微信", "wechat", "weixin", "企业微信", "企微"],
        "send_intent_keywords": [
            "发消息",
            "发信息",
            "发微信",
            "发给",
            "私信",
            "告诉",
            "通知",
            "说一下",
            "留言",
            "发一条",
            "替我发",
            "帮我发",
            "用微信发",
            "微信上发",
        ],
        "guard_send_keywords": [
            "发消息",
            "发信息",
            "发给",
            "告诉",
            "通知",
            "私信",
            "发微信",
            "发一条",
            "发送",
            "说下",
            "说一下",
        ],
        "login_only_keywords": [
            "是否登录",
            "登录了没",
            "登没登",
            "有没有登录",
            "登录状态",
            "在线吗",
            "登着吗",
            "检查一下",
        ],
    }


def load_wechat_triggers() -> dict[str, Any]:
    global _TRIGGERS
    if _TRIGGERS is not None:
        return _TRIGGERS
    base = _defaults()
    path = Path(__file__).resolve().parent / "wechat_triggers.yaml"
    if path.is_file():
        try:
            import yaml  # type: ignore

            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            for key in (
                "product_markers_extra",
                "send_intent_keywords_extra",
                "guard_send_keywords_extra",
                "login_only_keywords_extra",
            ):
                if key not in data:
                    continue
                extra = data.get(key)
                if not isinstance(extra, list):
                    continue
                base_key = key.replace("_extra", "")
                if base_key not in base:
                    base_key = {
                        "product_markers_extra": "product_markers",
                        "send_intent_keywords_extra": "send_intent_keywords",
                        "guard_send_keywords_extra": "guard_send_keywords",
                        "login_only_keywords_extra": "login_only_keywords",
                    }.get(key, base_key)
                if base_key in base:
                    merged = list(base[base_key])
                    for x in extra:
                        s = str(x).strip()
                        if s and s not in merged:
                            merged.append(s)
                    base[base_key] = merged
        except Exception:
            pass
    _TRIGGERS = base
    return _TRIGGERS


def wechat_send_or_open_intent(text: str) -> bool:
    """是否为微信内发消息/打开聊天意图（用于避免「搜索」等词误触全网检索）。"""
    t = (text or "").strip()
    if not t:
        return False
    tl = t.lower()
    if not ("微信" in t or "wechat" in tl or "weixin" in tl or "企业微信" in t or "企微" in t):
        return False
    tr = load_wechat_triggers()
    sends = tr.get("send_intent_keywords") or _defaults()["send_intent_keywords"]
    if any(k in t for k in sends):
        return True
    if re.search(r"给\s*[^\s，,。:：]{1,24}\s*发", t):
        return True
    if ("打开" in t or "点开" in t) and ("聊天" in t or "对话" in t or "会话" in t):
        return True
    return False


def heuristic_plan_wechat(
    text: str,
    requires_double_confirmation: Callable[[list[dict[str, Any]]], bool],
) -> dict[str, Any] | None:
    """用户明确提到微信/企业微信时，用规则生成可执行计划。"""
    t = (text or "").strip()
    if not t:
        return None
    tl = t.lower()
    is_ent = "企业微信" in t or "企微" in t
    if not (is_ent or "微信" in t or "wechat" in tl or "weixin" in tl):
        return None

    tr = load_wechat_triggers()
    login_kw = tr.get("login_only_keywords") or _defaults()["login_only_keywords"]
    guard_kw = tr.get("guard_send_keywords") or _defaults()["guard_send_keywords"]
    if any(k in t for k in login_kw):
        if not any(k in t for k in guard_kw) and not re.search(
            r"给\s*[^\s，,。]{1,24}\s*发", t
        ):
            action = {
                "type": "wechat_check_login",
                "target": "wechat",
                "filters": {},
                "params": {"is_enterprise": is_ent},
                "risk": "low",
                "reason": "用户请求检查微信/企业微信登录状态",
            }
            return {
                "mode": "action",
                "summary": "识别为微信登录状态检查",
                "requires_confirmation": True,
                "actions": [action],
                "requires_double_confirmation": False,
            }

    send_kw = tr.get("send_intent_keywords") or _defaults()["send_intent_keywords"]
    send_intent = any(k in t for k in send_kw) or bool(
        re.search(r"给\s*[^\s，,。:：]{1,24}\s*发", t)
    ) or "发给" in t or ("发送" in t and ("微信" in t or "消息" in t))

    open_only = ("打开" in t and ("聊天" in t or "对话" in t) and not send_intent) or (
        any(k in t for k in ("打开与", "点开")) and not send_intent
    )

    if not send_intent and not open_only:
        return None

    contact = ""
    m = re.search(r"给\s*([^\s，,。:：;；]{1,32}?)\s*(?:发|说|告诉|通知|留言|私聊)", t)
    if m:
        contact = m.group(1).strip().strip("的")
    if not contact:
        m2 = re.search(r"发给\s*([^\s，,。:：;；]{1,32})", t)
        if m2:
            contact = m2.group(1).strip()
    if not contact:
        m3 = re.search(r"(?:与|和|跟)\s*([^\s，,。:：;；]{1,32}?)\s*的(?:聊天|对话)", t)
        if m3:
            contact = m3.group(1).strip()
    if not contact:
        m3b = re.search(
            r"(?:微信|企业微信|企微).*?(?:给|与|和|跟)\s*([^\s，,。:：;；]{1,32}?)(?:发|说|告诉)",
            t,
        )
        if m3b:
            contact = m3b.group(1).strip()
    if not contact and open_only:
        m4 = re.search(r"打开(?:与|和|跟)?\s*([^\s，,。:：;；]{1,32}?)\s*的", t)
        if m4:
            contact = m4.group(1).strip()

    message = ""
    mq = re.search(r"[「\"]([^」\"]{1,4000})[」\"]", t)
    if mq:
        message = mq.group(1).strip()
    if not message:
        mq2 = re.search(r"(?:说|告诉|通知|留言)(?:给|他|她|Ta|ta)?[：:]\s*(.+)$", t)
        if mq2:
            message = mq2.group(1).strip()
    if not message:
        mq3 = re.search(r"发(?:消息|微信|信息)?[：:]\s*(.+)$", t)
        if mq3:
            message = mq3.group(1).strip()

    if open_only:
        if not contact:
            return None
        action = {
            "type": "wechat_open_chat",
            "target": contact,
            "filters": {},
            "params": {"contact_name": contact, "is_enterprise": is_ent},
            "risk": "medium",
            "reason": "用户请求打开微信聊天窗口",
        }
        return {
            "mode": "action",
            "summary": f"识别为打开与「{contact}」的微信会话",
            "requires_confirmation": True,
            "actions": [action],
            "requires_double_confirmation": requires_double_confirmation([action]),
        }

    if not contact or not message:
        return None

    action = {
        "type": "wechat_send_message",
        "target": contact,
        "filters": {},
        "params": {
            "contact_name": contact,
            "message": message,
            "is_enterprise": is_ent,
        },
        "risk": "medium",
        "reason": "用户请求通过微信发送消息（须用户确认后执行；未装 pywinauto/playwright 时执行会返回安装提示）",
    }
    return {
        "mode": "action",
        "summary": f"识别为向「{contact}」发送微信消息",
        "requires_confirmation": True,
        "actions": [action],
        "requires_double_confirmation": requires_double_confirmation([action]),
    }
