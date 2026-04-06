"""
Groq 推理后端

对实时交互任务（small_talk、快速问答）提供极低延迟的推理。
使用 OpenAI 兼容接口，无需额外 SDK。

配置：
  GROQ_API_KEY          : Groq API 密钥（必填）
  GROQ_MODEL            : 模型名称，默认 llama-3.3-70b-versatile
  GROQ_BASE_URL         : API 地址，默认 https://api.groq.com/openai/v1
  ARIA_GROQ_ENABLED     : 1/true/yes 启用（默认关闭）
"""
from __future__ import annotations

import os
from typing import Any, Iterator

_GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
_GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


def is_groq_enabled() -> bool:
    v = (os.getenv("ARIA_GROQ_ENABLED") or "0").strip().lower()
    return v in ("1", "true", "yes") and bool(os.getenv("GROQ_API_KEY", "").strip())


class GroqLLM:
    """
    Groq 推理后端，OpenAI 兼容接口。
    接口与 VolcengineLLM 保持一致，可作为 drop-in 替换。
    """

    def __init__(self):
        from openai import OpenAI
        self.model_name = _GROQ_MODEL
        self._client = OpenAI(
            api_key=os.getenv("GROQ_API_KEY", ""),
            base_url=_GROQ_BASE_URL,
        )

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 2048,
        stream: bool = False,
    ) -> str | Iterator[str]:
        kwargs: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if stream:
            return self._stream(kwargs)
        resp = self._client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""

    def _stream(self, kwargs: dict[str, Any]) -> Iterator[str]:
        for chunk in self._client.chat.completions.create(**kwargs):
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                yield delta

    def chat_json(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> str:
        """请求 JSON 输出（Groq 支持 response_format=json_object）。"""
        resp = self._client.chat.completions.create(
            model=self.model_name,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content or "{}"
