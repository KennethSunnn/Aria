"""
screen_watch — 主动屏幕感知

后台线程定期截图，与上一帧做差异检测；
变化超过阈值时触发回调（可接入 ARIA 事件总线）。

用法：
    watcher = ScreenWatcher(on_change=my_callback, interval=2.0, diff_threshold=0.02)
    watcher.start()
    ...
    watcher.stop()

回调签名：
    def on_change(event: ScreenChangeEvent) -> None: ...
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class ScreenChangeEvent:
    timestamp: float
    diff_ratio: float          # 变化像素占比 0.0–1.0
    screenshot_path: str       # 保存到磁盘的截图路径（空字符串表示未保存）
    region: Optional[tuple[int, int, int, int]] = None
    extra: dict[str, Any] = field(default_factory=dict)


class ScreenWatcher:
    """
    后台屏幕变化监视器。

    参数：
        on_change:       变化事件回调，接收 ScreenChangeEvent
        interval:        截图间隔（秒），默认 2.0
        diff_threshold:  触发回调的最小变化比例（0–1），默认 0.02（2%）
        region:          监视区域 (left, top, width, height)，None 表示全屏
        save_dir:        截图保存目录，None 表示不保存
        max_saved:       最多保留的截图数量（FIFO），0 表示不限
    """

    def __init__(
        self,
        on_change: Callable[[ScreenChangeEvent], None],
        *,
        interval: float = 2.0,
        diff_threshold: float = 0.02,
        region: Optional[tuple[int, int, int, int]] = None,
        save_dir: Optional[str] = None,
        max_saved: int = 20,
    ) -> None:
        self.on_change = on_change
        self.interval = max(0.1, float(interval))
        self.diff_threshold = max(0.0, min(1.0, float(diff_threshold)))
        self.region = region
        self.save_dir = save_dir
        self.max_saved = max_saved

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._prev_frame: Any = None  # numpy array or None
        self._saved_paths: list[str] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 公开接口                                                              #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """启动后台监视线程。"""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ScreenWatcher")
        self._thread.start()
        logger.info(f"ScreenWatcher started (interval={self.interval}s, threshold={self.diff_threshold:.1%})")

    def stop(self, timeout: float = 5.0) -> None:
        """停止后台监视线程。"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        logger.info("ScreenWatcher stopped")

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def snapshot(self) -> dict[str, Any]:
        """立即截图并返回结果（不触发回调）。"""
        try:
            frame = self._capture()
            path = self._save_frame(frame, prefix="snapshot")
            return {"success": True, "screenshot_path": path}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------ #
    # 内部实现                                                              #
    # ------------------------------------------------------------------ #

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                frame = self._capture()
                diff = self._compute_diff(frame)
                if diff >= self.diff_threshold:
                    path = self._save_frame(frame, prefix="change")
                    event = ScreenChangeEvent(
                        timestamp=time.time(),
                        diff_ratio=diff,
                        screenshot_path=path,
                        region=self.region,
                    )
                    try:
                        self.on_change(event)
                    except Exception as cb_err:
                        logger.debug(f"ScreenWatcher callback error: {cb_err}")
                with self._lock:
                    self._prev_frame = frame
            except Exception as e:
                logger.debug(f"ScreenWatcher capture error: {e}")
            self._stop_event.wait(self.interval)

    def _capture(self) -> Any:
        """截图并返回 numpy 数组。"""
        try:
            from PIL import ImageGrab
            import numpy as np

            bbox = None
            if self.region is not None:
                l, t, w, h = [int(x) for x in self.region]
                bbox = (l, t, l + max(1, w), t + max(1, h))
            img = ImageGrab.grab(bbox=bbox, all_screens=True)
            return np.array(img.convert("RGB"))
        except ImportError as e:
            raise RuntimeError(f"missing_dependency: {e}") from e

    def _compute_diff(self, frame: Any) -> float:
        """计算当前帧与上一帧的差异比例（0–1）。"""
        with self._lock:
            prev = self._prev_frame
        if prev is None:
            return 0.0
        try:
            import numpy as np

            if prev.shape != frame.shape:
                return 1.0
            diff = np.abs(frame.astype(int) - prev.astype(int))
            changed_pixels = int(np.any(diff > 10, axis=-1).sum())
            total_pixels = frame.shape[0] * frame.shape[1]
            return changed_pixels / max(1, total_pixels)
        except Exception:
            return 0.0

    def _save_frame(self, frame: Any, prefix: str = "frame") -> str:
        """将帧保存到磁盘，返回路径（save_dir 为 None 时返回空字符串）。"""
        if not self.save_dir:
            return ""
        try:
            import numpy as np
            from PIL import Image

            os.makedirs(self.save_dir, exist_ok=True)
            ts = int(time.time() * 1000)
            path = os.path.join(self.save_dir, f"{prefix}_{ts}.png")
            Image.fromarray(frame.astype("uint8")).save(path)

            # FIFO 清理
            if self.max_saved > 0:
                self._saved_paths.append(path)
                while len(self._saved_paths) > self.max_saved:
                    old = self._saved_paths.pop(0)
                    try:
                        os.remove(old)
                    except OSError:
                        pass
            return path
        except Exception as e:
            logger.debug(f"ScreenWatcher save_frame error: {e}")
            return ""


# ------------------------------------------------------------------ #
# 全局单例（供 aria_manager 注册 screen_watch action 使用）             #
# ------------------------------------------------------------------ #

_global_watcher: Optional[ScreenWatcher] = None
_global_lock = threading.Lock()


def get_or_create_watcher(
    on_change: Callable[[ScreenChangeEvent], None],
    *,
    interval: float = 2.0,
    diff_threshold: float = 0.02,
    region: Optional[tuple[int, int, int, int]] = None,
    save_dir: Optional[str] = None,
) -> ScreenWatcher:
    global _global_watcher
    with _global_lock:
        if _global_watcher is None or not _global_watcher.is_running():
            _global_watcher = ScreenWatcher(
                on_change=on_change,
                interval=interval,
                diff_threshold=diff_threshold,
                region=region,
                save_dir=save_dir,
            )
            _global_watcher.start()
        return _global_watcher


def stop_global_watcher() -> None:
    global _global_watcher
    with _global_lock:
        if _global_watcher:
            _global_watcher.stop()
            _global_watcher = None
