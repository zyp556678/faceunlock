#!/usr/bin/env python3
"""光照鲁棒性对照实验：评估 CLAHE 预处理对"本人/冒充者"区分度的影响。

背景：傍晚光照下，同一人的相似度从 0.75 掉到 0.37，而冒充者上限是 0.14。
需要在**当前真实光照**下重新标定，并检验 CLAHE（限制对比度自适应直方图均衡）
能否把光照变化的影响压下去。

方法：
  * 摄像头连拍 30 帧：前 10 帧当"录入集"，后 20 帧当"探针集"（同一人、不同时刻）
  * 冒充者用 test_output/negatives/ 里的公开人脸
  * 分别在「原始对齐图」和「CLAHE 对齐图」下计算余弦，比较类间可分性
"""
from __future__ import annotations

import pathlib
import sys
import time

import cv2 as cv
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from faceunlock.camera import Camera  # noqa: E402
from faceunlock.engine import FaceEngine  # noqa: E402


def clahe_aligned(aligned: np.ndarray, clip: float = 2.0, grid: int = 8) -> np.ndarray:
    """对已对齐的 112x112 BGR 人脸做光照归一化（在 LAB 的 L 通道上做 CLAHE）。"""
    lab = cv.cvtColor(aligned, cv.COLOR_BGR2LAB)
    l, a, b = cv.split(lab)
    cl = cv.createCLAHE(clipLimit=clip, tileGridSize=(grid, grid))
    l2 = cl.apply(l)
    return cv.cvtColor(cv.merge([l2, a, b]), cv.COLOR_LAB2BGR)


def main() -> int:
    engine = FaceEngine()
    cam = Camera(warmup=6)
    cam.open()

    raw_enroll, clahe_enroll = [], []
    raw_probe, clahe_probe = [], []
    stats = []
    print("采集 30 帧中（请保持自然坐姿）...")
    n = 0
    deadline = time.monotonic() + 25
    while n < 30 and time.monotonic() < deadline:
        frame = cam.read()
        if frame is None:
            time.sleep(0.02)
            continue
        face = engine.largest(engine.detect(frame))
        if face is None:
            continue
        aligned = engine._recognizer.alignCrop(frame, face.row.reshape(1, -1))
        q = engine.quality(frame, face)
        if n < 10:
            raw_enroll.append(engine.embed_aligned(aligned))
            clahe_enroll.append(engine.embed_aligned(clahe_aligned(aligned)))
        else:
            raw_probe.append(engine.embed_aligned(aligned))
            clahe_probe.append(engine.embed_aligned(clahe_aligned(aligned)))
        stats.append((q["height"], q["sharpness"], q["brightness"]))
        n += 1
        time.sleep(0.08)
    cam.close()

    st = np.array(stats)
    print(f"\n=== 当前成像条件（{n} 帧）===")
    print(f"  人脸高度 : 均值 {st[:,0].mean():6.1f} px  范围 {st[:,0].min():.0f}~{st[:,0].max():.0f}")
    print(f"  清晰度   : 均值 {st[:,1].mean():6.1f}      范围 {st[:,1].min():.0f}~{st[:,1].max():.0f}")
    print(f"  亮度     : 均值 {st[:,2].mean():6.1f}      范围 {st[:,2].min():.0f}~{st[:,2].max():.0f}")

    if len(raw_enroll) < 3 or len(raw_probe) < 3:
        print("\n样本不足，无法标定（人脸太小或没检出）")
        return 1

    # 冒充者
    imp_raw, imp_clahe = [], []
    for p in sorted((ROOT / "test_output" / "negatives").glob("*.jpg")):
        img = cv.imread(str(p))
        if img is None:
            continue
        for face, _ in engine.analyze(img, score_threshold=0.6):
            aligned = engine._recognizer.alignCrop(img, face.row.reshape(1, -1))
            imp_raw.append(engine.embed_aligned(aligned))
            imp_clahe.append(engine.embed_aligned(clahe_aligned(aligned)))

    def pair_stats(enroll, probe):
        g = [engine.similarity(e, p) for e in enroll for p in probe]
        return np.array(g)

    def imp_stats(enroll, imps):
        return np.array([engine.similarity(e, i) for e in enroll for i in imps])

    print("\n=== 区分度对比 ===")
    print(f"  {'模式':10s} {'genuine(min)':>14s} {'impostor(max)':>15s} {'类间距':>10s} {'建议阈值':>10s}")
    results = {}
    for name, en, pr, im in (
        ("原始", raw_enroll, raw_probe, imp_raw),
        ("CLAHE", clahe_enroll, clahe_probe, imp_clahe),
    ):
        gen = pair_stats(en, pr)
        imp = imp_stats(en, im)
        gap = gen.min() - imp.max()
        # 建议阈值 = 冒充者最大值 + 0.05，但不低于 0.30
        thr = max(0.30, float(imp.max()) + 0.05)
        results[name] = (gen, imp, thr)
        print(f"  {name:10s} {gen.min():14.4f} {imp.max():15.4f} {gap:+10.4f} {thr:10.2f}")

    for name, (gen, imp, thr) in results.items():
        far = float((imp >= thr).mean())
        frr = float((gen < thr).mean())
        print(f"    {name}: 阈值 {thr:.2f} -> FAR={far:.4f} FRR={frr:.4f}")

    gen_r = results["原始"][0].mean()
    gen_c = results["CLAHE"][0].mean()
    if gen_c > gen_r + 0.02:
        print(f"\n  结论: CLAHE 明显改善本人相似度（{gen_r:.3f} -> {gen_c:.3f}），建议启用")
    elif gen_c > gen_r:
        print(f"\n  结论: CLAHE 略有改善（{gen_r:.3f} -> {gen_c:.3f}）")
    else:
        print(f"\n  结论: CLAHE 无改善（{gen_r:.3f} -> {gen_c:.3f}），不启用，"
              f"问题更可能是距离/姿态而非光照")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
