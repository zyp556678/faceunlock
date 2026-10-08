"""faceunlock —— 本机人脸识别登录（PAM 集成 + GTK/Tauri 管理界面）。

包被安装到 /usr/lib/faceunlock/faceunlock/，运行时依赖（opencv）放在
/usr/lib/faceunlock/vendor/。开发态两者同构：<repo>/faceunlock/ 与 <repo>/vendor/。
"""
from __future__ import annotations

import os
import sys

__version__ = "0.1.3"

#: 安装根目录（开发态即仓库根目录）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 随包分发的 opencv，必须在 import cv2 之前加入 sys.path
VENDOR_DIR = os.environ.get("FACEUNLOCK_VENDOR", os.path.join(BASE_DIR, "vendor"))
if os.path.isdir(VENDOR_DIR) and VENDOR_DIR not in sys.path:
    sys.path.insert(0, VENDOR_DIR)

#: 模型目录：环境变量 > 系统安装路径 > 开发态仓库路径
_MODEL_CANDIDATES = [
    os.environ.get("FACEUNLOCK_MODELS", ""),
    "/usr/share/faceunlock/models",
    os.path.join(BASE_DIR, "models"),
]


def models_dir() -> str:
    for c in _MODEL_CANDIDATES:
        if c and os.path.isdir(c):
            return c
    return os.path.join(BASE_DIR, "models")


DETECTOR_MODEL = "face_detection_yunet_2023mar.onnx"
RECOGNIZER_MODEL = "face_recognition_sface_2021dec.onnx"

#: 模型标识写入模板，换模型即要求重新录入
MODEL_ID = "yunet2023mar+sface2021dec/opencv4.11"

#: 运行时状态目录（root 0700）
STORE_DIR = os.environ.get("FACEUNLOCK_STORE", "/var/lib/faceunlock")
CONFIG_FILE = os.environ.get("FACEUNLOCK_CONFIG", "/etc/faceunlock/config.json")

#: PAM 助手退出码约定
EXIT_MATCH = 0        # 认证成功
EXIT_NO_MATCH = 1     # 明确不匹配 -> PAM_AUTH_ERR，落到下一个模块（通常是密码）
EXIT_NOT_APPLICABLE = 2  # 未录入/未启用/无摄像头/忙/远程/冷却中 -> PAM_IGNORE
