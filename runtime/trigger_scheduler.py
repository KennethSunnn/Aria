"""
TriggerScheduler — 定时触发调度器

持久化 cron 任务管理，对标 claude-code-main 的 CronCreate/CronDelete/CronList。
任务存储在 data/scheduled_tasks.json。

依赖：croniter>=2.0.0
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .kairos import KAIROSEngine

_STORE_PATH = Path("data/scheduled_tasks.json")


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _calc_next(cron_expr: str) -> str | None:
    """计算下次触发时间（ISO 格式）。依赖 croniter；不可用时返回 None。"""
    try:
        from croniter import croniter  # type: ignore[import]
        it = croniter(cron_expr, datetime.now())
        return it.get_next(datetime).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


class TriggerScheduler:
    """
    定时触发调度器。

    由 KAIROSEngine._run_loop() 每 30 秒调用一次 tick()。
    支持 one-shot（recurring=False）和循环（recurring=True）两种模式。
    durable=True 时持久化到 data/scheduled_tasks.json。
    """

    def __init__(self, kairos: "KAIROSEngine") -> None:
        self._kairos = kairos
        self._tasks: dict[str, dict[str, Any]] = {}
        _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    # ------------------------------------------------------------------ #
    # 公开 API                                                              #
    # ------------------------------------------------------------------ #

    def create(
        self,
        cron: str,
        prompt: str,
        recurring: bool = True,
        durable: bool = True,
    ) -> str:
        """
        创建定时触发器。

        参数
        ----
        cron      : 标准 5 字段 cron 表达式（本地时区），如 "0 9 * * 1-5"
        prompt    : 触发时发送给 KAIROS 的任务描述
        recurring : True = 循环触发；False = 触发一次后自动删除
        durable   : True = 持久化到磁盘，重启后恢复

        返回
        ----
        task_id（8 位 hex）
        """
        task_id = uuid.uuid4().hex[:8]
        next_fire = _calc_next(cron)
        task: dict[str, Any] = {
            "id": task_id,
            "cron": cron,
            "prompt": prompt,
            "recurring": recurring,
            "durable": durable,
            "created_at": _now_iso(),
            "last_fired": None,
            "next_fire": next_fire,
        }
        self._tasks[task_id] = task
        if durable:
            self._persist()
        return task_id

    def delete(self, task_id: str) -> bool:
        """删除触发器。返回是否成功删除。"""
        if task_id not in self._tasks:
            return False
        del self._tasks[task_id]
        self._persist()
        return True

    def list_tasks(self) -> list[dict[str, Any]]:
        """返回所有触发器的副本列表。"""
        return [dict(t) for t in self._tasks.values()]

    def tick(self) -> None:
        """
        检查并触发到期任务。由 KAIROSEngine._run_loop() 调用。
        """
        now = datetime.now()
        to_delete: list[str] = []

        for task_id, task in list(self._tasks.items()):
            next_fire_str = task.get("next_fire")
            if not next_fire_str:
                continue
            try:
                next_fire_dt = datetime.strptime(next_fire_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue

            if next_fire_dt <= now:
                self._fire(task)
                task["last_fired"] = _now_iso()

                if task.get("recurring"):
                    task["next_fire"] = _calc_next(task["cron"])
                else:
                    to_delete.append(task_id)

        for task_id in to_delete:
            del self._tasks[task_id]

        if to_delete or any(t.get("last_fired") for t in self._tasks.values()):
            self._persist()

    # ------------------------------------------------------------------ #
    # 私有                                                                  #
    # ------------------------------------------------------------------ #

    def _fire(self, task: dict[str, Any]) -> None:
        prompt = str(task.get("prompt") or "")
        if not prompt:
            return
        try:
            self._kairos.fire_prompt(prompt)
        except Exception:
            pass

    def _persist(self) -> None:
        durable_tasks = {k: v for k, v in self._tasks.items() if v.get("durable")}
        try:
            _STORE_PATH.write_text(
                json.dumps(list(durable_tasks.values()), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _load(self) -> None:
        if not _STORE_PATH.exists():
            return
        try:
            raw = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                for task in raw:
                    if isinstance(task, dict) and task.get("id"):
                        self._tasks[task["id"]] = task
        except (OSError, json.JSONDecodeError):
            pass
