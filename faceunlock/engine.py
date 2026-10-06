"""人脸识别引擎：YuNet 检测 + SFace 128 维特征 + 余弦比对。

实现要点（均由本机实测确定，勿随意改动）：
  * Ubuntu 24.04 自带的 OpenCV 4.6 **无法运行** YuNet 2023mar(v2) 模型
    （报 `Layer with requested id=-1 not found`），因此随包分发 opencv 4.11。
  * `FaceRecognizerSF.feature()` 返回**未归一化**向量（实测 L2 范数 ~10.3），
    所以绝不能用裸点积当余弦；一律走 `match(FR_COSINE)`（内部归一化）。
  * 检测在降采样帧上做（~7ms），对齐/特征在全分辨率帧上做（质量更好）。
  * `alignCrop` 必须传入**原始 float 行**（Nx15），转成 int 会失败。
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field

import cv2 as cv
import numpy as np

from . import DETECTOR_MODEL, RECOGNIZER_MODEL, models_dir

#: 检测分辨率上限（宽度）。降采样后检测约 7ms，全分辨率约 25ms。
DETECT_WIDTH = 640


@dataclass
class Face:
    """一张检测到的人脸。row 为 YuNet 原始 15 元素输出。"""

    row: np.ndarray = field(repr=False)
    width: float = 0.0
    height: float = 0.0
    score: float = 0.0

    @property
    def box(self) -> tuple[int, int, int, int]:
        x, y, w, h = (float(v) for v in self.row[:4])
        return int(x), int(y), int(w), int(h)

    @property
    def area(self) -> float:
        return self.width * self.height


def _scale_row(row: np.ndarray, sx: float, sy: float) -> np.ndarray:
    """把检测坐标从降采样帧换算回全分辨率帧。"""
    out = row.copy().astype(np.float32)
    out[0] *= sx
    out[1] *= sy
    out[2] *= sx
    out[3] *= sy
    pts = out[4:14].reshape(-1, 2)
    pts[:, 0] *= sx
    pts[:, 1] *= sy
    return out


class FaceEngine:
    """线程安全的识别引擎。模型加载约 90ms，进程内复用。"""

    def __init__(self, model_dir: str | None = None) -> None:
        d = model_dir or models_dir()
        det_path = os.path.join(d, DETECTOR_MODEL)
        rec_path = os.path.join(d, RECOGNIZER_MODEL)
        for p in (det_path, rec_path):
            if not os.path.isfile(p):
                raise FileNotFoundError(f"缺少模型文件: {p}")
        self._lock = threading.Lock()
        self._detector = cv.FaceDetectorYN.create(det_path, "", (DETECT_WIDTH, 360), 0.6, 0.3, 5000)
        self._recognizer = cv.FaceRecognizerSF.create(rec_path, "")

    # ---------- 检测 ----------

    def detect(self, frame: np.ndarray, score_threshold: float = 0.7) -> list[Face]:
        """在降采样帧上检测，返回已换算到全分辨率坐标的人脸列表。"""
        h, w = frame.shape[:2]
        if w > DETECT_WIDTH:
            scale = DETECT_WIDTH / float(w)
            small = cv.resize(frame, (DETECT_WIDTH, max(1, int(round(h * scale)))),
                              interpolation=cv.INTER_AREA)
        else:
            small = frame
        sx = w / float(small.shape[1])
        sy = h / float(small.shape[0])
        with self._lock:
            self._detector.setInputSize((small.shape[1], small.shape[0]))
            _, faces = self._detector.detect(small)
        out: list[Face] = []
        if faces is None:
            return out
        for row in faces:
            if float(row[14]) < score_threshold:
                continue
            full = _scale_row(row, sx, sy)
            out.append(Face(row=full, width=float(full[2]), height=float(full[3]),
                            score=float(full[14])))
        return out

    @staticmethod
    def largest(faces: list[Face]) -> Face | None:
        return max(faces, key=lambda f: f.area) if faces else None

    # ---------- 特征 ----------

    def embed(self, frame: np.ndarray, face: Face) -> np.ndarray:
        """对齐裁剪 + 提取 128 维特征。返回 (1,128) float32 未归一化向量。"""
        with self._lock:
            aligned = self._recognizer.alignCrop(frame, face.row.reshape(1, -1))
            feat = self._recognizer.feature(aligned)
        return np.asarray(feat, dtype=np.float32).reshape(1, 128)

    def embed_aligned(self, aligned: np.ndarray) -> np.ndarray:
        with self._lock:
            feat = self._recognizer.feature(aligned)
        return np.asarray(feat, dtype=np.float32).reshape(1, 128)

    def similarity(self, f1: np.ndarray, f2: np.ndarray) -> float:
        """归一化余弦相似度（-1..1）。"""
        with self._lock:
            return float(self._recognizer.match(
                np.asarray(f1, dtype=np.float32).reshape(1, 128),
                np.asarray(f2, dtype=np.float32).reshape(1, 128),
                cv.FaceRecognizerSF_FR_COSINE))

    def analyze(self, frame: np.ndarray, score_threshold: float = 0.7
                ) -> list[tuple[Face, np.ndarray]]:
        """一次调用拿到「人脸 + 特征」，取最大人脸时用它。"""
        res = []
        for f in self.detect(frame, score_threshold):
            res.append((f, self.embed(frame, f)))
        res.sort(key=lambda t: t[0].area, reverse=True)
        return res

    # ---------- 质量评估（录入时用） ----------

    @staticmethod
    def quality(frame: np.ndarray, face: Face) -> dict[str, float]:
        """返回录入质量指标：人脸高度、清晰度、亮度。用于拒绝模糊/过小/过暗样本。"""
        x, y, w, h = face.box
        fh, fw = frame.shape[:2]
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(fw, x + w), min(fh, y + h)
        crop = frame[y0:y1, x0:x1]
        if crop.size == 0:
            return {"height": 0.0, "sharpness": 0.0, "brightness": 0.0}
        gray = cv.cvtColor(crop, cv.COLOR_BGR2GRAY)
        return {
            "height": float(h),
            "sharpness": float(cv.Laplacian(gray, cv.CV_64F).var()),
            "brightness": float(gray.mean()),
        }

    # ---------- 攻击模拟（安全评估用） ----------

    def cosine_min_norm(self, f1: np.ndarray, f2: np.ndarray) -> float:
        """手工归一化后的余弦，用于与 match() 交叉校验。"""
        a = np.asarray(f1, dtype=np.float32).ravel()
        b = np.asarray(f2, dtype=np.float32).ravel()
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))
