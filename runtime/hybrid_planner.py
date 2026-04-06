"""
Hybrid Plan-and-Execute planner for TAOR.

激活条件：ARIA_HYBRID_PLAN=1

为复杂任务添加轻量规划阶段：1 次 LLM 调用生成 advisory 子目标路线图，
注入 TAOR 系统提示以锚定模型，对抗全局漂移。

不替代 TAOR 执行——路线图仅供参考，执行时可偏离。
"""
from __future__ import annotations

import os
import re
from typing import Any

_COMPLEXITY_THRESHOLD = 4  # plan_actions complexity_score >= 此值触发规划
_MAX_SUB_GOALS = 5
_MIN_SUB_GOALS = 2


class HybridPlanner:
    def __init__(self, manager: Any) -> None:
        self._manager = manager

    def should_plan(self, user_input: str, pa_result: dict) -> bool:
        """判断是否需要规划阶段。"""
        if os.getenv("ARIA_HYBRID_PLAN", "0").strip().lower() not in ("1", "true", "yes"):
            return False
        mode = str(pa_result.get("mode") or "").lower()
        if mode in ("qa", "small_talk", "clarify"):
            return False
        task_form = str(pa_result.get("task_form") or "").lower()
        if task_form == "qa_only":
            return False
        score = int(pa_result.get("complexity_score") or 0)
        react_rec = bool(pa_result.get("react_recommended"))
        return score >= _COMPLEXITY_THRESHOLD and react_rec

    def build_plan(
        self,
        user_input: str,
        dialogue_context: str,
        pa_result: dict,
    ) -> dict:
        """1 次 LLM 调用生成子目标 JSON。规划失败时返回 fallback 单目标计划。"""
        allowed_types = ", ".join(sorted(self._manager.ALLOWED_ACTION_TYPES))
        score = int(pa_result.get("complexity_score") or 4)
        task_form = str(pa_result.get("task_form") or "mixed")
        n_goals = min(_MAX_SUB_GOALS, max(_MIN_SUB_GOALS, score - 1))

        messages = [
            {
                "role": "system",
                "content": (
                    "你是 ARIA 的任务规划器。根据用户任务，生成一个简洁的执行路线图。\n"
                    f"子目标数量：{_MIN_SUB_GOALS}-{n_goals} 个（不要过度拆分）。\n"
                    "每个子目标是可验证的中间状态，而非具体工具调用。\n"
                    "只输出严格 JSON，禁止 markdown 围栏，禁止前后缀文字。\n\n"
                    '格式：{"goal_summary":"...","sub_goals":[{"id":1,"description":"...","expected_tool_types":["..."],"success_signal":"..."}],'
                    '"estimated_turns":8,"plan_rationale":"..."}\n\n'
                    "规则：\n"
                    "- expected_tool_types 仅作参考，执行时可偏离\n"
                    "- 不要编造工具能力\n"
                    "- description ≤30字，success_signal ≤20字\n"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"【任务】{user_input}\n"
                    f"【任务形式】{task_form} / 复杂度评分 {score}\n"
                    f"【可用工具类型（部分）】{allowed_types[:300]}\n"
                    + (f"【近期对话】{dialogue_context[:200]}\n" if dialogue_context else "")
                ),
            },
        ]

        llm_text = self._manager._call_llm(
            messages,
            fallback_text="",
            agent_code="HybridPlanner",
            reasoning_effort="low",
        )
        data = self._manager._extract_json_object(llm_text or "")
        if not isinstance(data, dict) or not data.get("sub_goals"):
            return self._fallback_plan(user_input)
        data["sub_goals"] = data["sub_goals"][:_MAX_SUB_GOALS]
        return data

    def format_plan_for_system_prompt(self, plan: dict) -> str:
        """渲染为系统提示注入文本（< 200 tokens）。"""
        lines = [
            f"目标：{plan.get('goal_summary', '')}",
            f"预计轮数：{plan.get('estimated_turns', '?')}",
            "",
            "子目标：",
        ]
        for sg in plan.get("sub_goals", []):
            tools = ", ".join(sg.get("expected_tool_types") or []) or "待定"
            lines.append(
                f"[{sg['id']}] {sg['description']} | 预期工具：{tools} | 完成标志：{sg.get('success_signal', '')}"
            )
        return "\n".join(lines)

    def format_plan_reminder(self, plan: dict, completed_steps: list[int]) -> str:
        """短提醒文本，用于漂移修正注入。"""
        sub_goals = plan.get("sub_goals", [])
        total = len(sub_goals)
        done_ids = ", ".join(str(i) for i in sorted(completed_steps)) or "无"
        next_step = next(
            (sg for sg in sub_goals if sg["id"] not in completed_steps), None
        )
        next_desc = f"[{next_step['id']}] {next_step['description']}" if next_step else "全部完成"
        return (
            f"原始目标：{plan.get('goal_summary', '')}\n"
            f"已完成子目标：{done_ids}（共{total}个）\n"
            f"当前焦点：{next_desc}\n"
            "请确认当前行动仍服务于原始目标。"
        )

    def make_tracker(self, plan: dict) -> "PlanTracker":
        return PlanTracker(plan, self)

    @staticmethod
    def _fallback_plan(user_input: str) -> dict:
        return {
            "goal_summary": user_input[:80],
            "sub_goals": [
                {
                    "id": 1,
                    "description": "完成用户任务",
                    "expected_tool_types": [],
                    "success_signal": "任务完成",
                }
            ],
            "estimated_turns": 10,
            "plan_rationale": "规划失败，使用默认单目标",
        }


