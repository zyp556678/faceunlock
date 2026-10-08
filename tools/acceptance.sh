#!/bin/bash
# faceunlock 验收测试：一条命令验证"能不能用"和"会不会把人锁在外面"。
#
# 用法:  sudo tools/acceptance.sh            # 全量（含真实人脸识别，需本人在镜头前）
#        sudo tools/acceptance.sh --no-face  # 跳过需要人脸在场的项目
#
# 退出码 0 = 全部通过。
set -u
NOFACE=0
[ "${1:-}" = "--no-face" ] && NOFACE=1

PASS=0; FAIL=0; SKIP=0
ok()   { printf "  \033[32m✅\033[0m %s\n" "$1"; PASS=$((PASS+1)); }
bad()  { printf "  \033[31m❌\033[0m %s\n" "$1"; FAIL=$((FAIL+1)); }
skip() { printf "  \033[33m⏭\033[0m  %s\n" "$1"; SKIP=$((SKIP+1)); }
hdr()  { printf "\n\033[1m=== %s ===\033[0m\n" "$1"; }

if [ "$(id -u)" != "0" ]; then echo "请用 sudo 运行"; exit 2; fi

USER_TO_TEST="${FU_USER:-$(awk -F: '$3==1000{print $1}' /etc/passwd | head -1)}"

hdr "1. 安装完整性"
for f in /usr/bin/faceunlock \
         /usr/libexec/faceunlock-auth \
         /usr/libexec/faceunlock-admin \
         /usr/lib/x86_64-linux-gnu/security/pam_faceunlock.so \
         /usr/share/pam-configs/faceunlock \
         /usr/share/polkit-1/actions/org.faceunlock.manage.policy \
         /usr/share/faceunlock/models/face_detection_yunet_2023mar.onnx \
         /usr/share/faceunlock/models/face_recognition_sface_2021dec.onnx \
         /usr/lib/faceunlock/vendor/cv2/cv2.abi3.so; do
    [ -e "$f" ] && ok "存在 $f" || bad "缺失 $f"
done
[ -f /usr/bin/faceunlock-gui ] && ok "存在图形界面 /usr/bin/faceunlock-gui" \
    || skip "未安装图形界面（faceunlock-gui 子包）"

# 打包权限：/usr/lib/faceunlock 下的文件必须对所有人可读。
# 踩过的坑：install-tree.sh 用 cp -a 照抄开发机权限，新增的 .py 是 0600，
# 装完普通用户 import 直接 PermissionError（`faceunlock polkit-camera status` 崩）。
if [ -d /usr/lib/faceunlock ]; then
    n=$(find /usr/lib/faceunlock -type f ! -perm -004 2>/dev/null | wc -l)
    [ "$n" -eq 0 ] && ok "python/opencv 文件全部 world-readable（无 0600 残留）" \
        || bad "$n 个文件对其他人不可读（0600？普通用户导入会 PermissionError）"
fi

hdr "2. 权限边界"
store=/var/lib/faceunlock
if [ -d "$store" ]; then
    m=$(stat -c '%a %U:%G' "$store")
    [ "$m" = "700 root:root" ] && ok "模板库权限 $m" || bad "模板库权限异常: $m"
else
    skip "模板库尚未创建（还没录入任何人脸）"
fi
if [ -d "$store/templates" ]; then
    badperm=$(find "$store" -type f ! -perm 600 | wc -l)
    [ "$badperm" -eq 0 ] && ok "所有模板文件均为 0600" || bad "$badperm 个文件权限不是 0600"
fi
[ -f /etc/faceunlock/config.json ] && {
    m=$(stat -c '%a' /etc/faceunlock/config.json)
    [ "$m" = "644" ] || [ "$m" = "600" ] && ok "配置权限 $m" || bad "配置权限异常: $m"
}

hdr "3. 模型完整性（sha256）"
check_sum() {
    f="/usr/share/faceunlock/models/$1"; want="$2"
    if [ -f "$f" ]; then
        got=$(sha256sum "$f" | cut -d' ' -f1)
        [ "$got" = "$want" ] && ok "$1 校验通过" || bad "$1 校验失败"
    else bad "$1 不存在"; fi
}
check_sum face_detection_yunet_2023mar.onnx \
    8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4
check_sum face_recognition_sface_2021dec.onnx \
    0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79

hdr "4. 自检 (faceunlock doctor)"
if faceunlock doctor >/tmp/.fu_doctor 2>&1; then
    sed 's/^/  /' /tmp/.fu_doctor
    if grep -q '❌' /tmp/.fu_doctor; then bad "doctor 有 fail 项"; else ok "doctor 无失败项"; fi
