"""PAM 栈的启用/停用管理。

**关键经验（本机实测得出，勿改）**：
`[success=end default=ignore]` 里的 `end` 是 **pam-auth-update 的私有写法**，
它会把它改写成具体数字（本机 /etc/pam.d/common-auth 里 unix 的 end 被写成 success=2）。
libpam 运行时**不认识 end**——直接写进 /etc/pam.d/ 会让整条 auth 栈返回"拒绝"：

    auth [success=end default=ignore] pam_faceunlock.so   + pam_permit  -> 拒绝 ❌
    auth sufficient                   pam_faceunlock.so   + pam_permit  -> 成功 ✅
    auth [success=1 default=ignore]   pam_faceunlock.so   + deny+permit -> 成功 ✅

因此：
  * 我们随包提供的 pam-configs profile 用 `sufficient`（pam-auth-update 原样透传）；
  * 任何手工写入 /etc/pam.d/ 的场景也必须用 `sufficient`；
  * 绝不用 `required` —— 摄像头坏了不能连密码一起挡掉。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Any

PROFILE_NAME = "faceunlock"
PROFILE_PATH = f"/usr/share/pam-configs/{PROFILE_NAME}"
COMMON_AUTH = "/etc/pam.d/common-auth"
MARK_BEGIN = "# --- faceunlock begin (local module, kept by pam-auth-update) ---"
MARK_END = "# --- faceunlock end ---"

#: pam-auth-update 托管区的起始标记。我们的行必须插在它**之前**才能存活。
MANAGED_BEGIN = "here are the per-package modules"

#: 手工注入模式使用的模块行
MANUAL_LINE = "auth\tsufficient\tpam_faceunlock.so\n"


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:  # pragma: no cover
        return 127, str(e)


def profile_available() -> bool:
    return os.path.isfile(PROFILE_PATH)


def install_profile_from(src: str) -> bool:
    """开发态用：把仓库里的 profile 复制到系统目录。"""
    if not os.path.isfile(src):
        return False
    os.makedirs(os.path.dirname(PROFILE_PATH), exist_ok=True)
    shutil.copyfile(src, PROFILE_PATH)
    os.chmod(PROFILE_PATH, 0o644)
    return True


def is_enabled() -> bool:
    """判断人脸模块是否已进入 common-auth。"""
    try:
        with open(COMMON_AUTH, "r", encoding="utf-8") as fh:
            return "pam_faceunlock.so" in fh.read()
    except OSError:
        return False


def status() -> dict[str, Any]:
    return {
        "profile_installed": profile_available(),
        "enabled": is_enabled(),
        "file": COMMON_AUTH,
        "mode": "pam-auth-update" if profile_available() else "manual",
    }


def enable(robust: bool = False) -> tuple[bool, str]:
    """把 faceunlock 加入 common-auth（对所有 @include common-auth 的服务生效）。

    两条路径，各有用途：

    * ``robust=False``（默认）走 Ubuntu 官方的 ``pam-auth-update --enable``。
      好处是 pam-auth-update 界面里状态一致；代价是写进的是**托管区**，
      之后任何一次 pam-auth-update 运行（尤其是 libpam-runtime 的 dpkg
      trigger）都会按 debconf 状态把它重写掉。

    * ``robust=True`` 走"托管区之外的本地模块"注入。实测 pam-auth-update
      会**保留**托管区之前的内容（common-auth 头部注释也这么建议），
      因此 dpkg trigger 抹不掉它。

    为什么需要 robust：实测 `apt install ./faceunlock-full.deb`（卸旧包+装新包）
    的事务顺序是
        prerm(摘掉引用) → unpack → postinst(重新启用) → dpkg trigger(重写托管区)
    trigger 最后跑，会把 postinst 刚写进托管区的行按（已被清空的）debconf
    状态重新生成掉 —— 结果人脸认证静默失效。所以包维护脚本里的恢复动作
    必须用 robust 模式。
    """
    backup()
    if not robust and profile_available():
        rc, out = _run(["pam-auth-update", "--enable", PROFILE_NAME])
        if rc == 0 and is_enabled():
            return True, "已通过 pam-auth-update 启用"
        return False, f"pam-auth-update 失败(rc={rc}): {out}"
    return _manual_enable()


def disable() -> tuple[bool, str]:
    backup()
    if profile_available():
        rc, out = _run(["pam-auth-update", "--disable", PROFILE_NAME])
        if rc == 0 and not is_enabled():
            return True, "已停用"
    if is_enabled():
        return _manual_disable()
    return True, "本就未启用"


def panic() -> tuple[bool, str]:
    """一键还原：从 PAM 栈移除 + 关闭总开关。用于"人脸出问题进不去系统"。"""
    ok, msg = disable()
    try:
        from . import config as config_mod
        cfg = config_mod.load()
        cfg["enabled"] = False
        config_mod.save(cfg)
    except Exception as e:
        msg += f"（配置关闭失败: {e}）"
    return ok, f"panic 完成：{msg}；总开关已置为 false"


# ---------- 备份与手工注入 ----------

BACKUP_DIR = "/var/backups/faceunlock"


def backup() -> str | None:
    """把 common-auth 备份到 /var/backups/faceunlock/。

    刻意**不**放在 /etc/pam.d/ 里：那个目录下每个文件都会被 PAM 当成一个服务定义，
    塞备份文件进去只会造成困惑（虽然不会被引用）。
    """
    if not os.path.isfile(COMMON_AUTH):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)
    dst = os.path.join(BACKUP_DIR, f"common-auth.{time.strftime('%Y%m%d-%H%M%S')}")
    if not os.path.exists(dst):
        shutil.copy2(COMMON_AUTH, dst)
        # 只保留最近 20 份
        olds = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith("common-auth."))
        for f in olds[:-20]:
            try:
                os.unlink(os.path.join(BACKUP_DIR, f))
            except OSError:
                pass
    return dst


def _manual_enable() -> tuple[bool, str]:
    """把我们的行注入到 pam-auth-update **托管区之外**（托管区之前）。

    位置很关键：实测放在托管区之外的行会被 pam-auth-update 原样保留，
    而放在托管区之内（`# here are the per-package modules` 与
    `# end of pam-auth-update config` 之间）的行会被它按 debconf 状态重写掉。
    common-auth 的头部注释也明确建议把本地模块放在默认块之前或之后。
    """
    try:
        with open(COMMON_AUTH, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        if any("pam_faceunlock.so" in l for l in lines):
            return True, "已启用（本地模块注入）"
        out: list[str] = []
        inserted = False
        for l in lines:
            if not inserted and MANAGED_BEGIN in l:
                out.append(MARK_BEGIN + "\n")
                out.append(MANUAL_LINE)
                out.append(MARK_END + "\n")
                inserted = True
            out.append(l)
        if not inserted:
            # 没有托管区标记（非 Debian 系或文件被改写）时，退回到"插在第一条 auth 之前"
            out = []
            for l in lines:
                if not inserted and l.strip().startswith("auth"):
                    out.append(MARK_BEGIN + "\n")
                    out.append(MANUAL_LINE)
                    out.append(MARK_END + "\n")
                    inserted = True
                out.append(l)
        if not inserted:
            out.append(MARK_BEGIN + "\n")
            out.append(MANUAL_LINE)
            out.append(MARK_END + "\n")
        _write_atomic(COMMON_AUTH, "".join(out))
        return True, "已启用人脸认证（写入托管区之外的本地模块，抗 pam-auth-update 重写）"
    except OSError as e:
        return False, f"写入 {COMMON_AUTH} 失败: {e}"


def _manual_disable() -> tuple[bool, str]:
    try:
        with open(COMMON_AUTH, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        out = [l for l in lines
               if "pam_faceunlock.so" not in l
               and MARK_BEGIN not in l and MARK_END not in l]
        _write_atomic(COMMON_AUTH, "".join(out))
        return True, "已停用人脸认证"
    except OSError as e:
        return False, f"写入 {COMMON_AUTH} 失败: {e}"


def _write_atomic(path: str, content: str) -> None:
    tmp = f"{path}.faceunlock-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
