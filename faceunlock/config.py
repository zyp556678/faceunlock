"""全局配置：/etc/faceunlock/config.json（root 0600，不可被普通用户写入）。

配置只影响行为，不参与"信任决策"：普通用户无法写入此文件，因此阈值/服务开关
不会被非特权进程放宽。认证路径对它**只读**。
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from typing import Any

from . import CONFIG_FILE

DEFAULTS: dict[str, Any] = {
    "version": 1,
    # 总开关：false 时 PAM 助手立即返回"不适用"，等于彻底停用人脸
    "enabled": True,
    # 余弦相似度门限。本机实测（8 个姿态多样样本 + 6 个不同身份的冒充者）：
    #   坐正对屏幕：本人 0.86~0.96
    #   偏头/后仰等坐姿不佳时：本人 0.44~0.49（此时应尽量放行，否则用户老要输密码）
    #   6 个不同身份的冒充者最高：0.092
    # 取 0.40：能覆盖"坐姿不佳"那一档，同时对冒充者仍留 4.3 倍余量。
    # 注意纯 RGB 无法防照片翻拍（实测翻拍图能到 0.6~0.73），那是硬件限制，
    # 靠调高阈值解决不了，只能靠"密码兜底 + 不要用于高价值目标"。
    "threshold": 0.40,
    # 单次认证最长等待（毫秒）
    "timeout_ms": 4000,
    # 人脸完全没出现时的提前退出（毫秒）：避免没人在镜头前时白等 4 秒才弹密码框
    "no_face_timeout_ms": 1000,
    # 多帧投票：window_frames 帧内至少 required_frames 帧命中才判定通过
    "required_frames": 2,
    "window_frames": 3,
    # 场景开关。PAM 服务名 -> 是否启用人脸
    "services": {
        "gdm-password": True,   # GDM 登录界面 + GNOME 锁屏（锁屏走同一个 PAM 服务）
        "sudo": True,
        "sudo-i": True,
        "su": True,
        "polkit-1": True,       # pkexec / 图形界面管理员弹窗
        "login": False,         # 控制台 tty 登录
    },
    "rate_limit": {
        "max_failures": 5,
        "window_s": 300,
        "cooldown_s": 120,
    },
    "camera": {
        "device": 0,
        "width": 1280,
        "height": 720,
        "warmup_frames": 4,
    },
    # 是否保存人脸缩略图（供管理界面显示）。只存 128 维特征时不落任何图像。
    "save_thumbnails": True,
    # 认证路径诊断日志（写 syslog/journal）
    "debug": False,
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: str | None = None) -> dict[str, Any]:
    """读取配置；文件不存在或损坏时回落到默认值（绝不因配置问题阻断登录）。"""
    p = path or CONFIG_FILE
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("config root must be an object")
        return _deep_merge(DEFAULTS, data)
    except FileNotFoundError:
        return copy.deepcopy(DEFAULTS)
    except Exception:
        return copy.deepcopy(DEFAULTS)


def save(cfg: dict[str, Any], path: str | None = None) -> None:
    """原子写入（0600）。"""
    p = path or CONFIG_FILE
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def service_enabled(cfg: dict[str, Any], service: str) -> bool:
    if not cfg.get("enabled", True):
        return False
    return bool(cfg.get("services", {}).get(service, False))
