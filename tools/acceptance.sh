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
        ok "pam_faceunlock.so 会把助手的 stdout 重定向到 /dev/null（stderr 保留供排障）"
    else
        bad "pam_faceunlock.c 没有重定向子进程 stdout（polkit 场景会每秒重试、永不通过）"
    fi
    rm -f "$so" "$se"
fi

printf "\n\033[1m结果: %d 通过, %d 失败, %d 跳过\033[0m\n" "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
