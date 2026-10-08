"""特权助手：faceunlock-admin（由 pkexec 以 root 身份拉起）。

职责边界：
  * 只有它能读写 /var/lib/faceunlock（root 0700）与 /etc/faceunlock。
  * 只有它能增删 PAM 配置。
  * 它**不碰摄像头**，识别推理在普通用户的 capture 进程里完成；
     因此这里必须把"提交上来的 128 维特征"当作**不可信输入**做校验
     （维度、有限性、非零），并对"能管理谁"做严格鉴权。

鉴权模型：
  * 调用者身份 = pkexec 注入的 PKEXEC_UID（缺失时视为 root 直接调用）。
  * 管理自己的人脸：允许。
  * 管理他人人脸 / 改配置 / 开关 PAM：要求调用者在 sudo 或 admin 组（或 root）。

协议：JSON-Lines，一行请求一行响应。见 docs/GUI_API.md。
"""
from __future__ import annotations

import base64
import json
import os
import pwd
import sys
from typing import Any

from . import CONFIG_FILE, STORE_DIR, __version__
from . import config as config_mod
from . import pamctl
from .store import StoreError, StoreTampered, StoreUnreadable, TemplateStore, valid_username

ADMIN_GROUPS = {"sudo", "admin", "wheel", "root"}
MIN_UID = 1000


class Denied(Exception):
    def __init__(self, msg: str, code: str = "AUTH_REQUIRED") -> None:
        super().__init__(msg)
        self.code = code


def _caller_uid() -> int:
    v = os.environ.get("PKEXEC_UID")
    if v and v.isdigit():
        return int(v)
    return os.getuid()


def _user_groups(name: str) -> set[str]:
    try:
        import grp
        gids = set(os.getgroups()) if name == pwd.getpwuid(os.getuid()).pw_name else set()
        groups = {pwd.getpwuid(os.getuid()).pw_name}
        for g in grp.getgrall():
            if name in g.gr_mem:
                groups.add(g.gr_name)
        # 主组也要算
        try:
            groups.add(grp.getgrgid(pwd.getpwnam(name).pw_gid).gr_name)
        except KeyError:
            pass
        return groups
    except Exception:
        return set()


def caller_info() -> dict[str, Any]:
    uid = _caller_uid()
    try:
        pw = pwd.getpwuid(uid)
        name = pw.pw_name
    except KeyError:
        name = f"uid{uid}"
    groups = _user_groups(name) if uid != 0 else {"root"}
    is_admin = uid == 0 or bool(groups & ADMIN_GROUPS)
    return {"uid": uid, "user": name, "groups": sorted(groups), "admin": is_admin}


def require_manage(target: str) -> dict[str, Any]:
    info = caller_info()
    if info["uid"] == 0 or info["admin"] or info["user"] == target:
        return info
    raise Denied(f"当前用户 {info['user']} 无权管理 {target} 的人脸")


def require_admin() -> dict[str, Any]:
    info = caller_info()
    if info["uid"] == 0 or info["admin"]:
        return info
    raise Denied(f"当前用户 {info['user']} 无权修改系统配置")


def system_users() -> list[dict[str, Any]]:
    store = TemplateStore()
    out = []
    for pw in sorted(pwd.getpwall(), key=lambda p: p.pw_name):
        if pw.pw_uid < MIN_UID or pw.pw_uid >= 65534:
            continue
        try:
            n = len(store.faces(pw.pw_name, with_thumb=False))
        except StoreUnreadable:
            n = -2          # 非 root 读不到模板库，与"模板损坏"(-1)区分开
        except StoreTampered:
            n = -1
        out.append({"user": pw.pw_name, "uid": pw.pw_uid, "face_count": n})
    return out


# ---------------- 命令实现 ----------------

def cmd_hello(_: dict) -> dict:
    info = caller_info()
    return {"ok": True, "version": __version__, "uid": info["uid"],
            "user": info["user"], "admin": info["admin"], "groups": info["groups"]}


def cmd_list_users(_: dict) -> dict:
    info = caller_info()
    users = system_users()
    if not info["admin"]:
        # 非管理员只能看到自己
        users = [u for u in users if u["user"] == info["user"]]
    return {"ok": True, "users": users, "can_manage_others": info["admin"]}


def cmd_list_faces(req: dict) -> dict:
    user = str(req.get("user", ""))
    if not valid_username(user):
        raise Denied("非法用户名", "INVALID")
    require_manage(user)
    # with_embedding: 识别测试页需要在本机算实时相似度。这里不构成新的信息暴露——
    # 采集进程本来就以该用户身份在用户态计算 128 维特征；且鉴权已限定为
    # "只能取自己的人脸"（管理员除外）。
    with_emb = bool(req.get("with_embedding"))
    store = TemplateStore()
    # 注意：必须用 load() 拿原始记录。TemplateStore.faces() 会主动剥掉
    # "embedding" 字段（那是给列表展示用的视图），用它会得到一堆空特征。
    data = store.load(user) or {"faces": []}
    faces = []
    for f in data.get("faces", []):
        thumb = store.thumb(user, f["id"]) if req.get("with_thumb", True) else None
        item = {
            "id": f["id"],
            "label": f.get("label", ""),
            "created": f.get("created", 0),
            "quality": f.get("quality", {}),
            "thumb_b64": base64.b64encode(thumb).decode() if thumb else None,
        }
        if with_emb:
            item["embedding"] = [float(x) for x in f.get("embedding", [])]
        faces.append(item)
    return {"ok": True, "user": user, "faces": faces}


