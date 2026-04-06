"""
三模型配置
支持为 grounding / vision / action 三个角色独立配置 LLM Provider。

设计来源：open-computer-use 的 config.py 三模型分离架构。
在 ARIA 中，这三个角色用于 Computer Use 自动化流程中的专业化分工：
  - grounding_model : 将自然语言描述映射到屏幕像素坐标（如 OS-Atlas/ShowUI）
  - vision_model    : 分析截图语义内容，输出结构化观察（如 Qwen-VL/GPT-4o）
  - action_model    : 基于观察决策下一步具体动作（如 Llama-3.3/Claude）

环境变量配置：
  ARIA_VISION_PROVIDER   : vision 模型提供商名称（groq/openrouter/openai/anthropic/deepseek/gemini）
  ARIA_VISION_MODEL      : vision 模型名称
  ARIA_ACTION_PROVIDER   : action 模型提供商名称
  ARIA_ACTION_MODEL      : action 模型名称

若未设置，默认回退到 ARIA 现有的 VolcengineLLM（主 LLM）。

使用示例（在 computer use agent 中）：
  from llm.model_config import get_vision_provider, get_action_provider
  vision = get_vision_provider()
  action = get_action_provider()
  content = vision.call(messages)
  text, tool_calls = action.call(messages, functions=tools)
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llm.providers import LLMProvider

_PROVIDER_MAP: dict[str, type] = {}


def _get_provider_map() -> dict[str, type]:
    global _PROVIDER_MAP
    if _PROVIDER_MAP:
        return _PROVIDER_MAP
    from llm.providers import (
        OpenAIProvider,
        AnthropicProvider,
        GroqProvider,
        DeepSeekProvider,
        OpenRouterProvider,
        GeminiProvider,
        FireworksProvider,
        LlamaProvider,
        MistralProvider,
        MoonshotProvider,
    )
    _PROVIDER_MAP = {
        "openai":      OpenAIProvider,
        "anthropic":   AnthropicProvider,
        "groq":        GroqProvider,
        "deepseek":    DeepSeekProvider,
        "openrouter":  OpenRouterProvider,
        "gemini":      GeminiProvider,
        "fireworks":   FireworksProvider,
        "llama":       LlamaProvider,
        "mistral":     MistralProvider,
        "moonshot":    MoonshotProvider,
    }
    return _PROVIDER_MAP


def _build_provider(provider_env: str, model_env: str) -> "LLMProvider | None":
    """根据环境变量构建 Provider 实例，未配置时返回 None。"""
    provider_name = (os.getenv(provider_env) or "").strip().lower()
    model_name = (os.getenv(model_env) or "").strip()
    if not provider_name or not model_name:
        return None
    cls = _get_provider_map().get(provider_name)
    if cls is None:
        print(f"[model_config] 未知 provider: {provider_name!r}，可用: {list(_get_provider_map())}")
        return None
    return cls(model_name)


def get_vision_provider() -> "LLMProvider | None":
    """
    返回 vision 模型 provider（用于截图语义分析）。
    配置：ARIA_VISION_PROVIDER + ARIA_VISION_MODEL
    未配置时返回 None（调用方回退到主 LLM）。
    """
    return _build_provider("ARIA_VISION_PROVIDER", "ARIA_VISION_MODEL")


def get_action_provider() -> "LLMProvider | None":
    """
    返回 action 模型 provider（用于动作决策）。
    配置：ARIA_ACTION_PROVIDER + ARIA_ACTION_MODEL
    未配置时返回 None（调用方回退到主 LLM）。
    """
    return _build_provider("ARIA_ACTION_PROVIDER", "ARIA_ACTION_MODEL")
