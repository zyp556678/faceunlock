"""认证主逻辑：被 PAM 助手（root）调用，也可被 CLI 的 test 子命令复用。

失败一律"向密码回退"，绝不锁死用户：
  未启用 / 服务未开 / 未录入 / 摄像头被占用 / 远程会话 / 冷却中 / 模板被篡改
  / 任何未预期异常  ->  EXIT_NOT_APPLICABLE (PAM_IGNORE)
  明确识别为"不是本人"  ->  EXIT_NO_MATCH (PAM_AUTH_ERR，PAM 栈继续走密码)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from . import EXIT_MATCH, EXIT_NO_MATCH, EXIT_NOT_APPLICABLE
from . import config as config_mod
from .camera import Camera, CameraBusy, CameraError
from .engine import FaceEngine
from .store import StoreTampered, TemplateStore


@dataclass
class AuthResult:
    code: int
    reason: str
    service: str = ""
    user: str = ""
    score: float = 0.0
    hits: int = 0
    frames: int = 0
    elapsed_ms: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.code == EXIT_MATCH


class RateLimiter:
    """失败限速：窗口内失败次数超限 -> 冷却期内直接回退密码。

    存在 /var/lib/faceunlock/state.json（root 0600），普通用户无法清零。
    """

    def __init__(self, store: TemplateStore, cfg: dict[str, Any]) -> None:
        self.store = store
        rl = cfg.get("rate_limit", {})
        self.max_failures = int(rl.get("max_failures", 5))
        self.window_s = int(rl.get("window_s", 300))
        self.cooldown_s = int(rl.get("cooldown_s", 120))

    def _state(self) -> dict[str, Any]:
        st = self.store.state()
        st.setdefault("failures", {})
        st.setdefault("cooldown", {})
        return st

    def blocked_for(self, user: str) -> int:
        """返回剩余冷却秒数，0 表示未被限速。"""
        now = time.time()
        st = self._state()
        until = float(st["cooldown"].get(user, 0))
        return max(0, int(until - now))

    def record_failure(self, user: str) -> None:
        now = time.time()
        st = self._state()
        hist = [t for t in st["failures"].get(user, []) if now - t < self.window_s]
        hist.append(now)
        st["failures"][user] = hist
        if len(hist) >= self.max_failures:
            st["cooldown"][user] = now + self.cooldown_s
            st["failures"][user] = []
        self.store.save_state(st)

    def clear(self, user: str) -> None:
        st = self._state()
        st["failures"].pop(user, None)
        st["cooldown"].pop(user, None)
        self.store.save_state(st)


def _log(msg: str, debug: bool = False) -> None:
    """写 syslog；syslog 不可用**且 stderr 是终端**时才退回 stderr。

    为什么加 isatty 限制：在 polkit 的 PAM 栈里（Ubuntu 26.04 起
    polkit-agent-helper@.service 是 socket 激活的服务）stderr 既写不通、
    又是 polkit 与 gnome-shell 之间的协议流的一部分。往里写会让授权框
    每秒重建、用户连密码都输不进去（详见 pam/pam_faceunlock.c 的注释）。
    sudo / su 的终端里 isatty 为真，反馈照旧。
    """
    try:
        import syslog
        syslog.openlog("faceunlock")
        syslog.syslog(syslog.LOG_NOTICE, msg)
        syslog.closelog()
        return
    except Exception:
        pass
    try:
        import sys
        if sys.stderr is not None and sys.stderr.isatty():
            print(f"faceunlock: {msg}", file=sys.stderr)
    except Exception:
        pass


def authenticate(user: str, service: str, *, cfg: dict[str, Any] | None = None,
                 store: TemplateStore | None = None,
                 engine: FaceEngine | None = None,
                 rhost: str = "", tty: str = "",
                 camera_factory: Callable[..., Camera] = Camera,
                 announce: Callable[[str], None] | None = None) -> AuthResult:
    """执行一次人脸认证。返回 AuthResult（code 即 PAM 助手退出码）。"""
    t0 = time.monotonic()
    cfg = cfg or config_mod.load()
    store = store or TemplateStore()

    def done(code: int, reason: str, **kw) -> AuthResult:
        r = AuthResult(code=code, reason=reason, service=service, user=user,
                       elapsed_ms=(time.monotonic() - t0) * 1000.0, **kw)
        if cfg.get("debug") or code != EXIT_MATCH:
            _log(f"user={user} service={service} code={code} reason={reason} "
                 f"score={r.score:.3f} frames={r.frames} {r.elapsed_ms:.0f}ms")
        return r

    # --- 前置判定：任何一条不满足都直接回退密码 ---
    if rhost:
        return done(EXIT_NOT_APPLICABLE, "远程会话不使用人脸")
    if not config_mod.service_enabled(cfg, service):
        return done(EXIT_NOT_APPLICABLE, f"服务 {service} 未启用人脸")

    limiter = RateLimiter(store, cfg)
    left = limiter.blocked_for(user)
    if left > 0:
        return done(EXIT_NOT_APPLICABLE, f"失败次数过多，冷却中（剩余 {left}s）")

    try:
        known = store.embeddings(user)
    except StoreTampered as e:
        return done(EXIT_NOT_APPLICABLE, f"模板校验失败，拒绝使用: {e}")
    if known.shape[0] == 0:
        return done(EXIT_NOT_APPLICABLE, "该用户未录入人脸")

    # 廉价预检：还没有摄像头设备节点就**不要**加载 ONNX 模型、不要尝试打开设备。
    # polkit 127 的沙箱（PrivateDevices=yes）里 /dev/video* 不存在，这里是必经之路；
    # 少了这一步，每次授权都要白等模型加载（本机实测 5~6.5 s CPU）再撞 8 秒兜底
    # 超时，用户在授权框前看不到密码提示。预检失败一律回退密码，语义不变。
    # 只在用真摄像头时预检：单元测试会注入假 camera_factory，不受影响。
    if camera_factory is Camera:
        from . import preflight

        reason = preflight.camera_unavailable_reason(
            int((cfg.get("camera") or {}).get("device", 0)))
        if reason:
            return done(EXIT_NOT_APPLICABLE, f"摄像头不可用: {reason}")

    if engine is None:
        try:
            engine = FaceEngine()
        except Exception as e:  # 模型缺失等
            return done(EXIT_NOT_APPLICABLE, f"识别引擎不可用: {e}")

    threshold = float(cfg.get("threshold", 0.50))
    required = max(1, int(cfg.get("required_frames", 2)))
    window = max(required, int(cfg.get("window_frames", 3)))
    timeout_ms = int(cfg.get("timeout_ms", 4000))
    no_face_ms = int(cfg.get("no_face_timeout_ms", 1000))
    cam_cfg = cfg.get("camera", {})

    if announce:
        try:
            announce(f"正在识别人脸（{user}）…")
        except Exception:
            pass

    deadline = t0 + timeout_ms / 1000.0
    no_face_deadline = t0 + no_face_ms / 1000.0
    recent: list[bool] = []
    frames = 0
    best = 0.0
    saw_face = False

    try:
        cam = camera_factory(device=int(cam_cfg.get("device", 0)),
                             width=int(cam_cfg.get("width", 1280)),
                             height=int(cam_cfg.get("height", 720)),
                             warmup=int(cam_cfg.get("warmup_frames", 4)))
        cam.open()
    except CameraBusy as e:
        return done(EXIT_NOT_APPLICABLE, f"摄像头不可用（可能被占用）: {e}")
    except Exception as e:
        return done(EXIT_NOT_APPLICABLE, f"摄像头打开失败: {e}")

    try:
        for frame in cam.frames_until(deadline):
            frames += 1
            faces = engine.detect(frame)
            face = engine.largest(faces)
            if face is None:
                recent.append(False)
                if not saw_face and time.monotonic() > no_face_deadline:
                    return done(EXIT_NOT_APPLICABLE, "镜头前未检测到人脸",
                                frames=frames)
            else:
                saw_face = True
                emb = engine.embed(frame, face)
                sims = [engine.similarity(emb, k) for k in known]
                s = max(sims) if sims else 0.0
                best = max(best, s)
                recent.append(s >= threshold)
            recent = recent[-window:]
            if sum(recent) >= required:
                limiter.clear(user)
                return done(EXIT_MATCH, "人脸匹配成功", score=best, hits=sum(recent),
                            frames=frames, detail={"threshold": threshold})
    except CameraError as e:
        return done(EXIT_NOT_APPLICABLE, f"采集失败: {e}", frames=frames)
    except Exception as e:  # 兜底：绝不让异常变成"拒绝"
        return done(EXIT_NOT_APPLICABLE, f"识别过程异常: {e}", frames=frames)
    finally:
        cam.close()

    limiter.record_failure(user)
    if not saw_face:
        return done(EXIT_NOT_APPLICABLE, "镜头前未检测到人脸", frames=frames)
    return done(EXIT_NO_MATCH, "人脸不匹配", score=best, frames=frames,
                detail={"threshold": threshold})


def verify_once(frame: Any, engine: FaceEngine, known: np.ndarray,
                threshold: float) -> tuple[float, bool]:
    """对单帧做一次比对（管理界面"识别测试"用）。"""
    face = engine.largest(engine.detect(frame))
    if face is None:
        return 0.0, False
    emb = engine.embed(frame, face)
    if known.shape[0] == 0:
        return 0.0, False
    s = max(engine.similarity(emb, k) for k in known)
    return float(s), s >= threshold
