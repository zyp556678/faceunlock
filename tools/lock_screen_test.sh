#!/bin/bash
# 真实锁屏解锁端到端测试。
#
# 逻辑：
#   1) 先用摄像头确认"人在镜头前"——不在就直接跳过，绝不把没人的机器锁上
#   2) 记录当前 LockedHint
#   3) loginctl lock-session 真正锁屏
#   4) 轮询最多 40 秒，看 gnome-shell 是否通过 GDM reauth 通道用 gdm-password
#      把人脸认证跑通并自动解锁
#   5) 无论结果如何都打印 journal 证据
#
# 若人脸没能解锁，屏幕上就是正常的密码框——输入密码即可，不会锁死。
set -u
USER_NAME="${1:-$(awk -F: '$3==1000{print $1}' /etc/passwd | head -1)}"
SESSION_ID="${2:-$(loginctl list-sessions --no-legend 2>/dev/null | awk -v u="$USER_NAME" '$3==u{print $1}' | head -1)}"

echo "目标用户: $USER_NAME   会话: $SESSION_ID"

echo
echo "=== 1. 先确认人在镜头前（否则不锁屏）==="
out=$(timeout 25 /usr/bin/faceunlock test "$USER_NAME" 2>&1)
echo "$out" | sed 's/^/  /'
if ! echo "$out" | grep -q '通过 ✅'; then
    echo
    echo "⏭  镜头前没识别到本人，**跳过锁屏测试**（不冒险把没人的机器锁上）。"
    echo "   等人坐回来再跑一次即可。"
    exit 3
fi
echo "  → 人在镜头前，继续。"

before=$(loginctl show-session "$SESSION_ID" -p LockedHint --value 2>/dev/null)
echo
echo "=== 2. 锁屏前状态: LockedHint=$before ==="

t0=$(date +%s)
loginctl lock-session "$SESSION_ID" || { echo "loginctl lock-session 失败"; exit 2; }

echo "=== 3. 已发送锁屏，开始轮询最多 40 秒 ==="
unlocked_at=""
for i in $(seq 1 40); do
    sleep 1
    h=$(loginctl show-session "$SESSION_ID" -p LockedHint --value 2>/dev/null)
    if [ "$h" = "no" ]; then unlocked_at=$(( $(date +%s) - t0 )); break; fi
    [ $((i % 5)) -eq 0 ] && echo "  ${i}s: 仍锁定中…"
done

echo
echo "=== 4. 结果 ==="
if [ -n "$unlocked_at" ]; then
    echo "  ✅ 锁屏后 ${unlocked_at} 秒自动解锁 —— gnome-shell → GDM → gdm-password → pam_faceunlock 全线打通"
    rc=0
else
    echo "  ❌ 40 秒内未自动解锁（屏幕上应是密码框，输密码即可）"
    rc=1
fi
echo
echo "=== 5. journal 证据（GDM worker 与我们的模块）==="
journalctl --since "-2 min" --no-pager 2>/dev/null | grep -iE 'faceunlock|gdm-session-worker|gdm-password' | tail -12 | sed 's/^/  /'
exit $rc
