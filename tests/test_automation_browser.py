"""Unit tests for automation/browser_driver.py

测试策略：
  - 调用真实函数（browser_driver 的逻辑层）
  - 只在调用 Playwright _page 对象的最底层截断（monkeypatch 全局 _page 变量）
  - 验证：参数校验、错误传播、条件分支、环境变量开关

不覆盖：Playwright 真实 DOM 交互（那是 E2E/集成测试的范畴）
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automation import browser_driver

pytestmark = pytest.mark.automation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_page():
    """返回一个模拟 Playwright page 对象，所有方法默认成功。"""
    page = MagicMock()
    page.goto.return_value = None
    page.click.return_value = None
    page.fill.return_value = None
    page.hover.return_value = None
    page.select_option.return_value = None
    page.set_input_files.return_value = None
    page.evaluate.return_value = None
    page.content.return_value = "<html></html>"
    page.keyboard.press.return_value = None
    page.query_selector_all.return_value = []
    page.query_selector.return_value = None
    page.wait_for_selector.return_value = MagicMock()
    return page


@pytest.fixture(autouse=True)
def reset_browser_state(monkeypatch):
    """每个测试前重置 browser_driver 模块级全局状态，避免测试间污染。"""
    monkeypatch.setattr(browser_driver, "_page", None)
    monkeypatch.setattr(browser_driver, "_pw", None)
    monkeypatch.setattr(browser_driver, "_browser", None)
    monkeypatch.setattr(browser_driver, "_context", None)
    monkeypatch.setattr(browser_driver, "_import_error", None)
    yield


# ---------------------------------------------------------------------------
# is_playwright_enabled
# ---------------------------------------------------------------------------

class TestIsPlaywrightEnabled:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("ARIA_PLAYWRIGHT", raising=False)
        assert browser_driver.is_playwright_enabled() is False

    def test_enabled_via_env_1(self, monkeypatch):
        monkeypatch.setenv("ARIA_PLAYWRIGHT", "1")
        assert browser_driver.is_playwright_enabled() is True

    def test_enabled_via_env_true(self, monkeypatch):
        monkeypatch.setenv("ARIA_PLAYWRIGHT", "true")
        assert browser_driver.is_playwright_enabled() is True

    def test_disabled_via_env_0(self, monkeypatch):
        monkeypatch.setenv("ARIA_PLAYWRIGHT", "0")
        assert browser_driver.is_playwright_enabled() is False


# ---------------------------------------------------------------------------
# default_timeout_ms
# ---------------------------------------------------------------------------

class TestDefaultTimeoutMs:
    def test_returns_fallback_when_not_set(self, monkeypatch):
        monkeypatch.delenv("ARIA_PLAYWRIGHT_DEFAULT_TIMEOUT_MS", raising=False)
        assert browser_driver.default_timeout_ms(30_000) == 30_000

    def test_parses_valid_env(self, monkeypatch):
        monkeypatch.setenv("ARIA_PLAYWRIGHT_DEFAULT_TIMEOUT_MS", "5000")
        assert browser_driver.default_timeout_ms() == 5_000

    def test_clamps_below_minimum(self, monkeypatch):
        monkeypatch.setenv("ARIA_PLAYWRIGHT_DEFAULT_TIMEOUT_MS", "100")
        assert browser_driver.default_timeout_ms() == 500

    def test_clamps_above_maximum(self, monkeypatch):
        monkeypatch.setenv("ARIA_PLAYWRIGHT_DEFAULT_TIMEOUT_MS", "999999")
        assert browser_driver.default_timeout_ms() == 300_000

    def test_ignores_non_numeric(self, monkeypatch):
        monkeypatch.setenv("ARIA_PLAYWRIGHT_DEFAULT_TIMEOUT_MS", "fast")
        assert browser_driver.default_timeout_ms(20_000) == 20_000


# ---------------------------------------------------------------------------
# ensure_session — error path (playwright not installed)
# ---------------------------------------------------------------------------

class TestEnsureSessionError:
    def test_returns_false_when_playwright_import_fails(self, monkeypatch):
        """If playwright is not importable, ensure_session returns (False, error_msg)."""
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "playwright.sync_api":
                raise ImportError("No module named 'playwright'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)
        ok, err = browser_driver.ensure_session()
        assert ok is False
        assert "playwright" in err.lower()

    def test_returns_true_when_page_already_set(self, monkeypatch):
        """If _page is already set, ensure_session returns (True, '') immediately."""
        monkeypatch.setattr(browser_driver, "_page", _make_mock_page())
        ok, err = browser_driver.ensure_session()
        assert ok is True
        assert err == ""


# ---------------------------------------------------------------------------
# navigate
# ---------------------------------------------------------------------------

class TestNavigate:
    def test_adds_https_prefix(self, monkeypatch):
        """navigate() prepends https:// if the URL has no scheme."""
        mock_page = _make_mock_page()
        monkeypatch.setattr(browser_driver, "_page", mock_page)
        ok, err = browser_driver.navigate("example.com")
        assert ok is True
        call_args = mock_page.goto.call_args
        assert call_args[0][0].startswith("https://")

    def test_keeps_existing_scheme(self, monkeypatch):
        mock_page = _make_mock_page()
        monkeypatch.setattr(browser_driver, "_page", mock_page)
        ok, err = browser_driver.navigate("http://example.com")
        assert ok is True
        assert mock_page.goto.call_args[0][0] == "http://example.com"

    def test_returns_false_on_playwright_exception(self, monkeypatch):
        mock_page = _make_mock_page()
        mock_page.goto.side_effect = Exception("net::ERR_NAME_NOT_RESOLVED")
        monkeypatch.setattr(browser_driver, "_page", mock_page)
        ok, err = browser_driver.navigate("https://notexist.invalid")
        assert ok is False
        assert "ERR_NAME" in err

    def test_returns_false_when_session_unavailable(self, monkeypatch):
        """navigate() propagates ensure_session failure."""
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "playwright.sync_api":
                raise ImportError("no playwright")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)
        ok, err = browser_driver.navigate("https://example.com")
        assert ok is False