else
    bad "doctor 执行失败"; cat /tmp/.fu_doctor
fi
rm -f /tmp/.fu_doctor

hdr "5. 安全属性单元测试"
repo=$(cd "$(dirname "$0")/.." && pwd)
if [ -f "$repo/tools/selftest.py" ]; then
    if out=$(python3 "$repo/tools/selftest.py" 2>&1); then
        ok "$(echo "$out" | tail -1)"
    else
        bad "自测失败"; echo "$out" | tail -8 | sed 's/^/     /'
    fi
else
    skip "缺少 tools/selftest.py"
fi

hdr "6. PAM 集成状态"
if grep -q 'pam_faceunlock\.so' /etc/pam.d/common-auth 2>/dev/null; then
    ok "common-auth 已引用 pam_faceunlock.so"
    if grep -q 'success=end' /etc/pam.d/common-auth; then
        bad "出现 libpam 不认识的 success=end（必须改成 sufficient）"
    else
        ok "未使用 success=end（libpam 不认这个关键字）"
    fi
    # 已在栈中，模块文件必须存在，否则认证全挂
    [ -f /usr/lib/x86_64-linux-gnu/security/pam_faceunlock.so ] \
        && ok "模块文件存在（栈引用有效）" \
        || bad "栈里有引用但模块文件不存在 —— 会导致所有认证失败！"
    # 备份存在性
    ls /var/backups/faceunlock/common-auth.* >/dev/null 2>&1 \
        && ok "存在 common-auth 自动备份" || skip "尚无备份（首次 enable 时才创建）"
else
    skip "PAM 未启用人脸（sudo faceunlock enable）"
fi

hdr "7. 失败回退（最关键的安全性质）"
if [ "$NOFACE" = "1" ]; then
    skip "按要求跳过需要人脸在场的项目"
else
    # 未录入用户：必须走到密码提示，而不是拒绝或卡死
    start=$(date +%s%N)
    out=$(echo "definitely-wrong" | timeout 30 pamtester gdm-password nobody authenticate 2>&1)
    rc=$?
    ms=$(( ($(date +%s%N) - start) / 1000000 ))
    if echo "$out" | grep -q 'Password:'; then
        ok "未录入用户弹出密码提示（回退成功，耗时 ${ms}ms）"
    else
        bad "未录入用户没有回退到密码：$out"
    fi
    [ "$rc" != "0" ] && ok "错误密码被正确拒绝" || bad "错误密码竟然通过了"
fi

hdr "8. 真实 PAM 服务栈（人脸）"
if [ "$NOFACE" = "1" ]; then
    skip "按要求跳过"
elif [ -z "$USER_TO_TEST" ]; then
    skip "找不到 uid=1000 的用户"
else
    for svc in sudo gdm-password polkit-1; do
        start=$(date +%s%N)
        out=$(timeout 30 pamtester "$svc" "$USER_TO_TEST" authenticate 2>&1)
        rc=$?
        ms=$(( ($(date +%s%N) - start) / 1000000 ))
        if [ "$rc" = "0" ]; then
            score=$(echo "$out" | grep -oE '相似度 [0-9.]+' | head -1)
            ok "$svc 人脸认证通过（$score，${ms}ms）"
        else
            bad "$svc 人脸认证未通过":$(echo "$out" | tr '\n' ' ' | head -c 200)
        fi
    done
fi

hdr "9. 卸载安全性（静态检查，不做真实卸载）"
if grep -q 'pam-auth-update' debian/faceunlock.prerm 2>/dev/null || \
   [ -f "$repo/debian/faceunlock.prerm" ]; then
    if grep -q 'pam_faceunlock' "$repo/debian/faceunlock.prerm" 2>/dev/null; then
        ok "prerm 会在删除模块前清理 PAM 引用"
    else
        bad "prerm 没有清理 PAM 引用的逻辑（卸载后会锁死系统）"
    fi
fi

hdr "10. 锁屏/登录界面架构（防止系统升级后静默失效）"
# GNOME 46 的解锁由 gnome-shell 经 GDM 的 D-Bus 发起，服务名写死在 libshell-*.so 里。
# 如果哪天发行版换成别的服务名，我们的模块就会静默不再被调用 —— 这条检查用来兜住它。
shell_lib=$(ls /usr/lib/gnome-shell/libshell-*.so 2>/dev/null | head -1)
if [ -n "$shell_lib" ]; then
    if grep -qa 'gdm-password' "$shell_lib"; then
        ok "gnome-shell 解锁使用 gdm-password（= 我们的 PAM 接入点）"
    else
        bad "gnome-shell 不再引用 gdm-password —— 锁屏可能已不走我们的模块"
    fi
    if grep -qa 'org.gnome.DisplayManager' "$shell_lib"; then
        ok "gnome-shell 通过 GDM D-Bus 做重认证（root worker 跑 PAM，摄像头可用）"
    else
        bad "gnome-shell 不再通过 GDM 重认证，需重新评估集成方式"
    fi