def cmd_add_face(req: dict) -> dict:
    user = str(req.get("user", ""))
    if not valid_username(user):
        raise Denied("非法用户名", "INVALID")
    require_manage(user)
    emb = req.get("embedding")
    if not isinstance(emb, list) or len(emb) != 128:
        raise Denied("特征数据非法（必须是 128 个浮点数）", "INVALID")
    thumb = None
    tb = req.get("thumb_b64")
    if isinstance(tb, str) and tb:
        try:
            thumb = base64.b64decode(tb, validate=True)
        except Exception:
            raise Denied("缩略图 base64 非法", "INVALID")
        if len(thumb) > 512 * 1024:
            raise Denied("缩略图过大", "INVALID")
        if not thumb.startswith(b"\xff\xd8"):
            raise Denied("缩略图不是 JPEG", "INVALID")
    store = TemplateStore()
    try:
        fid = store.add_face(user, str(req.get("label", ""))[:64], emb,
                             quality=req.get("quality") or {}, thumb_jpeg=thumb)
    except StoreError as e:
        raise Denied(f"写入模板失败: {e}", "INVALID")
    return {"ok": True, "id": fid}


def cmd_rename_face(req: dict) -> dict:
    user = str(req.get("user", ""))
    require_manage(user)
    label = str(req.get("label", ""))[:64]
    ok = TemplateStore().rename_face(user, str(req.get("id", "")), label)
    if not ok:
        raise Denied("未找到该人脸", "INVALID")
    return {"ok": True}


def cmd_delete_face(req: dict) -> dict:
    user = str(req.get("user", ""))
    require_manage(user)
    ok = TemplateStore().delete_face(user, str(req.get("id", "")))
    if not ok:
        raise Denied("未找到该人脸", "INVALID")
    return {"ok": True}


def cmd_delete_all(req: dict) -> dict:
    user = str(req.get("user", ""))
    require_manage(user)
    TemplateStore().delete_user(user)
    return {"ok": True}


def cmd_get_config(_: dict) -> dict:
    caller_info()
    return {"ok": True, "config": config_mod.load()}


VALID_SERVICES = {"gdm-password", "sudo", "sudo-i", "su", "polkit-1", "login"}


def cmd_set_config(req: dict) -> dict:
    require_admin()
    new = req.get("config")
    if not isinstance(new, dict):
        raise Denied("config 必须是对象", "INVALID")
    cfg = config_mod.load()
    if "enabled" in new:
        cfg["enabled"] = bool(new["enabled"])
    if "threshold" in new:
        try:
            t = float(new["threshold"])
        except (TypeError, ValueError):
            raise Denied("阈值必须是数字", "INVALID")
        if not 0.20 <= t <= 0.90:
            raise Denied("阈值必须在 0.20~0.90 之间", "INVALID")
        cfg["threshold"] = round(t, 3)
    for k in ("timeout_ms", "no_face_timeout_ms"):
        if k in new:
            try:
                v = int(new[k])
            except (TypeError, ValueError):
                raise Denied(f"{k} 必须是整数", "INVALID")
            if not 200 <= v <= 30000:
                raise Denied(f"{k} 必须在 200~30000 之间", "INVALID")
            cfg[k] = v
    for k in ("required_frames", "window_frames"):
        if k in new:
            try:
                v = int(new[k])
            except (TypeError, ValueError):
                raise Denied(f"{k} 必须是整数", "INVALID")
            if not 1 <= v <= 10:
                raise Denied(f"{k} 必须在 1~10 之间", "INVALID")
            cfg[k] = v
    if isinstance(new.get("services"), dict):
        for s, v in new["services"].items():
            if s in VALID_SERVICES:
                cfg["services"][s] = bool(v)
    if "save_thumbnails" in new:
        cfg["save_thumbnails"] = bool(new["save_thumbnails"])
    config_mod.save(cfg)
    return {"ok": True, "config": cfg}


def cmd_pam_status(_: dict) -> dict:
    caller_info()
    return {"ok": True, "pam": pamctl.status()}


def cmd_pam_enable(_: dict) -> dict:
    require_admin()
    ok, msg = pamctl.enable()
    return {"ok": ok, "message": msg, "pam": pamctl.status()}


def cmd_pam_disable(_: dict) -> dict:
    require_admin()
    ok, msg = pamctl.disable()
    return {"ok": ok, "message": msg, "pam": pamctl.status()}


def cmd_panic(_: dict) -> dict:
    require_admin()
    ok, msg = pamctl.panic()
    return {"ok": ok, "message": msg, "pam": pamctl.status()}


