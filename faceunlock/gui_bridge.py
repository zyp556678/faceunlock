"""faceunlock GUI 桥接服务（以**普通用户**身份运行的本地 HTTP 服务）。

严格实现 `docs/GUI_API.md`（冻结契约 v1）：

    Tauri 外壳 --stdout 握手--> 本服务 --JSON-Lines--> faceunlock-admin (root)

要点
----
* 只用标准库（`http.server`/`socketserver`/`subprocess`），不引入新依赖。
* 默认绑定 `127.0.0.1:0`（随机端口），启动后在 **stdout** 打印一行握手 JSON，
  其余所有日志一律走 stderr —— 因为 Tauri 只读 stdout 的第一行。
* 除 `GET /health` 外所有请求都要 `X-FaceUnlock-Token`；`Host` 头只允许
  `127.0.0.1`/`localhost`（防 DNS rebinding）。校验失败一律 401。
* 特权操作转发给**常驻的** root 助手子进程（JSON-Lines），懒启动、死掉自动重启一次；
  polkit 被取消时返回 `code=AUTH_REQUIRED`，界面不会崩。
* 所有 camera/session 操作加锁；任何异常/超时都转成规范里的 error body，
  HTTP 状态码除 401 外一律 200。
* Tauri webview 的页面源是 `http://tauri.localhost`，属于跨源请求，因此带 CORS 头；
  `OPTIONS` 预检不带 token 也能通过（只回 CORS 头，不泄露任何数据）。
"""
from __future__ import annotations

import argparse
import base64
import grp
import hmac
import json
import os
import pwd
import queue
import secrets
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__, config as config_mod

# ---------- 常量 ----------

DEFAULT_ADMIN_CMD = ["pkexec", "/usr/libexec/faceunlock-admin", "serve"]
DEFAULT_PORT = 0
DEFAULT_HOST = "127.0.0.1"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

#: 特权调用的默认超时。首次会弹 polkit 密码框，用户需要时间输入。
ADMIN_TIMEOUT = float(os.environ.get("FACEUNLOCK_ADMIN_TIMEOUT", "90"))
#: 非特权/常驻调用的超时
FAST_TIMEOUT = 20.0

SERVICE_KEYS = ("gdm-password", "sudo", "sudo-i", "su", "polkit-1", "login")
THRESHOLD_MIN, THRESHOLD_MAX = 0.20, 0.90
MAX_ENROLL_COUNT = 20

CODE_AUTH_REQUIRED = "AUTH_REQUIRED"
CODE_BUSY = "BUSY"
CODE_NO_FACE = "NO_FACE"
CODE_NOT_ENROLLED = "NOT_ENROLLED"
CODE_INVALID = "INVALID"
CODE_INTERNAL = "INTERNAL"
CODES = (CODE_AUTH_REQUIRED, CODE_BUSY, CODE_NO_FACE, CODE_NOT_ENROLLED,
         CODE_INVALID, CODE_INTERNAL)

CORS_HEADERS = [
    ("Access-Control-Allow-Origin", "*"),
    ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
    ("Access-Control-Allow-Headers", "X-FaceUnlock-Token, Content-Type"),
    ("Access-Control-Max-Age", "600"),
]


def log(msg: str) -> None:
    """日志一律走 stderr（stdout 只留给握手行）。"""
    print(f"[gui-bridge] {msg}", file=sys.stderr, flush=True)


# ---------- 错误 ----------


class BridgeError(Exception):
    def __init__(self, message: str, code: str = CODE_INTERNAL) -> None:
        super().__init__(message)
        self.message = message
        self.code = code if code in CODES else CODE_INTERNAL


# ---------- 当前用户 ----------


def _current_user() -> str:
    override = os.environ.get("FACEUNLOCK_GUI_USER", "").strip()
    if override:
        return override
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        return os.environ.get("USER") or "unknown"


def _in_sudo_group(user: str) -> bool:
    if os.geteuid() == 0:
        return True
    try:
        groups = os.getgrouplist(user, os.getgid())
    except Exception:
        return False
    for name in ("sudo", "wheel", "admin"):
        try:
            if grp.getgrnam(name).gr_gid in groups:
                return True
        except KeyError:
            continue
    return False


CURRENT_USER = _current_user()
CURRENT_UID = os.getuid()


# ---------- root 助手（JSON-Lines over stdio） ----------


def admin_cmd() -> list[str]:
    raw = (os.environ.get("FACEUNLOCK_ADMIN_CMD") or "").strip()
    if raw:
        return shlex.split(raw)
    return list(DEFAULT_ADMIN_CMD)


