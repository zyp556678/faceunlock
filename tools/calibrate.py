#!/usr/bin/env python3
"""P1 阈值标定：用真实采集的本人特征 + 公开人脸做冒充者测试，给出登录门限建议。

输入:
  test_output/smoke_feats.npy   本人多帧特征 (真实摄像头采集)
  test_output/negatives/*.jpg   其他身份人脸 (冒充者)
  models/                       YuNet + SFace

输出:
  本人内部(genuine) 与 本人-冒充者(impostor) 两个余弦分布, 以及不同阈值下的
  FAR/FRR 表, 并模拟「照片翻拍/打印」退化后是否仍能通过, 用于量化纯 RGB 的防伪弱点。
"""
from __future__ import annotations

import itertools
import pathlib
import sys

import cv2 as cv
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
DET_MODEL = ROOT / "models" / "face_detection_yunet_2023mar.onnx"
REC_MODEL = ROOT / "models" / "face_recognition_sface_2021dec.onnx"
FEATS = ROOT / "test_output" / "smoke_feats.npy"
NEG_DIR = ROOT / "test_output" / "negatives"


def load_models():
    det = cv.FaceDetectorYN.create(str(DET_MODEL), "", (640, 480), 0.7, 0.3, 5000)
    rec = cv.FaceRecognizerSF.create(str(REC_MODEL), "")
    return det, rec


def cos(rec, a, b) -> float:
    """归一化余弦（match 内部做归一化，裸点积不可用）。"""
    return float(rec.match(a.reshape(1, 128).astype(np.float32),
                           b.reshape(1, 128).astype(np.float32),
                           cv.FaceRecognizerSF_FR_COSINE))


def embed_image(det, rec, img) -> list[np.ndarray]:
    h, w = img.shape[:2]
    det.setInputSize((w, h))
    _, faces = det.detect(img)
    out = []
    if faces is None:
        return out
    for row in faces:
        aligned = rec.alignCrop(img, row)
        out.append(rec.feature(aligned).ravel().astype(np.float32))
    return out


def main() -> int:
    if not FEATS.is_file():
        sys.exit(f"缺少本人特征文件 {FEATS}，请先跑 tools/smoke_test.py --save")
    genuine = np.load(FEATS).astype(np.float32)
    print(f"本人样本(genuine): {genuine.shape[0]} 条 128 维特征")

    det, rec = load_models()
    impostors: dict[str, list[np.ndarray]] = {}
    for p in sorted(NEG_DIR.glob("*.jpg")):
        img = cv.imread(str(p))
        if img is None:
            print(f"  跳过无法解码: {p.name}")
            continue
        feats = embed_image(det, rec, img)
        if feats:
            impostors[p.name] = feats
        print(f"  冒充者 {p.name:16s} 检出 {len(feats)} 张人脸")

    imp = [f for v in impostors.values() for f in v]
    if not imp:
        sys.exit("没有可用的冒充者样本")

    gen_pairs = [cos(rec, a, b) for a, b in itertools.combinations(genuine, 2)]
    imp_pairs = [cos(rec, g, i) for g in genuine for i in imp]
    gen_pairs = np.array(gen_pairs)
    imp_pairs = np.array(imp_pairs)

    def stat(name, x):
        print(f"  {name:10s} n={len(x):5d}  均值 {x.mean():.4f}  "
              f"标准差 {x.std():.4f}  最小 {x.min():.4f}  最大 {x.max():.4f}")

    print("\n=== 余弦相似度分布 ===")
    stat("genuine", gen_pairs)
    stat("impostor", imp_pairs)
    gap = gen_pairs.min() - imp_pairs.max()
    print(f"  类间距 (genuine.min - impostor.max) = {gap:+.4f}  "
          f"{'可分' if gap > 0 else '存在重叠, 需靠阈值折衷'}")

    print("\n=== 阈值扫描 ===")
    print(f"  {'阈值':>6} {'FAR(误接受)':>12} {'FRR(误拒绝)':>12}")
    best = None
    for t in np.arange(0.30, 0.86, 0.05):
        far = float((imp_pairs >= t).mean())
        frr = float((gen_pairs < t).mean())
        mark = ""
        if far == 0.0 and best is None:
            best = t
            mark = "  <= FAR=0 的最小阈值"
        print(f"  {t:6.2f} {far:12.4f} {frr:12.4f}{mark}")

    rec_t = float(best if best is not None else 0.5)
    margin = float(imp_pairs.max()) + 0.05
    rec_t = max(rec_t, margin, 0.5)
    print(f"\n  建议登录阈值: {rec_t:.2f}  "
          f"(取 FAR=0 阈值与 impostor 最大值+0.05、以及 0.50 三者的较大值)")
    print(f"  对应 FRR ≈ {float((gen_pairs < rec_t).mean()):.4f}")

    print("\n=== 照片翻拍/打印退化模拟 (纯 RGB 防伪弱点量化) ===")
    print("  对公开人脸做「缩放到人脸约 90px + 高斯模糊 + JPEG 重压缩」模拟翻拍:")
    for name, feats in impostors.items():
        img = cv.imread(str(NEG_DIR / name))
        h, w = img.shape[:2]
        det.setInputSize((w, h))
        _, faces = det.detect(img)
        if faces is None or len(faces) == 0:
            continue
        row = max(faces, key=lambda r: float(r[2]) * float(r[3]))
        x, y, bw, bh = (int(v) for v in row[:4])
        pad = int(0.3 * max(bw, bh))
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(w, x + bw + pad), min(h, y + bh + pad)
        crop = img[y0:y1, x0:x1]
        if crop.size == 0:
            continue
        small = cv.resize(crop, (90, 90), interpolation=cv.INTER_AREA)
        small = cv.GaussianBlur(small, (5, 5), 1.2)
        ok, buf = cv.imencode(".jpg", small, [int(cv.IMWRITE_JPEG_QUALITY), 45])
        if not ok:
            continue
        deg = cv.imdecode(buf, cv.IMREAD_COLOR)
        canvas = np.full_like(crop, 128)
        ch, cw = crop.shape[:2]
        canvas[(ch - 90) // 2:(ch - 90) // 2 + 90, (cw - 90) // 2:(cw - 90) // 2 + 90] = deg
        f2 = embed_image(det, rec, canvas)
        if not f2:
            print(f"  {name:16s} 退化后未检出人脸（该情形下会被拒绝，属安全侧）")
            continue
        s = cos(rec, feats[0], f2[0])
        verdict = "仍会通过 ← 风险" if s >= rec_t else "会被拒绝"
        print(f"  {name:16s} 退化前 {cos(rec, feats[0], feats[0]):.4f} -> "
              f"退化后 {s:.4f}  [{verdict}]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
