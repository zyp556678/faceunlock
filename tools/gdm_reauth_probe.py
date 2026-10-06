#!/usr/bin/env python3
"""走 gnome-shell 锁屏用的那条 GDM D-Bus 重认证通道，验证人脸解锁。

为什么要这个探针：pamtester 只能证明 PAM 栈本身对，证明不了
「gnome-shell → libgdm → gdm 守护进程 → gdm-session-worker(root) → PAM」这一层。
本脚本直接调用 org.gnome.DisplayManager.Manager.OpenReauthenticationChannel
（就是 gnome-shell 锁屏解锁时调的那个方法），然后 Begin("gdm-password", user)，
观察 GDM 回来的是 VerificationComplete（人脸直接通过）还是密码询问。

**不会锁屏、不影响当前会话。**

用法: sudo python3 tools/gdm_reauth_probe.py [用户名] [等待秒数]
"""
from __future__ import annotations

import sys

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import GLib, Gio  # noqa: E402

DEST = "org.gnome.DisplayManager"
MANAGER_PATH = "/org/gnome/DisplayManager/Manager"
MANAGER_IFACE = "org.gnome.DisplayManager.Manager"
VERIFIER_IFACE = "org.gnome.DisplayManager.UserVerifier"

events: list[tuple[str, str]] = []
done = GLib.MainLoop()


def main() -> int:
    user = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("USER", "alice")
    wait_s = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0

    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    mgr = Gio.DBusProxy.new_sync(bus, Gio.DBusProxyFlags.NONE, None,
                                 DEST, MANAGER_PATH, MANAGER_IFACE, None)
    try:
        res = mgr.call_sync("OpenReauthenticationChannel",
                            GLib.Variant("(s)", (user,)),
                            Gio.DBusCallFlags.NONE, 10000, None)
    except GLib.Error as e:
        print(f"❌ OpenReauthenticationChannel 失败: {e.message}")
        return 2
    path = res.unpack()[0]
    print(f"✅ 已打开 {user} 的重认证通道: {path}")

    try:
        xml = Gio.DBusProxy.new_sync(
            bus, Gio.DBusProxyFlags.NONE, None, DEST, path,
            "org.freedesktop.DBus.Introspectable", None
        ).call_sync("Introspect", None, Gio.DBusCallFlags.NONE, 5000, None).unpack()[0]
        methods = [l.strip() for l in xml.splitlines() if "method name" in l or "signal name" in l]
        print("   接口成员:", ", ".join(m.split('"')[1] for m in methods))
    except GLib.Error as e:
        print(f"   （introspect 失败，继续: {e.message}）")

    verifier = Gio.DBusProxy.new_sync(bus, Gio.DBusProxyFlags.NONE, None,
                                      DEST, path, VERIFIER_IFACE, None)

    def on_signal(_proxy, _sender, signal, params):
        args = params.unpack() if params else ()
        text = args[0] if args and isinstance(args[0], str) else ""
        events.append((signal, text))
        show = f"  ← {signal}" + (f": {text!r}" if text else "")
        print(show, flush=True)
        if signal in ("VerificationComplete", "ConversationStopped"):
            GLib.timeout_add(200, done.quit)

    verifier.connect("g-signal", on_signal)

    try:
        verifier.call_sync("Begin", GLib.Variant("(ss)", ("gdm-password", user)),
                           Gio.DBusCallFlags.NONE, 10000, None)
        print("   已发送 Begin(service=gdm-password)")
    except GLib.Error as e:
        print(f"❌ Begin 失败: {e.message}")
        return 2

    GLib.timeout_add(int(wait_s * 1000), done.quit)
    done.run()

    try:
        verifier.call_sync("Cancel", None, Gio.DBusCallFlags.NONE, 5000, None)
    except GLib.Error:
        pass

    names = [n for n, _ in events]
    print()
    if "VerificationComplete" in names:
        print("✅ 结论: GDM 判定认证成功（人脸直接通过）—— 锁屏解锁路径可用")
        return 0
    if any(n in names for n in ("SecretInfoQuery", "InfoQuery", "Problem")):
        print("❌ 结论: GDM 在要密码 —— 人脸没通过，走了回退（安全但体验未达成）")
        return 1
    print(f"❌ 结论: 未收到最终结果，事件={names or '（无）'}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