class AdminClient:
    """常驻 root 助手的客户端。懒启动、串行化、死掉自动重启一次。"""

    def __init__(self, cmd: list[str] | None = None) -> None:
        self.cmd = cmd or admin_cmd()
        self.hello: dict[str, Any] | None = None
        self._proc: subprocess.Popen | None = None
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.RLock()
        self._stderr_tail: deque[str] = deque(maxlen=40)

    # ---- 生命周期 ----

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _ensure_proc(self) -> None:
        if self.alive:
            return
        self._kill()
        try:
            proc = subprocess.Popen(
                self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
            )
        except FileNotFoundError as e:
            raise BridgeError(
                f"找不到特权助手程序：{' '.join(self.cmd)}（{e}）。"
                f"开发期可用 FACEUNLOCK_ADMIN_CMD 指定假助手。", CODE_AUTH_REQUIRED)
        except Exception as e:
            raise BridgeError(f"无法启动特权助手：{e}", CODE_AUTH_REQUIRED)
        self._proc = proc
        self._queue = queue.Queue()
        self._stderr_tail = deque(maxlen=40)
        threading.Thread(target=self._pump_stdout, args=(proc,), daemon=True).start()
        threading.Thread(target=self._pump_stderr, args=(proc,), daemon=True).start()
        log(f"已启动特权助手 pid={proc.pid}: {' '.join(self.cmd)}")
        # 握手：确认对方实现了协议（也是检测 polkit 被取消的最早时机）
        resp = self._roundtrip({"cmd": "hello"}, ADMIN_TIMEOUT)
        if not resp.get("ok"):
            raise BridgeError(str(resp.get("error") or "助手 hello 失败"),
                              str(resp.get("code") or CODE_AUTH_REQUIRED))
        self.hello = resp
        log(f"助手就绪: uid={resp.get('uid')} user={resp.get('user')} admin={resp.get('admin')}")

    def _pump_stdout(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                self._queue.put(line)
        except Exception:
            pass
        finally:
            self._queue.put(None)  # EOF 哨兵

    def _pump_stderr(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stderr:  # type: ignore[union-attr]
                line = line.rstrip("\n")
                self._stderr_tail.append(line)
                if line.strip():
                    log(f"[admin] {line}")
        except Exception:
            pass

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        self.hello = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    proc.stdin.write('{"cmd":"quit"}\n')  # type: ignore[union-attr]
                    proc.stdin.flush()  # type: ignore[union-attr]
                except Exception:
                    pass
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    proc.kill()
        except Exception:
            pass
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream:
                        stream.close()
                except Exception:
                    pass

    def close(self) -> None:
        with self._lock:
            self._kill()

    # ---- 请求 ----

    @staticmethod
    def _looks_like_auth(rc: int | None, tail: str) -> bool:
        if rc in (125, 126, 127):
            return True
        low = tail.lower()
        for pat in ("not authorized", "dismissed", "error executing command as another user",
                    "authentication", "no such file", "permission denied",
                    "未授权", "认证", "取消", "密码"):
            if pat in low:
                return True
        return False

    def _dead_error(self, msg: str) -> BridgeError:
        rc: int | None = None
        if self._proc is not None:
            try:
                rc = self._proc.wait(timeout=1.0)
            except Exception:
                rc = self._proc.poll()
        tail = "\n".join(self._stderr_tail).strip()
        text = msg + (f"（退出码 {rc}）" if rc is not None else "")
        if tail:
            text += "：" + tail[-300:]
        code = CODE_AUTH_REQUIRED if self._looks_like_auth(rc, tail) else CODE_INTERNAL
        return BridgeError(text, code)

    def _roundtrip(self, req: dict[str, Any], timeout: float) -> dict[str, Any]:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise self._dead_error("特权助手未运行")
        try:
            proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except Exception as e:
            raise self._dead_error(f"向特权助手写入请求失败: {e}")
        try:
            line = self._queue.get(timeout=timeout)
        except queue.Empty:
            raise BridgeError(
                f"特权助手响应超时（{timeout:.0f}s，命令 {req.get('cmd')}）", CODE_INTERNAL)
        if line is None:
            raise self._dead_error(f"特权助手进程已退出（命令 {req.get('cmd')}）")
        try:
            resp = json.loads(line)
        except json.JSONDecodeError:
            raise BridgeError(f"特权助手返回了非法 JSON: {line[:200]!r}", CODE_INTERNAL)
        if not isinstance(resp, dict):
            raise BridgeError("特权助手返回的不是 JSON 对象", CODE_INTERNAL)
        return resp

    def call(self, req: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        """发一条命令并返回响应；失败抛 BridgeError。进程死了自动重启**一次**。"""
        tmo = ADMIN_TIMEOUT if timeout is None else timeout
        with self._lock:
            attempt = 0
            while True:
                attempt += 1
                try:
                    self._ensure_proc()
                    resp = self._roundtrip(req, tmo)
                except BridgeError as e:
                    err = e
                    self._kill()
                else:
                    if not resp.get("ok"):
                        code = str(resp.get("code") or CODE_INTERNAL)
                        msg = resp.get("error") or resp.get("message") or "特权操作失败"
                        raise BridgeError(str(msg), code)
                    return resp
                # AUTH_REQUIRED 绝不重试：重试会再弹一次 polkit 密码框
                if err.code == CODE_AUTH_REQUIRED or attempt >= 2:
                    raise err


# ---------- 配置校验 ----------


def _as_bool(v: Any, name: str) -> bool:
    if isinstance(v, bool):
        return v
    raise BridgeError(f"{name} 必须是布尔值", CODE_INVALID)


def _as_int(v: Any, name: str, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise BridgeError(f"{name} 必须是整数", CODE_INVALID)
    iv = int(v)
    if iv < lo or iv > hi:
        raise BridgeError(f"{name} 必须在 {lo}~{hi} 之间", CODE_INVALID)
    return iv


def validate_config_patch(patch: Any) -> dict[str, Any]:
    """校验 /api/config 的局部更新。未知字段忽略（前向兼容），非法值报 INVALID。"""
    if not isinstance(patch, dict):
        raise BridgeError("请求体必须是 JSON 对象", CODE_INVALID)
    out: dict[str, Any] = {}
    for key, val in patch.items():
        if key == "enabled":
            out["enabled"] = _as_bool(val, "enabled")
        elif key == "save_thumbnails":
            out["save_thumbnails"] = _as_bool(val, "save_thumbnails")
        elif key == "debug":
            out["debug"] = _as_bool(val, "debug")
        elif key == "threshold":
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise BridgeError("阈值必须是数字", CODE_INVALID)
            fv = float(val)
            if fv < THRESHOLD_MIN or fv > THRESHOLD_MAX:
                raise BridgeError(
                    f"阈值必须在 {THRESHOLD_MIN:.2f} ~ {THRESHOLD_MAX:.2f} 之间", CODE_INVALID)
            out["threshold"] = round(fv, 4)
        elif key in ("required_frames", "window_frames"):
            out[key] = _as_int(val, key, 1, 10)
        elif key in ("timeout_ms", "no_face_timeout_ms"):
            out[key] = _as_int(val, key, 200, 60000)
        elif key == "services":
            if not isinstance(val, dict):
                raise BridgeError("services 必须是对象", CODE_INVALID)
            svc: dict[str, bool] = {}
            for k, v in val.items():
                if k not in SERVICE_KEYS:
                    raise BridgeError(f"未知的场景开关: {k}", CODE_INVALID)
                svc[k] = _as_bool(v, f"services.{k}")
            out["services"] = svc
        elif key == "camera":
            if not isinstance(val, dict):
                raise BridgeError("camera 必须是对象", CODE_INVALID)
            cam: dict[str, Any] = {}
            if "device" in val:
                cam["device"] = _as_int(val["device"], "camera.device", 0, 64)
            if "width" in val:
                cam["width"] = _as_int(val["width"], "camera.width", 160, 4096)
            if "height" in val:
                cam["height"] = _as_int(val["height"], "camera.height", 120, 4096)
            if "warmup_frames" in val:
                cam["warmup_frames"] = _as_int(val["warmup_frames"], "camera.warmup_frames", 0, 60)
            out["camera"] = cam
        elif key == "rate_limit":
            if not isinstance(val, dict):
                raise BridgeError("rate_limit 必须是对象", CODE_INVALID)
            out["rate_limit"] = val
        elif key == "version":
            continue
        else:
            log(f"忽略未知配置字段: {key}")
    if out.get("required_frames") and out.get("window_frames"):
        if out["required_frames"] > out["window_frames"]:
            raise BridgeError("required_frames 不能大于 window_frames", CODE_INVALID)
    return out


def _merge_config(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """深合并 patch 到 base（不修改入参）。"""
    out = json.loads(json.dumps(base))
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_config(out[k], v)
        else:
            out[k] = v
    return out


# ---------- 桥接核心 ----------


class Bridge:
    """所有业务逻辑；HTTP 层只做路由/鉴权/序列化。"""

    def __init__(self) -> None:
        self.token = ""
        self.lock = threading.RLock()            # 会话状态机
        self.camera_lock = threading.RLock()     # 摄像头设备（会话或预览二者其一）
        self.admin = AdminClient()
        self.engine: Any = None
        self.engine_error: str | None = None
        self.session: Any = None
        self.verify_state = "idle"
        self.config: dict[str, Any] = config_mod.load()
        self.config_privileged = False
        self.users: list[dict[str, Any]] = []
        self.pam: dict[str, Any] = self._local_pam()
        self.preview_stop = threading.Event()
        self.preview_active = False
        self.started = time.time()

    # ---- 基础 ----

    @staticmethod
    def _local_pam() -> dict[str, Any]:
        try:
            from . import pamctl
            return pamctl.status()
        except Exception as e:
            log(f"读取 PAM 状态失败: {e}")
            return {"profile_installed": False, "enabled": False,
                    "file": "/etc/pam.d/common-auth", "error": str(e)}

    def engine_obj(self) -> Any:
        if self.engine is None:
            try:
                from .engine import FaceEngine
                self.engine = FaceEngine()
                self.engine_error = None
            except Exception as e:
                self.engine_error = str(e)
                raise BridgeError(f"人脸模型加载失败：{e}", CODE_INTERNAL)
        return self.engine

    def can_manage_others(self) -> bool:
        if self.admin.alive and self.admin.hello:
            return bool(self.admin.hello.get("admin"))
        return os.geteuid() == 0 or _in_sudo_group(CURRENT_USER)

    def require_valid_user(self, user: Any) -> str:
        if user is None or user == "":
            return CURRENT_USER
        if not isinstance(user, str):
            raise BridgeError("user 必须是字符串", CODE_INVALID)
        from .store import valid_username
        if not valid_username(user):
            raise BridgeError(f"非法用户名: {user!r}", CODE_INVALID)
        return user

    def refresh_config_from_admin(self) -> None:
        resp = self.admin.call({"cmd": "get_config"}, FAST_TIMEOUT)
        cfg = resp.get("config")
        if isinstance(cfg, dict):
            self.config = _merge_config(config_mod.DEFAULTS, cfg)
            self.config_privileged = True

    def refresh_users(self) -> None:
        resp = self.admin.call({"cmd": "list_users"}, FAST_TIMEOUT)
        users = resp.get("users")
        if isinstance(users, list):
            self.users = [u for u in users if isinstance(u, dict)]

    # ---- 会话 ----

    def _release_camera(self) -> None:
        try:
            if self.camera_lock.acquire(blocking=False):
                self.camera_lock.release()
            else:
                log("摄像头锁仍被占用（预览未退出？）")
        except Exception:
            pass

    def _new_session(self, user: str, label: str = "", required: int = 5) -> Any:
        from .capture import CaptureSession
        engine = self.engine_obj()
        return CaptureSession(engine, user, self.config, label=label, required=required,
                              threshold=float(self.config.get("threshold", 0.5)))

    def start_enroll(self, user: str, label: str, count: int) -> dict[str, Any]:
        with self.lock:
            self._stop_preview()
            self._close_session()
            if not self.camera_lock.acquire(timeout=3.0):
                raise BridgeError(
                    "摄像头正被其它功能占用（预览或识别测试），请先停止后再试", CODE_BUSY)
            try:
                session = self._new_session(user, label, count)
                if not session.start(label, count):
                    raise BridgeError(session.error or "无法开始采集",
                                      session.error_code or CODE_INTERNAL)
            except BridgeError:
                self.camera_lock.release()
                raise
            except Exception as e:
                self.camera_lock.release()
                raise BridgeError(f"无法开始采集：{e}", CODE_INTERNAL)
            self.session = session
            return {"ok": True, "state": "running", "user": user, "label": label,
                    "required": session.required}

    def _close_session(self) -> None:
        session, self.session = self.session, None
        if session is None:
            return
        try:
            session.close()
        except Exception:
            pass
        try:
            self.camera_lock.release()
        except RuntimeError:
            pass

    def stop_verify(self) -> None:
        with self.lock:
            if self.session is not None and getattr(self.session, "_mode", None) == "verify":
                self._close_session()
            self.verify_state = "stopped"

    def _stop_preview(self) -> None:
        if self.preview_active:
            self.preview_stop.set()

    # ---- 各端点实现 ----

    def api_state(self) -> dict[str, Any]:
        if self.admin.alive:
            try:
                self.refresh_users()
                if not self.config_privileged:
                    self.refresh_config_from_admin()
            except BridgeError as e:
                log(f"/api/state 刷新特权信息失败: {e.message}")
        return {
            "ok": True,
            "version": __version__,
            "current_user": CURRENT_USER,
            "can_manage_others": self.can_manage_others(),
            "privileged": self.admin.alive,
            "config": self.config,
            "pam": self.pam,
            "users": self.users,
        }

    def api_faces(self, user: str) -> dict[str, Any]:
        resp = self.admin.call({"cmd": "list_faces", "user": user})
        faces = []
        for f in resp.get("faces") or []:
            if not isinstance(f, dict):
                continue
            faces.append({
                "id": f.get("id"),
                "label": f.get("label"),
                "created": f.get("created"),
                "quality": f.get("quality") or {},
                "thumb": f.get("thumb_b64") or f.get("thumb"),
            })
        self._note_user(user, len(faces))
        return {"ok": True, "user": user, "faces": faces}

    def _note_user(self, user: str, face_count: int) -> None:
        for u in self.users:
            if u.get("user") == user:
                u["face_count"] = face_count
                return
        uid = CURRENT_UID
        try:
            uid = pwd.getpwnam(user).pw_uid
        except Exception:
            pass
        self.users.append({"user": user, "uid": uid, "face_count": face_count})

    def api_enroll_poll(self) -> dict[str, Any]:
        with self.lock:
            session = self.session
            if session is None or getattr(session, "_mode", None) != "enroll":
                return {"ok": True, "state": "idle"}
            frame, box, message, hint, captured, required, _score = session.poll()
            if session.state == "error":
                code = session.error_code or CODE_INTERNAL
                err = session.error or "采集失败"
                self._close_session()
                return {"ok": False, "error": err, "code": code, "state": "error"}
            payload = {
                "ok": True,
                "state": session.state,
                "session": {
                    "user": session.user,
                    "label": session.label,
                    "captured": captured,
                    "required": required,
                    "frame": base64.b64encode(frame).decode("ascii") if frame else None,
                    "box": box,
                    "message": message,
                    "hint": hint,
                },
            }
            if session.state == "done":
                # 采满了就放开摄像头（模板数据仍在内存里等 commit）
                try:
                    session.close()
                except Exception:
                    pass
                try:
                    self.camera_lock.release()
                except RuntimeError:
                    pass
            return payload

    def api_enroll_commit(self) -> dict[str, Any]:
        with self.lock:
            session = self.session
            if session is None or getattr(session, "_mode", None) != "enroll":
                raise BridgeError("没有正在进行的录入会话", CODE_INVALID)
            samples = session.take_samples()
            if not samples:
                raise BridgeError("还没有采集到任何合格样本", CODE_INVALID)
            ids: list[str] = []
            for s in samples:
                req = {
                    "cmd": "add_face",
                    "user": session.user,
                    "label": session.label or s.get("label") or "未命名",
                    "embedding": s["embedding"],
                    "quality": s.get("quality") or {},
                }
                thumb = s.get("thumb")
                if thumb:
                    req["thumb_b64"] = base64.b64encode(thumb).decode("ascii")
                resp = self.admin.call(req)
                ids.append(str(resp.get("id")))
            self._note_user(session.user, len(self.users and []) or 0)
            self._close_session()
            self._sync_user_count(session.user)
            return {"ok": True, "added": len(ids), "ids": ids}

    def _sync_user_count(self, user: str) -> None:
        try:
            resp = self.admin.call({"cmd": "list_faces", "user": user}, FAST_TIMEOUT)
            n = len(resp.get("faces") or [])
            self._note_user(user, n)
        except BridgeError as e:
            log(f"刷新人脸数量失败: {e.message}")

    def start_verify(self, user: str) -> dict[str, Any]:
        known = self._known_embeddings(user)
        with self.lock:
            self._stop_preview()
            self._close_session()
            if not self.camera_lock.acquire(timeout=3.0):
                self.verify_state = "idle"
                raise BridgeError(
                    "摄像头正被其它功能占用（预览或录入），请先停止后再试", CODE_BUSY)
            try:
                session = self._new_session(user)
                if not session.verify_start(user, known):
                    raise BridgeError(session.error or "无法开始识别测试",
                                      session.error_code or CODE_INTERNAL)
            except BridgeError:
                self.camera_lock.release()
                self.verify_state = "idle"
                raise
            except Exception as e:
                self.camera_lock.release()
                self.verify_state = "idle"
                raise BridgeError(f"无法开始识别测试：{e}", CODE_INTERNAL)
            self.session = session
            self.verify_state = "running"
            return {"ok": True, "state": "running", "user": user,
                    "threshold": float(self.config.get("threshold", 0.5)),
                    "templates": int(known.shape[0])}

    def _known_embeddings(self, user: str) -> Any:
        """取得该用户的模板特征（用于本地比对）。

        模板库是 root 0700，普通用户读不到，所以向特权助手要：
        `list_faces` 带可选字段 `with_embedding=true`，助手应在每条 face 上附带
        `embedding`（128 floats）。助手若忽略该字段，这里会给出明确错误，
        而不是误报"还没录入人脸"。
        """
        import numpy as np
        resp = self.admin.call({"cmd": "list_faces", "user": user, "with_embedding": True})
        faces = [f for f in (resp.get("faces") or []) if isinstance(f, dict)]
        rows = []
        missing = 0
        for f in faces:
            emb = f.get("embedding") or f.get("embeddings")
            arr = np.asarray(emb, dtype=np.float32).ravel() if emb is not None \
                else np.zeros((0,), dtype=np.float32)
            if arr.size == 128:
                rows.append(arr)
            else:
                missing += 1
        if faces and not rows:
            raise BridgeError(
                f"特权助手没有返回模板特征（{missing} 条人脸都缺少 embedding）。"
                f"识别测试需要 faceunlock-admin 支持 list_faces 的可选字段 with_embedding=true。",
                CODE_NOT_ENROLLED)
        if not rows:
            return np.zeros((0, 128), dtype=np.float32)
        return np.asarray(rows, dtype=np.float32)

    def api_verify_poll(self) -> dict[str, Any]:
        with self.lock:
            session = self.session
            if session is None or getattr(session, "_mode", None) != "verify":
                return {"ok": True, "state": self.verify_state if self.verify_state == "stopped"
                        else "idle", "score": None, "passed": False,
                        "threshold": float(self.config.get("threshold", 0.5))}
            frame, box, message, _hint, _c, _r, score = session.poll()
            if session.state == "error":
                code = session.error_code or CODE_INTERNAL
                err = session.error or "识别测试失败"
                self._close_session()
                self.verify_state = "idle"
                return {"ok": False, "error": err, "code": code, "state": "idle"}
            if session.state == "idle":
                self.verify_state = "stopped"
            return {
                "ok": True,
                "state": "running" if session.state == "running" else self.verify_state,
                "score": score,
                "passed": bool(session.passed),
                "threshold": float(session.threshold),
                "frame": base64.b64encode(frame).decode("ascii") if frame else None,
                "box": box,
                "message": message,
            }

    def api_face_rename(self, user: str, fid: str, label: str) -> dict[str, Any]:
        self.admin.call({"cmd": "rename_face", "user": user, "id": fid, "label": label})
        return {"ok": True}

    def api_face_delete(self, user: str, fid: str) -> dict[str, Any]:
        self.admin.call({"cmd": "delete_face", "user": user, "id": fid})
        self._sync_user_count(user)
        return {"ok": True}

    def api_face_delete_all(self, user: str) -> dict[str, Any]:
        self.admin.call({"cmd": "delete_all", "user": user})
        self._note_user(user, 0)
        return {"ok": True}

    def api_config(self, patch: Any) -> dict[str, Any]:
        clean = validate_config_patch(patch)
        if not clean:
            return {"ok": True, "config": self.config}
        resp = self.admin.call({"cmd": "set_config", "config": clean})
        cfg = resp.get("config")
        if isinstance(cfg, dict):
            self.config = _merge_config(config_mod.DEFAULTS, cfg)
        else:
            self.config = _merge_config(self.config, clean)
        self.config_privileged = True
        return {"ok": True, "config": self.config}

    def api_pam(self, action: str) -> dict[str, Any]:
        if action not in ("enable", "disable", "panic"):
            raise BridgeError(f"未知的 PAM 动作: {action}", CODE_INVALID)
        # 协议里的命令名：pam_enable / pam_disable / panic（见 docs/GUI_API.md）
        cmd = {"enable": "pam_enable", "disable": "pam_disable", "panic": "panic"}[action]
        resp = self.admin.call({"cmd": cmd})
        pam = resp.get("pam")
        if isinstance(pam, dict):
            self.pam = pam
        else:
            self.pam = self._local_pam()
        if action == "panic":
            # panic = 关总开关 + 移除 PAM 栈
            self.config = _merge_config(self.config, {"enabled": False})
            try:
                self.refresh_config_from_admin()
            except BridgeError as e:
                log(f"panic 后刷新配置失败: {e.message}")
        return {"ok": True, "pam": self.pam}

    def api_doctor(self) -> dict[str, Any]:
        checks: list[dict[str, str]] = []
        # 摄像头
        from .capture import fake_mode
        mode = fake_mode()
        if mode == "busy":
            checks.append({"name": "摄像头", "status": "warn",
                           "detail": "模拟设备被占用（FACEUNLOCK_FAKE_CAMERA=busy）"})
        elif mode in ("frames", "image"):
            checks.append({"name": "摄像头", "status": "warn",
                           "detail": f"模拟摄像头（FACEUNLOCK_FAKE_CAMERA={mode}），未使用真实设备"})
        else:
            checks.append(self._probe_camera())
        # 模型
        try:
            self.engine_obj()
            checks.append({"name": "模型", "status": "ok", "detail": "YuNet+SFace 已加载"})
        except BridgeError as e:
            checks.append({"name": "模型", "status": "fail", "detail": e.message})
        # 本地 PAM 状态
        pam = self.pam
        if pam.get("enabled"):
            checks.append({"name": "PAM", "status": "ok",
                           "detail": f"已启用（{pam.get('file')}）"})
        elif pam.get("profile_installed"):
            checks.append({"name": "PAM", "status": "warn", "detail": "faceunlock profile 未启用"})
        else:
            checks.append({"name": "PAM", "status": "warn",
                           "detail": "未安装 pam-configs profile，人脸登录未接入"})
        # 特权项（模板库 / 管理员助手）
        try:
            resp = self.admin.call({"cmd": "doctor"})
            admin_checks = [c for c in (resp.get("checks") or []) if isinstance(c, dict)]
            if admin_checks:
                names = {c["name"] for c in admin_checks}
                checks = [c for c in checks if c["name"] not in names] + admin_checks
            else:
                self.refresh_users()
                checks.append({"name": "模板库", "status": "ok",
                               "detail": f"管理员助手可用，{len(self.users)} 个用户"})
        except BridgeError as e:
            status = "warn" if e.code == CODE_AUTH_REQUIRED else "fail"
            checks.append({"name": "模板库", "status": status,
                           "detail": "需要管理员授权" if e.code == CODE_AUTH_REQUIRED
                           else f"不可用：{e.message}"})
            if e.code == CODE_AUTH_REQUIRED:
                checks.append({"name": "管理员助手", "status": "warn",
                               "detail": "授权被取消或未安装 faceunlock-admin（特权操作不可用）"})
        return {"ok": True, "checks": checks}

    def _probe_camera(self) -> dict[str, str]:
        from .camera import Camera, CameraBusy
        conf = self.config.get("camera") or {}
        device = int(conf.get("device", 0))
        if not self.camera_lock.acquire(blocking=False):
            return {"name": "摄像头", "status": "warn",
                    "detail": f"/dev/video{device} 正被本程序占用（预览/会话中）"}
        cam = None
        try:
            cam = Camera(device=device, width=int(conf.get("width", 1280)),
                         height=int(conf.get("height", 720)), warmup=1)
            cam.open()
            frame = cam.read()
            if frame is None:
                return {"name": "摄像头", "status": "warn",
                        "detail": f"/dev/video{device} 打开成功但读不到画面"}
            h, w = frame.shape[:2]
            return {"name": "摄像头", "status": "ok",
                    "detail": f"/dev/video{device} {w}x{h} MJPG"}
        except CameraBusy as e:
            return {"name": "摄像头", "status": "warn",
                    "detail": f"/dev/video{device} 被其它程序占用（{e}）"}
        except Exception as e:
            return {"name": "摄像头", "status": "fail",
                    "detail": f"/dev/video{device} 不可用：{e}"}
        finally:
            if cam is not None:
                try:
                    cam.close()
                except Exception:
                    pass
            try:
                self.camera_lock.release()
            except RuntimeError:
                pass


# ---------- HTTP 层 ----------


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    bridge: Bridge


class Handler(BaseHTTPRequestHandler):
    server_version = f"faceunlock-gui-bridge/{__version__}"
    protocol_version = "HTTP/1.1"

    # ---- 基础设施 ----

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log(f"{self.address_string()} {fmt % args}")

    @property
    def bridge(self) -> Bridge:
        return self.server.bridge  # type: ignore[attr-defined]

    def _send(self, body: bytes, status: int = 200, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in CORS_HEADERS:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, obj: dict[str, Any], status: int = 200) -> None:
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"), status)

    def _error(self, message: str, code: str = CODE_INTERNAL, status: int = 200) -> None:
        self._send_json({"ok": False, "error": message, "code": code}, status)

    def _host_ok(self) -> bool:
        raw = (self.headers.get("Host") or "").strip()
        if not raw:
            return False
        host = raw
        if host.startswith("["):            # IPv6 字面量
            host = host.split("]")[0] + "]"
        elif ":" in host:
            host = host.rsplit(":", 1)[0]
        return host.lower() in ALLOWED_HOSTS

    def _token_ok(self, query: dict[str, list[str]]) -> bool:
        given = self.headers.get("X-FaceUnlock-Token") or ""
        if not given:
            given = (query.get("token") or [""])[0]
        return bool(given) and hmac.compare_digest(given, self.bridge.token)

    def _body_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > 8 * 1024 * 1024:
            raise BridgeError("请求体过大", CODE_INVALID)
        raw = self.rfile.read(length)
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise BridgeError(f"请求体不是合法 JSON: {e}", CODE_INVALID)
        if not isinstance(data, dict):
            raise BridgeError("请求体必须是 JSON 对象", CODE_INVALID)
        return data

    # ---- HTTP 方法 ----

    def do_OPTIONS(self) -> None:  # noqa: N802
        # 预检请求不带自定义头（浏览器规定），因此不做 token 校验，只回 CORS 头。
        if not self._host_ok():
            self._error("非法的 Host 头", CODE_INVALID, status=401)
            return
        self._send(b"", 200)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("GET")

    # ---- 路由 ----

    def _dispatch(self, method: str) -> None:
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parts.query)

        if not self._host_ok():
            self._error("非法的 Host 头（只允许 127.0.0.1/localhost）", CODE_INVALID, status=401)
            return

        # /health 不需要 token（Tauri/Rust 侧与排障用）
        if path == "/health":
            self._send_json({"ok": True, "version": __version__, "pid": os.getpid(),
                             "uptime": round(time.time() - self.bridge.started, 3)})
            return

        if not self._token_ok(query):
            self._error("缺少或错误的 X-FaceUnlock-Token", CODE_AUTH_REQUIRED, status=401)
            return

        try:
            if path == "/" and method == "GET":
                self._send_json({"ok": True, "service": "faceunlock-gui-bridge",
                                 "version": __version__, "api": "/api/state"})
            elif path == "/api/preview.mjpg" and method == "GET":
                self._preview_mjpg()
            elif path == "/api/state" and method == "GET":
                self._send_json(self.bridge.api_state())
            elif path == "/api/doctor" and method == "GET":
                self._send_json(self.bridge.api_doctor())
            elif path == "/api/faces" and method == "GET":
                user = self.bridge.require_valid_user((query.get("user") or [""])[0])
                self._send_json(self.bridge.api_faces(user))
            elif path == "/api/enroll/start" and method == "POST":
                body = self._body_json()
                user = self.bridge.require_valid_user(body.get("user"))
                label = str(body.get("label") or "未命名")[:64]
                count = body.get("count", 5)
                if isinstance(count, bool) or not isinstance(count, (int, float)):
                    raise BridgeError("count 必须是整数", CODE_INVALID)
                count = max(1, min(MAX_ENROLL_COUNT, int(count)))
                self._send_json(self.bridge.start_enroll(user, label, count))
            elif path == "/api/enroll/poll" and method == "GET":
                self._send_json(self.bridge.api_enroll_poll())
            elif path == "/api/enroll/commit" and method == "POST":
                self._body_json()
                self._send_json(self.bridge.api_enroll_commit())
            elif path == "/api/enroll/cancel" and method == "POST":
                with self.bridge.lock:
                    self.bridge._close_session()
                self._send_json({"ok": True, "state": "idle"})
            elif path == "/api/verify/start" and method == "POST":
                body = self._body_json()
                user = self.bridge.require_valid_user(body.get("user"))
                self._send_json(self.bridge.start_verify(user))
            elif path == "/api/verify/poll" and method == "GET":
                self._send_json(self.bridge.api_verify_poll())
            elif path == "/api/verify/stop" and method == "POST":
                self.bridge.stop_verify()
                self._send_json({"ok": True, "state": "stopped"})
            elif path == "/api/face/rename" and method == "POST":
                body = self._body_json()
                user = self.bridge.require_valid_user(body.get("user"))
                fid = str(body.get("id") or "")
                if not fid:
                    raise BridgeError("缺少 id", CODE_INVALID)
                label = str(body.get("label") or "未命名")[:64]
                self._send_json(self.bridge.api_face_rename(user, fid, label))
            elif path == "/api/face/delete" and method == "POST":
                body = self._body_json()
                user = self.bridge.require_valid_user(body.get("user"))
                fid = str(body.get("id") or "")
                if not fid:
                    raise BridgeError("缺少 id", CODE_INVALID)
                self._send_json(self.bridge.api_face_delete(user, fid))
            elif path == "/api/face/delete_all" and method == "POST":
                body = self._body_json()
                user = self.bridge.require_valid_user(body.get("user"))
                self._send_json(self.bridge.api_face_delete_all(user))
            elif path == "/api/config" and method == "POST":
                body = self._body_json()
                resp = self.bridge.api_config(body)
                # 兼容两种读法：顶层直接是完整 config，同时提供 config 字段
                merged = dict(resp["config"])
                merged.update(resp)
                self._send_json(merged)
            elif path == "/api/pam" and method == "POST":
                body = self._body_json()
                action = str(body.get("action") or "")
                self._send_json(self.bridge.api_pam(action))
            else:
                self._error(f"未知接口: {method} {path}", CODE_INVALID)
        except BridgeError as e:
            self._error(e.message, e.code)
        except BrokenPipeError:
            self.close_connection = True
        except Exception as e:  # 兜底：任何异常都是 200 + error body
            log(f"处理 {method} {path} 时未捕获异常: {e!r}")
            self._error(f"内部错误: {e}", CODE_INTERNAL)

    # ---- MJPEG ----

    def _preview_mjpg(self) -> None:
        bridge = self.bridge
        with bridge.lock:
            session_running = (bridge.session is not None
                               and getattr(bridge.session, "state", "") == "running")
        if session_running:
            self._error("正在录入/识别测试中，预览不可用", CODE_BUSY)
            return
        if not bridge.camera_lock.acquire(blocking=False):
            self._error("已有其它预览或采集正在使用摄像头", CODE_BUSY)
            return

        bridge.preview_stop.clear()
        bridge.preview_active = True
        cam = None
        try:
            from .capture import FakeCamera, fake_mode
            from .camera import Camera, CameraBusy, encode_jpeg
            conf = bridge.config.get("camera") or {}
            mode = fake_mode()
            if mode == "busy":
                raise BridgeError("摄像头被其它程序占用", CODE_BUSY)
            if mode in ("frames", "image"):
                cam = FakeCamera(device=int(conf.get("device", 0)),
                                 width=int(conf.get("width", 1280)),
                                 height=int(conf.get("height", 720)))
            else:
                cam = Camera(device=int(conf.get("device", 0)),
                             width=int(conf.get("width", 1280)),
                             height=int(conf.get("height", 720)))
            try:
                cam.open()
            except CameraBusy as e:
                raise BridgeError(f"摄像头被其它程序占用：{e}", CODE_BUSY)
        except BridgeError as e:
            bridge.preview_active = False
            try:
                bridge.camera_lock.release()
            except RuntimeError:
                pass
            self._error(e.message, e.code)
            return

        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Connection", "close")
        for k, v in CORS_HEADERS:
            self.send_header(k, v)
        self.end_headers()
        self.close_connection = True
        log("MJPEG 预览开始")
        frames = 0
        try:
            while not bridge.preview_stop.is_set():
                frame = cam.read()
                if frame is None:
                    time.sleep(0.05)
                    continue
                try:
                    jpg = encode_jpeg(frame, 70)
                except Exception:
                    continue
                header = (b"--frame\r\nContent-Type: image/jpeg\r\n"
                          b"Content-Length: " + str(len(jpg)).encode() + b"\r\n\r\n")
                self.wfile.write(header)
                self.wfile.write(jpg)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                frames += 1
                time.sleep(0.06)  # ~15fps 足够，省 CPU
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception as e:
            log(f"预览流异常: {e!r}")
        finally:
            if cam is not None:
                try:
                    cam.close()
                except Exception:
                    pass
            bridge.preview_active = False
            bridge.preview_stop.clear()
            try:
                bridge.camera_lock.release()
            except RuntimeError:
                pass
            log(f"MJPEG 预览结束（{frames} 帧）")


# ---------- 启动 ----------


def build_server(host: str, port: int, bridge: Bridge) -> BridgeServer:
    server = BridgeServer((host, port), Handler)
    server.bridge = bridge
    return server


def _watchdog_parent(interval: float = 3.0) -> None:
    """父进程（Tauri 应用）没了就一起退出。

    为什么需要：Rust 侧是用 Stdio::null() 起我们的，stdin 上没有 EOF 可用；
    而 Tauri 窗口被强杀（SIGKILL / 崩溃）时不会走正常清理路径，
    网桥就成了孤儿进程常驻在后台占端口、占内存（实测踩到过：反复启动
    GUI 会攒下好几个孤儿网桥）。这里用最通用的判据——被 init 收养
    （getppid()==1）或父 PID 变了——来收尾。
    """
    original = os.getppid()
    while True:
        time.sleep(interval)
        ppid = os.getppid()
        if ppid != original or ppid == 1:
            log(f"父进程已退出（ppid {original} -> {ppid}），网桥随之退出")
            os._exit(0)


def _start_parent_watchdog() -> None:
    threading.Thread(target=_watchdog_parent, daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="faceunlock-gui-bridge",
        description="faceunlock 管理界面后端（本地 HTTP + 特权助手代理）")
    ap.add_argument("--host", default=DEFAULT_HOST, help="监听地址（默认 127.0.0.1）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口（默认 0=随机）")
    ap.add_argument("--token", default=None, help="固定 token（默认随机生成）")
    ap.add_argument("--fake-camera", action="store_true",
                    help="等价于 FACEUNLOCK_FAKE_CAMERA=1（无相机开发）")
    ap.add_argument("--no-handshake", action="store_true",
                    help="不向 stdout 打印握手行（手工调试用）")
    args = ap.parse_args(argv)

    if args.fake_camera:
        os.environ.setdefault("FACEUNLOCK_FAKE_CAMERA", "1")

    bridge = Bridge()
    bridge.token = args.token or secrets.token_urlsafe(32)

    if args.host not in ALLOWED_HOSTS:
        log(f"警告：绑定到非回环地址 {args.host}，token 可能被同网段嗅探")

    try:
        server = build_server(args.host, args.port, bridge)
    except OSError as e:
        log(f"无法绑定 {args.host}:{args.port} -> {e}")
        return 2

    _start_parent_watchdog()

    port = server.server_address[1]
    handshake = {"event": "ready", "port": port, "token": bridge.token, "pid": os.getpid()}
    if not args.no_handshake:
        # stdout 只输出这一行，之后不再使用 stdout
        print(json.dumps(handshake, ensure_ascii=False), flush=True)
    log(f"监听 http://{args.host}:{port}  admin_cmd={' '.join(bridge.admin.cmd)}")

    stopping = threading.Event()

    def _shutdown(signum: int, _frame: Any) -> None:
        if stopping.is_set():
            return
        stopping.set()
        log(f"收到信号 {signum}，正在退出…")
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except Exception:
            pass

    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        with bridge.lock:
            bridge._close_session()
        bridge.admin.close()
        try:
            server.server_close()
        except Exception:
            pass
        log("已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
