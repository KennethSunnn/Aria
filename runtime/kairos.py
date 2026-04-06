"""
KAIROSEngine — 主动执行引擎

KAIROS（Knowledge-Aware Intelligent Reactive/Opportunistic System）
是 ARIA 的主动执行协调层，统一管理：
  - TriggerScheduler（定时触发）
  - AutoDreamEngine（后台记忆整合）
  - 主动发起 TAOR 任务

配置项：
  ARIA_KAIROS_ENABLED=1   在 aria_manager.__init__ 中启用
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Any

_TICK_INTERVAL = 30  # 主循环间隔（秒）


class KAIROSEngine:
    """
    KAIROS 主动执行引擎。

    在 ARIAManager.__init__ 末尾初始化并启动后台守护线程。
    线程每 30 秒 tick 一次：检查定时触发器 + 判断是否触发 AutoDream。
    """

    def __init__(self, manager: Any) -> None:
        self._manager = manager
        self._running = False
        self._thread: threading.Thread | None = None

        from .trigger_scheduler import TriggerScheduler
        from .auto_dream import AutoDreamEngine

        self.scheduler = TriggerScheduler(self)
        self.dream_engine = AutoDreamEngine(manager)

    # ------------------------------------------------------------------ #
    # 生命周期                                                               #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """启动后台守护线程。"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name="KAIROSEngine",
            daemon=True,
        )
        self._thread.start()
        self._manager.push_event(
            "kairos",
            "success",
            "KAIROSEngine",
            "KAIROS 引擎已启动",
            {},
        )

    def stop(self) -> None:
        """停止后台线程（优雅退出）。"""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    # ------------------------------------------------------------------ #
    # 主动触发                                                               #
    # ------------------------------------------------------------------ #

    def fire_prompt(
        self,
        prompt: str,
        conversation_id: str = "",
    ) -> None:
        """
        主动发起一个 TAOR 任务。

        在后台线程中异步执行，不阻塞调用方。
        """
        if not prompt:
            return

        def _run() -> None:
            try:
                # 生成一个内部 conversation_id
                cid = conversation_id or f"kairos-{uuid.uuid4().hex[:8]}"
                self._manager.push_event(
                    "kairos",
                    "running",
                    "KAIROSEngine",
                    f"KAIROS 主动触发任务：{prompt[:80]}",
                    {"conversation_id": cid},
                )

                # 通过 TAOR 循环执行
                run_taor = getattr(self._manager, "run_taor_pipeline", None)
                if callable(run_taor):
                    result = run_taor(
                        user_input=prompt,
                        conversation_id=cid,
                    )
                    self._manager.push_event(
                        "kairos",
                        "success" if result.get("is_success") else "warning",
                        "KAIROSEngine",
                        f"KAIROS 任务完成：{result.get('final_result', '')[:100]}",
                        {"is_success": result.get("is_success")},
                    )
                else:
                    self._manager.push_event(
                        "kairos",
                        "error",
                        "KAIROSEngine",
                        "KAIROS 无法触发任务：run_taor_pipeline 不可用",
                        {},
                    )
            except Exception as exc:
                try:
                    self._manager.push_event(
                        "kairos",
                        "error",
                        "KAIROSEngine",
                        f"KAIROS 任务异常：{exc}",
                        {},
                    )
                except Exception:
                    pass

        t = threading.Thread(target=_run, name="kairos-fire", daemon=True)
        t.start()

    # ------------------------------------------------------------------ #
    # 状态查询                                                               #
    # ------------------------------------------------------------------ #

    def get_status(self) -> dict[str, Any]:
        """返回 KAIROS 运行状态，供 /api/kairos/status 使用。"""
        last_dream = self.dream_engine._last_dream_time
        return {
            "running": self._running,
            "trigger_count": len(self.scheduler.list_tasks()),
            "last_dream": (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_dream))
                if last_dream > 0
                else None
            ),
            "idle_seconds": int(time.time() - self.dream_engine._last_activity_time),
        }

    # ------------------------------------------------------------------ #
    # 私有：主循环                                                            #
    # ------------------------------------------------------------------ #

    def _run_loop(self) -> None:
        """后台守护线程主循环。"""
        while self._running:
            try:
                self.scheduler.tick()
                self.dream_engine.maybe_dream()
            except Exception:
                pass
            # 分段 sleep，使 stop() 能快速响应
            for _ in range(_TICK_INTERVAL):
                if not self._running:
                    break
                time.sleep(1)
