from pathlib import Path

import pytest

from aria_manager import ARIAManager


def test_personality_map_has_full_14_departments_and_178_agents() -> None:
    manager = ARIAManager()
    cfg = manager.personality_map_config if isinstance(manager.personality_map_config, dict) else {}
    stats = cfg.get("stats") if isinstance(cfg.get("stats"), dict) else {}
    assert int(stats.get("departments", 0) or 0) == 14
    assert int(stats.get("agents", 0) or 0) == 178
    global_profiles = cfg.get("global_profiles") if isinstance(cfg.get("global_profiles"), list) else []
    assert len(global_profiles) == 178


def test_personality_catalog_candidates_cover_full_pool_for_exec_agents() -> None:
    manager = ARIAManager()
    root = manager._resolve_agency_agents_root()
    if root is None or not root.is_dir():
        pytest.skip(
            "agency-agents upstream assets not present (clone third_party/agency-agents per THIRD_PARTY_NOTICES.md)"
        )
    for agent_type in ("TextExecAgent", "VisionExecAgent", "SpeechExecAgent"):
        rows = manager.personality_catalog.get(agent_type) or []
        assert len(rows) == 178
        for row in rows:
            source_file = str(row.get("source_file") or row.get("file") or "").strip()
            assert source_file
            assert (Path(root) / source_file).is_file()
            assert isinstance(row.get("excerpt", ""), str)