# ---------------------------------------------------------------------------
# click
# ---------------------------------------------------------------------------

class TestClick:
    def test_missing_selector_returns_false(self, monkeypatch):
        mock_page = _make_mock_page()
        monkeypatch.setattr(browser_driver, "_page", mock_page)
        ok, err = browser_driver.click("")
        assert ok is False
        assert err == "missing_selector"

    def test_none_selector_returns_false(self, monkeypatch):
        mock_page = _make_mock_page()
        monkeypatch.setattr(browser_driver, "_page", mock_page)
        ok, err = browser_driver.click(None)
        assert ok is False
        assert err == "missing_selector"

    def test_click_calls_page_click(self, monkeypatch):
        mock_page = _make_mock_page()
        monkeypatch.setattr(browser_driver, "_page", mock_page)
        ok, err = browser_driver.click("#submit")
        assert ok is True
        mock_page.click.assert_called_once()
        assert mock_page.click.call_args[0][0] == "#submit"

    def test_navigate_url_triggers_goto_before_click(self, monkeypatch):
        mock_page = _make_mock_page()
        monkeypatch.setattr(browser_driver, "_page", mock_page)
        ok, err = browser_driver.click("#btn", navigate_url="https://example.com")
        assert ok is True
        mock_page.goto.assert_called_once()
        mock_page.click.assert_called_once()
        # goto must be called before click
        call_order = [c[0] for c in mock_page.method_calls if c[0] in ("goto", "click")]
        assert call_order == ["goto", "click"]

    def test_propagates_playwright_exception(self, monkeypatch):
        mock_page = _make_mock_page()
        mock_page.click.side_effect = Exception("Timeout exceeded")
        monkeypatch.setattr(browser_driver, "_page", mock_page)
        ok, err = browser_driver.click("#btn")
        assert ok is False
        assert "Timeout" in err


# ---------------------------------------------------------------------------
# fill
# ---------------------------------------------------------------------------

