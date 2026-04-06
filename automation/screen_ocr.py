"""
屏幕 OCR 识别 - 让 ARIA「看懂」屏幕上有什么

功能：
- 截取指定区域/全屏截图
- OCR 识别文字内容
- 返回文字位置 + 内容
- 支持中英文混合

依赖：
- pip install pytesseract Pillow（可选：pyautogui，用于更稳定的点击/输入模拟）
- Windows 需安装 Tesseract OCR：https://github.com/UB-Mannheim/tesseract/wiki
"""

from __future__ import annotations

import os
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DOTENV_LOADED = False


def _load_project_dotenv() -> None:
    """从项目根目录 .env 注入环境变量（不覆盖已存在项），便于 TESSERACT_CMD 等配置生效。"""
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    try:
        from dotenv import load_dotenv

        env_path = Path(__file__).resolve().parent.parent / ".env"
        load_dotenv(dotenv_path=env_path, override=False)
    except Exception:
        pass


def _check_dependencies() -> tuple[bool, str]:
    """检查依赖是否已安装"""
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
        return True, ""
    except ImportError as e:
        return False, f"missing_dependency:{e}"


def _check_tesseract() -> tuple[bool, str]:
    """检查 Tesseract OCR 是否可用"""
    _load_project_dotenv()
    try:
        import pytesseract
        # 支持环境变量直接指定 tesseract.exe 路径（避免强依赖 PATH）
        tesseract_cmd = os.getenv("TESSERACT_CMD", "").strip()
        if tesseract_cmd:
            try:
                pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
            except Exception:
                pass
        # 尝试获取 tesseract 版本
        pytesseract.get_tesseract_version()
        return True, ""
    except Exception as e:
        return False, f"tesseract_not_found:{e}"


def capture_screen(region: tuple[int, int, int, int] | None = None):
    """
    截取屏幕（全屏或指定区域）
    
    Args:
        region: (left, top, width, height) 或 None（全屏）
    
    Returns:
        PIL.Image 截图对象
    """
    # 优先用 pyautogui（兼容历史行为）；缺失时回退到 PIL.ImageGrab，
    # 避免 OCR 因 pyautogui 缺失而完全不可用。
    try:
        import pyautogui

        screenshot = pyautogui.screenshot(region=region)
        return screenshot
    except Exception as pyauto_err:
        try:
            from PIL import ImageGrab
        except Exception as pil_err:
            raise RuntimeError(
                f"screen_capture_failed:pyautogui={pyauto_err};imagegrab_import={pil_err}"
            ) from pil_err

        bbox = None
        if region is not None:
            left, top, width, height = [int(x) for x in region]
            bbox = (left, top, left + max(1, width), top + max(1, height))
        try:
            # all_screens 在 Windows 多屏下更稳妥
            return ImageGrab.grab(bbox=bbox, all_screens=True)
        except Exception as grab_err:
            raise RuntimeError(
                f"screen_capture_failed:pyautogui={pyauto_err};imagegrab_grab={grab_err}"
            ) from grab_err


