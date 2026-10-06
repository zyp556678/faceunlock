#!/bin/bash
# 获取本项目不入库的两个大体积依赖：ONNX 模型 + 随包分发的 OpenCV。
#
# 为什么需要这个脚本：仓库为保持轻量，把以下内容排除在版本控制之外
#   * models/*.onnx                38 MB（YuNet + SFace）
#   * vendor/                     127 MB（opencv-python-headless 4.11 解包结果）
# 详见 .gitignore 与 README「获取依赖」。
#
# 用法:
#   scripts/fetch-deps.sh            只取缺失的部分
#   scripts/fetch-deps.sh --force    全部重新下载
#   scripts/fetch-deps.sh --models   只取模型
#   scripts/fetch-deps.sh --vendor   只取 OpenCV
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")/.."

OPENCV_VERSION="4.11.0.86"
# opencv_zoo 的模型走 git-LFS。raw.githubusercontent.com 只会返回 131 字节的
# LFS 指针文件，必须用 media.githubusercontent.com（踩过的坑，见 README）。
ZOO_BASE="https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models"
DETECTOR="face_detection_yunet_2023mar.onnx"
RECOGNIZER="face_recognition_sface_2021dec.onnx"

force=0
do_models=1
do_vendor=1
for arg in "$@"; do
    case "$arg" in
        --force)  force=1 ;;
        --models) do_vendor=0 ;;
        --vendor) do_models=0 ;;
        -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
        *) echo "未知参数: $arg（-h 看用法）" >&2; exit 2 ;;
    esac
done

# ONNX 模型文件头必须是 "ONNX" 魔数；LFS 指针文件是纯文本，用这个判据拦截。
verify_onnx() {
    local f="$1"
    [ -s "$f" ] || return 1
    [ "$(head -c 4 "$f" | tr -d '\0')" = "ONNX" ]
}

fetch_models() {
    echo "==> 模型 ($ZOO_BASE)"
    install -d models
    local name url dest
    for name in "$DETECTOR" "$RECOGNIZER"; do
        case "$name" in
            "$DETECTOR")   url="$ZOO_BASE/face_detection_yunet/$name" ;;
            "$RECOGNIZER") url="$ZOO_BASE/face_recognition_sface/$name" ;;
        esac
        dest="models/$name"
        if [ "$force" -eq 0 ] && verify_onnx "$dest"; then
            echo "    已存在，跳过: $dest ($(du -h "$dest" | cut -f1))"
            continue
        fi
        echo "    下载 $name ..."
        curl -fL --retry 3 --progress-bar -o "$dest.tmp" "$url"
        if ! verify_onnx "$dest.tmp"; then
            rm -f "$dest.tmp"
            echo "❌ $name 不是有效的 ONNX 文件。" >&2
            echo "   多半是拿到了 git-LFS 指针（131 字节文本）。" >&2
            echo "   确认 URL 里是 media.githubusercontent.com/media/... 而不是 raw.githubusercontent.com。" >&2
            exit 1
        fi
        mv "$dest.tmp" "$dest"
        echo "    ✓ $dest ($(du -h "$dest" | cut -f1))"
    done
}

fetch_vendor() {
    echo "==> OpenCV $OPENCV_VERSION (opencv-python-headless)"
    if [ "$force" -eq 0 ] && [ -d vendor/cv2 ]; then
        echo "    vendor/cv2 已存在，跳过（--force 可强制重装）"
        return 0
    fi

    local py
    py="$(command -v python3)" || { echo "❌ 需要 python3" >&2; exit 1; }

    # 优先用 pip download（不需要 root，也不用碰系统环境）
    if "$py" -m pip --version >/dev/null 2>&1; then
        echo "    用 pip 下载 wheel（不安装到系统）"
        rm -rf vendor-wheels && install -d vendor-wheels
        "$py" -m pip download --no-deps --only-binary=:all: \
            --platform manylinux2014_x86_64 --python-version 3.12 \
            --implementation cp --dest vendor-wheels \
            "opencv-python-headless==$OPENCV_VERSION"
    else
        echo "    没有 pip，改用 PyPI 直接取 wheel"
        # 轮询 PyPI JSON API 找到 manylinux x86_64 的那个 wheel
        install -d vendor-wheels
        local url
        url="$("$py" - <<'PY'
import json, sys, urllib.request
from urllib.error import URLError
try:
    with urllib.request.urlopen(
            "https://pypi.org/pypi/opencv-python-headless/4.11.0.86/json", timeout=30) as r:
        data = json.load(r)
except (URLError, OSError) as e:
    sys.exit(f"无法访问 PyPI: {e}")
for f in data["urls"]:
    n = f["filename"]
    if "manylinux" in n and "x86_64" in n and n.endswith(".whl"):
        print(f["url"]); break
else:
    sys.exit("没有找到 manylinux x86_64 的 wheel")
PY
)"
        echo "    下载 $(basename "$url")"
        curl -fL --retry 3 --progress-bar -o "vendor-wheels/$(basename "$url")" "$url"
    fi

    local whl
    whl="$(ls vendor-wheels/opencv_python_headless-*.whl 2>/dev/null | head -1)"
    [ -n "$whl" ] || { echo "❌ vendor-wheels 里没有 wheel" >&2; exit 1; }

    echo "    解包 $(basename "$whl") -> vendor/"
    rm -rf vendor && install -d vendor
    "$py" - "$whl" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
    z.extractall("vendor")
PY
    # 只保留运行时需要的：cv2 包 + 它私有加载的 .libs 第三方 .so
    rm -rf vendor/*.dist-info
    find vendor -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    echo "    ✓ vendor/ ($(du -sh vendor | cut -f1))"
}

[ "$do_models" -eq 1 ] && fetch_models
[ "$do_vendor" -eq 1 ] && fetch_vendor

echo
echo "完成。下一步: make && sudo faceunlock doctor"
