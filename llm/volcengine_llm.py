from __future__ import annotations

import json
import os
import re
import time
from typing import Literal, Optional, cast

from dotenv import load_dotenv

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

# 加载环境变量（强制覆盖同名系统变量，避免读取到空值）
load_dotenv(override=True)

# 默认：火山引擎方舟 OpenAI 兼容接口（文本对话使用 chat.completions，与 ARIA 现有 messages 结构一致）
DEFAULT_OPENAI_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL_NAME = "doubao-seed-2-0-lite-260215"

ReasoningEffort = Literal["minimal", "low", "medium", "high"]
_REASONING_EFFORT_SET = frozenset({"minimal", "low", "medium", "high"})


def _normalize_reasoning_effort(value: str | None) -> Optional[ReasoningEffort]:
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in _REASONING_EFFORT_SET:
        return cast(ReasoningEffort, s)
    return None


def _is_ark_base_url(base_url: str) -> bool:
    u = (base_url or "").lower()
    return "volces.com" in u or "ark.cn-beijing" in u


def resolve_inference_api_key(dotenv_path: Optional[str] = None) -> str:
    """
    按 OPENAI_BASE_URL 选择密钥：方舟域名优先 ARK_API_KEY；百炼域名优先 DASHSCOPE_API_KEY。
    web_app 传入项目根 .env 路径以保证与工作目录无关。
    """
    if dotenv_path:
        load_dotenv(dotenv_path=dotenv_path, override=True)
    else:
        load_dotenv(override=True)

    base = (os.getenv("OPENAI_BASE_URL") or DEFAULT_OPENAI_BASE_URL).lower()
    oa = (os.getenv("OPENAI_API_KEY") or "").strip()
    vo = (os.getenv("VOLCANO_API_KEY") or "").strip()
    ar = (os.getenv("ARK_API_KEY") or "").strip()
    ds = (os.getenv("DASHSCOPE_API_KEY") or "").strip()
    ali = (os.getenv("ALIYUN_API_KEY") or "").strip()

    if _is_ark_base_url(base):
        return ar or vo or oa
    return ds or ali or oa or ar or vo


