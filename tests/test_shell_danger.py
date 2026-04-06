"""runtime.shell_danger 与 aria_manager shell 校验一致。"""

import pytest

from aria_manager import ARIAManager
from runtime.shell_danger import shell_command_blocked_reason

pytestmark = pytest.mark.smoke


@pytest.fixture
def manager() -> ARIAManager:
    return ARIAManager(api_key="")


def test_shell_danger_blocks_substring_rm_rf_root():
    assert shell_command_blocked_reason("rm -rf /tmp") is not None


def test_shell_danger_blocks_regex_drop_table():
    assert shell_command_blocked_reason("SELECT 1; DROP TABLE users;") is not None


def test_sanitize_shell_matches_shell_danger(manager: ARIAManager):
    with pytest.raises(ValueError) as exc:
        manager._sanitize_shell_command("rm -rf /")
    assert "blocked_command" in str(exc.value)
