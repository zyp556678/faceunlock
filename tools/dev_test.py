#!/usr/bin/env python3
"""开发态端到端自测：不触碰 /etc 与 /var/lib，用临时 store + 临时配置跑完整认证链路。

用法:
  tools/dev_test.py seed     用 test_output/smoke_feats.npy 造一份模板
  tools/dev_test.py auth     跑一次真实认证（需要本人坐在摄像头前）
  tools/dev_test.py all      两者都做
"""
from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTROOT = pathlib.Path("/tmp/faceunlock-devtest")

os.environ.setdefault("FACEUNLOCK_STORE", str(TESTROOT / "store"))
os.environ.setdefault("FACEUNLOCK_CONFIG", str(TESTROOT / "config.json"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from faceunlock import config as config_mod  # noqa: E402
from faceunlock.store import TemplateStore  # noqa: E402

USER = os.environ.get("FU_TEST_USER", os.environ.get("USER", "alice"))


def seed() -> int:
    import numpy as np
    feats = ROOT / "test_output" / "smoke_feats.npy"
    if not feats.is_file():
        print(f"缺少 {feats}，请先运行 tools/smoke_test.py --save")
        return 1
    store = TemplateStore()
    store.ensure()
    store.delete_user(USER)
    data = np.load(feats)
    # 取前 5 条作为模板（模拟录入 5 个样本），其余留作验证
    for i, emb in enumerate(data[:5]):
        store.add_face(USER, f"样本{i + 1}", emb, quality={"source": "devtest"})
    cfg = config_mod.load()
    cfg["debug"] = True
    cfg["timeout_ms"] = 6000
    config_mod.save(cfg)
    print(f"已写入 {len(store.faces(USER))} 条模板 -> {store.root}")
    print(f"配置 -> {os.environ['FACEUNLOCK_CONFIG']}")
    return 0


def auth() -> int:
    from faceunlock.auth import authenticate
    res = authenticate(USER, "sudo")
    code_name = {0: "MATCH 匹配成功", 1: "NO_MATCH 不匹配", 2: "NOT_APPLICABLE 不适用"}[res.code]
    print(f"\n结果: {code_name}")
    print(f"  原因   : {res.reason}")
    print(f"  最高分 : {res.score:.4f}")
    print(f"  命中   : {res.hits}")
    print(f"  帧数   : {res.frames}")
    print(f"  耗时   : {res.elapsed_ms:.0f} ms")
    return 0 if res.code in (0, 1, 2) else 1


def main() -> int:
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("seed", "all"):
        rc = seed()
        if rc:
            return rc
    if what in ("auth", "all"):
        return auth()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
