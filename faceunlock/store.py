"""人脸模板库：/var/lib/faceunlock（root 0700，文件 0600）。

安全设计：
  * 只保存 128 维特征向量（不可逆），不保存原始人脸图像；缩略图为可选。
  * 整个模板文件用 HMAC-SHA256 签名，密钥 root 0600，防止本地用户篡改模板
    （篡改模板 == 决定谁能解锁该账户，属提权路径）。
  * 写入一律"临时文件 + fsync + 原子替换"，崩溃不会损坏既有模板。
  * 签名校验失败时**拒绝使用**该模板（认证侧视为"不适用"，回退密码），不静默通过。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from typing import Any

import numpy as np

from . import MODEL_ID, STORE_DIR

EMBED_DIM = 128


class StoreError(Exception):
    pass


class StoreTampered(StoreError):
    """模板 HMAC 校验失败。"""


class StoreUnreadable(StoreTampered):
    """读不到模板库——通常是权限不足（非 root），**不是**篡改或损坏。

    故意继承 StoreTampered：认证路径上所有既有的 `except StoreTampered`
    分支都会原样接住它，新增异常类型不会在 PAM 栈里变成漏网异常；失败
    语义也一致（不适用 -> PAM_IGNORE -> 回退密码），安全上不作任何放松。

    展示层（cli / admin）必须**优先**匹配 StoreUnreadable，否则会把
    "你不是 root" 说成 "模板被篡改"——安全模块里这种误报很危险：
    真被篡改时反而分不出来。
    """


def _atomic_write(path: str, data: bytes, mode: int = 0o600) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _validate_embedding(emb: Any) -> list[float]:
    arr = np.asarray(emb, dtype=np.float32).ravel()
    if arr.size != EMBED_DIM:
        raise StoreError(f"特征维度必须是 {EMBED_DIM}，收到 {arr.size}")
    if not np.all(np.isfinite(arr)):
        raise StoreError("特征包含非法数值")
    if float(np.linalg.norm(arr)) < 1e-6:
        raise StoreError("特征向量为零向量")
    return [float(x) for x in arr]


def valid_username(user: str) -> bool:
    """防止路径穿越：模板文件名直接来自 PAM_USER。"""
    if not user or len(user) > 32 or user in (".", ".."):
        return False
    return all(c.isalnum() or c in "-_." for c in user) and not user.startswith(".")


class TemplateStore:
    def __init__(self, root: str | None = None) -> None:
        self.root = root or STORE_DIR
        self.tpl_dir = os.path.join(self.root, "templates")
        self.thumb_dir = os.path.join(self.root, "thumbs")
        self.key_path = os.path.join(self.root, "secret.key")
        self.state_path = os.path.join(self.root, "state.json")

    # ---------- 基础设施 ----------

    def ensure(self) -> None:
        for d in (self.root, self.tpl_dir, self.thumb_dir):
            os.makedirs(d, exist_ok=True)
            try:
                os.chmod(d, 0o700)
            except OSError:
                pass

    def _key(self) -> bytes:
        try:
            with open(self.key_path, "rb") as fh:
                k = fh.read()
            if len(k) >= 32:
                return k
        except FileNotFoundError:
            pass
        self.ensure()
        k = secrets.token_bytes(32)
        _atomic_write(self.key_path, k, 0o600)
        return k

    def _path(self, user: str) -> str:
        if not valid_username(user):
            raise StoreError(f"非法用户名: {user!r}")
        return os.path.join(self.tpl_dir, f"{user}.json")

    def _sign(self, payload: dict[str, Any]) -> str:
        return hmac.new(self._key(), _canonical(payload), hashlib.sha256).hexdigest()

    # ---------- 读写 ----------

    def load(self, user: str) -> dict[str, Any] | None:
        """读取并校验模板；不存在返回 None，被篡改抛 StoreTampered。"""
        p = self._path(user)
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return None
        except PermissionError as e:
            # 先于 OSError 捕获：权限不足不是"文件损坏"，必须区分开
            raise StoreUnreadable(
                f"权限不足，读不到模板库（{self.tpl_dir}，需要 root）") from e
        except (json.JSONDecodeError, OSError) as e:
            raise StoreTampered(f"模板文件损坏: {e}") from e
        sig = data.pop("hmac", None)
        if not isinstance(sig, str) or not hmac.compare_digest(sig, self._sign(data)):
            raise StoreTampered(f"{user} 的模板签名校验失败")
        return data

    def save(self, user: str, data: dict[str, Any]) -> None:
        self.ensure()
        payload = dict(data)
        payload.pop("hmac", None)
        payload["user"] = user
        payload["model_id"] = MODEL_ID
        payload["updated"] = int(time.time())
        payload["hmac"] = self._sign(payload)
        _atomic_write(self._path(user),
                      json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8"),
                      0o600)

    # ---------- 查询 ----------

    def users(self) -> list[str]:
        try:
            return sorted(f[:-5] for f in os.listdir(self.tpl_dir) if f.endswith(".json"))
        except FileNotFoundError:
            return []
        except PermissionError as e:
            raise StoreUnreadable(
                f"权限不足，列不出模板库（{self.tpl_dir}，需要 root）") from e

    def exists(self, user: str) -> bool:
        try:
            return self.load(user) is not None
        except StoreTampered:
            return False

    def faces(self, user: str, with_thumb: bool = True) -> list[dict[str, Any]]:
        data = self.load(user)
        if not data:
            return []
        out = []
        for f in data.get("faces", []):
            item = {k: v for k, v in f.items() if k != "embedding"}
            if with_thumb:
                item["has_thumb"] = bool(f.get("thumb")) and os.path.isfile(
                    self._thumb_path(user, f["id"]))
            out.append(item)
        return out

    def embeddings(self, user: str) -> np.ndarray:
        """返回 (N,128) float32；无模板返回 (0,128)。"""
        data = self.load(user)
        if not data:
            return np.zeros((0, EMBED_DIM), dtype=np.float32)
        rows = [_validate_embedding(f["embedding"]) for f in data.get("faces", [])
                if f.get("embedding")]
        if not rows:
            return np.zeros((0, EMBED_DIM), dtype=np.float32)
        return np.asarray(rows, dtype=np.float32)

    # ---------- 缩略图 ----------

    def _thumb_path(self, user: str, fid: str) -> str:
        return os.path.join(self.thumb_dir, user, f"{fid}.jpg")

    def thumb(self, user: str, fid: str) -> bytes | None:
        try:
            with open(self._thumb_path(user, fid), "rb") as fh:
                return fh.read()
        except OSError:
            return None

    # ---------- 变更 ----------

    def add_face(self, user: str, label: str, embedding: Any,
                 quality: dict[str, Any] | None = None,
                 thumb_jpeg: bytes | None = None) -> str:
        emb = _validate_embedding(embedding)
        data = self.load(user) or {"version": 1, "user": user, "faces": []}
        fid = secrets.token_hex(4)
        data.setdefault("faces", []).append({
            "id": fid,
            "label": (label or "未命名")[:64],
            "created": int(time.time()),
            "embedding": emb,
            "quality": quality or {},
            "thumb": f"{fid}.jpg" if thumb_jpeg else None,
        })
        if thumb_jpeg:
            _atomic_write(self._thumb_path(user, fid), thumb_jpeg, 0o600)
        self.save(user, data)
        return fid

    def rename_face(self, user: str, fid: str, label: str) -> bool:
        data = self.load(user)
        if not data:
            return False
        for f in data.get("faces", []):
            if f["id"] == fid:
                f["label"] = (label or "未命名")[:64]
                self.save(user, data)
                return True
        return False

    def delete_face(self, user: str, fid: str) -> bool:
        data = self.load(user)
        if not data:
            return False
        before = len(data.get("faces", []))
        data["faces"] = [f for f in data.get("faces", []) if f["id"] != fid]
        if len(data["faces"]) == before:
            return False
        try:
            os.unlink(self._thumb_path(user, fid))
        except OSError:
            pass
        self.save(user, data)
        return True

    def delete_user(self, user: str) -> bool:
        """删除该用户全部人脸（用于注销）。"""
        p = self._path(user)
        existed = os.path.exists(p)
        try:
            os.unlink(p)
        except OSError:
            pass
        d = os.path.join(self.thumb_dir, user)
        if os.path.isdir(d):
            for f in os.listdir(d):
                try:
                    os.unlink(os.path.join(d, f))
                except OSError:
                    pass
            try:
                os.rmdir(d)
            except OSError:
                pass
        return existed

    # ---------- 限速状态 ----------

    def state(self) -> dict[str, Any]:
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}

    def save_state(self, st: dict[str, Any]) -> None:
        self.ensure()
        _atomic_write(self.state_path,
                      json.dumps(st, ensure_ascii=False, indent=1).encode("utf-8"), 0o600)