def ocr_screen(
    region: tuple[int, int, int, int] | None = None,
    lang: str = "chi_sim+eng",
    *,
    min_confidence: int = 30,
    scale: float = 1.0,
    tesseract_config: str | None = None,
) -> dict[str, Any]:
    """
    OCR 识别屏幕文字

    后端由环境变量 ARIA_OCR_BACKEND 控制：
    - deepseek_local: 本地 DeepSeek-OCR-2 HF 模型（需 CUDA）；不可用时自动降级 tesseract
    - deepseek_vlm:   远程 VLM API；不可用时自动降级 tesseract
    - tesseract（默认）: Tesseract OCR

    Args:
        region: (left, top, width, height) 或 None（全屏）
        lang: 'chi_sim+eng'（中英文），'eng'（仅英文），'chi_sim'（仅中文）
        min_confidence: 词块最低置信度（0–100）；微信等场景英文常低于 60，可降到 15–30
        scale: 截图放大倍数（>1 有利于小字号英文）
        tesseract_config: 传给 pytesseract 的额外 config（如 --psm 6）

    Returns:
        {
            "success": bool,
            "error": str|None,
            "text": "完整识别文本",
            "blocks": [
                {"text": "每行文字", "bbox": [x, y, w, h], "confidence": int},
                ...
            ]
        }
    """
    _load_project_dotenv()

    backend = os.getenv("ARIA_OCR_BACKEND", "tesseract").strip().lower()

    if backend == "deepseek_local":
        try:
            from automation.deepseek_ocr_adapter import ocr_screen_with_local_model
            result = ocr_screen_with_local_model(region)
            if result.get("success"):
                return result
            logger.warning(f"deepseek_local OCR 失败，降级到 tesseract: {result.get('error')}")
        except Exception as e:
            logger.warning(f"deepseek_local OCR 异常，降级到 tesseract: {e}")

    elif backend == "deepseek_vlm":
        try:
            from automation.deepseek_ocr_adapter import ocr_screen_with_vlm
            result = ocr_screen_with_vlm(region, task="free_ocr")
            if result.get("success"):
                return result
            logger.warning(f"deepseek_vlm OCR 失败，降级到 tesseract: {result.get('error')}")
        except Exception as e:
            logger.warning(f"deepseek_vlm OCR 异常，降级到 tesseract: {e}")

    # 检查依赖
    ok, err = _check_dependencies()
    if not ok:
        return {"success": False, "error": err, "text": "", "blocks": []}
    
    # 检查 Tesseract
    ok, err = _check_tesseract()
    if not ok:
        return {
            "success": False,
            "error": err,
            "text": "",
            "blocks": [],
            "hint": "请安装 Tesseract OCR 并添加到 PATH"
        }
    
    try:
        import pytesseract
        from PIL import Image
        # 同样支持环境变量指定 tesseract.exe 路径
        tesseract_cmd = os.getenv("TESSERACT_CMD", "").strip()
        if tesseract_cmd:
            try:
                pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
            except Exception:
                pass
        
        screenshot = capture_screen(region)
        if scale and float(scale) != 1.0:
            w, h = screenshot.size
            nw = max(1, int(w * float(scale)))
            nh = max(1, int(h * float(scale)))
            try:
                resample = Image.Resampling.LANCZOS
            except AttributeError:
                resample = Image.LANCZOS  # type: ignore[attr-defined]
            screenshot = screenshot.resize((nw, nh), resample)

        cfg = (tesseract_config or "").strip() or None

        # OCR 识别完整文本
        try:
            text = pytesseract.image_to_string(screenshot, lang=lang, config=cfg)
        except Exception as tess_err:
            raise RuntimeError(f"tesseract_ocr_failed:{tess_err}") from tess_err
        if text is None:
            text = ""

        # OCR 识别详细数据（位置 + 置信度）
        try:
            data = pytesseract.image_to_data(
                screenshot,
                lang=lang,
                output_type=pytesseract.Output.DICT,
                config=cfg,
            )
        except Exception:
            data = {"text": [], "conf": [], "left": [], "top": [], "width": [], "height": []}

        blocks = []
        for i in range(len(data["text"])):
            conf = int(data["conf"][i])
            if conf < 0 or conf < min_confidence:
                continue
            word = (data["text"][i] or "").strip()
            if not word:
                continue
            blocks.append(
                {
                    "text": word,
                    "bbox": [
                        data["left"][i],
                        data["top"][i],
                        data["width"][i],
                        data["height"][i],
                    ],
                    "confidence": conf,
                }
            )

        if scale and float(scale) != 1.0:
            inv = 1.0 / float(scale)
            for b in blocks:
                bb = b["bbox"]
                b["bbox"] = [
                    int(round(bb[0] * inv)),
                    int(round(bb[1] * inv)),
                    int(round(bb[2] * inv)),
                    int(round(bb[3] * inv)),
                ]

        return {
            "success": True,
            "error": None,
            "text": (text or "").strip(),
            "blocks": blocks,
        }
        
    except Exception as e:
        logger.error(f"ocr_screen_failed: {e}")
        return {
            "success": False,
            "error": f"ocr_failed:{str(e)}",
            "text": "",
            "blocks": []
        }


def find_text_on_screen(
    search_text: str,
    region: tuple[int, int, int, int] | None = None,
    lang: str = 'chi_sim+eng',
    *,
    scale: float = 2.0,
    min_confidence: int = 30,
) -> dict[str, Any]:
    """
    在屏幕上查找指定文字的位置

    Args:
        search_text:    要查找的文字
        region:         (left, top, width, height) 或 None（全屏）
        lang:           OCR 语言
        scale:          截图放大倍数（>1 有利于 UI 小字号识别，默认 2.0）
        min_confidence: Tesseract 词块最低置信度（0–100），默认 30（UI 文字置信度普遍偏低）

    Returns:
        {
            "success": bool,
            "error": str|None,
            "matches": [
                {
                    "text": "匹配的文字",
                    "bbox": [x, y, w, h],
                    "center": [cx, cy],
                    "confidence": int
                },
                ...
            ]
        }
    """
    if not search_text.strip():
        return {"success": False, "error": "empty_search_text", "matches": []}

    result = ocr_screen(region, lang, scale=scale, min_confidence=min_confidence)
    if not result["success"]:
        return result

    matches = []
    search_lower = search_text.lower()

    for block in result["blocks"]:
        block_text = block["text"]
        if search_lower in block_text.lower():
            matches.append({
                "text": block_text,
                "bbox": block["bbox"],
                "center": [
                    block["bbox"][0] + block["bbox"][2] // 2,
                    block["bbox"][1] + block["bbox"][3] // 2
                ],
                "confidence": block["confidence"]
            })

    # 若 Tesseract 无匹配，按 ARIA_OCR_VLM_FALLBACK 决定是否 VLM 兜底（默认开启）
    if not matches:
        vlm_fallback = os.getenv("ARIA_OCR_VLM_FALLBACK", "1").strip().lower() not in ("0", "false", "off", "no")
        if vlm_fallback:
            try:
                from automation.deepseek_ocr_adapter import ocr_screen_with_vlm
                vlm_result = ocr_screen_with_vlm(region, task="free_ocr")
                if vlm_result.get("success"):
                    for block in vlm_result.get("blocks", []):
                        block_text = block.get("text", "")
                        if search_lower in block_text.lower():
                            matches.append({
                                "text": block_text,
                                "bbox": block.get("bbox", [0, 0, 0, 0]),
                                "center": [
                                    block.get("bbox", [0, 0, 0, 0])[0] + block.get("bbox", [0, 0, 0, 0])[2] // 2,
                                    block.get("bbox", [0, 0, 0, 0])[1] + block.get("bbox", [0, 0, 0, 0])[3] // 2
                                ],
                                "confidence": block.get("confidence", 50),
                            })
            except Exception as e:
                logger.debug(f"find_text VLM fallback skipped: {e}")

    return {
        "success": True,
        "error": None,
        "matches": matches
    }


