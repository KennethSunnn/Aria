"""
tests/test_hybrid_planner.py — HybridPlanner + PlanTracker 单元测试
"""
from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# 确保项目根目录在 sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtime.hybrid_planner import HybridPlanner, PlanTracker, _COMPLEXITY_THRESHOLD


# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #

def _make_manager(llm_return: str = "{}") -> MagicMock:
    mgr = MagicMock()
    mgr.ALLOWED_ACTION_TYPES = {"browser_open", "file_write", "web_fetch"}
    mgr._call_llm.return_value = llm_return
    mgr._extract_json_object.side_effect = lambda text: (
        __import__("json").loads(text) if text and text.strip().startswith("{") else None
    )
    return mgr


def _pa(mode="action", task_form="mixed", score=5, react=True) -> dict:
    return {
        "mode": mode,
        "task_form": task_form,
        "complexity_score": score,
        "react_recommended": react,
    }


# ------------------------------------------------------------------ #
# should_plan
# ------------------------------------------------------------------ #

class TestShouldPlan:
    def test_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("ARIA_HYBRID_PLAN", "0")
        p = HybridPlanner(_make_manager())
        assert p.should_plan("task", _pa()) is False

    def test_skips_qa_mode(self, monkeypatch):
        monkeypatch.setenv("ARIA_HYBRID_PLAN", "1")
        p = HybridPlanner(_make_manager())
        assert p.should_plan("task", _pa(mode="qa")) is False

    def test_skips_small_talk(self, monkeypatch):
        monkeypatch.setenv("ARIA_HYBRID_PLAN", "1")
        p = HybridPlanner(_make_manager())
        assert p.should_plan("task", _pa(mode="small_talk")) is False

    def test_skips_qa_only_task_form(self, monkeypatch):
        monkeypatch.setenv("ARIA_HYBRID_PLAN", "1")
        p = HybridPlanner(_make_manager())
        assert p.should_plan("task", _pa(task_form="qa_only")) is False

    def test_skips_low_complexity(self, monkeypatch):
        monkeypatch.setenv("ARIA_HYBRID_PLAN", "1")
        p = HybridPlanner(_make_manager())
        assert p.should_plan("task", _pa(score=_COMPLEXITY_THRESHOLD - 1)) is False

    def test_skips_when_react_not_recommended(self, monkeypatch):
        monkeypatch.setenv("ARIA_HYBRID_PLAN", "1")
        p = HybridPlanner(_make_manager())
        assert p.should_plan("task", _pa(score=6, react=False)) is False

    def test_triggers_on_complex(self, monkeypatch):
        monkeypatch.setenv("ARIA_HYBRID_PLAN", "1")
        p = HybridPlanner(_make_manager())
        assert p.should_plan("task", _pa(score=5, react=True)) is True

    def test_triggers_at_threshold(self, monkeypatch):
        monkeypatch.setenv("ARIA_HYBRID_PLAN", "1")
        p = HybridPlanner(_make_manager())
        assert p.should_plan("task", _pa(score=_COMPLEXITY_THRESHOLD, react=True)) is True


# ------------------------------------------------------------------ #
# build_plan
# ------------------------------------------------------------------ #

VALID_PLAN_JSON = (
    '{"goal_summary":"搜索并整理资料","sub_goals":['
    '{"id":1,"description":"搜索相关网页","expected_tool_types":["web_fetch"],"success_signal":"获取到内容"},'
    '{"id":2,"description":"整理并保存","expected_tool_types":["file_write"],"success_signal":"文件已保存"}'
    '],"estimated_turns":6,"plan_rationale":"线性两步"}'
)