else
    skip "找不到 /usr/lib/gnome-shell/libshell-*.so"
fi
if [ -r /usr/libexec/gdm-session-worker ]; then
    grep -qa 'gdm-password' /usr/libexec/gdm-session-worker \
        && ok "gdm-session-worker 内含 gdm-password 服务映射" \
        || bad "gdm-session-worker 里找不到 gdm-password"
fi
# 现场证据：是否真的有过"GDM worker 触发人脸解锁成功"的记录
if journalctl --since "-7 days" --no-pager 2>/dev/null \
     | grep -q 'gdm-session-worker.*人脸识别通过'; then
    ok "日志中存在 gdm-session-worker 人脸解锁成功的记录（真机验证过）"
else
    skip "尚无锁屏解锁日志，可运行 sudo tools/lock_screen_test.sh 做一次真机验证"
fi

hdr "11. PAM 助手不得污染 stdout（polkit 协议通道）"
# 血泪教训：polkit-agent-helper-1 把 **stdout 当作和 gnome-shell agent 通信的协议通道**
# （写 "PAM_TEXT_INFO <文本>" / "SUCCESS"）。我们的助手只要往 stdout 多写一行普通文本，
# 对方的协议解析就失败，表现为「人脸每次识别都成功，授权对话框却每秒重试一次、
# 摄像头灯不停闪」，而且永远不会授权通过。这条检查防止回归。
if [ "$NOFACE" = "1" ]; then
    skip "按要求跳过（需要真实调用一次助手）"
else
    so=$(mktemp); se=$(mktemp)
    timeout 25 /usr/libexec/faceunlock-auth "${USER_TO_TEST:-nobody}" polkit-1 >"$so" 2>"$se"
    rc=$?
    if [ -s "$so" ]; then
        bad "助手往 stdout 写了内容（会破坏 polkit 协议）：$(head -c 120 "$so")"
    else
        ok "助手 stdout 干净（诊断输出全部走 stderr / syslog），rc=$rc"
    fi
    # C 模块本身也必须把子进程的 stdout 接走
    if grep -q 'STDOUT_FILENO' "$repo/pam/pam_faceunlock.c" 2>/dev/null; then
        ok "pam_faceunlock.so 会把助手的 stdout 重定向到 /dev/null"
    else
        bad "pam_faceunlock.c 没有重定向子进程 stdout（polkit 场景会每秒重试、永不通过）"
    fi
    # 模块必须动态依赖 libpam：静态链进 libpam.a 会让它自带 pam_* 符号定义，
    # 在同一个进程里劫持 PAM 栈（构建脚本里踩到过一次 dangling libpam.so 的坑）
    mod=/usr/lib/$ARCH_TRIPLET/security/pam_faceunlock.so
    if [ -r "$mod" ] && command -v readelf >/dev/null 2>&1; then
        if readelf -d "$mod" | grep -q 'libpam\.so\.0'; then
            ok "pam_faceunlock.so 动态依赖 libpam.so.0"
        else
            bad "pam_faceunlock.so 未动态依赖 libpam.so.0（疑似静态链了 libpam.a）"
        fi
        if command -v nm >/dev/null 2>&1 && \
           nm -D --defined-only "$mod" 2>/dev/null | grep -q ' T pam_get_user'; then
            bad "pam_faceunlock.so 自带 pam_get_user 定义（静态链接会劫持 PAM 符号）"
        else
            ok "pam_faceunlock.so 未导出 pam_* 符号（无符号劫持）"
        fi
    fi
    rm -f "$so" "$se"
fi

hdr "12. polkit 127 沙箱：摄像头不可见时必须毫秒级回退密码"
# Ubuntu 26.04 起，polkit-1 的 PAM 栈跑在 socket 激活的
# polkit-agent-helper@.service 里，默认 PrivateDevices=yes + DevicePolicy=strict：
# /dev/video* 根本不在沙箱的 /dev 里。这一节用 systemd-run 复现同样的沙箱，
# 断言"没有摄像头时要在毫秒级（而不是 OpenCV 加载 + 8 秒兜底超时）回退密码"。
if ! command -v systemd-run >/dev/null 2>&1 || [ ! -d /run/systemd/system ]; then
    skip "没有可用的 systemd-run，无法构造等价沙箱"