def click_text(
    search_text: str,
    region: tuple[int, int, int, int] | None = None,
    lang: str = 'chi_sim+eng',
    button: str = 'left'
) -> dict[str, Any]:
    """
    点击屏幕上的指定文字
    
    Args:
        search_text: 要点击的文字
        region: (left, top, width, height) 或 None（全屏）
        lang: OCR 语言
        button: 'left' / 'right' / 'middle'
    
    Returns:
        {
            "success": bool,
            "error": str|None,
            "clicked": bool,
            "position": [x, y]|None
        }
    """
    result = find_text_on_screen(search_text, region, lang)
    
    if not result["success"]:
        return result
    
    if not result["matches"]:
        return {
            "success": False,
            "error": "text_not_found",
            "clicked": False,
            "position": None,
            "message": f"未在屏幕上找到文字：{search_text}"
        }
    
    # 使用第一个匹配（最相关）
    match = result["matches"][0]
    x, y = match["center"]
    
    try:
        import pyautogui
        # 点击前稍微移动，避免机械感
        pyautogui.moveTo(x, y, duration=0.3)
        pyautogui.click(x=x, y=y, button=button)
        
        return {
            "success": True,
            "error": None,
            "clicked": True,
            "position": [x, y],
            "message": f"已点击文字「{match['text']}」位置 ({x}, {y})"
        }
    except Exception as e:
        logger.error(f"click_text_failed: {e}")
        return {
            "success": False,
            "error": f"click_failed:{str(e)}",
            "clicked": False,
            "position": None
        }


def type_text(text: str, interval: float = 0.05) -> dict[str, Any]:
    """
    向前台窗口输入文本（使用 pyautogui）
    
    Args:
        text: 要输入的文本
        interval: 每个字符间隔（秒）
    
    Returns:
        {
            "success": bool,
            "error": str|None,
            "message": str
        }
    """
    if not text.strip():
        return {"success": False, "error": "empty_text", "message": "输入文本为空"}
    
    try:
        import pyautogui
        pyautogui.write(text, interval=interval)
        
        return {
            "success": True,
            "error": None,
            "message": f"已输入 {len(text)} 个字符"
        }
    except Exception as e:
        logger.error(f"type_text_failed: {e}")
        return {
            "success": False,
            "error": f"type_failed:{str(e)}",
            "message": ""
        }


def get_capability_summary() -> str:
    """获取屏幕 OCR 能力描述（用于系统提示词）"""
    _load_project_dotenv()
    backend = os.getenv("ARIA_OCR_BACKEND", "tesseract").strip().lower()

    if backend == "deepseek_local":
        try:
            from automation.deepseek_ocr_adapter import _check_local_model_available
            ok, err = _check_local_model_available()
            if ok:
                return "【屏幕 OCR】已配置（DeepSeek-OCR-2 本地模型）：screen_ocr 识别屏幕文字，screen_find_text 查找文字位置，screen_click_text 点击文字。支持中英文混合识别，识别质量优于 Tesseract。"
            return f"【屏幕 OCR】DeepSeek-OCR-2 本地模型不可用（{err}），将降级到 Tesseract。"
        except Exception:
            pass

    if backend == "deepseek_vlm":
        try:
            from automation.deepseek_ocr_adapter import _check_vlm_available
            ok, _ = _check_vlm_available()
            if ok:
                return "【屏幕 OCR】已配置（DeepSeek VLM 远程）：screen_ocr 识别屏幕文字，screen_find_text 查找文字位置，screen_click_text 点击文字。"
        except Exception:
            pass

    deps_ok, _ = _check_dependencies()
    tess_ok, _ = _check_tesseract()

    if not deps_ok:
        return "【屏幕 OCR】未配置（需安装 pytesseract/Pillow；可选 pyautogui）。"

    if not tess_ok:
        return "【屏幕 OCR】Tesseract OCR 未检测到。请安装 Tesseract 并添加到 PATH（Windows: https://github.com/UB-Mannheim/tesseract/wiki）。"

    return "【屏幕 OCR】已配置（Tesseract）：screen_ocr 识别屏幕文字，screen_find_text 查找文字位置，screen_click_text 点击文字。支持中英文混合识别。"