class TestBuildPlan:
    def test_returns_valid_plan(self, monkeypatch):
        monkeypatch.setenv("ARIA_HYBRID_PLAN", "1")
        mgr = _make_manager(VALID_PLAN_JSON)
        p = HybridPlanner(mgr)
        plan = p.build_plan("搜索资料", "", _pa())
        assert plan["goal_summary"] == "搜索并整理资料"
        assert len(plan["sub_goals"]) == 2

    def test_fallback_on_bad_llm(self, monkeypatch):
        monkeypatch.setenv("ARIA_HYBRID_PLAN", "1")
        mgr = _make_manager("not json at all")
        mgr._extract_json_object.return_value = None
        p = HybridPlanner(mgr)
        plan = p.build_plan("some task", "", _pa())
        assert len(plan["sub_goals"]) == 1
        assert plan["sub_goals"][0]["id"] == 1

    def test_fallback_on_empty_sub_goals(self, monkeypatch):
        monkeypatch.setenv("ARIA_HYBRID_PLAN", "1")
        mgr = _make_manager('{"goal_summary":"x","sub_goals":[]}')
        mgr._extract_json_object.return_value = {"goal_summary": "x", "sub_goals": []}
        p = HybridPlanner(mgr)
        plan = p.build_plan("task", "", _pa())
        assert len(plan["sub_goals"]) == 1  # fallback

    def test_clamps_sub_goals_to_max(self, monkeypatch):
        monkeypatch.setenv("ARIA_HYBRID_PLAN", "1")
        many = [{"id": i, "description": f"步骤{i}", "expected_tool_types": [], "success_signal": "done"} for i in range(1, 10)]
        big_plan = {"goal_summary": "big", "sub_goals": many, "estimated_turns": 20, "plan_rationale": "x"}
        import json
        mgr = _make_manager(json.dumps(big_plan))
        mgr._extract_json_object.return_value = big_plan
        p = HybridPlanner(mgr)
        plan = p.build_plan("task", "", _pa())
        assert len(plan["sub_goals"]) <= 5


# ------------------------------------------------------------------ #
# format_plan_for_system_prompt
# ------------------------------------------------------------------ #

class TestFormatPlan:
    def _sample_plan(self):
        return {
            "goal_summary": "完成任务",
            "estimated_turns": 8,
            "sub_goals": [
                {"id": 1, "description": "步骤一", "expected_tool_types": ["web_fetch"], "success_signal": "完成"},
                {"id": 2, "description": "步骤二", "expected_tool_types": [], "success_signal": "保存"},
            ],
        }

    def test_contains_all_sub_goal_ids(self):
        p = HybridPlanner(_make_manager())
        text = p.format_plan_for_system_prompt(self._sample_plan())
        assert "[1]" in text
        assert "[2]" in text

    def test_contains_goal_summary(self):
        p = HybridPlanner(_make_manager())
        text = p.format_plan_for_system_prompt(self._sample_plan())
        assert "完成任务" in text

    def test_empty_tool_types_shows_pending(self):
        p = HybridPlanner(_make_manager())
        text = p.format_plan_for_system_prompt(self._sample_plan())
        assert "待定" in text


# ------------------------------------------------------------------ #
# PlanTracker
# ------------------------------------------------------------------ #

class TestPlanTracker:
    def _tracker(self):
        plan = {
            "goal_summary": "目标",
            "sub_goals": [
                {"id": 1, "description": "步骤一", "expected_tool_types": [], "success_signal": "done"},
                {"id": 2, "description": "步骤二", "expected_tool_types": [], "success_signal": "done"},
                {"id": 3, "description": "步骤三", "expected_tool_types": [], "success_signal": "done"},
            ],
        }
        planner = HybridPlanner(_make_manager())
        return PlanTracker(plan, planner)

    def test_mark_complete_updates_list(self):
        t = self._tracker()
        t.mark_complete(1)
        assert 1 in t.completed_steps

    def test_mark_complete_no_duplicates(self):
        t = self._tracker()
        t.mark_complete(1)
        t.mark_complete(1)
        assert t.completed_steps.count(1) == 1

    def test_scan_thought_detects_marker(self):
        t = self._tracker()
        t.scan_thought("已完成 ✓ 子目标1完成，继续下一步")
        assert 1 in t.completed_steps

    def test_scan_thought_detects_multiple(self):
        t = self._tracker()
        t.scan_thought("✓ 子目标1完成 ✓ 子目标2完成")
        assert 1 in t.completed_steps
        assert 2 in t.completed_steps

    def test_scan_thought_no_false_positive(self):
        t = self._tracker()
        t.scan_thought("正在执行步骤1，尚未完成")
        assert t.completed_steps == []

    def test_reminder_text_shows_next_step(self):
        t = self._tracker()
        t.mark_complete(1)
        reminder = t.reminder_text()
        assert "步骤二" in reminder  # next uncompleted
        assert "步骤一" not in reminder or "已完成" in reminder

    def test_reminder_text_all_done(self):
        t = self._tracker()
        t.mark_complete(1)
        t.mark_complete(2)
        t.mark_complete(3)
        reminder = t.reminder_text()
        assert "全部完成" in reminder
