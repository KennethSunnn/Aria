"""
统一 LLM Provider 抽象层
移植自 open-computer-use 的多提供商架构，扩展为 ARIA 可用的形式。

使用场景：
  - 三模型配置（grounding / vision / action）：见 model_config.py
  - 快速切换不同 LLM 后端（实验/测试）
  - Computer Use 流程中的独立 vision/action 模型调用

使用示例：
  from llm.providers import GroqProvider, OpenRouterProvider, AnthropicProvider
  vision = OpenRouterProvider("qwen-2.5-vl")
  action = GroqProvider("llama-3.3")
  content, tool_calls = action.call(messages, functions=tools)
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
from typing import Any

from dotenv import load_dotenv

load_dotenv(override=True)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _parse_json(s: str) -> dict | None:
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        print(f"[providers] JSON 解析失败: {s[:120]}")
        return None


def _text_block(text: str) -> dict:
    return {"type": "text", "text": text}


# ---------------------------------------------------------------------------
# 基类
# ---------------------------------------------------------------------------

class LLMProvider:
    """
    统一 LLM Provider 基类。

    子类需声明：
      base_url: str | None
      api_key:  str | None
      aliases:  dict[str, str]  （可选，模型别名映射）

    调用入口：
      provider.call(messages, functions=None)
        → str                     （无工具调用时）
        → (str | None, list[dict]) （有工具调用时）
    """

    base_url: str | None = None
    api_key:  str | None = None
    aliases:  dict[str, str] = {}

    def __init__(self, model: str):
        self.model = self.aliases.get(model, model)
        self.client = self._create_client()

    def _create_client(self):
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Tool schema helpers（子类可覆盖）
    # ------------------------------------------------------------------

    def _make_function_def(
        self,
        name: str,
        description: str,
        properties: dict[str, dict],
        required: list[str],
    ) -> dict:
        raise NotImplementedError

    def create_function_schema(self, definitions: dict[str, Any]) -> list[dict]:
        functions = []
        for name, details in definitions.items():
            properties = {}
            required = []
            for param_name, param_desc in details["params"].items():
                properties[param_name] = {"type": "string", "description": param_desc}
                required.append(param_name)
            functions.append(
                self._make_function_def(name, details["description"], properties, required)
            )
        return functions

    def create_tool_call(self, name: str, parameters: dict) -> dict:
        return {"type": "function", "name": name, "parameters": parameters}

    # ------------------------------------------------------------------
    # 图像内容块（子类可覆盖）
    # ------------------------------------------------------------------

    def _wrap_image(self, image_data: bytes) -> dict:
        raise NotImplementedError

    def _wrap_block(self, block) -> dict:
        if isinstance(block, bytes):
            return self._wrap_image(block)
        return _text_block(block)

    def _transform_message(self, message: dict) -> dict:
        content = message["content"]
        if isinstance(content, list):
            return {**message, "content": [self._wrap_block(b) for b in content]}
        return message

    # ------------------------------------------------------------------
    # call()
    # ------------------------------------------------------------------

    def call(self, messages: list[dict], functions: dict | None = None):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# OpenAI 兼容基类
# ---------------------------------------------------------------------------

class OpenAIBaseProvider(LLMProvider):
    """适配所有 OpenAI SDK 兼容接口（Groq/DeepSeek/OpenRouter/Fireworks 等）。"""

    def _create_client(self):
        from openai import OpenAI
        return OpenAI(base_url=self.base_url, api_key=self.api_key).chat.completions

    def _make_function_def(self, name, description, properties, required):
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    def _wrap_image(self, image_data: bytes) -> dict:
        image_type = "png"
        try:
            from PIL import Image
            with Image.open(io.BytesIO(image_data)) as img:
                image_type = (img.format or "png").lower()
        except Exception:
            pass
        encoded = base64.b64encode(image_data).decode("utf-8")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:image/{image_type};base64,{encoded}"},
        }

    def call(self, messages: list[dict], functions: dict | None = None):
        tools = self.create_function_schema(functions) if functions else None
        transformed = [self._transform_message(m) for m in messages]
        filtered_kwargs = {"tools": tools} if tools else {}
        completion = self.client.create(
            messages=transformed, model=self.model, **filtered_kwargs
        )
        if hasattr(completion, "error"):
            raise RuntimeError(f"[providers] 模型错误: {completion.error}")

        message = completion.choices[0].message

        if functions:
            tool_calls = message.tool_calls or []
            combined = [
                self.create_tool_call(tc.function.name, _parse_json(tc.function.arguments))
                for tc in tool_calls
                if _parse_json(tc.function.arguments) is not None
            ]
            # 部分推理服务将 tool call 内嵌在正文 JSON 中
            if message.content and not tool_calls:
                m = re.search(r"\{.*\}", message.content)
                if m:
                    tc = _parse_json(m.group(0))
                    if tc:
                        params = tc.get("parameters", tc.get("arguments"))
                        if tc.get("name") and params:
                            combined.append(self.create_tool_call(tc["name"], params))
                            return None, combined
            return message.content, combined

        return message.content


# ---------------------------------------------------------------------------
# Anthropic 基类
# ---------------------------------------------------------------------------

class AnthropicBaseProvider(LLMProvider):
    """适配 Anthropic Messages API。"""

    def _create_client(self):
        from anthropic import Anthropic
        return Anthropic(api_key=self.api_key).messages

    def _make_function_def(self, name, description, properties, required):
        return {
            "name": name,
            "description": description,
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }

    def _wrap_image(self, image_data: bytes) -> dict:
        encoded = base64.b64encode(image_data).decode("utf-8")
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": encoded},
        }

    def call(self, messages: list[dict], functions: dict | None = None):
        tools = self.create_function_schema(functions) if functions else None

        # Anthropic 要求 system 单独传参
        system = "\n".join(
            m.get("content", "") for m in messages if m.get("role") == "system"
        )
        msgs = [self._transform_message(m) for m in messages if m.get("role") != "system"]

        kwargs: dict[str, Any] = {"messages": msgs, "model": self.model, "max_tokens": 4096}
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        completion = self.client.create(**kwargs)
        text = "".join(getattr(b, "text", "") for b in completion.content)

        if functions:
            tool_calls = [
                self.create_tool_call(b.name, b.input)
                for b in completion.content
                if b.type == "tool_use"
            ]
            return text, tool_calls

        return text


# ---------------------------------------------------------------------------
# Mistral 基类（继承 OpenAI，修正消息格式）
# ---------------------------------------------------------------------------

class MistralBaseProvider(OpenAIBaseProvider):
    def _make_function_def(self, name, description, properties, required):
        # 部分 Mistral 版本 description 可能是嵌套 dict
        if isinstance(description, dict):
            description = description.get("description", "")
        return super()._make_function_def(name, description, properties, required)

    def call(self, messages: list[dict], functions: dict | None = None):
        # Mistral 不允许最后一条消息是 assistant 角色
        msgs = list(messages)
        if msgs and msgs[-1].get("role") == "assistant":
            prefix = msgs.pop()["content"]
            if msgs and msgs[-1].get("role") == "user":
                msgs[-1]["content"] = prefix + "\n" + msgs[-1].get("content", "")
            else:
                msgs.append({"role": "user", "content": prefix})
        return super().call(msgs, functions)


# ---------------------------------------------------------------------------
# 具体提供商（每个只需声明 base_url / api_key / aliases）
# ---------------------------------------------------------------------------

class OpenAIProvider(OpenAIBaseProvider):
    base_url = "https://api.openai.com/v1"
    api_key = os.getenv("OPENAI_API_KEY")


class AnthropicProvider(AnthropicBaseProvider):
    api_key = os.getenv("ANTHROPIC_API_KEY")
    aliases = {
        "claude-3.5-sonnet": "claude-3-5-sonnet-20241022",
        "claude-3.5-haiku":  "claude-3-5-haiku-20241022",
        "claude-3-opus":     "claude-3-opus-20240229",
        "claude-sonnet-4-6": "claude-sonnet-4-6",
        "claude-opus-4-6":   "claude-opus-4-6",
    }


class GroqProvider(OpenAIBaseProvider):
    base_url = "https://api.groq.com/openai/v1"
    api_key = os.getenv("GROQ_API_KEY")
    aliases = {
        "llama-3.2": "llama-3.2-90b-vision-preview",
        "llama-3.3": "llama-3.3-70b-versatile",
    }


class DeepSeekProvider(OpenAIBaseProvider):
    base_url = "https://api.deepseek.com"
    api_key = os.getenv("DEEPSEEK_API_KEY")
    aliases = {
        "deepseek-chat":     "deepseek-chat",
        "deepseek-reasoner": "deepseek-reasoner",
    }


class OpenRouterProvider(OpenAIBaseProvider):
    base_url = "https://openrouter.ai/api/v1"
    api_key = os.getenv("OPENROUTER_API_KEY")
    aliases = {
        "llama-3.2":  "meta-llama/llama-3.2-90b-vision-instruct",
        "qwen-2.5-vl": "qwen/qwen2.5-vl-72b-instruct:free",
    }


class GeminiProvider(OpenAIBaseProvider):
    base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
    api_key = os.getenv("GEMINI_API_KEY")
    aliases = {
        "gemini-2.0-flash": "gemini-2.0-flash",
        "gemini-1.5-pro":   "gemini-1.5-pro",
    }


class FireworksProvider(OpenAIBaseProvider):
    base_url = "https://api.fireworks.ai/inference/v1"
    api_key = os.getenv("FIREWORKS_API_KEY")
    aliases = {
        "llama-3.2": "accounts/fireworks/models/llama-v3p2-90b-vision-instruct",
        "llama-3.3": "accounts/fireworks/models/llama-v3p3-70b-instruct",
    }


class LlamaProvider(OpenAIBaseProvider):
    base_url = "https://api.llama-api.com"
    api_key = os.getenv("LLAMA_API_KEY")
    aliases = {
        "llama-3.2": "llama-3.2-90b-vision",
        "llama-3.3": "llama-3.3-70b",
    }


class MistralProvider(MistralBaseProvider):
    base_url = "https://api.mistral.ai/v1"
    api_key = os.getenv("MISTRAL_API_KEY")
    aliases = {
        "mistral": "mistral-large-latest",
        "pixtral": "pixtral-large-latest",
    }


class MoonshotProvider(OpenAIBaseProvider):
    base_url = "https://api.moonshot.cn/v1"
    api_key = os.getenv("MOONSHOT_API_KEY")
    aliases = {
        "moonshot-v1":        "moonshot-v1-128k",
        "moonshot-v1-vision": "moonshot-v1-128k-vision-preview",
    }
