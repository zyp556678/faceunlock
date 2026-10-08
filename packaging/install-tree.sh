#!/bin/bash
# 把工程内容安装到某个 debian 打包目录。
#
# 为什么单独抽成脚本：现在有三个二进制包共用同一批内容——
#   faceunlock       核心（PAM + 助手 + CLI + 模型 + 随包 OpenCV）
#   faceunlock-gui   图形界面
#   faceunlock-full  自包含完整包（核心 + 图形界面）
# 如果把这堆 install 命令在 debian/rules 里抄三遍，迟早改一处漏两处。
#
# 用法: packaging/install-tree.sh <core|gui|all> <目标目录>
set -euo pipefail

mode="${1:?用法: install-tree.sh <core|gui|all> <目标目录>}"
dest="${2:?缺少目标目录}"

cd "$(dirname "$(readlink -f "$0")")/.."          # 仓库根
ARCH_TRIPLET="${ARCH_TRIPLET:-$(dpkg-architecture -qDEB_HOST_MULTIARCH)}"

install_core() {
    local d="$1"
    # --- Python 包 + 随包分发的 opencv ---
    install -d "$d/usr/lib/faceunlock"
    cp -a faceunlock "$d/usr/lib/faceunlock/"
    cp -a vendor     "$d/usr/lib/faceunlock/"
    find "$d/usr/lib/faceunlock" -name '__pycache__' -type d -prune -exec rm -rf {} + || true

    # 权限归一化：`cp -a` 会把开发机上的权限原样抄进包里（只受 umask/编辑器影响）。
    # 踩过的坑：新增的 .py 在开发机上生成时是 0600，装完普通用户 import 直接
    # PermissionError —— `faceunlock polkit-camera status` 就是这么崩的。
    # 统一成目录 0755 / 文件 0644；vendor 里只有 .so/.pyi，0644 足够（dlopen 不需要 x 位）。
    find "$d/usr/lib/faceunlock" -type d -exec chmod 0755 {} +
    find "$d/usr/lib/faceunlock" -type f -exec chmod 0644 {} +

    # --- 模型（拒绝 0 字节残留，踩过一次）---
    install -d "$d/usr/share/faceunlock/models"
    local m
    for m in models/*.onnx; do
        [ -s "$m" ] || { echo "ERROR: $m 是空文件，拒绝打包" >&2; exit 1; }
    done
    install -m 0644 models/*.onnx "$d/usr/share/faceunlock/models/"

    # --- 可执行文件 ---
    install -d "$d/usr/bin" "$d/usr/libexec"
    install -m 0755 bin/faceunlock       "$d/usr/bin/faceunlock"
    install -m 0755 bin/faceunlock-auth  "$d/usr/libexec/faceunlock-auth"
    install -m 0755 bin/faceunlock-admin "$d/usr/libexec/faceunlock-admin"
    if [ -f bin/faceunlock-gui-bridge ]; then
        install -m 0755 bin/faceunlock-gui-bridge "$d/usr/libexec/faceunlock-gui-bridge"
    fi

    # --- PAM 模块 + pam-configs profile ---
    install -d "$d/usr/lib/$ARCH_TRIPLET/security"
    install -m 0644 pam/pam_faceunlock.so \
        "$d/usr/lib/$ARCH_TRIPLET/security/pam_faceunlock.so"
    install -d "$d/usr/share/pam-configs"
    install -m 0644 pam/faceunlock.pam-configs "$d/usr/share/pam-configs/faceunlock"

    # --- polkit 动作 ---
    install -d "$d/usr/share/polkit-1/actions"
    install -m 0644 packaging/org.faceunlock.manage.policy \
        "$d/usr/share/polkit-1/actions/org.faceunlock.manage.policy"

    # --- polkit 授权框的摄像头权限（Ubuntu 26.04 / polkit 127 沙箱）---
    # 两处：
    #  1) /usr/share/faceunlock/...  权威正文，faceunlock polkit-camera on 用它重写；
    #  2) /etc/systemd/system/polkit-agent-helper@.service.d/...  生效的 drop-in。
    #     drop-in 默认随包安装；`faceunlock polkit-camera off` / `faceunlock panic`
    #     会删掉它，把 polkit 恢复成上游强沙箱（授权框只认密码）。
    #     它是 conffile：用户删掉后，同版本的升级不会把它偷偷装回来。
    install -m 0644 "packaging/polkit-agent-helper@.service.d/10-faceunlock-camera.conf" \
        "$d/usr/share/faceunlock/polkit-agent-helper-camera.conf"
    install -D -m 0644 "packaging/polkit-agent-helper@.service.d/10-faceunlock-camera.conf" \
        "$d/etc/systemd/system/polkit-agent-helper@.service.d/10-faceunlock-camera.conf"

    # --- 默认配置（conffile）---
    install -d "$d/etc/faceunlock"
    install -m 0644 packaging/config.json "$d/etc/faceunlock/config.json"
}

install_gui() {
    local d="$1"
    local bin=gui/src-tauri/target/release/faceunlock-gui
    if [ ! -x "$bin" ]; then
        echo "ERROR: 缺少 Tauri 产物 $bin" >&2
        echo "       请先在 gui/ 下执行: npm install && npm run tauri build -- --no-bundle" >&2
        exit 1
    fi
    install -D -m0755 "$bin" "$d/usr/bin/faceunlock-gui"
    install -D -m0644 packaging/faceunlock.desktop \
        "$d/usr/share/applications/faceunlock.desktop"
    # 图标必须放进与实际像素尺寸一致的 hicolor 子目录，否则 lintian 报
    # icon-size-and-directory-name-mismatch。Tauri 的 128x128@2x.png 正好 256x256。
    install -D -m0644 gui/src-tauri/icons/128x128@2x.png \
        "$d/usr/share/icons/hicolor/256x256/apps/faceunlock.png"
    install -D -m0644 gui/src-tauri/icons/icon.png \
        "$d/usr/share/icons/hicolor/512x512/apps/faceunlock.png"
    install -D -m0644 gui/src-tauri/icons/128x128.png \
        "$d/usr/share/icons/hicolor/128x128/apps/faceunlock.png"
    install -D -m0644 gui/src-tauri/icons/32x32.png \
        "$d/usr/share/pixmaps/faceunlock.png"
}

install -d "$dest"
case "$mode" in
    core) install_core "$dest" ;;
    gui)  install_gui  "$dest" ;;
    all)  install_core "$dest"; install_gui "$dest" ;;
    *)    echo "ERROR: 未知模式 '$mode'（应为 core|gui|all）" >&2; exit 2 ;;
esac
