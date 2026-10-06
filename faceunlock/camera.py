"""摄像头采集：直接走 V4L2 后端（不经 ffmpeg）。

为什么不用 cv.VideoCapture(0) 的默认后端：随包分发的 opencv 是 headless wheel，
默认会尝试 ffmpeg/gstreamer；显式指定 CAP_V4L2 行为最稳定且零额外依赖。
"""
from __future__ import annotations

import time
from typing import Iterator

import cv2 as cv
import numpy as np


class CameraError(Exception):
    pass


class CameraBusy(CameraError):
    """设备被其它程序占用（视频会议/浏览器等）——调用方应视为"不适用"并回退密码。"""


class Camera:
    def __init__(self, device: int = 0, width: int = 1280, height: int = 720,
                 warmup: int = 4) -> None:
        self.device = device
        self.width = width
        self.height = height
        self.warmup = warmup
        self._cap: cv.VideoCapture | None = None

    def open(self) -> None:
        cap = cv.VideoCapture(self.device, cv.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            raise CameraBusy(f"无法打开 /dev/video{self.device}（被占用或无权限）")
        cap.set(cv.CAP_PROP_FOURCC, cv.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv.CAP_PROP_BUFFERSIZE, 1)  # 只要最新帧，降低延迟
        self._cap = cap
        # 预热：UVC 前几帧常为曝光未收敛的废帧
        for _ in range(max(0, self.warmup)):
            if not cap.read()[0]:
                break

    def read(self) -> np.ndarray | None:
        if self._cap is None:
            raise CameraError("摄像头未打开")
        ok, frame = self._cap.read()
        return frame if ok and frame is not None else None

    def frames_until(self, deadline: float, max_frames: int = 120,
                     max_consecutive_failures: int = 10) -> Iterator[np.ndarray]:
        """在 deadline（time.monotonic）之前持续产出帧。

        连续读失败超过阈值即抛 CameraError：这通常意味着设备虽能 open，但
        已被别的进程独占（UVC 只允许一个 STREAMON）。此时必须**立刻**放弃并
        回退密码，而不是干等到认证总超时——那会让 sudo 卡好几秒。
        """
        n = 0
        failures = 0
        while time.monotonic() < deadline and n < max_frames:
            f = self.read()
            n += 1
            if f is None:
                failures += 1
                if failures >= max_consecutive_failures:
                    raise CameraError(
                        f"连续 {failures} 次读帧失败，设备可能被其它程序占用")
                time.sleep(0.02)
                continue
            failures = 0
            yield f

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def __enter__(self) -> "Camera":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def encode_jpeg(frame: np.ndarray, quality: int = 80) -> bytes:
    ok, buf = cv.imencode(".jpg", frame, [int(cv.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise CameraError("JPEG 编码失败")
    return buf.tobytes()
