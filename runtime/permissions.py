"""
五级权限谱模型

权限系统是 UX 设计，可组合的信任谱让 ARIA 适应从"高度受控企业环境"
到"个人全自动运行"的各种场景。

通过 ARIA_PERMISSION_LEVEL 环境变量配置（默认 default）。
"""
from __future__ import annotations

import os
from enum import Enum
from typing import FrozenSet


class PermissionLevel(str, Enum):
    PLAN = "plan"               # 只读预览，不执行任何写操作
    DEFAULT = "default"         # 写操作和 shell 均需询问（默认）
    ACCEPT_EDITS = "accept_edits"  # 文件/浏览器/桌面操作自动批准，shell/消息仍需询问
    DONT_ASK = "dont_ask"       # 白名单内所有操作自动批准
    BYPASS = "bypass"           # 所有操作自动批准（CI/CD 模式）


_ENV_KEY = "ARIA_PERMISSION_LEVEL"

# 文档/TAOR「低风险探测」核心集合（须为 ALLOWED_ACTION_TYPES 子集，由测试保证）
SAFE_ACTION_TYPES: FrozenSet[str] = frozenset(
    {
        "web_understand",
        "web_fetch",
        "browser_find",
        "browser_scroll",
        "browser_wait",
        "computer_screenshot",
        "screen_ocr",
        "screen_find_text",
    }
)

# 只读操作集合（非 plan 模式下：任何权限级别均可执行，无需确认）
_READ_ONLY_TYPES: FrozenSet[str] = frozenset(
    {
        "browser_screenshot",
        "browser_get_text",
        "browser_get_url",
        "browser_find",
        "computer_screenshot",
        "desktop_read",
        "file_read",
        "shell_query",
    }
)

# plan 模式允许的动作 = 只读 ∪ SAFE（TAOR / allows_under_plan_mode 单一真源）
PLAN_MODE_ALLOWED_TYPES: FrozenSet[str] = _READ_ONLY_TYPES | SAFE_ACTION_TYPES

# shell 类操作（写副作用风险高）
_SHELL_TYPES: FrozenSet[str] = frozenset({"shell_run", "shell_exec"})

# 消息发送类（不可撤销）
_MESSAGING_TYPES: FrozenSet[str] = frozenset(
    {
        "messaging_send",
        "messaging_send_group",
        "wechat_send",
        "wecom_send",
        "email_send",
    }
)

# 文件写操作
_FILE_WRITE_TYPES: FrozenSet[str] = frozenset(
    {
        "file_write",
        "file_move",
        "file_delete",
        "file_create_dir",
        "file_append",
    }
)

# 浏览器/桌面/computer 可信交互（通常可接受）
_INTERACTION_TYPES: FrozenSet[str] = frozenset(
    {
        "browser_open",
        "browser_click",
        "browser_type",
        "browser_scroll",
        "browser_submit",
        "browser_navigate",
        "desktop_open_app",
        "desktop_click",
        "desktop_type",
        "desktop_hotkey",
        "desktop_focus",
        "computer_click",
        "computer_double_click",
        "computer_type",
        "computer_key",
        "computer_drag",
        "computer_scroll",
        "computer_move",
        "computer_wait",
        "window_activate",
    }
)


class PermissionModel:
    """
    决定一个 action_type 在当前权限级别下是否需要用户确认。

    使用方式：
        model = PermissionModel()          # 从环境变量读取
        if model.requires_confirmation(action_type, risk_level):
            # 显示确认对话框 / 返回 gate_required 结果
    """

    def __init__(self, level: PermissionLevel | str | None = None) -> None:
        if level is None:
            raw = os.getenv(_ENV_KEY, "default").strip().lower().replace("-", "_")
            level = raw
        try:
            self.level = PermissionLevel(str(level).lower().replace("-", "_"))
        except ValueError:
            self.level = PermissionLevel.DEFAULT

    # ------------------------------------------------------------------ #
    # 主判断接口                                                             #
    # ------------------------------------------------------------------ #

    def requires_confirmation(self, action_type: str, risk: str = "low") -> bool:
        """
        返回 True 表示该操作需要在执行前向用户确认。

        Parameters
        ----------
        action_type : 动作类型字符串，如 "file_write"、"shell_run"
        risk        : 操作的风险标签，"low" | "medium" | "high"
        """
        atype = (action_type or "").strip().lower()

        # BYPASS 模式：永不拦截
        if self.level == PermissionLevel.BYPASS:
            return False

        # PLAN 模式：仅 PLAN_MODE_ALLOWED_TYPES 可免确认，其余一律需确认/拦截
        if self.level == PermissionLevel.PLAN:
            return atype not in PLAN_MODE_ALLOWED_TYPES

        # 只读操作：任何级别均不需要确认
        if atype in _READ_ONLY_TYPES:
            return False

        # DEFAULT 模式：非只读一律询问
        if self.level == PermissionLevel.DEFAULT:
            return True

        # ACCEPT_EDITS 模式：文件写和浏览器/桌面/computer 交互自动通过
        if self.level == PermissionLevel.ACCEPT_EDITS:
            if atype in _FILE_WRITE_TYPES or atype in _INTERACTION_TYPES:
                return False
            # shell 和 messaging 仍需询问
            return True

        # DONT_ASK 模式：high-risk 操作仍询问，其余全部通过
        if self.level == PermissionLevel.DONT_ASK:
            return risk == "high"

        return True  # 保守默认

    def is_readonly_only(self) -> bool:
        """PLAN 模式：不允许任何写副作用操作。"""
        return self.level == PermissionLevel.PLAN

    def allows_under_plan_mode(self, action_type: str) -> bool:
        """PLAN 级别下允许的动作与 PLAN_MODE_ALLOWED_TYPES 一致（见模块级常量）。"""
        atype = (action_type or "").strip().lower()
        return atype in PLAN_MODE_ALLOWED_TYPES

    def allow_shell(self) -> bool:
        """是否允许在无提示情况下执行 shell 命令。"""
        return self.level in (PermissionLevel.DONT_ASK, PermissionLevel.BYPASS)

    def auto_approve_file_ops(self) -> bool:
        """是否自动批准文件写操作（无需确认）。"""
        return self.level in (
            PermissionLevel.ACCEPT_EDITS,
            PermissionLevel.DONT_ASK,
            PermissionLevel.BYPASS,
        )

    def __repr__(self) -> str:  # pragma: no cover
        return f"PermissionModel(level={self.level.value!r})"