else
    if systemd-run --quiet --wait --collect -p PrivateDevices=yes \
           /usr/bin/test -c /dev/video0 >/dev/null 2>&1; then
        bad "PrivateDevices=yes 的沙箱里还能看到 /dev/video0 —— 本节的复现前提不成立"
    else
        ok "沙箱里看不到 /dev/video0（= polkit 127 helper 的默认环境）"
    fi
    if [ -n "$USER_TO_TEST" ]; then
        start=$(date +%s%N)
        systemd-run --quiet --wait --collect -p PrivateDevices=yes \
            /usr/libexec/faceunlock-auth "$USER_TO_TEST" polkit-1 >/dev/null 2>&1
        ms=$(( ($(date +%s%N) - start) / 1000000 ))
        if [ "$ms" -lt 1500 ]; then
            ok "沙箱内助手 ${ms}ms 内返回（预检短路，未加载 OpenCV）"
        else
            bad "沙箱内助手耗时 ${ms}ms —— 没有快速回退，用户会在授权框前干等"
        fi
    fi
    # 整条真实 PAM 栈（polkit-1 → common-auth → pam_faceunlock.so）在沙箱里的表现
    if command -v pamtester >/dev/null 2>&1 && [ -n "$USER_TO_TEST" ]; then
        start=$(date +%s%N)
        out=$(echo "definitely-wrong" | timeout 30 systemd-run --quiet --wait --collect --pipe \
                -p PrivateDevices=yes -p DevicePolicy=strict -p DeviceAllow=/dev/null \
                pamtester polkit-1 "$USER_TO_TEST" authenticate 2>&1)
        ms=$(( ($(date +%s%N) - start) / 1000000 ))
        if [ "$ms" -lt 1500 ]; then
            ok "沙箱内真实 PAM 栈 ${ms}ms 内回退密码"
        else
            bad "沙箱内真实 PAM 栈耗时 ${ms}ms（应毫秒级回退）"
        fi
        echo "$out" | grep -q 'Password:' \
            && ok "沙箱内仍然弹出 Password: 提示（回退链路完整）" \
            || bad "沙箱内没出现密码提示：$(echo "$out" | tr '\n' ' ' | head -c 160)"
    else
        skip "没有 pamtester，跳过沙箱内的整栈检查"
    fi
fi

hdr "13. polkit 授权框的摄像头权限（systemd drop-in）"
dropin=/etc/systemd/system/polkit-agent-helper@.service.d/10-faceunlock-camera.conf
if [ ! -f /usr/lib/systemd/system/polkit-agent-helper@.service ]; then
    skip "本机 polkit 不使用 socket 激活的 helper（老版本），无需 drop-in"
elif [ ! -f "$dropin" ]; then
    skip "drop-in 未安装：授权框只走密码（sudo faceunlock polkit-camera on 可启用）"
else
    ok "drop-in 存在：$dropin"
    if [ -f /usr/share/faceunlock/polkit-agent-helper-camera.conf ] && \
       cmp -s /usr/share/faceunlock/polkit-agent-helper-camera.conf "$dropin"; then
        ok "drop-in 内容与随包权威副本一致"
    else
        bad "drop-in 与 /usr/share/faceunlock/polkit-agent-helper-camera.conf 不一致"
    fi
    private=$(systemctl show polkit-agent-helper@0.service -p PrivateDevices --value 2>/dev/null)
    allow=$(systemctl show polkit-agent-helper@0.service -p DeviceAllow --value 2>/dev/null)
    [ "$private" = "no" ] \
        && ok "运行时 PrivateDevices=no（沙箱不再隐藏 /dev/video*）" \
        || bad "运行时 PrivateDevices=$private —— 需要 systemctl daemon-reload"
    echo "$allow" | grep -q 'video4linux' \
        && ok "运行时 DeviceAllow 含 video4linux" \
        || bad "运行时 DeviceAllow 不含 video4linux：$allow"
    # 等价沙箱里"两行 drop-in"必须正好让设备可见，且不多给别的设备
    if systemd-run --quiet --wait --collect -p PrivateDevices=no \
           -p DevicePolicy=strict -p DeviceAllow="/dev/null rw" \
           -p DeviceAllow="char-video4linux rw" /usr/bin/test -c /dev/video0 >/dev/null 2>&1; then
        ok "等价沙箱里 /dev/video0 可见（drop-in 的两行确实起作用）"
    else
        bad "等价沙箱里仍看不到 /dev/video0 —— drop-in 可能没生效"
    fi
    if systemd-run --quiet --wait --collect -p PrivateDevices=no \
           -p DevicePolicy=strict -p DeviceAllow="/dev/null rw" \
           -p DeviceAllow="char-video4linux rw" /usr/bin/test -c /dev/sda >/dev/null 2>&1; then
        ok "（提示）/dev/sda 也可见：说明主机确实有这个节点，不是沙箱漏洞"
    else
        ok "除视频设备外的块设备仍不可见（DevicePolicy=strict 保持生效）"
    fi
