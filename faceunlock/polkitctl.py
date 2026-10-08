"""polkit 授权框（pkexec）里的摄像头权限管理 —— Ubuntu 26.04 / polkit 127 专用。

问题（本机实测，日志与机制见 README「Ubuntu 26.04 / polkit 127」一节）：

  polkit 127 起，polkit-1 服务的 PAM 栈不再跑在普通 setuid-root 进程里，而是
  跑在 systemd socket 激活的 `polkit-agent-helper@.service` 里：

      PrivateDevices=yes          -> 私有 /dev，里面根本没有 /dev/video*
      DevicePolicy=strict
      DeviceAllow=/dev/null rw    -> 视频设备被设备 cgroup 拒绝

  于是 pkexec / 图形界面授权框里的人脸认证**永远**打不开摄像头（日志：
  `无法打开 /dev/video0（被占用或无权限）`），而且还要白等一次 OpenCV 导入
  加 8 秒兜底超时，用户在授权框前等不到人脸通过、也来不及输密码。

本模块管理一个 drop-in：

  /etc/systemd/system/polkit-agent-helper@.service.d/10-faceunlock-camera.conf

  内容只有两行：`PrivateDevices=no` + `DeviceAllow=char-video4linux rw`，
  也就是只放开"视频设备"这一类字符设备。polkit 的其余加固（DevicePolicy=strict
  依然只放行 /dev/null 与视频设备、ProtectSystem/ProtectHome/PrivateTmp/
  NoNewPrivileges/系统调用过滤…）一律不动。

安全立场：这是**便捷因子**的取舍，所以做成用户可开关的：
  * 安装后默认放开 —— 否则 pkexec 场景的人脸功能形同虚设；
  * `faceunlock polkit-camera off`（以及 `faceunlock panic`）会删掉它并
    daemon-reload，把 polkit 恢复成上游默认的强沙箱，授权框只认密码。

所有操作都不碰 PAM 栈：drop-in 只影响 polkit 自己的 helper 进程。
"""
from __future__ import annotations

import os
import shutil
import subprocess

DROPIN_DIR = "/etc/systemd/system/polkit-agent-helper@.service.d"
DROPIN_NAME = "10-faceunlock-camera.conf"
DROPIN_PATH = os.path.join(DROPIN_DIR, DROPIN_NAME)

#: 随包安装一份"权威内容"；`polkit-camera on` 从它复制，避免正文在代码里抄两份
REFERENCE_PATH = "/usr/share/faceunlock/polkit-agent-helper-camera.conf"

#: 用模板实例查运行时属性即可拿到整份 [Service] 配置
SERVICE = "polkit-agent-helper@0.service"
UNIT_FILE = "/usr/lib/systemd/system/polkit-agent-helper@.service"

#: 设备 cgroup 白名单里表示"视频设备"的写法（major 81）
VIDEO_ALLOW = "char-video4linux"
VIDEO_ALLOW_NUMERIC = "char-81"


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:  # pragma: no cover
        return 127, str(e)


def systemd_present() -> bool:
    return os.path.isdir("/run/systemd/system") and shutil.which("systemctl") is not None


def unit_present() -> bool:
    """本机 polkit 是否使用（受沙箱影响的）socket 激活 helper。"""
    return os.path.isfile(UNIT_FILE)


def properties() -> dict[str, str]:
    """读**运行时**单元属性（写入 drop-in 后需要 daemon-reload 才会反映）。

    注意：`systemctl show -p DeviceAllow` 对列表型属性会输出**多行**
    （`DeviceAllow=char-rtc r` / `DeviceAllow=char-video4linux rw` / …），
    必须累积而不是让后者覆盖前者 —— 否则最关键的 video4linux 那行会被
    `/dev/null rw` 盖掉，把"授权框能开摄像头"误报成"不能"。
    """
    if not systemd_present() or not unit_present():
        return {}
    rc, out = _run(["systemctl", "show", SERVICE, "-p", "PrivateDevices",
                    "-p", "DeviceAllow"])
    if rc != 0:
        return {}
    props: dict[str, str] = {}
    for line in out.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if not v:
            continue
        props[k] = f"{props[k]} {v}" if k in props else v
    return props


def _camera_allowed(props: dict[str, str]) -> bool:
    allow = props.get("DeviceAllow", "")
    private_dev = props.get("PrivateDevices", "")
    if private_dev == "yes":
        # 私有 /dev 里压根没有 /dev/video*，DeviceAllow 再怎么写也没用
        return False
    return VIDEO_ALLOW in allow or VIDEO_ALLOW_NUMERIC in allow


