"""
屏幕 OCR 识别 - 让 ARIA「看懂」屏幕上有什么

功能：
- 截取指定区域/全屏截图
- OCR 识别文字内容
- 返回文字位置 + 内容
- 支持中英文混合

依赖：
- pip install pyautogui pytesseract Pillow
- Windows 需安装 Tesseract OCR：https://github.com/UB-Mannheim/tesseract/wiki
"""

from __future__ import annotations

import os
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _check_dependencies() -> tuple[bool, str]:
    """检查依赖是否已安装"""
    try:
        import pyautogui  # noqa: F401
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
        return True, ""
    except ImportError as e:
        return False, f"missing_dependency:{e}"


def _check_tesseract() -> tuple[bool, str]:
    """检查 Tesseract OCR 是否可用"""
    try:
        import pytesseract
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
    import pyautogui
    screenshot = pyautogui.screenshot(region=region)
    return screenshot


def ocr_screen(region: tuple[int, int, int, int] | None = None, lang: str = 'chi_sim+eng') -> dict[str, Any]:
    """
    OCR 识别屏幕文字
    
    Args:
        region: (left, top, width, height) 或 None（全屏）
        lang: 'chi_sim+eng'（中英文），'eng'（仅英文），'chi_sim'（仅中文）
    
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
        
        screenshot = capture_screen(region)
        
        # OCR 识别完整文本
        text = pytesseract.image_to_string(screenshot, lang=lang)
        
        # OCR 识别详细数据（位置 + 置信度）
        data = pytesseract.image_to_data(
            screenshot,
            lang=lang,
            output_type=pytesseract.Output.DICT
        )
        
        blocks = []
        for i in range(len(data['text'])):
            conf = int(data['conf'][i])
            if conf > 60 and data['text'][i].strip():  # 置信度>60 且有内容
                blocks.append({
                    "text": data['text'][i].strip(),
                    "bbox": [
                        data['left'][i],
                        data['top'][i],
                        data['width'][i],
                        data['height'][i]
                    ],
                    "confidence": conf
                })
        
        return {
            "success": True,
            "error": None,
            "text": text.strip(),
            "blocks": blocks
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
    lang: str = 'chi_sim+eng'
) -> dict[str, Any]:
    """
    在屏幕上查找指定文字的位置
    
    Args:
        search_text: 要查找的文字
        region: (left, top, width, height) 或 None（全屏）
        lang: OCR 语言
    
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
    
    result = ocr_screen(region, lang)
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
    deps_ok, _ = _check_dependencies()
    tess_ok, _ = _check_tesseract()
    
    if not deps_ok:
        return "【屏幕 OCR】未配置（需安装 pyautogui/pytesseract/Pillow）。"
    
    if not tess_ok:
        return "【屏幕 OCR】pyautogui 已安装，但 Tesseract OCR 未检测到。请安装 Tesseract 并添加到 PATH（Windows: https://github.com/UB-Mannheim/tesseract/wiki）。"
    
    return "【屏幕 OCR】已配置：screen_ocr 识别屏幕文字，screen_find_text 查找文字位置，screen_click_text 点击文字。支持中英文混合识别。"