class VolcengineLLM:
    """
    OpenAI 官方 Python SDK，对接：
    - 火山方舟：https://www.volcengine.com/docs/82379/1399008 （默认）
    - 阿里云百炼兼容模式（将 OPENAI_BASE_URL 改为 dashscope 兼容地址即可）
    使用 chat.completions；user 消息的 content 可为字符串或 OpenAI 多模态片段列表（text + image_url）。
    """

    def __init__(self, api_key=None):
        self.base_url = os.getenv("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL).rstrip("/")
        self.model_name = os.getenv("MODEL_NAME", DEFAULT_MODEL_NAME)
        self.api_key = (api_key or "").strip() or resolve_inference_api_key()
        self.max_retries = 3
        self.timeout_s = float(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
        self.enable_thinking = os.getenv("ENABLE_THINKING", "true").strip().lower() in ("1", "true", "yes", "on")
        self._client: OpenAI | None = None
        self._rebuild_client()
        if self.api_key:
            print("API Key配置成功")
        else:
            print("等待设置API Key")

    def _dashscope_thinking_extra(self) -> bool:
        """仅百炼兼容接口传 enable_thinking；方舟等网关勿传，避免 400。"""
        if not self.enable_thinking:
            return False
        u = (self.base_url or "").lower()
        return "dashscope" in u or "aliyuncs.com" in u

    def _rebuild_client(self) -> None:
        if not self.api_key:
            self._client = None
            return
        self._client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            max_retries=0,
        )

    def set_api_key(self, api_key):
        self.api_key = (api_key or "").strip()
        self._rebuild_client()
        ok = bool(self.api_key)
        print("API Key设置成功" if ok else "设置API Key失败: empty_api_key")
        return ok

    @staticmethod
    def _sanitize_for_user(text: str, max_len: int = 450) -> str:
        s = (text or "").replace("\r", " ").replace("\n", " ").strip()
        s = re.sub(r"sk-[a-zA-Z0-9]{8,}", "sk-***", s)
        if len(s) > max_len:
            s = s[: max_len - 3] + "..."
        return s or "未知错误"

    @staticmethod
    def _is_invalid_api_key_error(e: APIStatusError) -> bool:
        if e.status_code == 401:
            return True
        body = e.body
        if not isinstance(body, dict):
            return False
        err = body.get("error")
        if isinstance(err, dict) and err.get("code") == "invalid_api_key":
            return True
        return False

    def _invalid_api_key_user_message(self) -> str:
        if _is_ark_base_url(self.base_url):
            return (
                "API Key 无效（HTTP 401）。方舟接口不接受当前密钥，重试无效。\n\n"
                "请在 .env 配置火山方舟 API Key：ARK_API_KEY=...\n"
                "并确认 OPENAI_BASE_URL=https://ark.cn-beijing.volces.com/api/v3\n\n"
                "文档：https://www.volcengine.com/docs/82379/1399008"
            )
        return (
            "API Key 无效（HTTP 401，invalid_api_key）。阿里云已拒绝本次请求，重试不会解决。\n\n"
            "请按下面检查：\n"
            "1）在百炼控制台创建或复制「API-KEY」，粘贴到项目根目录 .env：DASHSCOPE_API_KEY=你的密钥\n"
            "2）密钥完整、无多余空格或引号；不要用其它云产品的 Key 冒充百炼 Key\n"
            "3）保存 .env 后重新发一条消息；若仍报错，关闭窗口后重新运行 launch_aria.bat\n\n"
            "获取 Key：https://help.aliyun.com/zh/model-studio/get-api-key\n"
            "错误说明：https://help.aliyun.com/zh/model-studio/error-code#apikey-error"
        )

    @staticmethod
    def _status_error_summary(e: APIStatusError) -> str:
        parts = [f"HTTP {e.status_code}"]
        body = e.body
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                msg = err.get("message") or err.get("code")
                if msg:
                    parts.append(str(msg))
            elif isinstance(err, str) and err.strip():
                parts.append(err.strip())
        if len(parts) == 1 and getattr(e, "message", None):
            parts.append(str(e.message))
        return " ".join(parts)

    @staticmethod
    def _usage_from_completion(completion) -> dict[str, int] | None:
        u = getattr(completion, "usage", None)
        if u is None:
            return None
        if hasattr(u, "model_dump"):
            d = u.model_dump()
        elif isinstance(u, dict):
            d = u
        else:
            d = {
                "prompt_tokens": getattr(u, "prompt_tokens", None),
                "completion_tokens": getattr(u, "completion_tokens", None),
                "total_tokens": getattr(u, "total_tokens", None),
            }
        pt = int(d.get("prompt_tokens") or 0)
        ct = int(d.get("completion_tokens") or 0)
        tt = int(d.get("total_tokens") or 0)
        if pt == 0 and ct == 0 and tt == 0:
            return None
        if tt <= 0:
            tt = pt + ct
        return {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": tt}

    def _merge_completion_usage_into(self, completion, totals: dict[str, int]) -> None:
        chunk = self._usage_from_completion(completion)
        if not chunk:
            return
        totals["prompt_tokens"] += chunk["prompt_tokens"]
        totals["completion_tokens"] += chunk["completion_tokens"]
        totals["total_tokens"] += chunk["total_tokens"]
        totals["llm_calls"] += 1

    def handle_api_error(self, error, attempt):
        print(f"API错误处理 (尝试 {attempt}/3): {str(error)}")
        err_s = str(error)
        if "401" in err_s or "invalid_api_key" in err_s.lower():
            print("认证失败（API Key），跳过重试等待")
            return
        if "429" in err_s:
            wait_time = attempt * 10
            print(f"速率限制，等待 {wait_time} 秒后重试...")
            time.sleep(wait_time)
        elif "404" in err_s:
            print("检查模型配置...")
            time.sleep(attempt * 2)
        elif "500" in err_s:
            wait_time = attempt * 5
            print(f"服务器错误，等待 {wait_time} 秒后重试...")
            time.sleep(wait_time)
        else:
            wait_time = attempt * 3
            print(f"未知错误，等待 {wait_time} 秒后重试...")
            time.sleep(wait_time)

    @staticmethod
    def _message_to_text(message) -> str:
        if message is None:
            return ""
        content = getattr(message, "content", None)
        reasoning = getattr(message, "reasoning_content", None)

        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            text_parts = []
            for p in content:
                if isinstance(p, dict):
                    text_parts.append(p.get("text", ""))
                else:
                    text_parts.append(getattr(p, "text", "") or "")
            merged = "".join(text_parts).strip()
            if merged:
                return merged
        if isinstance(reasoning, str) and reasoning.strip():
            return reasoning.strip()
        return ""

    @staticmethod
    def _messages_have_multimodal_content(messages) -> bool:
        for m in messages or []:
            if isinstance((m or {}).get("content"), list):
                return True
        return False

    def generate(
        self,
        messages,
        model_name=None,
        reasoning_effort: str | None = None,
    ) -> tuple[str, dict[str, int]]:
        """OpenAI 兼容 chat.completions；返回 (文本, 本次调用累计的 token 用量)。"""
        totals: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "llm_calls": 0}
        if not self.api_key or not self._client:
            if _is_ark_base_url(self.base_url):
                return "请先设置 ARK_API_KEY（火山方舟），见 https://www.volcengine.com/docs/82379/1399008", totals
            return "请先设置 API Key（.env 中 DASHSCOPE_API_KEY 等）", totals

        selected_model = (model_name or self.model_name or "").strip() or self.model_name
        last_error = ""
        thinking_feature_on = self._dashscope_thinking_extra()
        multimodal = self._messages_have_multimodal_content(messages)
        eff = _normalize_reasoning_effort(reasoning_effort)
        is_ark = _is_ark_base_url(self.base_url)

        if is_ark:
            use_thinking = False
        elif eff == "minimal":
            use_thinking = False
        elif eff in ("low", "medium", "high"):
            # 百炼：显式档位时仍遵守多模态下关闭 thinking，避免 400
            use_thinking = thinking_feature_on and not multimodal
        else:
            use_thinking = thinking_feature_on and not multimodal

        thinking_fallback_done = False
        ark_reasoning_fallback_done = False

        openai_messages = [
            {"role": m.get("role", "user"), "content": m.get("content", "")} for m in (messages or [])
        ]

        for attempt in range(self.max_retries):
            try:
                print("OpenAI 兼容 Chat Completions:")
                ark_eff = eff if is_ark and eff and not ark_reasoning_fallback_done else None
                re_log = (ark_eff if is_ark else eff) or "—"
                print(
                    f"  base_url={self.base_url}  model={selected_model}  "
                    f"dashscope_thinking_extra={use_thinking}  reasoning_effort={re_log}"
                )

                create_kwargs: dict = {
                    "model": selected_model,
                    "messages": openai_messages,
                    "timeout": self.timeout_s,
                }
                extra_body: dict = {}
                if is_ark and ark_eff:
                    extra_body["reasoning_effort"] = ark_eff
                if use_thinking:
                    extra_body["enable_thinking"] = True
                if extra_body:
                    create_kwargs["extra_body"] = extra_body

                completion = self._client.chat.completions.create(**create_kwargs)
                self._merge_completion_usage_into(completion, totals)
                choice = completion.choices[0] if completion.choices else None
                text = self._message_to_text(choice.message if choice else None)
                if text:
                    return text, totals

                last_error = "接口返回 200 但未解析到正文；可尝试 ENABLE_THINKING=false 或更换 MODEL_NAME"
                if thinking_feature_on and use_thinking and not thinking_fallback_done:
                    print("正文为空，关闭 enable_thinking 后重试")
                    use_thinking = False
                    thinking_fallback_done = True
                    continue

                raw = completion.model_dump() if hasattr(completion, "model_dump") else str(completion)
                snippet = self._sanitize_for_user(json.dumps(raw, ensure_ascii=False, default=str), 400)
                return f"未收到有效模型输出。{last_error} 响应摘要：{snippet}", totals

            except APIStatusError as e:
                if self._is_invalid_api_key_error(e):
                    print(f"API调用失败（无效密钥，不再重试）: {self._status_error_summary(e)}")
                    return self._invalid_api_key_user_message(), totals
                summary = self._status_error_summary(e)
                last_error = summary
                print(f"API调用失败 ({attempt + 1}/{self.max_retries}): {last_error}")
                if (
                    is_ark
                    and eff
                    and not ark_reasoning_fallback_done
                    and e.status_code in (400, 422)
                ):
                    print("方舟接口拒绝 reasoning_effort，去掉该字段后重试一次")
                    ark_reasoning_fallback_done = True
                    continue
                if (
                    thinking_feature_on
                    and use_thinking
                    and not thinking_fallback_done
                    and e.status_code in (400, 422)
                ):
                    print("关闭 enable_thinking 后重试一次（兼容部分模型/网关）")
                    use_thinking = False
                    thinking_fallback_done = True
                    continue
                if attempt < self.max_retries - 1:
                    self.handle_api_error(e, attempt + 1)
                else:
                    hint = self._failure_hint()
                    return f"API调用失败：{self._sanitize_for_user(last_error)}\n\n{hint}", totals

            except APITimeoutError as e:
                last_error = f"请求超时（{int(self.timeout_s)}s）：{e}"
                print(f"API调用失败 ({attempt + 1}/{self.max_retries}): {last_error}")
                if attempt < self.max_retries - 1:
                    self.handle_api_error(e, attempt + 1)
                else:
                    return f"API调用失败：{self._sanitize_for_user(last_error)}", totals

            except APIConnectionError as e:
                last_error = f"网络连接失败：{e}"
                print(f"API调用失败 ({attempt + 1}/{self.max_retries}): {last_error}")
                if attempt < self.max_retries - 1:
                    self.handle_api_error(e, attempt + 1)
                else:
                    return f"API调用失败：{self._sanitize_for_user(last_error)}", totals

            except Exception as e:
                last_error = str(e)
                print(f"API调用失败 ({attempt + 1}/{self.max_retries}): {last_error}")
                if attempt < self.max_retries - 1:
                    self.handle_api_error(e, attempt + 1)
                else:
                    return f"API调用失败：{self._sanitize_for_user(last_error)}\n\n{self._failure_hint()}", totals

        return f"API调用失败：{self._sanitize_for_user(last_error)}", totals

    def _failure_hint(self) -> str:
        if _is_ark_base_url(self.base_url):
            return (
                "请核对：1) ARK_API_KEY 为方舟控制台密钥；2) MODEL_NAME 与方舟推理接入点一致；"
                "3) OPENAI_BASE_URL=https://ark.cn-beijing.volces.com/api/v3；4) pip install -U \"openai>=1.0\"。"
            )
        return (
            "请核对：1) DASHSCOPE_API_KEY；2) MODEL_NAME；"
            "3) OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1；4) ENABLE_THINKING=false。"
        )