fi

# 普通用户也必须能查状态（不需要 root）。
# 踩过的坑：argparse 的 choices 漏了 status，`faceunlock polkit-camera status`
# 被直接拒绝；另外新增的 .py 曾被装成 0600，普通用户导入会 PermissionError。
if command -v faceunlock >/dev/null 2>&1 && [ -n "$USER_TO_TEST" ]; then
    uout=$(su -s /bin/sh "$USER_TO_TEST" -c 'faceunlock polkit-camera status' 2>&1)
    if echo "$uout" | grep -q 'PrivateDevices'; then
        ok "普通用户可运行 faceunlock polkit-camera status（无需 root）"
    else
        bad "普通用户跑 polkit-camera status 失败：$(echo "$uout" | tr '\n' ' ' | head -c 160)"
    fi
fi

hdr "14. 助手 stderr 隔离（polkit 127 的 EPIPE 回归）"
# 关键回归：polkit 127 里 stderr 既写不通、又是协议流的一部分。旧版助手会因此
# 以 120 退出（CPython 刷不出标准流），并被 gnome-shell 当成协议垃圾，
# 表现为"授权框每秒重试、密码也输不进去"。这里把 stderr 接到写不通的管道来复现。
if [ "$NOFACE" = "1" ]; then
    skip "按要求跳过（需要真实调用一次助手）"
else
    res=$(FU_USER_TEST="${USER_TO_TEST:-nobody}" python3 - <<'PY' 2>/dev/null
import os, subprocess
u = os.environ.get("FU_USER_TEST") or "nobody"
r, w = os.pipe(); os.close(r)          # 读端关闭：写 stderr 必然 EPIPE
p = subprocess.Popen(["/usr/libexec/faceunlock-auth", u, "polkit-1", "", ""],
                     stderr=w, stdout=subprocess.PIPE)
os.close(w)
out = p.stdout.read()
print(f"{p.wait()} {len(out)}")
PY
)
    rc=${res%% *}; so=${res##* }
    case "$res" in
        "")  bad "无法启动助手做 stderr 隔离测试" ;;
        *)
            if [ "$rc" = "120" ]; then
                bad "助手在 stderr 不可写时以 120 退出（旧版行为：授权框会每秒重试）"
            elif [ "$so" != "0" ]; then
                bad "助手往 stdout 写了 $so 字节（会破坏 polkit 协议）"
            else
                ok "stderr 不可写时助手仍正常返回（rc=$rc，stdout 干净）"
            fi
            ;;
    esac
    # C 模块侧：stderr 只在是终端时保留；并且 fork 之前先看 /dev 有没有视频设备
    if grep -q 'isatty(STDERR_FILENO)' "$repo/pam/pam_faceunlock.c" 2>/dev/null; then
        ok "pam_faceunlock.c 只在 stderr 是终端时才保留它（其余场景接 /dev/null）"
    else
        bad "pam_faceunlock.c 没有对 stderr 做 isatty 判断（polkit 场景会 EPIPE/污染协议）"
    fi
    if grep -q 'has_video_device' "$repo/pam/pam_faceunlock.c" 2>/dev/null && \
       grep -q 'video_probe' "$repo/pam/pam_faceunlock.c" 2>/dev/null; then
        ok "pam_faceunlock.c 含 /dev 视频设备预检（video_probe=0 可关）"
    else
        bad "pam_faceunlock.c 缺少 /dev 视频设备预检（沙箱里会白等到超时）"
    fi
    if grep -q 'camera_unavailable_reason' "$repo/faceunlock/preflight.py" 2>/dev/null; then
        ok "faceunlock/preflight.py 提供廉价预检（不 import cv2）"
    else
        bad "缺少 faceunlock/preflight.py 的预检实现"
    fi
fi

printf "\n\033[1m结果: %d 通过, %d 失败, %d 跳过\033[0m\n" "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
