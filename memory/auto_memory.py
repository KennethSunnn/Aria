"""
Auto-Memory System — 跨 Session 用户模式学习

每次成功任务结束后，从对话中提取值得长期记住的用户偏好与行为模式，
写入 memory/entries/{slug}.md（带 frontmatter），并重建 memory/MEMORY.md 索引。

下次 Session 启动时，aria_manager 从 MEMORY.md 加载并注入系统提示。
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Literal

MemoryType = Literal["user_preference", "task_pattern", "feedback"]

_MEMORY_DIR = Path("memory")
_ENTRIES_DIR = _MEMORY_DIR / "entries"
_MEMORY_INDEX_PATH = _MEMORY_DIR / "MEMORY.md"
_MAX_INDEX_LINES = 200
_ENABLED_ENV = "ARIA_AUTO_MEMORY_ENABLED"


class MemoryEntry:
    """一条记忆条目，对应 memory/entries/{slug}.md。"""

    def __init__(
        self,
        name: str,
        type_: MemoryType,
        description: str,
        body: str,
        task_id: str = "",
        created_at: str = "",
        updated_at: str = "",
    ) -> None:
        self.name = name
        self.type_ = type_
        self.description = description
        self.body = body
        self.task_id = task_id
        self.created_at = created_at or time.strftime("%Y-%m-%d %H:%M:%S")
        self.updated_at = updated_at or self.created_at

    def to_markdown(self) -> str:
        fm = (
            f"---\n"
            f'name: "{self.name}"\n'
            f"type: {self.type_}\n"
            f'description: "{self.description}"\n'
            f'created_at: "{self.created_at}"\n'
            f'updated_at: "{self.updated_at}"\n'
            f'task_id: "{self.task_id}"\n'
            f"---\n\n"
        )
        return fm + self.body.strip()

    @classmethod
    def from_file(cls, path: Path) -> "MemoryEntry | None":
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        m = re.match(r"^---\n(.*?)\n---\n?(.*)", text, re.DOTALL)
        if not m:
            return None
        fm_text, body = m.group(1), m.group(2)
        fm: dict[str, str] = {}
        for line in fm_text.splitlines():
            kv = line.split(":", 1)
            if len(kv) == 2:
                fm[kv[0].strip()] = kv[1].strip().strip('"')
        return cls(
            name=fm.get("name", path.stem),
            type_=fm.get("type", "user_preference"),  # type: ignore[arg-type]
            description=fm.get("description", ""),
            body=body.strip(),
            task_id=fm.get("task_id", ""),
            created_at=fm.get("created_at", ""),
            updated_at=fm.get("updated_at", ""),
        )


class AutoMemoryManager:
    """
    负责提取、持久化和加载跨 Session 用户记忆。

    设计原则：记忆是索引，不是存储。
    只记录无法从代码推导的信息：用户偏好、工作流规律、明确的纠正反馈。
    """

    def __init__(self, manager: Any) -> None:
        self._manager = manager
        _ENTRIES_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # Session 启动：加载 MEMORY.md                                         #
    # ------------------------------------------------------------------ #

    def load_into_stm(self) -> str:
        """
        读取 memory/MEMORY.md，返回内容字符串，供注入系统提示。
        文件不存在时返回空字符串。
        """
        if not _MEMORY_INDEX_PATH.exists():
            return ""
        try:
            return _MEMORY_INDEX_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    @staticmethod
    def _task_keywords_from_text(task_text: str) -> list[str]:
        """从用户任务句抽取关键词，供 load_into_stm_with_context 排序（轻量、无第三方依赖）。"""
        s = (task_text or "").strip()
        if not s:
            return []
        seen: dict[str, None] = {}
        for w in re.findall(r"[a-z0-9_]{2,}", s.lower()):
            if w not in seen:
                seen[w] = None
        cjk = [ch for ch in s if "\u4e00" <= ch <= "\u9fff"]
        for n in (2, 3):
            for i in range(0, max(0, len(cjk) - n + 1)):
                gram = "".join(cjk[i : i + n])
                if gram not in seen:
                    seen[gram] = None
        return list(seen.keys())[:24]

    def get_system_prompt_fragment(self, task_text: str = "") -> str:
        """
        供 TAOR / 系统提示拼接：注入跨会话记忆（MEMORY.md 或按任务关键词排序的索引）。
        禁用时或无可读内容时返回空字符串。
        """
        if not self._is_enabled():
            return ""
        kws = self._task_keywords_from_text(task_text)
        has_entries = _ENTRIES_DIR.exists() and any(_ENTRIES_DIR.glob("*.md"))
        body = (self.load_into_stm_with_context(kws) if has_entries else self.load_into_stm()).strip()
        if not body:
            return ""
        return f"\n\n【ARIA 持续记忆】\n{body}\n"

    # ------------------------------------------------------------------ #
    # 任务结束：提取并持久化模式                                             #
    # ------------------------------------------------------------------ #

    def analyze_and_persist(
        self,
        task_info: dict[str, Any],
        result_payload: dict[str, Any],
        tool_trace: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        """
        成功任务结束后，让 LLM 提取值得记住的用户模式，并持久化。

        参数
        ----
        task_info      : 包含 user_input、task_id 等字段的任务信息字典
        result_payload : 包含 is_success、final_result 等字段的结果字典
        tool_trace     : TAOR 模式的工具调用链（可为 None）

        返回
        ----
        已创建或更新的条目 slug 列表
        """
        if not self._is_enabled():
            return []
        if not isinstance(result_payload, dict) or not result_payload.get("is_success"):
            return []

        user_input = str(task_info.get("user_input") or "")
        final_result = str(result_payload.get("final_result") or "")
        task_id = str(task_info.get("task_id") or "")

        trace_text = self._format_tool_trace(tool_trace or [])
        conversation_snapshot = (
            f"用户输入：{user_input}\n\n"
            f"最终结果摘要：{final_result[:600]}\n\n"
            + (f"工具调用记录：\n{trace_text}\n\n" if trace_text else "")
        )

        extracted = self._extract_patterns(conversation_snapshot, task_id)
        if not extracted:
            return []

        saved_names: list[str] = []
        for pattern in extracted:
            name = self._persist_entry(pattern, task_id)
            if name:
                saved_names.append(name)

        if saved_names:
            self._rebuild_index()
            self._manager.push_event(
                "auto_memory",
                "success",
                "AutoMemoryManager",
                f"已记录 {len(saved_names)} 条用户模式",
                {"entry_names": saved_names},
            )

        return saved_names

    # ------------------------------------------------------------------ #
    # 私有：LLM 提取                                                        #
    # ------------------------------------------------------------------ #

    def _extract_patterns(
        self,
        conversation_snapshot: str,
        task_id: str,
    ) -> list[dict[str, Any]]:
        existing_names = self._existing_entry_names()
        existing_hint = (
            f"\n已记录的模式（避免重复）：{', '.join(existing_names[:30])}\n"
            if existing_names
            else ""
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 ARIA 的学习分析器。分析以下任务对话，提取值得长期记住的用户偏好或行为模式。\n"
                    "只提取无法从代码推导的信息：用户偏好、工作流规律、明确的纠正反馈。\n"
                    "忽略：任务内容本身（不要记任务结论）、单次随机需求、可从代码推导的行为。\n"
                    + existing_hint
                    + "\n输出严格 JSON 数组（可为空 []），每个元素字段：\n"
                    '  "name": 唯一英文 slug（如 "output-format-markdown"）\n'
                    '  "type": "user_preference" | "task_pattern" | "feedback"\n'
                    '  "description": 一句话中文描述（≤30字）\n'
                    '  "body": 完整中文记录（≤150字）\n'
                    "最多提取 3 条，若无值得记录的内容输出 []。"
                ),
            },
            {
                "role": "user",
                "content": conversation_snapshot[:3000],
            },
        ]
        raw = self._manager._call_llm(
            messages,
            fallback_text="[]",
            agent_code="AutoMemoryManager",
            reasoning_effort="low",
        )
        cleaned = re.sub(r"^```(?:json)?\s*", "", (raw or "").strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r"```$", "", cleaned).strip()
        m = re.search(r"\[[\s\S]*\]", cleaned)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
        except (json.JSONDecodeError, ValueError):
            pass
        return []

    # ------------------------------------------------------------------ #
    # 私有：持久化                                                           #
    # ------------------------------------------------------------------ #

    def _persist_entry(self, pattern: dict[str, Any], task_id: str) -> str:
        raw_name = str(pattern.get("name") or "").strip()
        if not raw_name:
            raw_name = str(uuid.uuid4())[:8]
        slug = re.sub(r"[^a-zA-Z0-9\-_]", "-", raw_name)[:64].strip("-")
        if not slug:
            slug = str(uuid.uuid4())[:8]

        entry_path = _ENTRIES_DIR / f"{slug}.md"
        now = time.strftime("%Y-%m-%d %H:%M:%S")

        if entry_path.exists():
            existing = MemoryEntry.from_file(entry_path)
            if existing:
                existing.updated_at = now
                existing.body = str(pattern.get("body") or existing.body)
                existing.description = str(pattern.get("description") or existing.description)[:80]
                try:
                    entry_path.write_text(existing.to_markdown(), encoding="utf-8")
                except OSError:
                    pass
                return slug

        entry = MemoryEntry(
            name=slug,
            type_=pattern.get("type", "user_preference"),  # type: ignore[arg-type]
            description=str(pattern.get("description") or "")[:80],
            body=str(pattern.get("body") or ""),
            task_id=task_id,
            created_at=now,
            updated_at=now,
        )
        try:
            entry_path.write_text(entry.to_markdown(), encoding="utf-8")
        except OSError:
            return ""
        return slug

    def _rebuild_index(self) -> None:
        entries: list[MemoryEntry] = []
        for path in sorted(_ENTRIES_DIR.glob("*.md")):
            e = MemoryEntry.from_file(path)
            if e:
                entries.append(e)

        lines: list[str] = [
            "# ARIA Memory Index",
            f"Last updated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]
        by_type: dict[str, list[MemoryEntry]] = {}
        for e in entries:
            by_type.setdefault(e.type_, []).append(e)

        for type_name in ("user_preference", "task_pattern", "feedback"):
            group = by_type.get(type_name, [])
            if not group:
                continue
            lines.append(f"## {type_name}")
            for e in group:
                rel_path = f"entries/{e.name}.md"
                lines.append(f"- [{e.name}]({rel_path}): {e.description}")
            lines.append("")

        if len(lines) > _MAX_INDEX_LINES:
            lines = lines[:_MAX_INDEX_LINES]
            lines.append("... (truncated)")

        try:
            _MEMORY_INDEX_PATH.write_text("\n".join(lines), encoding="utf-8")
        except OSError:
            pass

    def _existing_entry_names(self) -> list[str]:
        if not _ENTRIES_DIR.exists():
            return []
        return [p.stem for p in sorted(_ENTRIES_DIR.glob("*.md"))]

    @staticmethod
    def _format_tool_trace(tool_trace: list[dict[str, Any]]) -> str:
        if not tool_trace:
            return ""
        lines: list[str] = []
        for row in tool_trace[:10]:
            act = row.get("action") or {}
            obs = row.get("observation") or {}
            success = obs.get("success", "?")
            lines.append(f"- [{act.get('type', '?')}] success={success}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 相关性评分与记忆衰减                                                    #
    # ------------------------------------------------------------------ #

    def score_relevance(self, entry: "MemoryEntry", current_task_keywords: list[str]) -> float:
        """
        基于关键词重叠给记忆条目打分（0.0 ~ 1.0）。
        用于 load_into_stm 时按相关性排序。
        """
        if not current_task_keywords:
            return 0.5  # 无关键词时中性分
        text = (entry.name + " " + entry.description + " " + entry.body).lower()
        hits = sum(1 for kw in current_task_keywords if kw.lower() in text)
        return min(1.0, hits / max(1, len(current_task_keywords)))

    def decay_old_entries(self, days: int = 90) -> int:
        """
        清理超过 days 天未更新的低分条目（description 极短或 body 极短）。
        返回删除的条目数。
        """
        if not _ENTRIES_DIR.exists():
            return 0
        cutoff = time.time() - days * 86400
        deleted = 0
        for path in list(_ENTRIES_DIR.glob("*.md")):
            e = MemoryEntry.from_file(path)
            if e is None:
                continue
            # 解析 updated_at
            try:
                updated_ts = time.mktime(time.strptime(e.updated_at, "%Y-%m-%d %H:%M:%S"))
            except (ValueError, OverflowError):
                continue
            if updated_ts < cutoff and len(e.body.strip()) < 20:
                try:
                    path.unlink()
                    deleted += 1
                except OSError:
                    pass
        if deleted:
            self._rebuild_index()
        return deleted

    def load_into_stm_with_context(self, task_keywords: list[str]) -> str:
        """
        读取 memory/MEMORY.md，按与当前任务的相关性排序后返回。
        比 load_into_stm() 更智能，适合 TAOR 系统提示注入。
        """
        if not _ENTRIES_DIR.exists():
            return self.load_into_stm()

        entries: list[MemoryEntry] = []
        for path in sorted(_ENTRIES_DIR.glob("*.md")):
            e = MemoryEntry.from_file(path)
            if e:
                entries.append(e)

        if not entries:
            return self.load_into_stm()

        # 按相关性排序
        scored = sorted(
            entries,
            key=lambda e: self.score_relevance(e, task_keywords),
            reverse=True,
        )

        lines: list[str] = ["# ARIA Memory Index (按相关性排序)", ""]
        by_type: dict[str, list[MemoryEntry]] = {}
        for e in scored:
            by_type.setdefault(e.type_, []).append(e)

        for type_name in ("user_preference", "task_pattern", "feedback"):
            group = by_type.get(type_name, [])
            if not group:
                continue
            lines.append(f"## {type_name}")
            for e in group:
                rel_path = f"entries/{e.name}.md"
                lines.append(f"- [{e.name}]({rel_path}): {e.description}")
            lines.append("")

        result = "\n".join(lines)
        if len(result.splitlines()) > _MAX_INDEX_LINES:
            result = "\n".join(result.splitlines()[:_MAX_INDEX_LINES]) + "\n... (truncated)"
        return result.strip()

    @staticmethod
    def _is_enabled() -> bool:
        return os.getenv(_ENABLED_ENV, "1").strip().lower() in ("1", "true", "yes")
