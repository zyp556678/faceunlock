#!/usr/bin/env python3
"""安全属性自测：验证不能靠"看着没问题"的那几条性质。

  A. 模板 HMAC 篡改必须被拒绝（且认证侧视为"不适用"而非放行）
  B. 限速：连续失败达阈值后进入冷却，冷却期内直接回退密码
  C. 特权层拒绝非法配置（阈值越界、非法服务名）
  D. 越权：普通用户不能管理他人的人脸
  E. 特征输入校验：维度错误/NaN/零向量必须被拒
  F. 路径穿越：非法用户名不能构造出模板文件路径
  G. 无摄像头 / 无人脸时的回退时延（必须快速回退密码）
  H. 只读模板库（polkit 127 的 ProtectSystem=strict 沙箱）不能吞掉成功匹配

全部在临时目录里跑，不碰 /var/lib 与 /etc。
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="faceunlock-selftest-")
os.environ["FACEUNLOCK_STORE"] = TMP
os.environ["FACEUNLOCK_CONFIG"] = os.path.join(TMP, "config.json")

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

import numpy as np  # noqa: E402

from faceunlock import config as config_mod  # noqa: E402
from faceunlock import admin  # noqa: E402
from faceunlock.auth import RateLimiter  # noqa: E402
from faceunlock.store import (StoreError, StoreTampered, TemplateStore,  # noqa: E402
                              valid_username)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{detail}]" if detail else ""))


def emb(seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.normal(size=128).astype(np.float32)
    return v / np.linalg.norm(v)


def test_hmac() -> None:
    print("\n=== A. 模板 HMAC 篡改检测 ===")
    s = TemplateStore()
    s.ensure()
    s.delete_user("alice")
    s.add_face("alice", "正面", emb(1))
    check("正常写入后可读回", s.embeddings("alice").shape == (1, 128))

    p = pathlib.Path(s.tpl_dir) / "alice.json"
    raw = p.read_text(encoding="utf-8")
    # 篡改：把特征向量整体 +1（等于换了一张脸）
    import json
    data = json.loads(raw)
    data["faces"][0]["embedding"] = [float(x) + 1.0 for x in data["faces"][0]["embedding"]]
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    raised = False
    try:
        s.embeddings("alice")
    except StoreTampered:
        raised = True
    check("篡改特征后读取抛 StoreTampered", raised)
    check("exists() 对损坏模板返回 False（不会拿去比对）", s.exists("alice") is False)

    # 认证侧遇到损坏模板必须返回"不适用"(2)，而不是放行(0)或拒绝(1)
    from faceunlock import EXIT_NOT_APPLICABLE
    from faceunlock.auth import authenticate
    cfg = config_mod.load()
    cfg["services"]["selftest"] = True
    res = authenticate("alice", "selftest", cfg=cfg, store=s)
    check("损坏模板 -> EXIT_NOT_APPLICABLE（回退密码）",
          res.code == EXIT_NOT_APPLICABLE, f"code={res.code} reason={res.reason}")


def test_ratelimit() -> None:
    print("\n=== B. 失败限速 ===")
    s = TemplateStore()
    cfg = config_mod.load()
    cfg["rate_limit"] = {"max_failures": 3, "window_s": 300, "cooldown_s": 60}
    rl = RateLimiter(s, cfg)
    rl.clear("bob")
    check("初始未限速", rl.blocked_for("bob") == 0)
    for _ in range(2):
        rl.record_failure("bob")
    check("2 次失败仍未限速", rl.blocked_for("bob") == 0)
    rl.record_failure("bob")           # 第 3 次触发冷却
    left = rl.blocked_for("bob")
    check("3 次失败后进入冷却", left > 0, f"剩余 {left}s")
    rl.clear("bob")
    check("clear() 可解除冷却（认证成功后调用）", rl.blocked_for("bob") == 0)


def call(req: dict) -> dict:
    """通过协议入口调用特权层。

    注意：命令函数本身会抛 Denied，异常到 JSON 响应的映射在 admin.handle() 里，
    所以测试必须走 handle()——这也正是 GUI 网桥走的那条路径。
    """
    return admin.handle(req)


def test_config_validation() -> None:
    print("\n=== C. 特权层配置校验 ===")
    os.environ["PKEXEC_UID"] = "0"      # 冒充 root 调用者（本测试进程确实是 root）
    r = call({"cmd": "set_config", "config": {"threshold": 0.99}})
    check("阈值 0.99 被拒", r.get("ok") is False and r.get("code") == "INVALID",
          r.get("error", ""))
    r = call({"cmd": "set_config", "config": {"threshold": 0.1}})
    check("阈值 0.10 被拒", r.get("ok") is False)
    r = call({"cmd": "set_config", "config": {"threshold": "abc"}})
    check("非数字阈值被拒", r.get("ok") is False)
    r = call({"cmd": "set_config", "config": {"threshold": 0.55}})
    check("合法阈值 0.55 被接受", r.get("ok") is True
          and abs(r["config"]["threshold"] - 0.55) < 1e-9)
    r = call({"cmd": "set_config", "config": {"services": {"cron": True}}})
    check("非白名单服务名被忽略", "cron" not in r["config"]["services"])
    r = call({"cmd": "set_config", "config": {"required_frames": 999}})
    check("required_frames 越界被拒", r.get("ok") is False)
    r = call({"cmd": "不存在的命令"})
    check("未知命令返回 INVALID", r.get("ok") is False and r.get("code") == "INVALID")
    os.environ.pop("PKEXEC_UID", None)


def test_authz() -> None:
    print("\n=== D. 越权拒绝 ===")
    # 假装调用者是 uid 1000 的普通用户（不在 sudo 组里的场景无法构造，
    # 因此这里只验证"非 admin 且非本人"这条分支）
    import pwd
    os.environ["PKEXEC_UID"] = "1000"
    info = admin.caller_info()
    print(f"     调用者: {info['user']} admin={info['admin']} groups={info['groups']}")
    if not info["admin"]:
        denied = False
        try:
            admin.require_manage("someoneelse")
        except admin.Denied:
            denied = True
        check("非管理员不能管理他人", denied)
        denied = False
        try:
            admin.require_admin()
        except admin.Denied:
            denied = True
        check("非管理员不能改系统配置", denied)
    else:
        # uid 1000 若在 sudo 组则确实是管理员，属于预期配置
        ok = admin.caller_info()["admin"]
        check("uid1000 属于管理员组（本机预期如此）", ok)
    os.environ.pop("PKEXEC_UID", None)

    # 本人管理自己：任何情况下都应放行
    me = pwd.getpwuid(os.getuid()).pw_name
    os.environ["PKEXEC_UID"] = str(os.getuid())
    allowed = True
    try:
        admin.require_manage(me)
    except admin.Denied:
        allowed = False
    check("本人管理自己的人脸被放行", allowed)
    os.environ.pop("PKEXEC_UID", None)


def test_embedding_validation() -> None:
    print("\n=== E. 特征输入校验 ===")
    s = TemplateStore()
    for name, bad in [("维度 64", [0.1] * 64),
                      ("含 NaN", [float("nan")] * 128),
                      ("零向量", [0.0] * 128)]:
        try:
            s.add_face("carol", "x", bad)
            check(f"{name} 被拒", False)
        except StoreError:
            check(f"{name} 被拒", True)
    r = call({"cmd": "add_face", "user": "carol", "label": "x", "embedding": [0.1] * 64})
    check("特权层拒绝 64 维特征",
          r.get("ok") is False and r.get("code") == "INVALID", r.get("error", ""))
    r = call({"cmd": "add_face", "user": "carol", "label": "x",
               "embedding": [0.1] * 128, "thumb_b64": "bm90LWEtanBlZw=="})
    check("特权层拒绝非 JPEG 缩略图", r.get("ok") is False, r.get("error", ""))


def test_path_traversal() -> None:
    print("\n=== F. 用户名路径穿越 ===")
    for bad in ["../../etc/passwd", "a/b", ".hidden", "", "x" * 40, "a b"]:
        check(f"拒绝用户名 {bad!r}", not valid_username(bad))
    for good in ["alice", "user-1", "a.b_c"]:
        check(f"接受用户名 {good!r}", valid_username(good))
    s = TemplateStore()
    try:
        s.load("../../etc/passwd")
        check("load() 拒绝穿越用户名", False)
    except StoreError:
        check("load() 拒绝穿越用户名", True)


def test_no_face_latency() -> None:
    """镜头前没人时必须**快速**回退密码，而不是干等到总超时。

    这决定 sudo / 登录界面的体感：没人时若要等满 4 秒才出密码框，是不能接受的。
    这里注入假摄像头（全黑帧 => 检不到人脸）验证实际返回码与耗时。
    """
    print("\n=== G. 无摄像头/无人脸时的回退时延 ===")
    import time as _t

    from faceunlock import EXIT_NOT_APPLICABLE
    from faceunlock.auth import authenticate

    class FakeCamera:
        """全黑帧摄像头：永远检不到人脸。"""

        def __init__(self, **kw) -> None:
            self.kw = kw

        def open(self) -> None:
            pass

        def read(self):
            return np.zeros((720, 1280, 3), dtype=np.uint8)

        def frames_until(self, deadline, max_frames=120, max_consecutive_failures=10):
            while _t.monotonic() < deadline:
                yield self.read()
                _t.sleep(0.03)

        def close(self) -> None:
            pass

    class DeadCamera(FakeCamera):
        """读不到任何帧：模拟设备被别的程序独占。"""

        def frames_until(self, deadline, max_frames=120, max_consecutive_failures=10):
            return
            yield  # pragma: no cover

    s = TemplateStore()
    s.delete_user("dave")
    s.add_face("dave", "正面", emb(7))
    cfg = config_mod.load()
    cfg["services"]["selftest"] = True
    cfg["timeout_ms"] = 4000
    cfg["no_face_timeout_ms"] = 1000

    t0 = _t.monotonic()
    res = authenticate("dave", "selftest", cfg=cfg, store=s, camera_factory=FakeCamera)
    dt = (_t.monotonic() - t0) * 1000
    check("全黑帧 -> EXIT_NOT_APPLICABLE", res.code == EXIT_NOT_APPLICABLE,
          f"code={res.code} reason={res.reason}")
    check("无人脸时提前退出（< 2.0s；配置的 no_face_timeout 是 1s）", dt < 2000,
          f"实测 {dt:.0f} ms")

    t0 = _t.monotonic()
    res = authenticate("dave", "selftest", cfg=cfg, store=s, camera_factory=DeadCamera)
    dt = (_t.monotonic() - t0) * 1000
    check("读不到帧 -> EXIT_NOT_APPLICABLE", res.code == EXIT_NOT_APPLICABLE,
          f"code={res.code}")
    check("采集失败也快速返回（< 1.0s）", dt < 1000, f"实测 {dt:.0f} ms")


def test_readonly_state() -> None:
    """模板库只读时（polkit 127 的 ProtectSystem=strict 沙箱），限速状态写不进去
    也**绝不能丢掉一次成功的人脸匹配**。

    真机证据（2026-10-08，pkexec 授权框）：
        service=polkit-1 code=2 reason=识别过程异常:
        [Errno 30] Read-only file system: '/var/lib/faceunlock/.tmp-xxxx' frames=2
    这条 reason 来自 authenticate() 的兜底 except —— 真实经过是人脸已经匹配 ≥2 帧，
    紧跟的 limiter.clear() 抛 EROFS，把成功匹配变成了"不适用"：
    pkexec 授权框永远拿不到授权（用户能看到摄像头灯亮、人脸也识别了，却仍然要输密码）。
    """
    print("\n=== H. 只读模板库（polkit 沙箱）不能吞掉成功匹配 ===")
    import time as _time

    from faceunlock import EXIT_MATCH, EXIT_NO_MATCH
    from faceunlock.auth import authenticate

    class ReadOnlyStore(TemplateStore):
        """写 state.json 一定失败：等价于 ProtectSystem=strict 下的 /var/lib。"""

        def save_state(self, st) -> None:
            raise OSError(30, "Read-only file system")

    class MatchedEngine:
        """永远在人脸上拿到满分相似度。"""

        def detect(self, frame):
            return ["face"]

        def largest(self, faces):
            return faces[0] if faces else None

        def embed(self, frame, face):
            return emb(7)

        def similarity(self, a, b) -> float:
            return 1.0

    class NoMatchEngine(MatchedEngine):
        def similarity(self, a, b) -> float:
            return 0.0

    class OneFrameCamera:
        def __init__(self, **kw) -> None:
            self.kw = kw

        def open(self) -> None:
            pass

        def read(self):
            return np.zeros((720, 1280, 3), dtype=np.uint8)

        def frames_until(self, deadline, max_frames=120, max_consecutive_failures=10):
            while _time.monotonic() < deadline:
                yield self.read()
                _time.sleep(0.02)

        def close(self) -> None:
            pass

    s = ReadOnlyStore()
    s.delete_user("erin")
    s.add_face("erin", "正面", emb(7))
    cfg = config_mod.load()
    cfg["services"]["selftest"] = True
    cfg["timeout_ms"] = 3000
    cfg["no_face_timeout_ms"] = 1000
    cfg["required_frames"] = 2
    cfg["window_frames"] = 3

    res = authenticate("erin", "selftest", cfg=cfg, store=s,
                       engine=MatchedEngine(), camera_factory=OneFrameCamera)
    check("只读模板库下，人脸匹配仍然返回 EXIT_MATCH（授权框能拿到授权）",
          res.code == EXIT_MATCH, f"code={res.code} reason={res.reason} score={res.score:.2f}")

    res = authenticate("erin", "selftest", cfg=cfg, store=s,
                       engine=NoMatchEngine(), camera_factory=OneFrameCamera)
    check("只读模板库下，失败路径仍正常返回 EXIT_NO_MATCH（不是异常兜底）",
          res.code == EXIT_NO_MATCH, f"code={res.code} reason={res.reason}")


def main() -> int:
    print(f"faceunlock 安全属性自测（临时目录 {TMP}）")
    test_hmac()
    test_ratelimit()
    test_config_validation()
    test_authz()
    test_embedding_validation()
    test_path_traversal()
    test_no_face_latency()
    test_readonly_state()
    print(f"\n结果: {len(PASS)} 通过, {len(FAIL)} 失败")
    if FAIL:
        print("失败项: " + ", ".join(FAIL))
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