class PlanTracker:
    def __init__(self, plan: dict, planner: HybridPlanner) -> None:
        self.plan = plan
        self._planner = planner
        self.completed_steps: list[int] = []
        self._step_turns: dict[int, int] = {}   # step_id → 在该步骤上消耗的轮数
        self._current_step_id: int | None = None
        self._stall_threshold: int = int(os.getenv("ARIA_PLAN_STALL_TURNS", "5"))

    # ------------------------------------------------------------------ #
    # 完成标记                                                              #
    # ------------------------------------------------------------------ #

    def mark_complete(self, step_id: int) -> None:
        if step_id not in self.completed_steps:
            self.completed_steps.append(step_id)
            if self._current_step_id == step_id:
                self._current_step_id = self._next_pending_id()

    def _next_pending_id(self) -> int | None:
        for sg in self.plan.get("sub_goals", []):
            if sg["id"] not in self.completed_steps:
                return sg["id"]
        return None

    # ------------------------------------------------------------------ #
    # 自动扫描                                                              #
    # ------------------------------------------------------------------ #

    def scan_thought(self, thought: str) -> None:
        """从 thought 文本中检测完成信号，支持多种标记格式。"""
        # 格式1：✓ 子目标N
        for m in re.finditer(r"[✓✅]\s*子目标\s*(\d+)", thought):
            self.mark_complete(int(m.group(1)))
        # 格式2：sub_goal N (done|complete|completed|finished)
        for m in re.finditer(r"sub[_\s]?goal\s*(\d+)\s*(?:done|complet\w*|finish\w*)", thought, re.IGNORECASE):
            self.mark_complete(int(m.group(1)))
        # 格式3：步骤N已完成 / 第N步完成
        for m in re.finditer(r"(?:步骤|第)\s*(\d+)\s*(?:步)?\s*(?:已完成|完成|done)", thought):
            self.mark_complete(int(m.group(1)))

    def scan_observation(self, step_id: int, observation: dict) -> None:
        """
        根据 observation 结果和当前子目标的 success_signal 自动判断是否完成。
        仅在 observation.success=True 时触发。
        """
        if not observation.get("success", True):
            return
        sub_goals = self.plan.get("sub_goals", [])
        sg = next((s for s in sub_goals if s["id"] == step_id), None)
        if sg is None:
            return
        signal = str(sg.get("success_signal") or "").strip().lower()
        if not signal or signal in ("任务完成", "完成"):
            return  # 太模糊，不自动标记

        # 在 observation 的文本字段中搜索 success_signal 关键词
        obs_text = " ".join(
            str(v) for v in observation.values() if isinstance(v, (str, int, float))
        ).lower()
        # 取 signal 前 8 个字符做模糊匹配（避免过长信号误匹配）
        keyword = signal[:8]
        if keyword and keyword in obs_text:
            self.mark_complete(step_id)

    # ------------------------------------------------------------------ #
    # 轮数追踪 & 阻塞检测                                                   #
    # ------------------------------------------------------------------ #

    def tick(self, turn: int) -> None:
        """每轮调用一次，更新当前子目标的消耗轮数。"""
        if self._current_step_id is None:
            self._current_step_id = self._next_pending_id()
        sid = self._current_step_id
        if sid is not None:
            self._step_turns[sid] = self._step_turns.get(sid, 0) + 1

    def is_stalled(self) -> bool:
        """当前子目标是否已超过阻塞阈值。"""
        sid = self._current_step_id
        if sid is None:
            return False
        return self._step_turns.get(sid, 0) >= self._stall_threshold

    def stall_hint(self) -> str:
        """阻塞时注入的提示文本。"""
        sid = self._current_step_id
        if sid is None:
            return ""
        sg = next((s for s in self.plan.get("sub_goals", []) if s["id"] == sid), None)
        desc = sg["description"] if sg else f"子目标{sid}"
        turns = self._step_turns.get(sid, 0)
        return (
            f"⚠️ 子目标[{sid}]「{desc}」已执行 {turns} 轮仍未完成。\n"
            "请重新评估当前方法：换一种工具或路径，或将该子目标标记为已完成并继续推进。"
        )

    # ------------------------------------------------------------------ #
    # 提醒文本                                                              #
    # ------------------------------------------------------------------ #

    def reminder_text(self) -> str:
        base = self._planner.format_plan_reminder(self.plan, self.completed_steps)
        if self.is_stalled():
            base += f"\n\n{self.stall_hint()}"
        return base

    @property
    def all_done(self) -> bool:
        sub_goals = self.plan.get("sub_goals", [])
        return bool(sub_goals) and all(sg["id"] in self.completed_steps for sg in sub_goals)
