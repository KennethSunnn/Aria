"""Guardrails: action allowlists and permission read-only sets stay aligned."""

import pytest

from aria_manager import ARIAManager
from runtime.permissions import (
    _READ_ONLY_TYPES,
    PLAN_MODE_ALLOWED_TYPES,
    SAFE_ACTION_TYPES,
    PermissionModel,
)

pytestmark = pytest.mark.smoke


def test_safe_action_types_are_allowed():
    missing = ARIAManager.SAFE_ACTION_TYPES - ARIAManager.ALLOWED_ACTION_TYPES
    assert not missing, f"SAFE_ACTION_TYPES must be subset of ALLOWED_ACTION_TYPES: {missing}"


def test_plan_mode_allowed_is_readonly_union_safe():
    assert PLAN_MODE_ALLOWED_TYPES == (_READ_ONLY_TYPES | SAFE_ACTION_TYPES)
    assert ARIAManager.SAFE_ACTION_TYPES is SAFE_ACTION_TYPES


def test_plan_mode_readonly_vs_write():
    pm = PermissionModel("plan")
    assert pm.allows_under_plan_mode("browser_find") is True
    assert pm.allows_under_plan_mode("file_read") is True
    assert pm.allows_under_plan_mode("web_fetch") is True
    assert pm.allows_under_plan_mode("file_write") is False
    assert pm.requires_confirmation("web_fetch", "low") is False
    assert pm.requires_confirmation("file_write", "low") is True


def test_new_file_actions_registered():
    """file_read/list/find/append/create_dir must be in ALLOWED_ACTION_TYPES and action_registry."""
    new_actions = {"file_read", "file_list", "file_find", "file_append", "file_create_dir"}
    missing_allowed = new_actions - ARIAManager.ALLOWED_ACTION_TYPES
    assert not missing_allowed, f"Missing from ALLOWED_ACTION_TYPES: {missing_allowed}"


def test_clipboard_actions_registered():
    """clipboard_read / clipboard_write must be in ALLOWED_ACTION_TYPES."""
    clipboard_actions = {"clipboard_read", "clipboard_write"}
    missing = clipboard_actions - ARIAManager.ALLOWED_ACTION_TYPES
    assert not missing, f"Missing from ALLOWED_ACTION_TYPES: {missing}"


def test_file_read_is_readonly():
    """file_read must be in _READ_ONLY_TYPES (plan-mode safe)."""
    assert "file_read" in _READ_ONLY_TYPES, "file_read must be in _READ_ONLY_TYPES"
    pm = PermissionModel("plan")
    assert pm.allows_under_plan_mode("file_read") is True


def test_clipboard_write_requires_confirmation_in_default():
    """clipboard_write is a USER_GATE action, should need confirmation in default mode."""
    assert "clipboard_write" in ARIAManager.USER_GATE_ACTION_TYPES
    pm = PermissionModel("default")
    assert pm.requires_confirmation("clipboard_write", "low") is True


def test_wechat_actions_registered():
    """wechat_* actions must be in ALLOWED_ACTION_TYPES and USER_GATE_ACTION_TYPES."""
    wechat_actions = {"wechat_check_login", "wechat_open_chat", "wechat_send_message"}
    missing_allowed = wechat_actions - ARIAManager.ALLOWED_ACTION_TYPES
    assert not missing_allowed, f"Missing from ALLOWED_ACTION_TYPES: {missing_allowed}"
    gated = {"wechat_open_chat", "wechat_send_message"}
    missing_gate = gated - ARIAManager.USER_GATE_ACTION_TYPES
    assert not missing_gate, f"Missing from USER_GATE_ACTION_TYPES: {missing_gate}"
