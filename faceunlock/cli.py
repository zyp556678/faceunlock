"""faceunlock 命令行工具。

特权子命令（enroll/list/rename/delete/delete-all/target/enable/disable/panic/
service）需要 root；图形界面走的是 faceunlock-admin 的 JSON-Lines 协议。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import CONFIG_FILE, STORE_DIR, __version__
from . import config as config_mod
from . import pamctl
from .camera import Camera, CameraBusy
from .engine import FaceEngine
from .store import StoreTampered, StoreUnreadable, TemplateStore

# 录入质量门限——这几个数字是**在本机实测标定**出来的，不要凭直觉改：
#   * 本机摄像头（Luxvisions 30c9:008c）在出厂 ISP 参数下，
#     人脸区域的 Laplacian 方差（"清晰度"）实测只有 10~18；
#     把摄像头的数字锐化拉满(sharpness=7)才到 42~100；
#     而人在移动时抓到的帧会掉到 5 以下。
#     曾一度把门限设成 60，结果正常坐姿下**永远录不进去**——这是踩过的坑。
#   * 人脸高度：SFace 需要 112x112 的对齐裁剪，低于 140px 时上采样太糊。
#   * 亮度：实测正常室内 100~160；< 45 太暗，> 215 过曝（逆光/正对灯）。
# 另外实测：改摄像头的 contrast/gamma/数字锐化 **不会**提升同人相似度
# （默认参数下同人相似度 0.958，调参后反而降到 0.85~0.94），所以不要动它。
QUALITY_MIN_HEIGHT = 140.0
QUALITY_MAX_HEIGHT = 700.0   # 太近 -> 固定焦距镜头失焦，图像反而糊
QUALITY_HARD_SHARPNESS_FLOOR = 5.0  # 极低才算"在动/糊"，正常波动不该挡人
BRIGHT_RANGE = (45.0, 215.0)
#: 自校准门限：新样本必须与已采集样本至少这么像，否则说明这一帧废了
#: （同人正常帧之间实测 0.90+，换姿态也在 0.5~0.8；画面糊/人在动会掉到 0.4 以下）
QUALITY_SELF_CONSISTENCY = 0.40

#: 录入时依次给出的姿态提示，提升模板多样性
POSE_HINTS = ["请正对镜头", "请稍微左转", "请稍微右转", "请稍微抬头", "请稍微低头",
              "请正对镜头，保持自然表情"]


def _is_root() -> bool:
    return os.geteuid() == 0


def _need_root() -> None:
    if not _is_root():
        sys.exit("该操作需要 root 权限，请用: sudo faceunlock ...")


# ---------------- 只读命令 ----------------

def cmd_doctor(args: argparse.Namespace) -> int:
    from .admin import cmd_doctor
    try:
        res = cmd_doctor({})
    except StoreUnreadable as e:
        sys.exit(f"{e}\n请用 root 运行：sudo faceunlock doctor（或打开图形界面 faceunlock-gui）")
    icon = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}
    print(f"faceunlock {__version__} 自检\n")
    for c in res["checks"]:
        print(f"  {icon.get(c['status'], '?')} {c['name']:8s} {c['detail']}")
    print()
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = config_mod.load()
    st = pamctl.status()
    store = TemplateStore()
    try:
        users = {u: len(store.faces(u, with_thumb=False)) for u in store.users()}
        users_err = ""
    except StoreUnreadable as e:
        # 只读命令不该因为"不是 root"而崩；如实说明并指向特权路径
        users, users_err = {}, str(e)
    info = {
        "version": __version__,
        "store": store.root,
        "config_file": CONFIG_FILE,
        "enabled": cfg["enabled"],
        "threshold": cfg["threshold"],
        "services": cfg["services"],
        "pam": st,
        "users": users,
        "users_error": users_err or None,
    }
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    print(f"faceunlock {info['version']}")
    print(f"  总开关     : {'开' if info['enabled'] else '关'}")
    print(f"  阈值       : {info['threshold']}")
    print(f"  模板库     : {info['store']}")
    print(f"  PAM 已启用 : {'是' if st['enabled'] else '否'}  ({st['mode']})")
    print("  已录入用户 :")
    for u, n in (info["users"] or {}).items():
        print(f"      {u}: {n} 张人脸")
    if users_err:
        print(f"      （本用户查不到：{users_err}）")
        print("      （用 sudo faceunlock status 或 faceunlock-gui 查看）")
    elif not info["users"]:
        print("      （无）")
    print("  场景开关   :")
    for s, v in info["services"].items():
        print(f"      {s:14s} {'开' if v else '关'}")
    return 0


def cmd_users(args: argparse.Namespace) -> int:
    from .admin import system_users
    users = system_users()
    if args.json:
        print(json.dumps(users, ensure_ascii=False, indent=2))
        return 0
    for u in users:
        n = u["face_count"]
        if n == -2:
            mark = "🔒 读不到模板库（需要 root）"
        elif n == -1:
            mark = "⚠️ 模板损坏"
        elif n > 0:
            mark = f"{n} 张人脸"
        else:
            mark = "未录入"
        print(f"  {u['user']:20s} uid={u['uid']:<6d} {mark}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    store = TemplateStore()
    try:
        faces = store.faces(args.user, with_thumb=False)
    except StoreUnreadable as e:
        sys.exit(f"{e}\n请用 sudo faceunlock list {args.user}，或打开 faceunlock-gui")
    except StoreTampered as e:
        sys.exit(f"模板损坏或被篡改: {e}")
    if args.json:
        print(json.dumps(faces, ensure_ascii=False, indent=2))
        return 0
    if not faces:
        print(f"{args.user} 尚未录入人脸")
        return 1
    print(f"{args.user} 的人脸（{len(faces)} 张）:")
    for f in faces:
        t = time.strftime("%Y-%m-%d %H:%M", time.localtime(f.get("created", 0)))
        q = f.get("quality", {})
        print(f"  {f['id']}  {f.get('label', ''):16s} {t}  "
              f"高度={q.get('height', 0):.0f} 清晰度={q.get('sharpness', 0):.0f}")
    return 0


# ---------------- 写入命令 ----------------

def cmd_enroll(args: argparse.Namespace) -> int:
    _need_root()
    store = TemplateStore()
    store.ensure()
    try:
        engine = FaceEngine()
    except FileNotFoundError as e:
        sys.exit(str(e))
    cfg = config_mod.load()
    cam_cfg = cfg.get("camera", {})

    print(f"为 {args.user} 录入人脸：需要 {args.samples} 个合格样本")
    print("提示：正对镜头，光线充足，人脸高度至少 "
          f"{QUALITY_MIN_HEIGHT:.0f}px\n")
    collected: list[tuple] = []
    try:
        cam = Camera(device=int(cam_cfg.get("device", 0)),
                     width=int(cam_cfg.get("width", 1280)),
                     height=int(cam_cfg.get("height", 720)))
        cam.open()
    except CameraBusy as e:
        sys.exit(f"摄像头不可用: {e}")

    last_msg = ""
    try:
        deadline = time.monotonic() + args.timeout
        while len(collected) < args.samples and time.monotonic() < deadline:
            frame = cam.read()
            if frame is None:
                time.sleep(0.05)
                continue
            face = engine.largest(engine.detect(frame))
            if face is None:
                msg = "没有检测到人脸"
            else:
                q = engine.quality(frame, face)
                emb = None
                if q["height"] < QUALITY_MIN_HEIGHT:
                    msg = f"请靠近一些（当前人脸高度 {q['height']:.0f}px）"
                elif q["height"] > QUALITY_MAX_HEIGHT:
                    msg = f"请离远一点（人脸 {q['height']:.0f}px，太近会失焦）"
                elif not BRIGHT_RANGE[0] <= q["brightness"] <= BRIGHT_RANGE[1]:
                    msg = f"光线不佳（亮度 {q['brightness']:.0f}）"
                elif q["sharpness"] < QUALITY_HARD_SHARPNESS_FLOOR:
                    msg = f"画面在晃动或严重失焦（清晰度 {q['sharpness']:.0f}）"
                else:
                    emb = engine.embed(frame, face)
                    # 自校准：靠"与已采集样本像不像"判断这一帧能不能用，
                    # 而不是靠某个摄像头相关的绝对清晰度数字。
                    if collected:
                        sim = max(engine.similarity(emb, e) for e, _ in collected)
                        if sim < QUALITY_SELF_CONSISTENCY:
                            msg = (f"这一帧不够稳定（与已采集样本相似度仅 {sim:.2f}），"
                                   f"请坐正、保持不动")
                            emb = None
                if emb is not None:
                    collected.append((emb, q))
                    msg = (f"已采集 {len(collected)}/{args.samples} —— "
                           f"{POSE_HINTS[min(len(collected), len(POSE_HINTS) - 1)]}")
                    time.sleep(0.35)
            if msg != last_msg:
                print(f"  {msg}")
                last_msg = msg
    except KeyboardInterrupt:
        print("\n已取消")
        return 1
    finally:
        cam.close()

    if not collected:
        print("没有采集到合格样本，未做任何修改")
        return 1
    label = args.label or time.strftime("录入-%m%d-%H%M")
    ids = []
    for emb, q in collected:
        ids.append(store.add_face(args.user, label, emb, quality=q))
    print(f"\n已保存 {len(ids)} 个样本，标签「{label}」")
    print(f"  id: {', '.join(ids)}")
    return 0


def cmd_rename(args: argparse.Namespace) -> int:
    _need_root()
    ok = TemplateStore().rename_face(args.user, args.id, args.label)
    print("已更新" if ok else "未找到该人脸")
    return 0 if ok else 1


def cmd_delete(args: argparse.Namespace) -> int:
    _need_root()
    store = TemplateStore()
    if args.id == "all":
        if not args.yes:
            sys.exit("删除全部人脸需要显式加 --yes")
        ok = store.delete_user(args.user)
        print("已清空" if ok else "本就没有")
        return 0
    ok = store.delete_face(args.user, args.id)
    print("已删除" if ok else "未找到该人脸")
    return 0 if ok else 1


def cmd_test(args: argparse.Namespace) -> int:
    from .auth import authenticate
    cfg = config_mod.load()
    # 让 test 一定真的跑识别：临时把服务名切到内置测试服务
    cfg["services"]["faceunlock-cli-test"] = True
    res = authenticate(args.user, "faceunlock-cli-test", cfg=cfg)
    names = {0: "通过 ✅", 1: "不匹配 ❌", 2: "不适用（回退密码）"}
    print(f"结果: {names.get(res.code, res.code)}")
    print(f"  原因   : {res.reason}")
    print(f"  最高分 : {res.score:.4f}  (阈值 {cfg['threshold']})")
    print(f"  命中   : {res.hits}  帧数: {res.frames}  耗时: {res.elapsed_ms:.0f} ms")
    return 0


# ---------------- PAM / 配置 ----------------

def cmd_enable(args: argparse.Namespace) -> int:
    _need_root()
    # --robust：写进 pam-auth-update 托管区之外，防 dpkg trigger 重写。
    # 包维护脚本在"换包"时必须用它（详见 pamctl.enable 的注释）。
    ok, msg = pamctl.enable(robust=getattr(args, "robust", False))
    print(msg)
    if ok:
        print("\n人脸认证已加入 common-auth，对所有 @include common-auth 的服务生效：")
        print("  gdm-password（开机登录界面 + GNOME 锁屏）、sudo、sudo-i、su、polkit-1")
        print("请在**另开一个 root 终端**的前提下测试：sudo -k true")
    return 0 if ok else 1


def cmd_disable(args: argparse.Namespace) -> int:
    _need_root()
    ok, msg = pamctl.disable()
    print(msg)
    return 0 if ok else 1


def cmd_panic(args: argparse.Namespace) -> int:
    _need_root()
    ok, msg = pamctl.panic()
    print(msg)
    print("已彻底停用人脸认证，现在只认密码。")
    return 0 if ok else 1


def cmd_threshold(args: argparse.Namespace) -> int:
    _need_root()
    cfg = config_mod.load()
    if args.value is None:
        print(f"当前阈值: {cfg['threshold']}")
        return 0
    if not 0.20 <= args.value <= 0.90:
        sys.exit("阈值必须在 0.20~0.90 之间")
    cfg["threshold"] = round(args.value, 3)
    config_mod.save(cfg)
    print(f"阈值已设为 {cfg['threshold']}"
          f"（本机实测：本人最低 0.70，冒充者最高 0.14）")
    return 0


def cmd_service(args: argparse.Namespace) -> int:
    _need_root()
    from .admin import VALID_SERVICES
    if args.name not in VALID_SERVICES:
        sys.exit(f"未知服务，可选: {', '.join(sorted(VALID_SERVICES))}")
    cfg = config_mod.load()
    if args.state == "on":
        cfg["services"][args.name] = True
    elif args.state == "off":
        cfg["services"][args.name] = False
    else:
        print(f"{args.name}: {'开' if cfg['services'].get(args.name) else '关'}")
        return 0
    config_mod.save(cfg)
    print(f"{args.name} -> {'开' if cfg['services'][args.name] else '关'}")
    return 0


def cmd_polkit_camera(args: argparse.Namespace) -> int:
    """polkit 授权框（pkexec）的摄像头权限开关。

    Ubuntu 26.04 / polkit 127 把 polkit-1 的 PAM 栈放进 PrivateDevices=yes
    的沙箱，里面没有 /dev/video*，人脸认证必然打不开摄像头。`on` 会写一个
    只放开"视频设备"的 systemd drop-in，`off` 则把 polkit 恢复成上游强沙箱。
    """
    from . import polkitctl

    if args.state in ("on", "off"):
        _need_root()
        ok, msg = polkitctl.enable() if args.state == "on" else polkitctl.disable()
        print(msg)
        if ok and args.state == "on":
            print("\n说明：只放开了 polkit-agent-helper@.service 的**视频设备**访问权，"
                  "\n      DevicePolicy=strict 仍然拒绝其它设备，配置文件系统与系统调用"
                  "\n      过滤等加固一律不变；授权框任何时候都能改用密码。")
        if ok and args.state == "off":
            print("授权框现在只会提示输入密码（人脸认证在 pkexec 场景不再尝试）。")
        return 0 if ok else 1

    st = polkitctl.status()
    if args.json:
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0
    print(f"polkit helper 单元 : {'存在' if st['unit_present'] else '不存在（无需处理）'}")
    print(f"drop-in            : {st['dropin_path']}")
    print(f"                     {'已安装' if st['dropin_present'] else '未安装'}")
    print(f"PrivateDevices     : {st['private_devices']}")
    print(f"DeviceAllow        : {st['device_allow']}")
    if st.get("camera_allowed") is not None:
        print(f"授权框可用摄像头   : {'是' if st['camera_allowed'] else '否'}")
    print(f"说明               : {st['detail']}")
    return 0


def cmd_admin(args: argparse.Namespace) -> int:
    """把参数当 JSON 请求交给 admin 层（调试用）。"""
    from .admin import handle
    print(json.dumps(handle(json.loads(args.request)), ensure_ascii=False, indent=2))
    return 0


# ---------------- 入口 ----------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="faceunlock",
                                description="人脸识别登录管理工具")
    p.add_argument("--version", action="version", version=f"faceunlock {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn, help_: str, **kw):
        sp = sub.add_parser(name, help=help_, **kw)
        sp.set_defaults(func=fn)
        return sp

    sp = add("doctor", cmd_doctor, "系统自检")
    sp.add_argument("--json", action="store_true")

    sp = add("status", cmd_status, "查看当前状态")
    sp.add_argument("--json", action="store_true")

    sp = add("users", cmd_users, "列出系统用户及录入情况")
    sp.add_argument("--json", action="store_true")

    sp = add("list", cmd_list, "列出某用户的人脸")
    sp.add_argument("user")
    sp.add_argument("--json", action="store_true")

    sp = add("enroll", cmd_enroll, "为某用户录入人脸（需要 root）")
    sp.add_argument("user")
    sp.add_argument("--label", default="")
    sp.add_argument("--samples", type=int, default=5)
    sp.add_argument("--timeout", type=float, default=60.0)

    sp = add("rename", cmd_rename, "重命名人脸（需要 root）")
    sp.add_argument("user")
    sp.add_argument("--id", required=True)
    sp.add_argument("--label", required=True)

    sp = add("delete", cmd_delete, "删除人脸（需要 root），id 传 all 清空")
    sp.add_argument("user")
    sp.add_argument("id")
    sp.add_argument("--yes", action="store_true")

    sp = add("test", cmd_test, "跑一次真实识别测试")
    sp.add_argument("user", nargs="?", default=os.environ.get("USER", ""))

    sp = add("enable", cmd_enable, "把人脸认证加入 PAM（需要 root）")
    sp.add_argument("--robust", action="store_true",
                    help="写入 pam-auth-update 托管区之外，抗 dpkg trigger 重写（换包时用）")
    add("disable", cmd_disable, "从 PAM 移除人脸认证（需要 root）")
    add("panic", cmd_panic, "一键停用：PAM 移除 + 总开关关闭（需要 root）")

    sp = add("threshold", cmd_threshold, "查看/设置识别阈值（需要 root）")
    sp.add_argument("value", nargs="?", type=float)

    sp = add("service", cmd_service, "开关某个场景")
    sp.add_argument("name")
    sp.add_argument("state", nargs="?", choices=["on", "off"])

    sp = add("polkit-camera", cmd_polkit_camera,
             "polkit 授权框的摄像头权限（Ubuntu 26 沙箱，需要 root 才能改）")
    # 注意把 status 也列进 choices：不带参数等价于 status，但文档/脚本里写
    # `polkit-camera status` 更明确 —— 曾经漏掉它导致该写法被 argparse 拒绝。
    sp.add_argument("state", nargs="?", choices=["on", "off", "status"],
                    help="on=放开摄像头权限，off=恢复上游强沙箱，status=查看（默认）")
    sp.add_argument("--json", action="store_true")

    sp = add("admin", cmd_admin, "直接调用特权层（调试）")
    sp.add_argument("request")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
