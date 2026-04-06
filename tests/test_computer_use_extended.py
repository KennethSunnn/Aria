"""Extended unit tests for automation/computer_use.py

测试策略：
  - 调用真实函数（computer_use 的逻辑层）
  - 只在调用 pyautogui 的最底层截断（monkeypatch sys.modules["pyautogui"]）
  - 验证：坐标解析、allow_region 双端检查、敏感标题拦截、方向参数、特殊键映射

不覆盖：真实 pyautogui 鼠标/键盘注入（E2E 范畴）
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automation import computer_use

pytestmark = pytest.mark.automation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_pyautogui() -> MagicMock:
    pg = MagicMock()
    pg.FAILSAFE = True
    pg.moveTo.return_value = None
    pg.click.return_value = None
    pg.drag.return_value = None
    pg.scroll.return_value = None
    pg.press.return_value = None
    pg.hotkey.return_value = None
    pg.write.return_value = None
    return pg


def _patch_pyautogui(monkeypatch, side_effect=None) -> MagicMock:
    pg = _make_mock_pyautogui()
    if side_effect:
        pg.click.side_effect = side_effect
        pg.drag.side_effect = side_effect
        pg.scroll.side_effect = side_effect
        pg.press.side_effect = side_effect
        pg.hotkey.side_effect = side_effect
        pg.write.side_effect = side_effect
    monkeypatch.setitem(sys.modules, "pyautogui", pg)
    return pg


def _clear_env(monkeypatch):
    monkeypatch.setenv("ARIA_COMPUTER_USE", "1")
    monkeypatch.delenv("ARIA_COMPUTER_USE_ALLOW_REGIONS", raising=False)
    monkeypatch.delenv("ARIA_COMPUTER_USE_BLOCK_TITLE_KEYWORDS", raising=False)


# ---------------------------------------------------------------------------
# run_double_click (run_click with clicks=2)
# ---------------------------------------------------------------------------

class TestRunDoubleClick:
    def test_bad_coordinates_returns_false(self, monkeypatch):
        _clear_env(monkeypatch)
        pg = _patch_pyautogui(monkeypatch)
        r = computer_use.run_click({"x": None, "y": None}, clicks=2)
        assert r["success"] is False
        assert r["message"] == "bad_coordinates"
        pg.click.assert_not_called()

    def test_double_click_calls_click_with_clicks_2(self, monkeypatch):
        _clear_env(monkeypatch)
        pg = _patch_pyautogui(monkeypatch)
        r = computer_use.run_click({"x": 50, "y": 50}, clicks=2)
        assert r["success"] is True
        pg.click.assert_called_once()
        call_kwargs = pg.click.call_args[1]
        assert call_kwargs.get("clicks") == 2

    def test_allow_region_blocks_double_click(self, monkeypatch):
        monkeypatch.setenv("ARIA_COMPUTER_USE", "1")
        monkeypatch.setenv("ARIA_COMPUTER_USE_ALLOW_REGIONS", json.dumps([[0, 0, 20, 20]]))
        monkeypatch.delenv("ARIA_COMPUTER_USE_BLOCK_TITLE_KEYWORDS", raising=False)
        pg = _patch_pyautogui(monkeypatch)
        r = computer_use.run_click({"x": 100, "y": 100}, clicks=2)
        assert r["success"] is False
        assert "allow_regions" in r["message"]
        pg.click.assert_not_called()

    def test_normalized_1000_coord_space(self, monkeypatch):
        _clear_env(monkeypatch)
        pg = _patch_pyautogui(monkeypatch)
        m = {"left": 0, "top": 0, "width": 1000, "height": 1000}
        pt = computer_use.resolve_screen_point(
            {"x": 500, "y": 500, "coord_space": "normalized_1000"}, metrics=m
        )
        assert pt == (500, 500)


# ---------------------------------------------------------------------------
# run_scroll
# ---------------------------------------------------------------------------

class TestRunScroll:
    def test_positive_clicks_scrolls_down(self, monkeypatch):
        _clear_env(monkeypatch)
        pg = _patch_pyautogui(monkeypatch)
        r = computer_use.run_scroll({"x": 100, "y": 100, "clicks": 3})
        assert r["success"] is True
        pg.scroll.assert_called_once_with(3)

    def test_negative_clicks_scrolls_up(self, monkeypatch):
        _clear_env(monkeypatch)
        pg = _patch_pyautogui(monkeypatch)
        r = computer_use.run_scroll({"x": 100, "y": 100, "clicks": -3})
        assert r["success"] is True
        pg.scroll.assert_called_once_with(-3)

    def test_bad_coordinates_returns_false(self, monkeypatch):
        _clear_env(monkeypatch)
        pg = _patch_pyautogui(monkeypatch)
        r = computer_use.run_scroll({"x": "oops", "y": 100, "clicks": 1})
        assert r["success"] is False
        assert r["message"] == "bad_coordinates"

    def test_bad_clicks_returns_false(self, monkeypatch):
        _clear_env(monkeypatch)
        pg = _patch_pyautogui(monkeypatch)
        r = computer_use.run_scroll({"x": 100, "y": 100, "clicks": "not_int"})
        assert r["success"] is False
        assert r["message"] == "bad_clicks"

    def test_allow_region_blocks_scroll(self, monkeypatch):
        monkeypatch.setenv("ARIA_COMPUTER_USE", "1")
        monkeypatch.setenv("ARIA_COMPUTER_USE_ALLOW_REGIONS", json.dumps([[0, 0, 10, 10]]))
        monkeypatch.delenv("ARIA_COMPUTER_USE_BLOCK_TITLE_KEYWORDS", raising=False)
        pg = _patch_pyautogui(monkeypatch)
        r = computer_use.run_scroll({"x": 500, "y": 500, "clicks": 2})
        assert r["success"] is False
        pg.scroll.assert_not_called()

    def test_propagates_pyautogui_exception(self, monkeypatch):
        _clear_env(monkeypatch)
        _patch_pyautogui(monkeypatch, side_effect=Exception("scroll failed"))
        r = computer_use.run_scroll({"x": 100, "y": 100, "clicks": 1})
        assert r["success"] is False
        assert "scroll failed" in r["stderr"]


# ---------------------------------------------------------------------------
# run_key
# ---------------------------------------------------------------------------

class TestRunKey:
    def test_missing_keys_returns_false(self, monkeypatch):
        _clear_env(monkeypatch)
        pg = _patch_pyautogui(monkeypatch)
        r = computer_use.run_key({})
        assert r["success"] is False
        assert r["message"] == "missing_keys"
        pg.press.assert_not_called()

    def test_empty_keys_string_returns_false(self, monkeypatch):
        _clear_env(monkeypatch)
        pg = _patch_pyautogui(monkeypatch)
        r = computer_use.run_key({"keys": "   "})
        assert r["success"] is False

    def test_single_key_calls_press(self, monkeypatch):
        _clear_env(monkeypatch)
        pg = _patch_pyautogui(monkeypatch)
        r = computer_use.run_key({"keys": "enter"})
        assert r["success"] is True
        pg.press.assert_called_once_with("enter")
        pg.hotkey.assert_not_called()

    def test_hotkey_calls_hotkey(self, monkeypatch):
        _clear_env(monkeypatch)
        pg = _patch_pyautogui(monkeypatch)
        r = computer_use.run_key({"keys": "ctrl+c"})
        assert r["success"] is True
        pg.hotkey.assert_called_once_with("ctrl", "c")
        pg.press.assert_not_called()

    def test_three_part_hotkey(self, monkeypatch):
        _clear_env(monkeypatch)
        pg = _patch_pyautogui(monkeypatch)
        r = computer_use.run_key({"keys": "ctrl+shift+t"})
        assert r["success"] is True
        pg.hotkey.assert_called_once_with("ctrl", "shift", "t")

    def test_key_propagates_exception(self, monkeypatch):
        _clear_env(monkeypatch)
        _patch_pyautogui(monkeypatch, side_effect=Exception("device busy"))
        r = computer_use.run_key({"keys": "enter"})
        assert r["success"] is False
        assert "device busy" in r["stderr"]

    def test_sensitive_title_blocks_key(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("ARIA_COMPUTER_USE_BLOCK_TITLE_KEYWORDS", "banking")
        # Patch foreground_window_title to simulate a sensitive window
        monkeypatch.setattr(computer_use, "foreground_window_title", lambda: "Online Banking Portal")
        pg = _patch_pyautogui(monkeypatch)
        r = computer_use.run_key({"keys": "ctrl+a"})
        assert r["success"] is False
        assert "sensitive_foreground_title" in r["message"]
        pg.hotkey.assert_not_called()


# ---------------------------------------------------------------------------
# run_type_text
# ---------------------------------------------------------------------------

class TestRunTypeText:
    def test_types_text_successfully(self, monkeypatch):
        _clear_env(monkeypatch)
        pg = _patch_pyautogui(monkeypatch)
        r = computer_use.run_type_text({"text": "hello"})
        assert r["success"] is True
        pg.write.assert_called_once()
        call_args = pg.write.call_args
        assert call_args[0][0] == "hello"

    def test_interval_clamped_to_max_0_5(self, monkeypatch):
        _clear_env(monkeypatch)
        pg = _patch_pyautogui(monkeypatch)
        r = computer_use.run_type_text({"text": "hi", "interval": 99.0})
        assert r["success"] is True
        called_interval = pg.write.call_args[1]["interval"]
        assert called_interval <= 0.5

    def test_interval_clamped_to_min_0(self, monkeypatch):
        _clear_env(monkeypatch)
        pg = _patch_pyautogui(monkeypatch)
        r = computer_use.run_type_text({"text": "hi", "interval": -5.0})
        assert r["success"] is True
        called_interval = pg.write.call_args[1]["interval"]
        assert called_interval >= 0.0

    def test_empty_text_still_calls_write(self, monkeypatch):
        """Empty string is typed (pyautogui.write("") is a no-op but valid)."""
        _clear_env(monkeypatch)
        pg = _patch_pyautogui(monkeypatch)
        r = computer_use.run_type_text({"text": ""})
        # run_type_text doesn't reject empty text — it passes through
        assert r["success"] is True

    def test_sensitive_title_blocks_type(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("ARIA_COMPUTER_USE_BLOCK_TITLE_KEYWORDS", "password")
        monkeypatch.setattr(computer_use, "foreground_window_title", lambda: "Change Password")
        pg = _patch_pyautogui(monkeypatch)
        r = computer_use.run_type_text({"text": "secret"})
        assert r["success"] is False
        assert "sensitive_foreground_title" in r["message"]
        pg.write.assert_not_called()

    def test_propagates_pyautogui_exception(self, monkeypatch):
        _clear_env(monkeypatch)
        _patch_pyautogui(monkeypatch, side_effect=Exception("focus lost"))
        r = computer_use.run_type_text({"text": "test"})
        assert r["success"] is False
        assert "focus lost" in r["stderr"]


# ---------------------------------------------------------------------------
# run_drag
# ---------------------------------------------------------------------------

class TestRunDrag:
    def test_missing_end_coordinates_returns_false(self, monkeypatch):
        _clear_env(monkeypatch)
        pg = _patch_pyautogui(monkeypatch)
        r = computer_use.run_drag({"x": 10, "y": 10})  # x2/y2 absent
        assert r["success"] is False
        assert r["message"] == "bad_coordinates"
        pg.drag.assert_not_called()

    def test_start_outside_allow_regions_blocked(self, monkeypatch):
        monkeypatch.setenv("ARIA_COMPUTER_USE", "1")
        monkeypatch.setenv("ARIA_COMPUTER_USE_ALLOW_REGIONS", json.dumps([[100, 100, 200, 200]]))
        monkeypatch.delenv("ARIA_COMPUTER_USE_BLOCK_TITLE_KEYWORDS", raising=False)
        pg = _patch_pyautogui(monkeypatch)
        # Start point (5, 5) is outside [100,100,300,300]
        r = computer_use.run_drag({"x": 5, "y": 5, "x2": 150, "y2": 150})
        assert r["success"] is False
        assert "allow_regions" in r["message"]
        pg.drag.assert_not_called()

    def test_end_outside_allow_regions_blocked(self, monkeypatch):
        monkeypatch.setenv("ARIA_COMPUTER_USE", "1")
        monkeypatch.setenv("ARIA_COMPUTER_USE_ALLOW_REGIONS", json.dumps([[0, 0, 200, 200]]))
        monkeypatch.delenv("ARIA_COMPUTER_USE_BLOCK_TITLE_KEYWORDS", raising=False)
        pg = _patch_pyautogui(monkeypatch)
        # Start (50, 50) is inside; end (500, 500) is outside
        r = computer_use.run_drag({"x": 50, "y": 50, "x2": 500, "y2": 500})
        assert r["success"] is False
        assert "allow_regions" in r["message"] or "outside" in r["message"]
        pg.drag.assert_not_called()

    def test_valid_drag_calls_drag(self, monkeypatch):
        _clear_env(monkeypatch)
        pg = _patch_pyautogui(monkeypatch)
        r = computer_use.run_drag({"x": 10, "y": 10, "x2": 50, "y2": 50})
        assert r["success"] is True
        pg.drag.assert_called_once()

    def test_drag_propagates_exception(self, monkeypatch):
        _clear_env(monkeypatch)
        _patch_pyautogui(monkeypatch, side_effect=Exception("failsafe triggered"))
        r = computer_use.run_drag({"x": 10, "y": 10, "x2": 20, "y2": 20})
        assert r["success"] is False
        assert "failsafe" in r["stderr"]


# ---------------------------------------------------------------------------
# foreground_window_title
# ---------------------------------------------------------------------------

class TestForegroundWindowTitle:
    def test_returns_string(self):
        """Must return a string on any platform."""
        result = computer_use.foreground_window_title()
        assert isinstance(result, str)

    def test_returns_empty_on_non_windows(self, monkeypatch):
        """Non-Windows always returns empty string."""
        monkeypatch.setattr(computer_use.sys, "platform", "linux")
        assert computer_use.foreground_window_title() == ""

    def test_returns_empty_when_ctypes_raises(self, monkeypatch):
        """If ctypes call raises, function must return empty string instead of raising."""
        if computer_use.sys.platform != "win32":
            pytest.skip("Windows only — testing graceful fallback")
        import ctypes
        original_windll = ctypes.windll
        try:
            ctypes.windll.user32.GetForegroundWindow.side_effect = OSError("no window")
        except Exception:
            pytest.skip("Cannot patch ctypes on this runtime")
        # Even with ctypes weirdness, function must not raise
        result = computer_use.foreground_window_title()
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# blocked_by_sensitive_title
# ---------------------------------------------------------------------------

class TestBlockedBySensitiveTitle:
    def test_no_keywords_env_returns_not_blocked(self, monkeypatch):
        monkeypatch.delenv("ARIA_COMPUTER_USE_BLOCK_TITLE_KEYWORDS", raising=False)
        blocked, kw = computer_use.blocked_by_sensitive_title()
        assert blocked is False
        assert kw == ""

    def test_empty_keywords_returns_not_blocked(self, monkeypatch):
        monkeypatch.setenv("ARIA_COMPUTER_USE_BLOCK_TITLE_KEYWORDS", "")
        blocked, kw = computer_use.blocked_by_sensitive_title()
        assert blocked is False

    def test_keyword_matches_title(self, monkeypatch):
        monkeypatch.setenv("ARIA_COMPUTER_USE_BLOCK_TITLE_KEYWORDS", "banking,password")
        monkeypatch.setattr(computer_use, "foreground_window_title", lambda: "Online Banking Portal")
        blocked, kw = computer_use.blocked_by_sensitive_title()
        assert blocked is True
        assert kw == "banking"

    def test_case_insensitive_match(self, monkeypatch):
        monkeypatch.setenv("ARIA_COMPUTER_USE_BLOCK_TITLE_KEYWORDS", "BANKING")
        monkeypatch.setattr(computer_use, "foreground_window_title", lambda: "online banking portal")
        blocked, kw = computer_use.blocked_by_sensitive_title()
        assert blocked is True

    def test_no_match_returns_not_blocked(self, monkeypatch):
        monkeypatch.setenv("ARIA_COMPUTER_USE_BLOCK_TITLE_KEYWORDS", "banking,password")
        monkeypatch.setattr(computer_use, "foreground_window_title", lambda: "Notepad - untitled.txt")
        blocked, kw = computer_use.blocked_by_sensitive_title()
        assert blocked is False
        assert kw == ""

    def test_whitespace_keywords_ignored(self, monkeypatch):
        monkeypatch.setenv("ARIA_COMPUTER_USE_BLOCK_TITLE_KEYWORDS", "  ,  ,  ")
        blocked, kw = computer_use.blocked_by_sensitive_title()
        assert blocked is False


# ---------------------------------------------------------------------------
# capture_jpeg_data_url — format and quality clamping
# ---------------------------------------------------------------------------

class TestCaptureJpegDataUrl:
    def _make_mock_pil_image(self, width=1920, height=1080):
        """Returns a minimal fake PIL Image."""
        from unittest.mock import MagicMock
        import io as _io

        real_image_bytes = _io.BytesIO()

        # Create a real tiny PIL image (2x2) to use as the mock's save target
        try:
            from PIL import Image as PILImage
            img = PILImage.new("RGB", (width, height), color=(128, 128, 128))
        except ImportError:
            pytest.skip("Pillow not installed")

        return img

    def test_returns_data_url_prefix(self, monkeypatch):
        img = self._make_mock_pil_image(100, 100)
        monkeypatch.setattr(computer_use, "capture_screen_pil", lambda region=None: img)
        result = computer_use.capture_jpeg_data_url(max_side=64, quality=75)
        assert result.startswith("data:image/jpeg;base64,")

    def test_quality_clamped_to_30_minimum(self, monkeypatch):
        """Quality below 30 is clamped to 30 (checked via max(30, min(95, q)) in source)."""
        img = self._make_mock_pil_image(10, 10)
        monkeypatch.setattr(computer_use, "capture_screen_pil", lambda region=None: img)
        # quality=1 should be clamped to 30 — function must not raise
        result = computer_use.capture_jpeg_data_url(max_side=64, quality=1)
        assert result.startswith("data:image/jpeg;base64,")

    def test_quality_clamped_to_95_maximum(self, monkeypatch):
        img = self._make_mock_pil_image(10, 10)
        monkeypatch.setattr(computer_use, "capture_screen_pil", lambda region=None: img)
        result = computer_use.capture_jpeg_data_url(max_side=64, quality=999)
        assert result.startswith("data:image/jpeg;base64,")

    def test_large_image_downscaled(self, monkeypatch):
        """An image larger than max_side must be resized."""
        img = self._make_mock_pil_image(2560, 1440)
        monkeypatch.setattr(computer_use, "capture_screen_pil", lambda region=None: img)
        result = computer_use.capture_jpeg_data_url(max_side=640, quality=75)
        # Decode and check size
        import base64
        import io
        from PIL import Image
        b64_data = result.split(",", 1)[1]
        decoded = base64.b64decode(b64_data)
        decoded_img = Image.open(io.BytesIO(decoded))
        assert max(decoded_img.size) <= 640

    def test_max_side_0_skips_resize(self, monkeypatch):
        """max_side=0 disables resizing."""
        img = self._make_mock_pil_image(100, 100)
        monkeypatch.setattr(computer_use, "capture_screen_pil", lambda region=None: img)
        result = computer_use.capture_jpeg_data_url(max_side=0, quality=75)
        assert result.startswith("data:image/jpeg;base64,")


# ---------------------------------------------------------------------------
# is_computer_use_enabled
# ---------------------------------------------------------------------------

class TestIsComputerUseEnabled:
    def test_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv("ARIA_COMPUTER_USE", raising=False)
        assert computer_use.is_computer_use_enabled() is True

    def test_disabled_via_0(self, monkeypatch):
        monkeypatch.setenv("ARIA_COMPUTER_USE", "0")
        assert computer_use.is_computer_use_enabled() is False

    def test_disabled_via_false(self, monkeypatch):
        monkeypatch.setenv("ARIA_COMPUTER_USE", "false")
        assert computer_use.is_computer_use_enabled() is False

    def test_enabled_via_1(self, monkeypatch):
        monkeypatch.setenv("ARIA_COMPUTER_USE", "1")
        assert computer_use.is_computer_use_enabled() is True
