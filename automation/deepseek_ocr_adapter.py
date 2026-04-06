"""
DeepSeek OCR 适配器 - 使用 VLM (Vision Language Model) 或本地 DeepSeek-OCR-2 模型进行智能 OCR

相比 Tesseract：
- 理解上下文，能区分联系人名和消息内容
- 更准确识别中英文混合文本
- 自动过滤噪音和无关内容

后端选项（ARIA_OCR_BACKEND 环境变量）：
- deepseek_local: 本地 DeepSeek-OCR-2 HF 模型（需 CUDA + transformers）
- deepseek_vlm:   远程 VLM API（需 ARK_API_KEY + vision 模型）
- tesseract:      Tesseract OCR（默认）

依赖（deepseek_local）：
- pip install transformers torch
- 需要 CUDA GPU
- DEEPSEEK_OCR2_MODEL_PATH: 模型路径或 HuggingFace hub ID（默认 deepseek-ai/DeepSeek-OCR-2）
"""

from __future__ import annotations

import base64
import io
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# --- 本地 DeepSeek-OCR-2 模型单例 ---
_local_model = None
_local_tokenizer = None
_local_model_load_error: str | None = None


def _get_local_model():
    """懒加载本地 DeepSeek-OCR-2 HF 模型（首次调用时初始化）"""
    global _local_model, _local_tokenizer, _local_model_load_error

    if _local_model is not None:
        return _local_model, _local_tokenizer

    if _local_model_load_error is not None:
        raise RuntimeError(_local_model_load_error)

    try:
        import torch
        from transformers import AutoModel, AutoTokenizer

        model_name = os.getenv(
            "DEEPSEEK_OCR2_MODEL_PATH",
            str(
                Path(__file__).parent.parent
                / "DeepSeek-OCR-2-main"
                / "DeepSeek-OCR-2-main"
                / "DeepSeek-OCR2-master"
                / "DeepSeek-OCR2-hf"
            ),
        )

        # 尝试 flash_attention_2，不可用时降级
        try:
            model = AutoModel.from_pretrained(
                model_name,
                _attn_implementation="flash_attention_2",
                trust_remote_code=True,
                use_safetensors=True,
            )
        except Exception:
            model = AutoModel.from_pretrained(
                model_name,
                trust_remote_code=True,
                use_safetensors=True,
            )

        tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
        model = model.eval().cuda().to(torch.bfloat16)

        _local_model = model
        _local_tokenizer = tokenizer
        logger.info("DeepSeek-OCR-2 本地模型加载成功")
        return _local_model, _local_tokenizer

    except Exception as e:
        _local_model_load_error = f"deepseek_ocr2_local_load_failed:{e}"
        raise RuntimeError(_local_model_load_error) from e


def _check_local_model_available() -> tuple[bool, str]:
    """检查本地 DeepSeek-OCR-2 是否可用（不触发加载）"""
    try:
        import torch  # noqa: F401
        from transformers import AutoModel  # noqa: F401

        if not torch.cuda.is_available():
            return False, "cuda_not_available"
        return True, ""
    except ImportError as e:
        return False, f"missing_dependency:{e}"


def ocr_screen_with_local_model(
    region: tuple[int, int, int, int] | None = None,
) -> dict[str, Any]:
    """
    使用本地 DeepSeek-OCR-2 HF 模型进行 OCR 识别

    Args:
        region: (left, top, width, height) 或 None（全屏）

    Returns:
        与 screen_ocr.ocr_screen() 相同格式的字典
    """
    try:
        from automation import screen_ocr

        screenshot = screen_ocr.capture_screen(region)
        model, tokenizer = _get_local_model()

        # model.infer() 需要文件路径，保存为临时 PNG
        with tempfile.TemporaryDirectory() as tmpdir:
            img_path = os.path.join(tmpdir, "screenshot.png")
            out_path = tmpdir
            screenshot.save(img_path)

            prompt = "<image>\nFree OCR."
            text = model.infer(
                tokenizer,
                prompt=prompt,
                image_file=img_path,
                output_path=out_path,
                base_size=1024,
                image_size=768,
                crop_mode=True,
                save_results=False,
            )

        if text is None:
            text = ""
        text = text.strip()

        # 按行构建 blocks（无精确坐标，估算位置）
        img_w, img_h = screenshot.size
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        blocks = []
        for i, line in enumerate(lines):
            y_ratio = (i + 0.5) / max(len(lines), 1)
            blocks.append({
                "text": line,
                "bbox": [int(img_w * 0.05), int(img_h * y_ratio), int(img_w * 0.9), 20],
                "confidence": 95,
            })

        return {
            "success": True,
            "error": None,
            "text": text,
            "blocks": blocks,
            "strategy": "deepseek_ocr2_local",
        }

    except Exception as e:
        logger.error(f"ocr_screen_with_local_model_failed: {e}")
        return {
            "success": False,
            "error": f"deepseek_local_ocr_failed:{e}",
            "text": "",
            "blocks": [],
            "strategy": "deepseek_ocr2_local",
        }


def _check_vlm_available() -> tuple[bool, str]:
    """检查 VLM 是否可用"""
    try:
        from llm.volcengine_llm import VolcengineLLM
        llm = VolcengineLLM()
        if not llm.api_key:
            return False, "vlm_api_key_missing"
        return True, ""
    except Exception as e:
        return False, f"vlm_unavailable:{e}"


