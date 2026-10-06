#!/usr/bin/env python3
"""P0 烟测：验证 apt 版 OpenCV + YuNet(2023mar) + SFace(2021dec) 全链路可用。

验证项：
  1. cv2 版本与 FaceDetectorYN / FaceRecognizerSF API 是否存在
  2. 4.6.0 能否加载 YuNet v2 时代(2023mar)模型  ← 本方案最大技术风险
  3. 摄像头取流（V4L2 后端）
  4. 检测 -> 对齐 -> 128 维特征 全链路与单帧耗时
  5. 同一人两帧之间的余弦相似度（自比对基线）

用法: python3 tools/smoke_test.py [--camera 0] [--save]
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

import cv2 as cv
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
DET_MODEL = ROOT / "models" / "face_detection_yunet_2023mar.onnx"
REC_MODEL = ROOT / "models" / "face_recognition_sface_2021dec.onnx"
OUT_DIR = ROOT / "test_output"

CAP_W, CAP_H = 1280, 720
DET_W, DET_H = 640, 480


def step(msg: str) -> None:
    print(f"\n=== {msg} ===", flush=True)


def check_models() -> None:
    step("1. 模型文件")
    for p in (DET_MODEL, REC_MODEL):
        if not p.is_file():
            sys.exit(f"缺少模型文件: {p}")
        print(f"  OK  {p.name:48s} {p.stat().st_size:>10,} bytes")


def check_api() -> None:
    step("2. OpenCV API 可用性")
    print(f"  cv2.__version__ = {cv.__version__}")
    checks = [
        "FaceDetectorYN_create",
        "FaceRecognizerSF_create",
        "FaceRecognizerSF_FR_COSINE",
        "FaceRecognizerSF_FR_NORM_L2",
    ]
    missing = [c for c in checks if not hasattr(cv, c)]
    for c in checks:
        print(f"  {'OK ' if hasattr(cv, c) else 'MISSING'} {c}")
    if missing:
        sys.exit(f"OpenCV 缺少必要 API: {missing} —— 需回退到 pip 版 opencv(清华镜像)")
    print(f"  cv2.CAP_V4L2 = {getattr(cv, 'CAP_V4L2', 'N/A')}")


def build_models():
    step("3. 加载 ONNX 模型 (风险点: 4.6.0 vs 2023mar v2 模型)")
    t0 = time.perf_counter()
    try:
        detector = cv.FaceDetectorYN.create(
            str(DET_MODEL), "", (DET_W, DET_H), 0.9, 0.3, 5000
        )
    except cv.error as e:
        sys.exit(f"FaceDetectorYN 加载失败: {e}")
    t1 = time.perf_counter()
    try:
        recognizer = cv.FaceRecognizerSF.create(str(REC_MODEL), "")
    except cv.error as e:
        sys.exit(f"FaceRecognizerSF 加载失败: {e}")
    t2 = time.perf_counter()
    print(f"  OK  FaceDetectorYN  加载耗时 {(t1 - t0) * 1000:.1f} ms")
    print(f"  OK  FaceRecognizerSF 加载耗时 {(t2 - t1) * 1000:.1f} ms")
    return detector, recognizer


def open_camera(index: int) -> cv.VideoCapture:
    step("4. 摄像头取流")
    cap = cv.VideoCapture(index, cv.CAP_V4L2)
    if not cap.isOpened():
        sys.exit(f"无法打开摄像头 /dev/video{index}")
    cap.set(cv.CAP_PROP_FOURCC, cv.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv.CAP_PROP_FRAME_WIDTH, CAP_W)
    cap.set(cv.CAP_PROP_FRAME_HEIGHT, CAP_H)
    ok, frame = False, None
    for _ in range(15):  # 预热，UVC 前几帧常为曝光未收敛
        ok, frame = cap.read()
        if ok:
            time.sleep(0.03)
    if not ok or frame is None:
        sys.exit("摄像头打开但读不到帧")
    h, w = frame.shape[:2]
    print(f"  OK  分辨率 {w}x{h}  fourcc={int(cap.get(cv.CAP_PROP_FOURCC)):#x}")
    return cap


def detect(detector, frame, size=(DET_W, DET_H)):
    detector.setInputSize(size)
    _, faces = detector.detect(frame)
    return faces


def embed(recognizer, frame, face_row):
    """face_row 必须是原始 float 行（Nx15），不能转 int，否则 alignCrop 会失败。"""
    aligned = recognizer.alignCrop(frame, face_row)
    feat = recognizer.feature(aligned)
    return aligned, feat


def cosine(recognizer, f1: np.ndarray, f2: np.ndarray) -> float:
    """SFace 余弦相似度。

    注意: OpenCV 4.11 的 recognizer.feature() 返回**未归一化**向量(实测 L2 范数 ~10.3),
    因此绝不能用裸点积当余弦。match(FR_COSINE) 内部会做归一化, 这里以它为准,
    并用归一化后的手算点积做交叉校验。
    """
    return float(recognizer.match(f1, f2, cv.FaceRecognizerSF_FR_COSINE))


def manual_cosine(f1: np.ndarray, f2: np.ndarray) -> float:
    a = f1.ravel() / np.linalg.norm(f1)
    b = f2.ravel() / np.linalg.norm(f2)
    return float(np.dot(a, b))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--save", action="store_true", help="保存测试帧到 test_output/")
    ap.add_argument("--frames", type=int, default=30, help="测速帧数")
    args = ap.parse_args()

    check_models()
    check_api()
    detector, recognizer = build_models()
    cap = open_camera(args.camera)

    step("5. 检测 + 特征 全链路")
    feats: list[np.ndarray] = []
    det_times, all_times = [], []
    annotated = None
    for i in range(args.frames):
        ok, frame = cap.read()
        if not ok:
            continue
        small = cv.resize(frame, (DET_W, DET_H), interpolation=cv.INTER_AREA)
        t0 = time.perf_counter()
        faces = detect(detector, small)
        t1 = time.perf_counter()
        det_times.append((t1 - t0) * 1000)

        if faces is not None and len(faces) > 0:
            # 取面积最大的人脸；注意对齐要在全分辨率帧上做，精度更高
            detector.setInputSize((frame.shape[1], frame.shape[0]))
            _, full_faces = detector.detect(frame)
            t2 = time.perf_counter()
            all_times.append((t2 - t0) * 1000)
            if full_faces is not None and len(full_faces) > 0:
                row = max(full_faces, key=lambda r: float(r[2]) * float(r[3]))
                aligned, feat = embed(recognizer, frame, row)
                feats.append(feat.ravel().copy())
                if annotated is None:
                    annotated = frame.copy()
                    x, y, w, h = (int(v) for v in row[:4])
                    cv.rectangle(annotated, (x, y), (x + w, y + h), (0, 255, 0), 2)
                    for px, py in row[4:14].reshape(-1, 2):
                        cv.circle(annotated, (int(px), int(py)), 2, (0, 0, 255), -1)
                    cv.putText(annotated, f"score={row[14]:.3f}", (x, max(20, y - 8)),
                               cv.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                if len(feats) == 1:
                    print(f"  首帧: 检测到 {len(full_faces)} 张人脸, "
                          f"bbox=({x},{y},{w},{h}), 置信度={row[14]:.4f}")
                    print(f"  特征维度={feat.shape}, L2范数={np.linalg.norm(feat):.4f}, "
                          f"aligned={aligned.shape}")

    if not det_times:
        sys.exit("一帧都没读到")
    step("6. 性能")
    print(f"  检测(640x480)  均值 {np.mean(det_times):6.1f} ms   "
          f"最小 {np.min(det_times):6.1f} ms   最大 {np.max(det_times):6.1f} ms")
    if all_times:
        print(f"  检出+特征(全分辨率) 均值 {np.mean(all_times):6.1f} ms")
        print(f"  理论帧率 ~{1000.0 / np.mean(all_times):.1f} fps (单线程)")
    print(f"  有效检出帧数: {len(feats)}/{args.frames}")

    step("7. 自比对基线 (同一人不同帧的余弦相似度)")
    if len(feats) >= 2:
        sims = [cosine(recognizer, feats[0], feats[i])
                for i in range(1, min(len(feats), 15))]
        chk = [manual_cosine(feats[0], feats[i])
               for i in range(1, min(len(feats), 15))]
        print(f"  样本数 {len(sims)}  均值 {np.mean(sims):.4f}  "
              f"最小 {np.min(sims):.4f}  最大 {np.max(sims):.4f}")
        print(f"  交叉校验(手动归一化点积) 均值 {np.mean(chk):.4f} "
              f"最大偏差 {np.max(np.abs(np.array(sims) - np.array(chk))):.2e}")
        print(f"  特征 L2 范数 {np.linalg.norm(feats[0]):.4f} (未归一化, 故不可直接用点积)")
        print(f"  官方 LFW 工作点阈值 0.363；建议登录门限 0.50")
        print(f"  当前均值 {'>= 0.50 可分性好' if np.mean(sims) >= 0.5 else '< 0.50 需复查光照/姿态'}")
    else:
        print("  未采集到足够人脸帧，请确认坐在摄像头前并正对镜头后重跑")

    if args.save:
        OUT_DIR.mkdir(exist_ok=True)
        if annotated is not None:
            p = OUT_DIR / "smoke_annotated.jpg"
            cv.imwrite(str(p), annotated)
            print(f"\n  已保存标注图: {p}")
        if feats:
            np.save(OUT_DIR / "smoke_feats.npy", np.stack(feats))
            print(f"  已保存特征:   {OUT_DIR / 'smoke_feats.npy'} ({len(feats)} 条)")
    cap.release()
    print("\n烟测通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
