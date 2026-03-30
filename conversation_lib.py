import json
import os
import time
import uuid
from typing import Any


class ConversationLibrary:
    def __init__(self, file_path: str = "data/conversations/conversations.json"):
        self.file_path = file_path
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        if not os.path.exists(self.file_path):
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False, indent=2)

    def _load(self) -> list[dict[str, Any]]:
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save(self, conversations: list[dict[str, Any]]) -> None:
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(conversations, f, ensure_ascii=False, indent=2)

    def _summary(self, text: str, limit: int = 36) -> str:
        text = (text or "").strip()
        if len(text) <= limit:
            return text or "新会话"
        return text[:limit] + "..."

    def _is_small_talk(self, text: str) -> bool:
        s = (text or "").strip().lower()
        if not s:
            return True
        compact = "".join(ch for ch in s if ch.isalnum() or ("\u4e00" <= ch <= "\u9fff"))
        greetings = ["你好", "您好", "hello", "hi", "hey", "在吗", "谢谢", "thank", "早上好", "晚上好"]
        return any(g in s for g in greetings) and len(compact) <= 12

    def _default_title(self) -> str:
        return "新会话 " + time.strftime("%H:%M")

    def create_conversation(self, title: str | None = None) -> dict[str, Any]:
        now = time.time()
        title_value = (title or "").strip()
        if not title_value or title_value == "新会话" or self._is_small_talk(title_value):
            title_value = self._default_title()
        conversation = {
            "conversation_id": str(uuid.uuid4()),
            "title": title_value,
            "archived": False,
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "last_message": "",
            "messages": [],
            "workflow_events": [],
        }
        conversations = self._load()
        conversations.append(conversation)
        self._save(conversations)
        return conversation

    def list_conversations(self, archived: bool | None = None) -> list[dict[str, Any]]:
        conversations = self._load()
        if archived is True:
            conversations = [c for c in conversations if bool(c.get("archived", False))]
        else:
            # None（默认）与 False：侧边栏仅展示未归档
            conversations = [c for c in conversations if not bool(c.get("archived", False))]
        conversations.sort(key=lambda c: c.get("updated_at", 0), reverse=True)
        return [
            {
                "conversation_id": c.get("conversation_id"),
                "title": c.get("title", "新会话"),
                "archived": bool(c.get("archived", False)),
                "status": c.get("status", "active"),
                "created_at": c.get("created_at", 0),
                "updated_at": c.get("updated_at", 0),
                "last_message": c.get("last_message", ""),
                "message_count": len(c.get("messages", [])),
            }
            for c in conversations
        ]

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        conversations = self._load()
        for c in conversations:
            if c.get("conversation_id") == conversation_id:
                return c
        return None

    def append_message(self, conversation_id: str, role: str, content: str, meta: dict[str, Any] | None = None) -> bool:
        conversations = self._load()
        now = time.time()
        for c in conversations:
            if c.get("conversation_id") == conversation_id:
                c.setdefault("messages", []).append(
                    {
                        "message_id": str(uuid.uuid4()),
                        "role": role,
                        "content": content,
                        "meta": meta or {},
                        "timestamp": now,
                    }
                )
                if role == "user":
                    current_title = str(c.get("title", "") or "").strip()
                    if (
                        not current_title
                        or current_title == "新会话"
                        or current_title.startswith("新会话 ")
                        or self._is_small_talk(current_title)
                    ) and not self._is_small_talk(content):
                        c["title"] = self._summary(content)
                c["last_message"] = self._summary(content)
                c["updated_at"] = now
                self._save(conversations)
                return True
        return False

    def set_archived(self, conversation_id: str, archived: bool) -> bool:
        conversations = self._load()
        now = time.time()
        for c in conversations:
            if c.get("conversation_id") == conversation_id:
                c["archived"] = bool(archived)
                c["status"] = "archived" if archived else "active"
                c["updated_at"] = now
                self._save(conversations)
                return True
        return False

    def delete_conversation(self, conversation_id: str) -> bool:
        conversations = self._load()
        before = len(conversations)
        conversations = [c for c in conversations if c.get("conversation_id") != conversation_id]
        if len(conversations) < before:
            self._save(conversations)
            return True
        return False

    def replace_workflow_events(self, conversation_id: str, events: list[dict[str, Any]]) -> bool:
        conversations = self._load()
        now = time.time()
        for c in conversations:
            if c.get("conversation_id") == conversation_id:
                c["workflow_events"] = events
                c["updated_at"] = now
                self._save(conversations)
                return True
        return False

    def format_dialogue_context_for_prompt(
        self,
        conversation_id: str,
        *,
        max_messages: int = 12,
        max_chars_per_message: int = 1000,
        max_total_chars: int = 9000,
    ) -> str:
        """在写入本轮 user 消息之前调用：用已有消息拼出近期对话，供规划器/解析器理解指代。"""
        conv = self.get_conversation(conversation_id)
        if not conv:
            return ""
        messages = conv.get("messages")
        if not isinstance(messages, list) or not messages:
            return ""
        tail = messages[-max_messages:]
        lines: list[str] = []
        total = 0
        for m in tail:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role") or "").strip().lower()
            if role not in ("user", "assistant"):
                continue
            raw = str(m.get("content") or "").strip()
            meta = m.get("meta") if isinstance(m.get("meta"), dict) else {}
            atts = meta.get("attachments")
            if isinstance(atts, list) and atts:
                names = [
                    str(a.get("name") or "").strip()
                    for a in atts
                    if isinstance(a, dict) and (a.get("name") or a.get("path"))
                ]
                if names:
                    hint = "[附件: " + ", ".join(names[:8]) + ("]" if len(names) <= 8 else "…]")
                    raw = (raw + " " if raw else "") + hint
            if not raw:
                continue
            if len(raw) > max_chars_per_message:
                raw = raw[: max_chars_per_message - 20] + "\n... [已截断]"
            label = "User" if role == "user" else "Assistant"
            block = f"{label}: {raw}"
            if total + len(block) + 1 > max_total_chars:
                lines.append("... [更早消息已省略]")
                break
            lines.append(block)
            total += len(block) + 1
        return "\n".join(lines).strip()

