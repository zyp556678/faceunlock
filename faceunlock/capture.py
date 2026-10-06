"""采集 / 识别测试会话（以**普通用户**身份运行，只碰摄像头与推理引擎）。

设计要点
--------
* 本模块不写模板库、不碰 /etc —— 只负责"从摄像头拿到合格的 128 维特征"，
  落库由 `faceunlock-admin`（root）完成，见 `gui_bridge.py`。
* `poll()` **永不抛异常**：摄像头被占用（`CameraBusy`）、读帧失败、推理异常
  一律转成 `state=error` + `error_code`（BUSY/INTERNAL），交给 HTTP 层翻译成
  规范里的错误体。界面因此永远不会因为摄像头问题崩掉。
* 无相机开发：`FACEUNLOCK_FAKE_CAMERA=1` 用纯色合成帧；`=busy` 假装设备被占用；
  `FACEUNLOCK_FAKE_IMAGE=<图片路径|smoke>` 用一张真实图片当帧源（可跑通完整
  检测→录入链路）；`FACEUNLOCK_FAKE_SAMPLE=1` 时用 `test_output/smoke_feats.npy`
  伪造样本（无任何人脸也能演练 录入→落库 的 API 流程）。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Sequence

import cv2 as cv
import numpy as np

from . import BASE_DIR
from .camera import Camera, CameraBusy, CameraError, encode_jpeg

# ---------- 录入质量门限 ----------
#
# 这几个数字是**本机实测标定**出来的（与 faceunlock/cli.py 保持一致），不要凭直觉改：
#   * 本机摄像头（Luxvisions 30c9:008c）出厂 ISP 参数下，人脸区域的 Laplacian 方差
#     （"清晰度"）实测只有 10~18；数字锐化拉满才到 42~100；人在移动时会掉到 5 以下。
#     曾把门限设成 60 —— 结果正常坐姿下**永远录不进去**（踩过的坑）。
#   * 人脸高度：SFace 需要 112x112 的对齐裁剪，低于 140px 上采样太糊；
#     高于 700px 说明离固定焦距镜头太近，反而失焦。
#   * 亮度：实测正常室内 100~160；< 45 太暗，> 215 过曝（逆光/正对灯）。
#   * 清晰度只保留一个**极低底线**，用来挡严重运动模糊，不再作为主要判据。
#   * 换用与摄像头无关的**自校准判据**：新样本必须与已采集样本足够像，
#     否则视为废帧（同人正常帧实测 0.90+，换姿态 0.5~0.8，糊/在动会掉到 0.4 以下）。
# 另外实测：调摄像头的 contrast/gamma/数字锐化 **不会**提升同人相似度
# （默认 0.958，调参后反而 0.85~0.94），所以本模块绝不碰 ISP 参数。
QUALITY_MIN_HEIGHT = 140.0
QUALITY_MAX_HEIGHT = 700.0
QUALITY_HARD_SHARPNESS_FLOOR = 5.0
BRIGHT_RANGE = (45.0, 215.0)
#: 自校准门限：新样本与"已采集样本"的最大余弦低于此值即判为废帧
QUALITY_SELF_CONSISTENCY = 0.40

#: 采集姿态提示，按顺序轮换以提升模板多样性
POSE_HINTS = ("正面", "稍微左转", "稍微右转", "稍微抬头", "稍微低头")

#: 会话状态（与 GUI_API.md §2.2 的 state 字段一致）
STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_ERROR = "error"

#: 错误码（与 GUI_API.md §2 的 code 取值一致）
CODE_BUSY = "BUSY"
CODE_NO_FACE = "NO_FACE"
CODE_NOT_ENROLLED = "NOT_ENROLLED"
CODE_INVALID = "INVALID"
CODE_INTERNAL = "INTERNAL"

#: 预览 JPEG 质量（每 100~200ms 一帧，太大没必要）
PREVIEW_QUALITY = 72

#: 两次成功采集之间的最小间隔（秒）——保证用户有时间按提示换姿态
SAMPLE_INTERVAL = float(os.environ.get("FACEUNLOCK_CAPTURE_INTERVAL", "0.6"))

_HINT_TOO_FAR = "请靠近一些"
_HINT_TOO_CLOSE = "请离远一点（太近会失焦）"
_HINT_BAD_LIGHT = "光线不佳"
_HINT_SHAKY = "画面在晃动"
_HINT_UNSTABLE = "这一帧不够稳定，请坐正"
_HINT_NO_FACE = "没有检测到人脸"


def fake_mode() -> str:
    """返回 'off' | 'frames' | 'image' | 'busy'。"""
    raw = (os.environ.get("FACEUNLOCK_FAKE_CAMERA") or "").strip().lower()
    if raw in ("busy", "occupied"):
        return "busy"
    if raw in ("1", "true", "yes", "on", "synthetic", "frames"):
        return "image" if _fake_image_path() else "frames"
    if raw in ("image", "photo", "smoke"):
        return "image"
    return "off"


def _fake_image_path() -> str | None:
    """解析 FACEUNLOCK_FAKE_IMAGE：可以是具体图片路径，也可以是 'smoke' 简写。

    'smoke' 会在几个可能的位置找 `test_output/smoke_annotated.jpg`
    （安装态的 /usr/lib/faceunlock 下通常没有 test_output，因此也会看 cwd）。
    """
    raw = (os.environ.get("FACEUNLOCK_FAKE_IMAGE") or "").strip()
    if not raw:
        return None
    if raw.lower() in ("smoke", "1", "true", "yes", "on"):
        for c in (
            os.path.join(BASE_DIR, "test_output", "smoke_annotated.jpg"),
            os.path.join(os.getcwd(), "test_output", "smoke_annotated.jpg"),
            os.path.join(BASE_DIR, "..", "test_output", "smoke_annotated.jpg"),
        ):
            if os.path.isfile(c):
                return os.path.abspath(c)
        return None
    return raw if os.path.isfile(raw) else None


def _synthetic_sample_enabled() -> bool:
    return (os.environ.get("FACEUNLOCK_FAKE_SAMPLE") or "").strip().lower() in (
        "1", "true", "yes", "on")


def fake_embeddings() -> np.ndarray:
    """`test_output/smoke_feats.npy` 里的真实特征（(N,128) float32），缺失时返回空。"""
    p = os.path.join(BASE_DIR, "test_output", "smoke_feats.npy")
    try:
        arr = np.load(p).astype(np.float32).reshape(-1, 128)
        return arr
    except Exception:
        return np.zeros((0, 128), dtype=np.float32)


class FakeCamera:
    """无相机开发用的假摄像头，接口与 `camera.Camera` 一致（open/read/close）。

    * 默认产出纯色合成帧（检测不出人脸，属预期）；
    * `FACEUNLOCK_FAKE_IMAGE` 指向真实图片时产出该图片，可跑通真实检测链路。
    """

    def __init__(self, device: int = 0, width: int = 1280, height: int = 720,
                 warmup: int = 0) -> None:
        self.device = device
        self.width = width
        self.height = height
        img_path = _fake_image_path()
        self._image: np.ndarray | None = None
        if img_path:
            img = cv.imread(img_path)
            if img is not None:
                self._image = img
        self._t = 0
        self._opened = False

    def open(self) -> None:
        self._opened = True

    def _synthetic(self) -> np.ndarray:
        """纯色合成帧：深蓝底 + 缓慢移动的亮块（保证 JPEG 编码/时间戳每帧都变）。"""
        self._t += 1
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        frame[:, :] = (48, 32, 24)  # BGR，深色
        phase = self._t % 120
        x = int((self.width - 240) * phase / 120.0)
        cv.rectangle(frame, (x, self.height // 3), (x + 200, self.height // 3 + 200),
                     (90, 90, 90), -1)
        cv.putText(frame, f"FAKE CAMERA {self._t}", (24, 48),
                   cv.FONT_HERSHEY_SIMPLEX, 1.0, (200, 200, 200), 2)
        return frame

    def read(self) -> np.ndarray | None:
        if not self._opened:
            raise CameraError("摄像头未打开")
        if self._image is not None:
            return self._image.copy()
        return self._synthetic()

    def frames_until(self, deadline: float, max_frames: int = 120,
                     max_consecutive_failures: int = 10):
        n = 0
        while time.monotonic() < deadline and n < max_frames:
            f = self.read()
            n += 1
            if f is None:
                continue
            yield f

    def close(self) -> None:
        self._opened = False

    def __enter__(self) -> "FakeCamera":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def quality_hint(q: dict[str, float]) -> tuple[bool, str, str]:
    """按质量指标给出 (是否合格, 中文 hint, 详细 message)。

    判定顺序与 faceunlock/cli.py 一致：高度下限 -> 高度上限 -> 亮度 -> 清晰度底线。
    （"与已采集样本像不像"的自校准判据需要特征，放在 `_enroll_face` 里做。）
    """
    height = float(q.get("height", 0.0))
    sharp = float(q.get("sharpness", 0.0))
    bright = float(q.get("brightness", 0.0))
    if height < QUALITY_MIN_HEIGHT:
        return False, _HINT_TOO_FAR, f"请靠近一些（当前人脸高度 {height:.0f}px）"
    if height > QUALITY_MAX_HEIGHT:
        return False, _HINT_TOO_CLOSE, f"请离远一点（人脸 {height:.0f}px，太近会失焦）"
    if not BRIGHT_RANGE[0] <= bright <= BRIGHT_RANGE[1]:
        detail = "光线太暗，请增加照明" if bright < BRIGHT_RANGE[0] else "光线太亮，请避开强光"
        return False, _HINT_BAD_LIGHT, f"{detail}（亮度 {bright:.0f}）"
    if sharp < QUALITY_HARD_SHARPNESS_FLOOR:
        return False, _HINT_SHAKY, f"画面在晃动或严重失焦（清晰度 {sharp:.0f}）"
    return True, "", ""


def _thumb_jpeg(frame: np.ndarray, box: Sequence[int], max_side: int = 240) -> bytes | None:
    """裁出人脸框（带 25% 边距）并缩到 max_side，用作模板缩略图。"""
    x, y, w, h = (int(v) for v in box[:4])
    if w <= 0 or h <= 0:
        return None
    mx, my = int(w * 0.25), int(h * 0.25)
    fh, fw = frame.shape[:2]
    x0, y0 = max(0, x - mx), max(0, y - my)
    x1, y1 = min(fw, x + w + mx), min(fh, y + h + my)
    crop = frame[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    ch, cw = crop.shape[:2]
    scale = max_side / float(max(ch, cw))
    if scale < 1.0:
        crop = cv.resize(crop, (max(1, int(cw * scale)), max(1, int(ch * scale))),
                         interpolation=cv.INTER_AREA)
    try:
        return encode_jpeg(crop, 82)
    except CameraError:
        return None


class CaptureSession:
    """一次录入或识别测试会话。**所有 public 方法都不抛异常**。"""

    def __init__(self, engine: Any, user: str, cfg: dict[str, Any] | None = None, *,
                 label: str = "", required: int = 5, threshold: float = 0.5) -> None:
        self.engine = engine
        self.user = user
        self.cfg = cfg or {}
        self.label = label
        self.required = max(1, int(required))
        self.threshold = float(threshold)
        self.state = STATE_IDLE
        self.error: str | None = None
        self.error_code: str | None = None
        self.message = ""
        self.hint = ""
        self.samples: list[dict[str, Any]] = []
        self.score: float | None = None
        self.passed = False
        self.frames = 0
        self._cam: Any = None
        self._mode: str | None = None
        self._known = np.zeros((0, 128), dtype=np.float32)
        self._last_capture = 0.0
        self._read_failures = 0
        self._synthetic_idx = 0
        self._lock = threading.RLock()

    # ---------- 生命周期 ----------

    @property
    def captured(self) -> int:
        return len(self.samples)

    def _camera_conf(self) -> dict[str, Any]:
        cam = self.cfg.get("camera") or {}
        return {
            "device": int(cam.get("device", 0)),
            "width": int(cam.get("width", 1280)),
            "height": int(cam.get("height", 720)),
            "warmup": int(cam.get("warmup_frames", 4)),
        }

    def _open_camera(self) -> bool:
        conf = self._camera_conf()
        mode = fake_mode()
        try:
            if mode == "busy":
                raise CameraBusy("假摄像头：模拟设备被其它程序占用")
            if mode in ("frames", "image"):
                cam = FakeCamera(**conf)
            else:
                cam = Camera(**conf)
            cam.open()
        except CameraBusy as e:
            self._fail(CODE_BUSY, f"摄像头被占用：{e}")
            return False
        except CameraError as e:
            self._fail(CODE_BUSY, f"摄像头不可用：{e}")
            return False
        except Exception as e:  # 驱动/权限等意外一律降级为 BUSY，不冒泡
            self._fail(CODE_BUSY, f"无法打开摄像头：{e}")
            return False
        self._cam = cam
        return True

    def _fail(self, code: str, message: str) -> None:
        self.state = STATE_ERROR
        self.error_code = code
        self.error = message
        self.message = message
        self.hint = ""

    def start(self, label: str | None = None, required: int | None = None) -> bool:
        """开始录入会话。返回是否成功启动。"""
        with self._lock:
            if label is not None:
                self.label = label
            if required is not None:
                self.required = max(1, int(required))
            self.samples = []
            self.score = None
            self.passed = False
            self.error = self.error_code = None
            self._read_failures = 0
            self._last_capture = 0.0
            self._mode = "enroll"
            self.message = "请正对摄像头"
            self.hint = POSE_HINTS[0]
            if not self._open_camera():
                return False
            self.state = STATE_RUNNING
            return True

    def verify_start(self, user: str, known_embeddings: Any) -> bool:
        """开始识别测试会话。`known_embeddings` 为 (N,128) 或 list。"""
        with self._lock:
            self.user = user
            self._mode = "verify"
            self.samples = []
            self.score = None
            self.passed = False
            self.error = self.error_code = None
            self._read_failures = 0
            arr = np.asarray(known_embeddings, dtype=np.float32)
            if arr.size == 0:
                self._known = np.zeros((0, 128), dtype=np.float32)
            else:
                self._known = arr.reshape(-1, 128)
            if not self._open_camera():
                return False
            if self._known.shape[0] == 0:
                self._fail(CODE_NOT_ENROLLED, f"{user} 还没有录入任何人脸，请先在「人脸管理」中添加")
                return False
            self.state = STATE_RUNNING
            self.message = "请正对摄像头"
            self.hint = POSE_HINTS[0]
            return True

    def close(self) -> None:
        with self._lock:
            if self._cam is not None:
                try:
                    self._cam.close()
                except Exception:
                    pass
                self._cam = None
            if self.state == STATE_RUNNING:
                self.state = STATE_IDLE

    # ---------- 轮询 ----------

    def poll(self) -> tuple[bytes | None, list[int] | None, str, str, int, int, float | None]:
        """返回 (frame_jpeg, box, message, hint, captured, required, score)。

        对应 GUI_API.md：录入用 box/message/hint/captured，识别测试用 score。
        任何异常都会被吞掉并转成 error 状态（`error_code` 供 HTTP 层使用）。
        """
        with self._lock:
            if self.state in (STATE_IDLE,):
                return None, None, self.message, self.hint, self.captured, self.required, self.score
            if self.state == STATE_ERROR:
                return None, None, self.error or "摄像头不可用", "", self.captured, self.required, None
            if self.state == STATE_DONE:
                return (None, None, self.message or "采集完成", self.hint,
                        self.captured, self.required, self.score)
            try:
                return self._poll_running()
            except Exception as e:  # 推理/编码异常也不能崩
                self._fail(CODE_INTERNAL, f"采集内部错误：{e}")
                return None, None, self.error or "内部错误", "", self.captured, self.required, None

    def _poll_running(self) -> tuple[bytes | None, list[int] | None, str, str, int, int, float | None]:
        frame = self._read_frame()
        if frame is None:
            return None, None, self.message, self.hint, self.captured, self.required, self.score

        try:
            jpeg = encode_jpeg(frame, PREVIEW_QUALITY)
        except CameraError:
            jpeg = None

        faces = self.engine.detect(frame)
        face = self.engine.largest(faces)
        if face is None:
            self.message = "没有检测到人脸，请坐到摄像头前"
            self.hint = _HINT_NO_FACE
            if self._mode == "enroll" and _synthetic_sample_enabled():
                self._maybe_synthetic_sample(frame)
            return jpeg, None, self.message, self.hint, self.captured, self.required, self.score

        box = list(face.box)
        if self._mode == "verify":
            self._score_face(frame, face)
            return jpeg, box, self.message, self.hint, self.captured, self.required, self.score

        self._enroll_face(frame, face, box)
        return jpeg, box, self.message, self.hint, self.captured, self.required, self.score

    def _read_frame(self) -> np.ndarray | None:
        if self._cam is None:
            self._fail(CODE_BUSY, "摄像头未打开")
            return None
        try:
            frame = self._cam.read()
        except Exception as e:
            self._fail(CODE_BUSY, f"读取摄像头失败：{e}")
            return None
        if frame is None:
            self._read_failures += 1
            if self._read_failures >= 10:
                self._fail(CODE_BUSY, "连续读帧失败，摄像头可能被其它程序占用")
            else:
                self.message = "正在等待摄像头画面…"
            return None
        self._read_failures = 0
        self.frames += 1
        return frame

    # ---------- 录入 ----------

    def _enroll_face(self, frame: np.ndarray, face: Any, box: list[int]) -> None:
        q = self.engine.quality(frame, face)
        ok, hint, detail = quality_hint(q)
        if not ok:
            self.message = detail
            self.hint = hint
            return
        idx = min(self.captured, len(POSE_HINTS) - 1)
        if self.captured >= self.required:
            self.state = STATE_DONE
            self.message = f"采集完成（{self.captured}/{self.required}）"
            self.hint = ""
            return
        now = time.monotonic()
        if now - self._last_capture < SAMPLE_INTERVAL:
            self.message = "很好，请保持"
            self.hint = POSE_HINTS[idx]
            return
        try:
            emb = self.engine.embed(frame, face)
        except Exception as e:
            self._fail(CODE_INTERNAL, f"特征提取失败：{e}")
            return
        # 自校准：靠"与已采集样本像不像"判断这一帧能不能用（与摄像头无关）
        if self.samples:
            try:
                sim = max(self.engine.similarity(
                    emb, np.asarray(s["embedding"], dtype=np.float32)) for s in self.samples)
            except Exception:
                sim = 1.0
            if sim < QUALITY_SELF_CONSISTENCY:
                self.message = (f"这一帧不够稳定（与已采集样本相似度仅 {sim:.2f}），"
                                f"请坐正、保持不动")
                self.hint = _HINT_UNSTABLE
                return
        self.samples.append({
            "embedding": [float(v) for v in np.asarray(emb, dtype=np.float32).ravel()],
            "quality": {k: float(v) for k, v in q.items()},
            "thumb": _thumb_jpeg(frame, box),
            "label": self.label or POSE_HINTS[idx],
        })
        self._last_capture = now
        nxt = min(self.captured, len(POSE_HINTS) - 1)
        if self.captured >= self.required:
            self.state = STATE_DONE
            self.message = f"采集完成（{self.captured}/{self.required}）"
            self.hint = ""
        else:
            self.message = f"已采集 {self.captured}/{self.required}"
            self.hint = POSE_HINTS[nxt]

    def _maybe_synthetic_sample(self, frame: np.ndarray) -> None:
        """纯色假帧下用 smoke_feats.npy 伪造样本，用于无相机/无人脸时演练落库 API。"""
        if self.captured >= self.required:
            self.state = STATE_DONE
            self.message = f"采集完成（{self.captured}/{self.required}）"
            self.hint = ""
            return
        now = time.monotonic()
        if now - self._last_capture < SAMPLE_INTERVAL:
            self.message = "（模拟）很好，请保持"
            self.hint = POSE_HINTS[min(self.captured, len(POSE_HINTS) - 1)]
            return
        feats = fake_embeddings()
        if feats.shape[0] == 0:
            self.message = "没有检测到人脸，请坐到摄像头前"
            self.hint = _HINT_NO_FACE
            return
        emb = feats[self._synthetic_idx % feats.shape[0]]
        self._synthetic_idx += 1
        h, w = frame.shape[:2]
        idx = min(self.captured, len(POSE_HINTS) - 1)
        box = [w // 3, h // 4, w // 3, h // 2]
        self.samples.append({
            "embedding": [float(v) for v in emb.ravel()],
            "quality": {"height": 265.0, "sharpness": 180.2, "brightness": 120.5,
                        "synthetic": True},
            "thumb": _thumb_jpeg(frame, box),
            "label": self.label or POSE_HINTS[idx],
        })
        self._last_capture = now
        if self.captured >= self.required:
            self.state = STATE_DONE
            self.message = f"采集完成（{self.captured}/{self.required}，模拟样本）"
            self.hint = ""
        else:
            self.message = f"已采集 {self.captured}/{self.required}（模拟样本）"
            self.hint = POSE_HINTS[min(self.captured, len(POSE_HINTS) - 1)]

    # ---------- 识别测试 ----------

    def _score_face(self, frame: np.ndarray, face: Any) -> None:
        try:
            emb = self.engine.embed(frame, face)
            sims = [self.engine.similarity(emb, k) for k in self._known]
        except Exception as e:
            self._fail(CODE_INTERNAL, f"特征比对失败：{e}")
            return
        self.score = float(max(sims)) if sims else None
        if self.score is None:
            self._fail(CODE_NOT_ENROLLED, "没有可用的模板，请先录入人脸")
            return
        self.passed = self.score >= self.threshold
        cmp_ = "≥" if self.passed else "<"
        self.message = f"相似度 {self.score:.3f} {cmp_} 阈值 {self.threshold:.2f}"
        self.hint = "通过" if self.passed else "不通过"

    # ---------- 供 bridge 取用 ----------

    def take_samples(self) -> list[dict[str, Any]]:
        return list(self.samples)

    def snapshot(self) -> dict[str, Any]:
        return {
            "mode": self._mode,
            "state": self.state,
            "user": self.user,
            "label": self.label,
            "captured": self.captured,
            "required": self.required,
            "message": self.message,
            "hint": self.hint,
            "score": self.score,
            "passed": self.passed,
            "error": self.error,
            "error_code": self.error_code,
        }