def cmd_doctor(_: dict) -> dict:
    caller_info()
    checks: list[dict[str, str]] = []

    # 模板库
    store = TemplateStore()
    if os.path.isdir(store.root):
        users = store.users()
        checks.append({"name": "模板库", "status": "ok",
                       "detail": f"{store.root}（{len(users)} 个用户已录入）"})
    else:
        checks.append({"name": "模板库", "status": "warn",
                       "detail": f"{store.root} 不存在（尚未录入任何人脸）"})

    # 模型
    try:
        from . import DETECTOR_MODEL, RECOGNIZER_MODEL, models_dir
        d = models_dir()
        miss = [m for m in (DETECTOR_MODEL, RECOGNIZER_MODEL)
                if not os.path.isfile(os.path.join(d, m))]
        checks.append({"name": "模型", "status": "fail" if miss else "ok",
                       "detail": f"{d}" + (f" 缺少 {miss}" if miss else " YuNet+SFace 就绪")})
    except Exception as e:
        checks.append({"name": "模型", "status": "fail", "detail": str(e)})

    # 摄像头
    dev = config_mod.load().get("camera", {}).get("device", 0)
    path = f"/dev/video{dev}"
    if os.path.exists(path):
        try:
            mode = oct(os.stat(path).st_mode & 0o777)
        except OSError:
            mode = "?"
        checks.append({"name": "摄像头", "status": "ok", "detail": f"{path} 存在（{mode}）"})
    else:
        checks.append({"name": "摄像头", "status": "fail", "detail": f"{path} 不存在"})

    # PAM
    st = pamctl.status()
    if st["enabled"]:
        checks.append({"name": "PAM", "status": "ok",
                       "detail": f"已启用（{st['mode']}）"})
    elif st["profile_installed"]:
        checks.append({"name": "PAM", "status": "warn",
                       "detail": "profile 已安装但未启用"})
    else:
        checks.append({"name": "PAM", "status": "warn",
                       "detail": "profile 未安装，人脸登录尚未生效"})

    # polkit 授权框（pkexec）—— Ubuntu 26.04 / polkit 127 的沙箱
    # 这里只在"本机确实是 socket 激活 + 强沙箱"时才提醒，老版本 polkit 直接标 ok。
    try:
        from . import polkitctl
        ps = polkitctl.status()
        if not ps["unit_present"]:
            checks.append({"name": "polkit", "status": "ok",
                           "detail": "本机 polkit 无 socket 激活沙箱，无需处理"})
        elif ps.get("camera_allowed"):
            checks.append({"name": "polkit", "status": "ok",
                           "detail": "授权框可用摄像头（已放开 video4linux）"})
        else:
            checks.append({"name": "polkit", "status": "warn",
                           "detail": (f"授权框拿不到摄像头（{ps.get('detail')}）；"
                                      f"可用 sudo faceunlock polkit-camera on 修复，"
                                      f"或忽略（授权框只走密码）")})
    except Exception as e:  # 自检本身绝不能因此失败
        checks.append({"name": "polkit", "status": "warn",
                       "detail": f"无法检测 polkit 沙箱状态: {e}"})

    # 配置
    cfg = config_mod.load()
    checks.append({"name": "配置", "status": "ok" if cfg.get("enabled") else "warn",
                   "detail": f"{CONFIG_FILE} 总开关={'开' if cfg.get('enabled') else '关'} "
                             f"阈值={cfg.get('threshold')}"})
    return {"ok": True, "checks": checks, "store": STORE_DIR}


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
    "panic": cmd_panic,
    "doctor": cmd_doctor,
}


def handle(req: dict) -> dict:
    cmd = req.get("cmd")
    if cmd == "quit":
        return {"ok": True, "quit": True}
    fn = COMMANDS.get(str(cmd))
    if fn is None:
        return {"ok": False, "error": f"未知命令: {cmd}", "code": "INVALID"}
    try:
        return fn(req)
    except Denied as e:
        return {"ok": False, "error": str(e), "code": e.code}
    except StoreTampered as e:
        return {"ok": False, "error": f"模板库被篡改或损坏: {e}", "code": "INTERNAL"}
    except Exception as e:
        return {"ok": False, "error": f"内部错误: {e}", "code": "INTERNAL"}


def serve() -> int:
    """JSON-Lines 服务模式：给 GUI 网桥长期使用。"""
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError("请求必须是对象")
        except Exception as e:
            resp = {"ok": False, "error": f"请求解析失败: {e}", "code": "INVALID"}
        else:
            resp = handle(req)
        out.write(json.dumps(resp, ensure_ascii=False) + "\n")
        out.flush()
        if resp.get("quit"):
            break
    return 0


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "serve":
        return serve()
    # 单发模式：从 argv 或 stdin 读一条请求
    if len(argv) > 1:
        try:
            req = json.loads(argv[1])
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e), "code": "INVALID"}))
            return 2
    else:
        req = json.loads(sys.stdin.read() or "{}")
    print(json.dumps(handle(req), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