def status() -> dict[str, object]:
    """给 CLI / doctor / GUI 用的完整状态快照。任何异常都不抛。"""
    out: dict[str, object] = {
        "unit_present": unit_present(),
        "systemd": systemd_present(),
        "dropin_path": DROPIN_PATH,
        "dropin_present": os.path.isfile(DROPIN_PATH),
        "reference_path": REFERENCE_PATH,
        "reference_present": os.path.isfile(REFERENCE_PATH),
        "private_devices": None,
        "device_allow": None,
        "camera_allowed": None,
        "needs_daemon_reload": False,
        "detail": "",
    }
    if not out["unit_present"]:
        out["detail"] = ("本机 polkit 不使用 socket 激活的 helper"
                         "（没有 polkit-agent-helper@.service），无需处理")
        return out
    if not out["systemd"]:
        out["detail"] = "本机不使用 systemd，无法读取单元属性"
        return out

    props = properties()
    if not props:
        out["detail"] = f"读不到 {SERVICE} 的属性（systemctl show 失败）"
        return out

    out["private_devices"] = props.get("PrivateDevices")
    out["device_allow"] = props.get("DeviceAllow")
    allowed = _camera_allowed(props)
    out["camera_allowed"] = allowed
    # drop-in 写了但运行时还没生效 -> 需要 daemon-reload
    out["needs_daemon_reload"] = bool(out["dropin_present"]) and not allowed and \
        props.get("PrivateDevices") == "yes"
    if allowed:
        out["detail"] = "授权框里的人脸认证可以打开摄像头"
    else:
        out["detail"] = ("polkit 沙箱会挡住摄像头：PrivateDevices=%s、"
                         "DeviceAllow=%s" % (props.get("PrivateDevices"),
                                             props.get("DeviceAllow") or "<空>"))
    return out


def _daemon_reload() -> tuple[bool, str]:
    if not systemd_present():
        return False, "本机没有运行中的 systemd"
    rc, out = _run(["systemctl", "daemon-reload"])
    if rc != 0:
        return False, f"systemctl daemon-reload 失败: {out}"
    return True, "已 systemctl daemon-reload"


def _write_dropin(content: str) -> None:
    os.makedirs(DROPIN_DIR, exist_ok=True)
    os.chmod(DROPIN_DIR, 0o755)
    tmp = f"{DROPIN_PATH}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, DROPIN_PATH)


def enable() -> tuple[bool, str]:
    """写入 drop-in 并 daemon-reload（需要 root）。"""
    if not unit_present():
        return True, ("本机 polkit 不使用 socket 激活的 helper，"
                      "无需放开沙箱（未做任何改动）")
    content = None
    for src in (REFERENCE_PATH, os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "packaging",
            "polkit-agent-helper@.service.d", DROPIN_NAME)):
        try:
            with open(src, "r", encoding="utf-8") as fh:
                content = fh.read()
            break
        except OSError:
            continue
    if not content:
        return False, (f"找不到 drop-in 正文（{REFERENCE_PATH}），"
                       f"请确认 faceunlock 包完整安装")
    try:
        _write_dropin(content)
    except OSError as e:
        return False, f"写入 {DROPIN_PATH} 失败: {e}"
    ok, msg = _daemon_reload()
    if not ok:
        return False, f"drop-in 已写入但 {msg}"
    st = status()
    if st.get("camera_allowed"):
        return True, f"已放开 polkit 授权框的摄像头权限（{DROPIN_PATH}）"
    return True, (f"drop-in 已写入并 daemon-reload，但运行时属性仍显示 "
                  f"{st.get('detail')}；请执行 systemctl daemon-reload 后重试")


def disable() -> tuple[bool, str]:
    """删除 drop-in 并 daemon-reload，把 polkit 恢复成上游强沙箱（需要 root）。"""
    removed = False
    try:
        os.unlink(DROPIN_PATH)
        removed = True
    except FileNotFoundError:
        pass
    except OSError as e:
        return False, f"删除 {DROPIN_PATH} 失败: {e}"
    try:
        os.rmdir(DROPIN_DIR)  # 只在我们建的目录为空时成功
    except OSError:
        pass
    if not removed:
        return True, "本就没有放开（polkit 保持上游强沙箱）"
    ok, msg = _daemon_reload()
    return ok, ("已恢复 polkit 默认沙箱：授权框只认密码" if ok
                else f"已删除 drop-in，但 {msg}")
