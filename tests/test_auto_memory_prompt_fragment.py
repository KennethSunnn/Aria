"""AutoMemoryManager.get_system_prompt_fragment 与 ARIAManager 接线。"""

from pathlib import Path

import pytest

from aria_manager import ARIAManager
from memory import auto_memory as auto_memory_mod


@pytest.fixture
def memory_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "memory"
    entries = root / "entries"
    entries.mkdir(parents=True)
    index = root / "MEMORY.md"
    monkeypatch.setattr(auto_memory_mod, "_MEMORY_DIR", root)
    monkeypatch.setattr(auto_memory_mod, "_ENTRIES_DIR", entries)
    monkeypatch.setattr(auto_memory_mod, "_MEMORY_INDEX_PATH", index)
    return root, entries, index


def test_task_keywords_from_text_empty():
    from memory.auto_memory import AutoMemoryManager

    assert AutoMemoryManager._task_keywords_from_text("") == []
    assert AutoMemoryManager._task_keywords_from_text("   ") == []


def test_task_keywords_extracts_ascii_and_cjk():
    from memory.auto_memory import AutoMemoryManager

    kws = AutoMemoryManager._task_keywords_from_text("run python 脚本")
    assert "python" in kws
    assert any("脚本" in k or k == "脚本" for k in kws) or "本脚" in kws


def test_get_system_prompt_fragment_disabled(memory_paths, monkeypatch: pytest.MonkeyPatch):
    from memory.auto_memory import AutoMemoryManager

    _, _, index = memory_paths
    index.write_text("# Index\n", encoding="utf-8")
    monkeypatch.setenv("ARIA_AUTO_MEMORY_ENABLED", "0")
    mgr = AutoMemoryManager(object())
    assert mgr.get_system_prompt_fragment("hello") == ""


def test_get_system_prompt_fragment_from_memory_md_only(memory_paths):
    from memory.auto_memory import AutoMemoryManager

    _, _, index = memory_paths
    index.write_text("# User notes\nalpha", encoding="utf-8")
    mgr = AutoMemoryManager(object())
    out = mgr.get_system_prompt_fragment("task")
    assert "【ARIA 持续记忆】" in out
    assert "User notes" in out


def test_get_system_prompt_fragment_empty_when_no_content(memory_paths):
    from memory.auto_memory import AutoMemoryManager

    mgr = AutoMemoryManager(object())
    assert mgr.get_system_prompt_fragment("x") == ""


def test_get_system_prompt_fragment_prefers_entries_index(memory_paths):
    from memory.auto_memory import AutoMemoryManager

    _, entries, index = memory_paths
    index.write_text("# flat", encoding="utf-8")
    entry = entries / "my_pref.md"
    entry.write_text(
        '---\nname: "my_pref"\n'
        'type: user_preference\ndescription: "prefers python tooling"\n'
        'created_at: "2026-01-01 00:00:00"\nupdated_at: "2026-01-01 00:00:00"\n'
        'task_id: ""\n---\n\nbody here\n',
        encoding="utf-8",
    )
    mgr = AutoMemoryManager(object())
    out = mgr.get_system_prompt_fragment("python automation")
    assert "【ARIA 持续记忆】" in out
    assert "my_pref" in out
    assert "ARIA Memory Index" in out


def test_aria_manager_memory_fragment_delegates(memory_paths, monkeypatch: pytest.MonkeyPatch):
    _, _, index = memory_paths
    index.write_text("stm bit", encoding="utf-8")
    m = ARIAManager(api_key="")
    frag = m._memory_system_prompt_fragment("hi")
    assert "【ARIA 持续记忆】" in frag
    assert "stm bit" in frag