def _image_to_base64(image) -> str:
    """将 PIL Image 转为 base64 编码"""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def ocr_screen_with_vlm(
    region: tuple[int, int, int, int] | None = None,
    *,
    task: str = "contact_search",
    contact_name: str = "",
) -> dict[str, Any]:
    """
    使用 VLM 进行智能 OCR 识别

    Args:
        region: (left, top, width, height) 或 None（全屏）
        task: 识别任务类型
            - "contact_search": 识别微信搜索结果中的联系人名
            - "free_ocr": 自由 OCR，识别所有文本
        contact_name: 目标联系人名（用于 contact_search 任务）

    Returns:
        {
            "success": bool,
            "error": str|None,
            "text": "完整识别文本",
            "blocks": [
                {"text": "联系人名", "bbox": [x, y, w, h], "confidence": int},
                ...
            ],
            "strategy": "vlm"  # 标识使用了 VLM
        }
    """
    # 检查 VLM 可用性
    ok, err = _check_vlm_available()
    if not ok:
        return {"success": False, "error": err, "text": "", "blocks": [], "strategy": "vlm"}

    try:
        from automation import screen_ocr
        from llm.volcengine_llm import VolcengineLLM

        # 截图
        screenshot = screen_ocr.capture_screen(region)

        # 转为 base64
        img_b64 = _image_to_base64(screenshot)

        # 构建 prompt
        if task == "contact_search":
            prompt = f"""这是微信搜索结果的截图。请识别图中所有可能的联系人名称。

目标联系人：{contact_name}

要求：
1. 只返回联系人名称，不要返回消息内容、时间、"微搜"等无关文本
2. 每行一个名称
3. 如果看到目标联系人，优先列出
4. 忽略明显的UI元素（如"搜一搜"、"小程序"、"公众号"）

直接输出联系人名称列表，不要解释："""
        else:
            prompt = "请识别图中的所有文本内容，按从上到下、从左到右的顺序输出："

        # 调用 VLM
        llm = VolcengineLLM()
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img_b64}"}
                    }
                ]
            }
        ]

        text, _usage = llm.generate(messages)

        # generate() 在无 api_key 时返回提示字符串而非抛异常
        if not text or text.startswith("请先设置"):
            return {
                "success": False,
                "error": f"vlm_call_failed:{text[:80]}",
                "text": "",
                "blocks": [],
                "strategy": "vlm"
            }

        text = text.strip()
        lines = [line.strip() for line in text.split("\n") if line.strip()]

        # 构建 blocks（VLM 通常不返回精确坐标，给个估算）
        blocks = []
        img_width, img_height = screenshot.size
        for i, line in enumerate(lines):
            # 估算位置：从上到下排列
            y_ratio = (i + 1) / (len(lines) + 1)
            blocks.append({
                "text": line,
                "bbox": [
                    int(img_width * 0.1),  # 左侧 10%
                    int(img_height * y_ratio),
                    int(img_width * 0.3),  # 宽度 30%
                    20  # 高度估算
                ],
                "confidence": 90  # VLM 置信度通常较高
            })

        return {
            "success": True,
            "error": None,
            "text": text,
            "blocks": blocks,
            "strategy": "vlm"
        }

    except Exception as e:
        logger.error(f"ocr_screen_with_vlm_failed: {e}")
        return {
            "success": False,
            "error": f"vlm_ocr_failed:{str(e)}",
            "text": "",
            "blocks": [],
            "strategy": "vlm"
        }


def find_contact_with_vlm(
    contact_name: str,
    region: tuple[int, int, int, int] | None = None,
) -> dict[str, Any]:
    """
    使用 VLM 在屏幕上查找联系人

    Args:
        contact_name: 要查找的联系人名
        region: (left, top, width, height) 或 None（全屏）

    Returns:
        {
            "success": bool,
            "error": str|None,
            "matches": [
                {
                    "text": "匹配的联系人名",
                    "bbox": [x, y, w, h],
                    "center": [cx, cy],
                    "confidence": int
                },
                ...
            ],
            "strategy": "vlm"
        }
    """
    result = ocr_screen_with_vlm(
        region=region,
        task="contact_search",
        contact_name=contact_name
    )

    if not result["success"]:
        return {**result, "matches": []}

    # 查找匹配项
    matches = []
    search_lower = contact_name.lower()

    for block in result["blocks"]:
        block_text = block["text"]
        if search_lower in block_text.lower() or block_text.lower() in search_lower:
            matches.append({
                "text": block_text,
                "bbox": block["bbox"],
                "center": [
                    block["bbox"][0] + block["bbox"][2] // 2,
                    block["bbox"][1] + block["bbox"][3] // 2
                ],
                "confidence": block["confidence"]
            })

    return {
        "success": True,
        "error": None,
        "matches": matches,
        "strategy": "vlm"
    }


def get_capability_summary() -> str:
    """获取 DeepSeek OCR 能力描述"""
    ok, _ = _check_vlm_available()

    if not ok:
        return "【DeepSeek OCR】未配置（需要支持 vision 的 API Key）。"

    return "【DeepSeek OCR】已配置：使用 VLM 进行智能 OCR，能理解上下文并过滤噪音。"
