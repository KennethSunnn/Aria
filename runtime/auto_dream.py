"""
AutoDream — 后台记忆整合引擎

在系统空闲时运行，整合近期成功任务的经验，更新持续记忆，生成主动洞察。

配置项：
  ARIA_AUTODREAM_ENABLED=1          启用 AutoDream（默认 1）
  ARIA_AUTODREAM_IDLE_SECONDS=300   空闲多少秒后触发（默认 300）
  ARIA_AUTODREAM_INTERVAL_SECONDS=3600  两次 Dream 最小间隔（默认 3600）
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

_ENABLED_ENV = "ARIA_AUTODREAM_ENABLED"
_IDLE_ENV = "ARIA_AUTODREAM_IDLE_SECONDS"
_INTERVAL_ENV = "ARIA_AUTODREAM_INTERVAL_SECONDS"

_DEFAULT_IDLE = 300
_DEFAULT_INTERVAL = 3600


class AutoDreamEngine:
    """
    AutoDream 后台记忆整合引擎。

    由 KAIROSEngine._run_loop() 每 30 秒调用 maybe_dream()。
    空闲时间超过阈值且距上次 Dream 超过最小间隔时，自动触发一次 Dream 周期。
    """

    def __init__(self, manager: Any) -> None:
        self._manager = manager
        self._last_activity_time: float = time.time()
        self._last_dream_time: float = 0.0

    # ------------------------------------------------------------------ #
    # 公开 API                                                              #
    # ------------------------------------------------------------------ #

    def record_activity(self) -> None:
        """记录用户活动，重置空闲计时器。每次处理用户请求时调用。"""
        self._last_activity_time = time.time()

    def maybe_dream(self) -> None:
        """
        检查是否应触发 Dream 周期。由 KAIROSEngine._run_loop() 调用。
        """
        if not self._is_enabled():
            return

        now = time.time()
        idle_seconds = now - self._last_activity_time
        since_last_dream = now - self._last_dream_time

        idle_threshold = self._get_int_env(_IDLE_ENV, _DEFAULT_IDLE)
        interval = self._get_int_env(_INTERVAL_ENV, _DEFAULT_INTERVAL)

        if idle_seconds >= idle_threshold and since_last_dream >= interval:
            self.run_dream_cycle()

    def run_dream_cycle(self) -> dict[str, Any]:
        """
        执行一次完整的 Dream 周期。可手动触发（POST /api/dream/run）。

        返回
        ----
        {"success": bool, "entries_updated": int, "message": str}
        """
        self._last_dream_time = time.time()

        try:
            self._manager.push_event(
                "autodream",
                "running",
                "AutoDreamEngine",
                "AutoDream 周期开始",
                {},
            )

            # 从 auto_memory 模块获取管理器
            from memory.auto_memory import AutoMemoryManager
            auto_mem = AutoMemoryManager(self._manager)

            # 整合近期成功任务
            entries = self._consolidate_recent_tasks(auto_mem)

            # 衰减旧记忆
            self._decay_old_entries(auto_mem)

            self._manager.push_event(
                "autodream",
                "success",
                "AutoDreamEngine",
                f"AutoDream 完成，更新 {entries} 条记忆",
                {"entries_updated": entries},
            )

            return {
                "success": True,
                "entries_updated": entries,
                "message": f"AutoDream 完成，更新 {entries} 条记忆",
            }

        except Exception as exc:
            self._manager.push_event(
                "autodream",
                "error",
                "AutoDreamEngine",
                f"AutoDream 失败：{exc}",
                {},
            )
            return {"success": False, "entries_updated": 0, "message": str(exc)}

    # ------------------------------------------------------------------ #
    # 私有                                                                  #
    # ------------------------------------------------------------------ #

    def _consolidate_recent_tasks(self, auto_mem: Any) -> int:
        """
        从近期成功任务中提取模式并持久化。
        遍历 STM 中的成功任务记录，调用 AutoMemoryManager.analyze_and_persist。
        """
        stm = getattr(self._manager, "stm", None)
        if stm is None:
            return 0

        # STM 中的近期任务记录
        recent_tasks = getattr(stm, "recent_successful_tasks", [])
        if not recent_tasks:
            return 0

        total_entries = 0
        for task_record in recent_tasks[-5:]:  # 最多处理最近 5 个任务
            task_info = task_record.get("task_info", {})
            result_payload = task_record.get("result_payload", {})
            tool_trace = task_record.get("tool_trace", [])

            if not result_payload.get("is_success"):
                continue

            saved = auto_mem.analyze_and_persist(task_info, result_payload, tool_trace)
            total_entries += len(saved)

        return total_entries

    def _decay_old_entries(self, auto_mem: Any) -> None:
        """清理超期低分记忆条目。"""
        decay_fn = getattr(auto_mem, "decay_old_entries", None)
        if callable(decay_fn):
            try:
                decay_fn(days=90)
            except Exception:
                pass

    @staticmethod
    def _is_enabled() -> bool:
        return os.getenv(_ENABLED_ENV, "1").strip().lower() in ("1", "true", "yes")

    @staticmethod
    def _get_int_env(key: str, default: int) -> int:
        try:
            return max(1, int(os.getenv(key, str(default)) or str(default)))
        except (TypeError, ValueError):
            return default