class TestFill:
    def test_missing_selector(self, monkeypatch):
        mock_page = _make_mock_page()
        monkeypatch.setattr(browser_driver, "_page", mock_page)
        ok, err = browser_driver.fill("", "text")
        assert ok is False
        assert err == "missing_selector"

    def test_fill_calls_page_fill(self, monkeypatch):
        mock_page = _make_mock_page()
        monkeypatch.setattr(browser_driver, "_page", mock_page)
        ok, err = browser_driver.fill("#name", "Alice")
        assert ok is True
        mock_page.fill.assert_called_once_with("#name", "Alice", timeout=mock_page.fill.call_args[1]["timeout"])

    def test_fill_propagates_exception(self, monkeypatch):
        mock_page = _make_mock_page()
        mock_page.fill.side_effect = Exception("element not found")
        monkeypatch.setattr(browser_driver, "_page", mock_page)
        ok, err = browser_driver.fill("#q", "hello")
        assert ok is False
        assert "not found" in err


# ---------------------------------------------------------------------------
# press_key
# ---------------------------------------------------------------------------

class TestPressKey:
    def test_missing_key_returns_false(self, monkeypatch):
        mock_page = _make_mock_page()
        monkeypatch.setattr(browser_driver, "_page", mock_page)
        ok, err = browser_driver.press_key("")
        assert ok is False
        assert err == "missing_key"

    def test_press_without_selector_uses_keyboard(self, monkeypatch):
        mock_page = _make_mock_page()
        monkeypatch.setattr(browser_driver, "_page", mock_page)
        ok, err = browser_driver.press_key("Enter")
        assert ok is True
        mock_page.keyboard.press.assert_called_once_with("Enter")

    def test_press_with_selector_uses_locator(self, monkeypatch):
        mock_page = _make_mock_page()
        mock_locator = MagicMock()
        mock_page.locator.return_value = mock_locator
        monkeypatch.setattr(browser_driver, "_page", mock_page)
        ok, err = browser_driver.press_key("Enter", selector="#input")
        assert ok is True
        mock_page.locator.assert_called_once_with("#input")
        mock_locator.press.assert_called_once()


# ---------------------------------------------------------------------------
# find_elements
# ---------------------------------------------------------------------------

class TestFindElements:
    def test_missing_selector_returns_false(self, monkeypatch):
        mock_page = _make_mock_page()
        monkeypatch.setattr(browser_driver, "_page", mock_page)
        ok, results, err = browser_driver.find_elements("")
        assert ok is False

    def test_returns_list_on_success(self, monkeypatch):
        mock_page = _make_mock_page()
        el = MagicMock()
        el.text_content.return_value = "Click me"
        el.bounding_box.return_value = {"x": 10, "y": 20, "width": 100, "height": 30}
        mock_page.query_selector_all.return_value = [el]
        monkeypatch.setattr(browser_driver, "_page", mock_page)
        ok, results, err = browser_driver.find_elements(".btn")
        assert ok is True
        assert len(results) == 1
        assert results[0]["text"] == "Click me"

    def test_text_filter_excludes_non_matching(self, monkeypatch):
        mock_page = _make_mock_page()
        el1 = MagicMock()
        el1.text_content.return_value = "Submit"
        el1.bounding_box.return_value = {"x": 0, "y": 0, "width": 50, "height": 20}
        el2 = MagicMock()
        el2.text_content.return_value = "Cancel"
        el2.bounding_box.return_value = {"x": 0, "y": 0, "width": 50, "height": 20}
        mock_page.query_selector_all.return_value = [el1, el2]
        monkeypatch.setattr(browser_driver, "_page", mock_page)
        ok, results, err = browser_driver.find_elements(".btn", text_contains="Submit")
        assert ok is True
        assert len(results) == 1
        assert results[0]["text"] == "Submit"


# ---------------------------------------------------------------------------
# playwright_package_installed
# ---------------------------------------------------------------------------

class TestPlaywrightPackageInstalled:
    def test_returns_false_when_not_installed(self, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "playwright":
                raise ImportError("no playwright")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)
        assert browser_driver.playwright_package_installed() is False
