#!/usr/bin/env python3
"""**假的特权助手**（开发/测试用）—— 实现 docs/GUI_API.md 里约定的 JSON-Lines 协议。

它用真实模块（`TemplateStore` / `faceunlock.config`）但把数据全部放在
`/tmp/fu-gui-test`（可用 FACEUNLOCK_STORE / FACEUNLOCK_CONFIG 覆盖），
并假装自己是 uid=1000 的 alice，从而在**不运行 pkexec、不碰 /etc 与
/var/lib** 的前提下完整验证 gui_bridge 的特权转发路径。

用法:
    python3 tools/fake_admin.py serve
    FACEUNLOCK_ADMIN_CMD="python3 tools/fake_admin.py serve" bin/faceunlock-gui-bridge

测试开关:
    serve --exit-auth        模拟 pkexec 授权被取消（stderr 打印后 exit 126）
    serve --die-after N      服务 N 条命令后自杀一次（配合 --die-marker 只死一次），
                             用于验证 bridge 的"助手死掉自动重启一次"
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(ROOT, "vendor"), ROOT):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

FAKE_USER = os.environ.get("FACEUNLOCK_FAKE_USER", "alice")
FAKE_UID = int(os.environ.get("FACEUNLOCK_FAKE_UID", "1000"))
TEST_ROOT = os.environ.get("FACEUNLOCK_STORE", "/tmp/fu-gui-test")
CONFIG_PATH = os.environ.get("FACEUNLOCK_CONFIG", "/tmp/fu-gui-test/config.json")
PAM_STATE = os.environ.get("FACEUNLOCK_FAKE_PAM", "/tmp/fu-gui-test/pam.json")


def _store():
    from faceunlock.store import TemplateStore
    return TemplateStore(TEST_ROOT)


def _config_mod():
    from faceunlock import config as config_mod
    return config_mod


def _load_config() -> dict:
    return _config_mod().load(CONFIG_PATH)


def _save_config(cfg: dict) -> dict:
    _config_mod().save(cfg, CONFIG_PATH)
    return _load_config()


def _load_pam() -> dict:
    try:
        with open(PAM_STATE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"profile_installed": True, "enabled": False,
                "file": "/etc/pam.d/common-auth", "mode": "fake"}


def _save_pam(pam: dict) -> dict:
    os.makedirs(os.path.dirname(PAM_STATE), exist_ok=True)
    with open(PAM_STATE, "w", encoding="utf-8") as fh:
        json.dump(pam, fh, ensure_ascii=False, indent=1)
    return _load_pam()


# ---------- 命令实现 ----------


def cmd_hello(_req) -> dict:
    return {"ok": True, "uid": FAKE_UID, "user": FAKE_USER, "admin": False,
            "fake": True, "store": TEST_ROOT, "config": CONFIG_PATH}


def cmd_list_users(_req) -> dict:
    store = _store()
    users = []
    for u in store.users():
        try:
            n = len(store.faces(u, with_thumb=False))
        except Exception:
            n = 0
        users.append({"user": u, "uid": FAKE_UID if u == FAKE_USER else 0, "face_count": n})
    if not any(u["user"] == FAKE_USER for u in users):
        users.append({"user": FAKE_USER, "uid": FAKE_UID, "face_count": 0})
    return {"ok": True, "users": users}


def cmd_list_faces(req) -> dict:
    user = req.get("user") or FAKE_USER
    with_emb = bool(req.get("with_embedding"))
    store = _store()
    data = store.load(user)
    faces = []
    for f in (data or {}).get("faces", []):
        item = {
            "id": f.get("id"),
            "label": f.get("label"),
            "created": f.get("created"),
            "quality": f.get("quality") or {},
        }
        thumb = store.thumb(user, f.get("id"))
        item["thumb_b64"] = base64.b64encode(thumb).decode("ascii") if thumb else None
        if with_emb:
            emb = f.get("embedding")
            if emb:
                item["embedding"] = [float(v) for v in emb]
        faces.append(item)
    faces.sort(key=lambda x: x.get("created") or 0, reverse=True)
    return {"ok": True, "faces": faces}


def cmd_add_face(req) -> dict:
    user = req.get("user") or FAKE_USER
    emb = req.get("embedding")
    if not isinstance(emb, list) or len(emb) != 128:
        return {"ok": False, "error": "embedding 必须是 128 个浮点数", "code": "INVALID"}
    thumb = None
    if req.get("thumb_b64"):
        try:
            thumb = base64.b64decode(req["thumb_b64"])
        except Exception:
            thumb = None
    fid = _store().add_face(user, req.get("label") or "未命名", emb,
                            quality=req.get("quality") or {}, thumb_jpeg=thumb)
    return {"ok": True, "id": fid}


def cmd_rename_face(req) -> dict:
    ok = _store().rename_face(req.get("user") or FAKE_USER, str(req.get("id")),
                              req.get("label") or "未命名")
    if not ok:
        return {"ok": False, "error": "找不到该人脸", "code": "INVALID"}
    return {"ok": True}


def cmd_delete_face(req) -> dict:
    ok = _store().delete_face(req.get("user") or FAKE_USER, str(req.get("id")))
    if not ok:
        return {"ok": False, "error": "找不到该人脸", "code": "INVALID"}
    return {"ok": True}


def cmd_delete_all(req) -> dict:
    _store().delete_user(req.get("user") or FAKE_USER)
    return {"ok": True}


def cmd_get_config(_req) -> dict:
    return {"ok": True, "config": _load_config()}


def cmd_set_config(req) -> dict:
    patch = req.get("config")
    if not isinstance(patch, dict):
        return {"ok": False, "error": "config 必须是对象", "code": "INVALID"}
    cur = _load_config()

    def merge(base, over):
        out = dict(base)
        for k, v in over.items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = merge(out[k], v)
            else:
                out[k] = v
        return out

    return {"ok": True, "config": _save_config(merge(cur, patch))}


def cmd_pam_status(_req) -> dict:
    return {"ok": True, "pam": _load_pam()}


def cmd_pam_enable(_req) -> dict:
    pam = _load_pam()
    pam.update({"enabled": True, "profile_installed": True, "mode": "fake"})
    return {"ok": True, "pam": _save_pam(pam)}


def cmd_pam_disable(_req) -> dict:
    pam = _load_pam()
    pam.update({"enabled": False, "mode": "fake"})
    return {"ok": True, "pam": _save_pam(pam)}


def cmd_pam_panic(_req) -> dict:
    """panic = 关闭总开关 + 从 PAM 栈移除。协议里的命令名是 `panic`。"""
    pam = _load_pam()
    pam.update({"enabled": False, "mode": "fake"})
    cfg = _load_config()
    cfg["enabled"] = False
    _save_config(cfg)
    return {"ok": True, "pam": _save_pam(pam), "message": "panic 完成：已停用并关闭总开关"}


def cmd_doctor(_req) -> dict:
    store = _store()
    checks = []
    try:
        n_users = len(store.users())
        checks.append({"name": "模板库", "status": "ok",
                       "detail": f"{store.root}（{n_users} 个用户）"})
    except Exception as e:
        checks.append({"name": "模板库", "status": "fail", "detail": str(e)})
    pam = _load_pam()
    checks.append({"name": "PAM", "status": "ok" if pam.get("enabled") else "warn",
                   "detail": "已启用" if pam.get("enabled") else "未启用"})
    checks.append({"name": "管理员助手", "status": "ok",
                   "detail": f"假助手 pid={os.getpid()} uid={FAKE_UID}({FAKE_USER})"})
    return {"ok": True, "checks": checks}


COMMANDS = {
    "hello": cmd_hello,
    "list_users": cmd_list_users,
    "list_faces": cmd_list_faces,
    "add_face": cmd_add_face,
    "rename_face": cmd_rename_face,
    "delete_face": cmd_delete_face,
    "delete_all": cmd_delete_all,
    "get_config": cmd_get_config,
    "set_config": cmd_set_config,
    "pam_status": cmd_pam_status,
    "pam_enable": cmd_pam_enable,
    "pam_disable": cmd_pam_disable,
    "pam_panic": cmd_pam_panic,
    "panic": cmd_pam_panic,          # 协议里的正式命令名（docs/GUI_API.md）
    "doctor": cmd_doctor,
}


def serve(args) -> int:
    if args.exit_auth:
        print("Error executing command as another user: Request dismissed",
              file=sys.stderr, flush=True)
        return 126
    os.makedirs(TEST_ROOT, exist_ok=True)
    if not os.path.isfile(CONFIG_PATH):
        _save_config(_load_config())
    if not os.path.isfile(PAM_STATE):
        _save_pam(_load_pam())
    served = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            resp = {"ok": False, "error": f"非法 JSON: {e}", "code": "INVALID"}
        else:
            cmd = str(req.get("cmd") or "")
            if cmd == "quit":
                print(json.dumps({"ok": True}), flush=True)
                return 0
            fn = COMMANDS.get(cmd)
            if fn is None:
                resp = {"ok": False, "error": f"未知命令: {cmd}", "code": "INVALID"}
            else:
                try:
                    resp = fn(req)
                except Exception as e:
                    resp = {"ok": False, "error": f"{type(e).__name__}: {e}",
                            "code": "INTERNAL"}
        print(json.dumps(resp, ensure_ascii=False), flush=True)
        served += 1
        if args.die_after and served >= args.die_after:
            marker = args.die_marker
            if not marker or not os.path.exists(marker):
                if marker:
                    open(marker, "w").close()
                print("fake-admin: 模拟崩溃退出", file=sys.stderr, flush=True)
                return 3
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="fake_admin")
    sub = ap.add_subparsers(dest="action", required=True)
    s = sub.add_parser("serve", help="以 JSON-Lines 协议服务 stdin/stdout")
    s.add_argument("--exit-auth", action="store_true", help="模拟 polkit 授权被取消（exit 126）")
    s.add_argument("--die-after", type=int, default=0, help="服务 N 条命令后模拟崩溃")
    s.add_argument("--die-marker", default="")
    sub.add_parser("show", help="打印当前假模板库内容")
    args = ap.parse_args(argv)
    if args.action == "show":
        store = _store()
        print(json.dumps({"root": store.root, "users": store.users(),
                          "config": _load_config(), "pam": _load_pam()},
                         ensure_ascii=False, indent=1))
        return 0
    return serve(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
