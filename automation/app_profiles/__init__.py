"""应用画像与可配置启发式（微信等）。"""

from automation.app_profiles.action_merge import (
    normalize_actions_with_merge_rules,
    wechat_heuristic_enabled,
)

__all__ = [
    "normalize_actions_with_merge_rules",
    "wechat_heuristic_enabled",
]
