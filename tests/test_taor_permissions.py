"""TAOR 工具分发与用户闸门、权限级别对齐。"""
import os
from unittest import mock

import pytest

from aria_manager import ARIAManager

pytestmark = pytest.mark.smoke


@pytest.fixture
def manager() -> ARIAManager:
    return ARIAManager(api_key="")


def test_taor_blocks_file_write_by_default(manager: ARIAManager):
    action = {"type": "file_write", "target": "", "params": {"path": "x.txt", "content": "a"}, "risk": "low"}
    blocked, code, _msg = manager.taor_action_blocked_for_dispatch(action)
    assert blocked is True
    assert code in ("user_gate_blocked", "confirmation_required")


def test_taor_allows_gated_when_env_set(manager: ARIAManager):
    action = {"type": "file_write", "target": "", "params": {"path": "x.txt", "content": "a"}, "risk": "low"}
    with mock.patch.dict(os.environ, {"ARIA_TAOR_ALLOW_GATED_ACTIONS": "1"}):
        m2 = ARIAManager(api_key="")
        blocked, _code, _msg = m2.taor_action_blocked_for_dispatch(action)
    assert blocked is False


def test_taor_plan_mode_blocks_non_safe(manager: ARIAManager):
    action = {"type": "file_write", "target": "", "params": {"path": "x.txt", "content": "a"}, "risk": "low"}
    with mock.patch.dict(os.environ, {"ARIA_PERMISSION_LEVEL": "plan"}):
        m2 = ARIAManager(api_key="")
        blocked, code, _msg = m2.taor_action_blocked_for_dispatch(action)
    assert blocked is True
    assert code == "permission_denied"


def test_taor_plan_allows_browser_find(manager: ARIAManager):
    action = {"type": "browser_find", "target": "body", "params": {}, "risk": "low"}
    with mock.patch.dict(os.environ, {"ARIA_PERMISSION_LEVEL": "plan"}):
        m2 = ARIAManager(api_key="")
        blocked, _code, _msg = m2.taor_action_blocked_for_dispatch(action)
    assert blocked is False


def test_taor_plan_allows_web_fetch(manager: ARIAManager):
    action = {"type": "web_fetch", "target": "", "params": {"url": "https://example.com"}, "risk": "low"}
    with mock.patch.dict(os.environ, {"ARIA_PERMISSION_LEVEL": "plan"}):
        m2 = ARIAManager(api_key="")
        blocked, _code, _msg = m2.taor_action_blocked_for_dispatch(action)
    assert blocked is False
