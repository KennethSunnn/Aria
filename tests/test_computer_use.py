"""Unit tests for Computer Use helpers (no real mouse movement)."""

import json

import pytest

from automation import computer_use


def test_virtual_screen_metrics_has_keys():
    m = computer_use.virtual_screen_metrics()
    assert {"left", "top", "width", "height"} <= m.keys()
    assert m["width"] > 0 and m["height"] > 0


def test_point_in_allow_regions():
    assert computer_use.point_in_allow_regions(5, 5, [(0, 0, 10, 10)]) is True
    assert computer_use.point_in_allow_regions(50, 50, [(0, 0, 10, 10)]) is False
    assert computer_use.point_in_allow_regions(1, 1, []) is True


def test_resolve_normalized_1000():
    m = {"left": 0, "top": 0, "width": 1000, "height": 500}
    p = computer_use.resolve_screen_point({"x": 500, "y": 250, "coord_space": "normalized_1000"}, metrics=m)
    assert p == (500, 125)


def test_ensure_mutation_allow_regions(monkeypatch):
    monkeypatch.setenv("ARIA_COMPUTER_USE_ALLOW_REGIONS", json.dumps([[0, 0, 10, 10]]))
    monkeypatch.delenv("ARIA_COMPUTER_USE_BLOCK_TITLE_KEYWORDS", raising=False)
    monkeypatch.setenv("ARIA_COMPUTER_USE", "1")
    ok, _ = computer_use.ensure_mutation_allowed(5, 5)
    assert ok is True
    ok2, reason = computer_use.ensure_mutation_allowed(20, 20)
    assert ok2 is False
    assert "allow_regions" in reason


def test_run_screenshot_info_disabled(monkeypatch):
    monkeypatch.setenv("ARIA_COMPUTER_USE", "0")
    r = computer_use.run_screenshot_info({})
    assert r.get("success") is False


def test_run_click_bad_coordinates_includes_diagnostic(monkeypatch):
    monkeypatch.setenv("ARIA_COMPUTER_USE", "1")
    monkeypatch.delenv("ARIA_COMPUTER_USE_ALLOW_REGIONS", raising=False)
    monkeypatch.delenv("ARIA_COMPUTER_USE_BLOCK_TITLE_KEYWORDS", raising=False)
    r = computer_use.run_click({"x": "nope", "y": 1})
    assert r.get("success") is False
    d = r.get("computer_diagnostic")
    assert isinstance(d, dict)
    assert d.get("error_kind") == "bad_coordinates"
    assert "virtual_screen" in d


def test_run_click_allow_regions_blocked_includes_diagnostic(monkeypatch):
    monkeypatch.setenv("ARIA_COMPUTER_USE", "1")
    monkeypatch.setenv("ARIA_COMPUTER_USE_ALLOW_REGIONS", json.dumps([[0, 0, 5, 5]]))
    monkeypatch.delenv("ARIA_COMPUTER_USE_BLOCK_TITLE_KEYWORDS", raising=False)
    r = computer_use.run_click({"x": 100, "y": 100, "coord_space": "absolute"})
    assert r.get("success") is False
    d = r.get("computer_diagnostic")
    assert d.get("allowlist_or_policy_blocked") is True
    assert d.get("resolved_pixel") == {"x": 100, "y": 100}


def test_screenshot_then_resolve_normalized_minimal_loop(monkeypatch):
    """Minimal closed-loop sanity: metrics + normalized mapping (no pyautogui click)."""
    monkeypatch.setenv("ARIA_COMPUTER_USE", "1")
    monkeypatch.delenv("ARIA_COMPUTER_USE_ALLOW_REGIONS", raising=False)
    monkeypatch.delenv("ARIA_COMPUTER_USE_BLOCK_TITLE_KEYWORDS", raising=False)
    info = computer_use.run_screenshot_info({})
    assert info.get("success") is True
    m = computer_use.virtual_screen_metrics()
    p = computer_use.resolve_screen_point(
        {"x": 500, "y": 500, "coord_space": "normalized_1000"},
        metrics=m,
    )
    assert p is not None
    assert len(p) == 2


def test_react_observation_serializes_computer_diagnostic():
    from aria_manager import ARIAManager

    m = ARIAManager(api_key="")
    row = {
        "status": "error",
        "action": "computer_click",
        "stdout": "",
        "stderr": "missing_or_invalid_x_y",
        "error_code": "execution_failed",
        "result": {
            "success": False,
            "computer_diagnostic": {"error_kind": "bad_coordinates", "coord_space": "absolute"},
        },
    }
    obs = m._react_observation_from_row(row)
    assert "computer_diagnostic=" in obs
    assert "bad_coordinates" in obs
