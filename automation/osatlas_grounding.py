"""
OS-Atlas Grounding 适配器

将自然语言 UI 描述（如"搜索框"、"确认按钮"）转换为屏幕像素坐标。
比 OCR+LLM 推理方案更快、更准确，尤其对非文字 UI 元素（图标、按钮图形）有效。

配置：
  ARIA_GROUNDING_BACKEND   : osatlas（默认）| disabled
  ARIA_OSATLAS_SOURCE      : HuggingFace Space 名称，默认 maxiw/OS-ATLAS
  ARIA_OSATLAS_MODEL       : 模型 ID，默认 OS-Copilot/OS-Atlas-Base-7B
  HF_TOKEN                 : HuggingFace API token（可选，公开 Space 不需要）
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from typing import Any

logger = logging.getLogger(__name__)

_OSATLAS_SOURCE = os.getenv("ARIA_OSATLAS_SOURCE", "maxiw/OS-ATLAS")
_OSATLAS_MODEL = os.getenv("ARIA_OSATLAS_MODEL", "OS-Copilot/OS-Atlas-Base-7B")
_OSATLAS_API = "/run_example"

_client = None  # lazy singleton


def _get_client():
    global _client
    if _client is None:
        from gradio_client import Client
        hf_token = os.getenv("HF_TOKEN") or None
        _client = Client(_OSATLAS_SOURCE, hf_token=hf_token)
    return _client


def _extract_bbox_midpoint(bbox_response: str) -> tuple[float, float] | None:
    """从 OS-Atlas 返回的 bbox 字符串中提取中心点坐标（归一化 0-1000）。"""
    match = re.search(r"<\|box_start\|>(.*?)<\|box_end\|>", bbox_response)
    inner = match.group(1) if match else bbox_response
    numbers = [float(n) for n in re.findall(r"\d+\.\d+|\d+", inner)]
    if len(numbers) == 2:
        return numbers[0], numbers[1]
    if len(numbers) >= 4:
        return (numbers[0] + numbers[2]) / 2, (numbers[1] + numbers[3]) / 2
    return None


def is_grounding_enabled() -> bool:
    backend = os.getenv("ARIA_GROUNDING_BACKEND", "osatlas").strip().lower()
    return backend not in ("disabled", "0", "false", "off")


def find_element(query: str, screenshot_path: str) -> tuple[int, int] | None:
    """
    用 OS-Atlas 在截图中定位 UI 元素，返回绝对像素坐标。

    Args:
        query: 自然语言描述，如"搜索框"、"发送按钮"
        screenshot_path: 截图文件路径（PNG/JPEG）

    Returns:
        (x, y) 绝对像素坐标，失败返回 None
    """
    if not is_grounding_enabled():
        return None
    try:
        from gradio_client import handle_file
        client = _get_client()
        result = client.predict(
            image=handle_file(screenshot_path),
            text_input=query + "\nReturn the response in the form of a bbox",
            model_id=_OSATLAS_MODEL,
            api_name=_OSATLAS_API,
        )
        raw_bbox = result[1] if isinstance(result, (list, tuple)) and len(result) > 1 else str(result)
        norm_pos = _extract_bbox_midpoint(raw_bbox)
        if norm_pos is None:
            logger.warning("osatlas_grounding_no_bbox query=%r response=%r", query, raw_bbox)
            return None

        # OS-Atlas 返回 0-1000 归一化坐标，转换为绝对像素
        from automation.computer_use import virtual_screen_metrics
        m = virtual_screen_metrics()
        x = m["left"] + int(round(norm_pos[0] / 1000.0 * m["width"]))
        y = m["top"] + int(round(norm_pos[1] / 1000.0 * m["height"]))
        logger.info("osatlas_grounding query=%r -> (%d, %d)", query, x, y)
        return x, y
    except Exception as e:
        logger.warning("osatlas_grounding_error query=%r error=%s", query, e)
        return None


def find_element_from_data_url(query: str, data_url: str) -> tuple[int, int] | None:
    """
    从 base64 data URL 截图中定位 UI 元素。
    ARIA 的 computer_screenshot 返回 data URL，此函数将其写入临时文件后调用 find_element。
    """
    if not is_grounding_enabled():
        return None
    try:
        import base64
        header, b64data = data_url.split(",", 1)
        ext = "jpg" if "jpeg" in header else "png"
        with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f:
            f.write(base64.b64decode(b64data))
            tmp_path = f.name
        result = find_element(query, tmp_path)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return result
    except Exception as e:
        logger.warning("osatlas_grounding_data_url_error query=%r error=%s", query, e)
        return None
