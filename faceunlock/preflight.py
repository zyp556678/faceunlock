"""摄像头可用性的**廉价**预检：只用 os/glob，绝不 import cv2 / numpy。

为什么需要它（Ubuntu 26.04 实测）：

  polkit 127 把 `polkit-1` 服务的 PAM 栈放进一个 socket 激活的沙箱
  （`polkit-agent-helper@.service`）：

      PrivateDevices=yes         -> 私有 /dev，里面根本没有 /dev/video*
      DevicePolicy=strict
      DeviceAllow=/dev/null rw   -> 视频设备被设备 cgroup 拒绝

  于是在 pkexec / 图形界面授权框里，摄像头**必然**打不开。如果不预检，
  每次授权都要先 import OpenCV（几十 MB、本机实测 5~6.5 s CPU）+ 加载
  ONNX 模型 + 尝试打开设备，最后才得出"不适用"——期间 C 模块的 8 秒兜底
  alarm 还会把助手 SIGKILL 掉。用户体验就是：授权框卡住、摄像头灯乱闪、
  连密码都来不及输（详见 README「Ubuntu 26.04 / polkit 127」一节）。

  预检把这条路径从"秒级 + 8 秒兜底"降到**毫秒级**，让密码提示立刻出现。

语义与 auth.py 完全一致：查不出来（异常）时返回 None（不拦），绝不因为
预检本身出错而把用户挡在门外——失败永远是"回退密码"。
"""
from __future__ import annotations

import glob
import os

#: 设备节点模式（V4L2 就是 /dev/video0、/dev/video1 …）
DEVICE_GLOB = "/dev/video*"


def video_device_nodes() -> list[str]:
    """当前可见的 V4L2 设备节点（排序后，便于日志复现）。"""
    try:
        return sorted(glob.glob(DEVICE_GLOB))
    except Exception:
        return []


def device_node(device: int) -> str:
    return f"/dev/video{int(device)}"


def camera_unavailable_reason(device: int = 0) -> str | None:
    """摄像头不可用则返回**给用户看的原因**，可用则返回 None。

    只做"这台机器现在有没有设备节点"这类不需要打开设备的判断：
    真正的占用/驱动故障仍由 camera.py 在打开时判定。
    """
    nodes = video_device_nodes()
    if not nodes:
        return ("/dev 下没有 video* 设备节点（可能是 polkit 沙箱："
                "polkit-agent-helper@.service 的 PrivateDevices=yes；"
                "也可能是摄像头未接或驱动未加载）")
    node = device_node(device)
    if not os.path.exists(node):
        return f"{node} 不存在（当前可见: {', '.join(nodes)}）"
    if not os.access(node, os.R_OK | os.W_OK):
        # 非 root 且不在 video 组、也没有 uaccess ACL 时会走到这里
        return f"{node} 不可读写（当前身份无权限；组 video 或 uaccess ACL 才有）"
    return None
