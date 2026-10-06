#!/usr/bin/env bash
# GUI_API.md 端点回归测试：全部走 curl，特权路径用 tools/fake_admin.py 假助手。
#
#   bash tools/gui_api_curltest.sh
#
# 数据只写 /tmp/fu-gui-test，不碰 /etc、/var/lib、/usr；不运行 pkexec。
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${FU_TEST_WORK:-/tmp/fu-gui-test}"
BRIDGE_PID=""
TOKEN=""
BASE=""

# ---------- 工具函数 ----------
sec()  { echo; echo "=============================================================="; echo "## $*"; echo "=============================================================="; }
cmd()  { echo; echo "\$ $*"; }
jqish() { python3 -c '
import json,sys
def trim(o, n=72):
    if isinstance(o,str):  return o if len(o)<=n else o[:n]+"...<%d chars>"%len(o)
    if isinstance(o,dict): return {k:trim(v,n) for k,v in o.items()}
    if isinstance(o,list): return [trim(v,n) for v in o]
    return o
sys.stdout.write(json.dumps(trim(json.load(sys.stdin)), ensure_ascii=False)+"\n")
'; }
field() { python3 -c '
import json,sys
cur=json.load(sys.stdin)
for k in sys.argv[1].split("."):
    if cur is None: break
    cur = cur.get(k) if isinstance(cur,dict) else None
print(json.dumps(cur, ensure_ascii=False))
' "$1"; }

get()  { curl -s -H "X-FaceUnlock-Token: $TOKEN" "$BASE$1"; }
post() { curl -s -H "X-FaceUnlock-Token: $TOKEN" -H 'Content-Type: application/json' \
                -d "${2:-{\}}" "$BASE$1"; }

start_bridge() {   # start_bridge <名称> [额外环境变量...]
    local name="$1"; shift
    local out="$WORK/$name.out" err="$WORK/$name.err"
    env "$@" python3 "$REPO/bin/faceunlock-gui-bridge" >"$out" 2>"$err" &
    BRIDGE_PID=$!
    for _ in $(seq 1 150); do
        grep -q '"event": *"ready"' "$out" 2>/dev/null && break
        sleep 0.1
    done
    local hand; hand="$(head -1 "$out")"
    if [ -z "$hand" ]; then echo "!! 桥接服务未握手，stderr:"; tail -5 "$err"; exit 1; fi
    TOKEN="$(python3 -c 'import json,sys;print(json.loads(sys.argv[1])["token"])' "$hand")"
    local port; port="$(python3 -c 'import json,sys;print(json.loads(sys.argv[1])["port"])' "$hand")"
    BASE="http://127.0.0.1:$port"
    echo "握手行: $hand"
}
stop_bridge() { [ -n "$BRIDGE_PID" ] && kill "$BRIDGE_PID" 2>/dev/null; wait "$BRIDGE_PID" 2>/dev/null; BRIDGE_PID=""; }

enroll_until_done() { # 轮询到 state != running
    local i r s
    for i in $(seq 1 60); do
        r="$(get /api/enroll/poll)"; s="$(echo "$r" | field state)"
        if [ "$s" != '"running"' ]; then echo "$r" | jqish; return 0; fi
        if [ "$i" -le 2 ] || [ $((i % 5)) -eq 0 ]; then echo "  (poll $i) $(echo "$r" | jqish)"; fi
        sleep 0.2
    done
    echo "!! 轮询超时"
}

# ---------- 环境 ----------
rm -rf "$WORK"; mkdir -p "$WORK"
export FACEUNLOCK_STORE="$WORK"
export FACEUNLOCK_CONFIG="$WORK/config.json"
export FACEUNLOCK_FAKE_PAM="$WORK/pam.json"
export FACEUNLOCK_BASE="$REPO"          # 强制用工作树，而不是 /usr/lib/faceunlock 的安装副本
export FACEUNLOCK_ADMIN_CMD="python3 $REPO/tools/fake_admin.py serve"
export FACEUNLOCK_GUI_USER="${FACEUNLOCK_GUI_USER:-alice}"   # 以 root 跑测试时模拟普通用户
export FACEUNLOCK_CAPTURE_INTERVAL=0.05
trap 'stop_bridge' EXIT

sec "0. 启动（假助手 + 假摄像头图片帧，可跑通真实检测链路）"
cmd "FACEUNLOCK_ADMIN_CMD=\"$FACEUNLOCK_ADMIN_CMD\" FACEUNLOCK_STORE=$WORK FACEUNLOCK_FAKE_CAMERA=1 FACEUNLOCK_FAKE_IMAGE=smoke bin/faceunlock-gui-bridge"
start_bridge main FACEUNLOCK_FAKE_CAMERA=1 FACEUNLOCK_FAKE_IMAGE=smoke

sec "1. /health（唯一免 token 的端点）"
cmd "curl -s $BASE/health"
curl -s "$BASE/health" | jqish

sec "2. 鉴权：缺 token / 错 token / 伪造 Host"
cmd "curl -s -o /dev/null -w '%{http_code}' $BASE/api/state"
curl -s -o /dev/null -w 'HTTP %{http_code}\n' "$BASE/api/state"
cmd "curl -s -H 'X-FaceUnlock-Token: wrong' $BASE/api/state   # 期望 401"
curl -s -H 'X-FaceUnlock-Token: wrong' "$BASE/api/state" | jqish
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -H 'X-FaceUnlock-Token: wrong' "$BASE/api/state"
cmd "curl -s -H 'Host: evil.example' -H 'X-FaceUnlock-Token: $TOKEN' $BASE/api/state   # DNS rebinding 防护"
curl -s -H 'Host: evil.example' -H "X-FaceUnlock-Token: $TOKEN" "$BASE/api/state" | jqish
cmd "curl -s -X OPTIONS -D - -o /dev/null $BASE/api/faces   # 浏览器预检（无 token）"
curl -s -X OPTIONS -D - -o /dev/null "$BASE/api/faces" | grep -iE 'HTTP/|access-control' 

sec "3. /api/state"
cmd "curl -s -H 'X-FaceUnlock-Token: \$TOKEN' $BASE/api/state"
get /api/state | jqish

sec "4. /api/enroll/start -> poll -> commit（5 张，自动换姿态提示）"
cmd "curl -s -X POST -d '{\"user\":\"alice\",\"label\":\"正面\",\"count\":5}' $BASE/api/enroll/start"
post /api/enroll/start '{"user":"alice","label":"正面","count":5}' | jqish
cmd "curl -s -H 'X-FaceUnlock-Token: \$TOKEN' $BASE/api/enroll/poll   # 反复调用直到 state=done"
enroll_until_done
cmd "curl -s -X POST $BASE/api/enroll/commit   # 内部走假助手 add_face"
post /api/enroll/commit | jqish
cmd "curl -s -H 'X-FaceUnlock-Token: \$TOKEN' $BASE/api/enroll/poll   # commit 后回到 idle"
get /api/enroll/poll | jqish

sec "5. /api/faces（缩略图 + 质量）"
cmd "curl -s -H 'X-FaceUnlock-Token: \$TOKEN' '$BASE/api/faces?user=alice'"
get "/api/faces?user=alice" | jqish
FID="$(get "/api/faces?user=alice" | python3 -c 'import json,sys;print(json.load(sys.stdin)["faces"][0]["id"])')"
echo "取第一张人脸 id = $FID"

sec "6. /api/face/rename"
cmd "curl -s -X POST -d '{\"user\":\"alice\",\"id\":\"$FID\",\"label\":\"重命名测试\"}' $BASE/api/face/rename"
post /api/face/rename "{\"user\":\"alice\",\"id\":\"$FID\",\"label\":\"重命名测试\"}" | jqish
get "/api/faces?user=alice" | python3 -c 'import json,sys;print("标签:", [f["label"] for f in json.load(sys.stdin)["faces"]])'

sec "7. /api/face/delete"
cmd "curl -s -X POST -d '{\"user\":\"alice\",\"id\":\"$FID\"}' $BASE/api/face/delete"
post /api/face/delete "{\"user\":\"alice\",\"id\":\"$FID\"}" | jqish
get "/api/faces?user=alice" | python3 -c 'import json,sys;d=json.load(sys.stdin);print("剩余人脸数:", len(d["faces"]))'

sec "8. /api/verify/start -> poll -> stop（真实余弦打分）"
cmd "curl -s -X POST -d '{\"user\":\"alice\"}' $BASE/api/verify/start"
post /api/verify/start '{"user":"alice"}' | jqish
cmd "curl -s -H 'X-FaceUnlock-Token: \$TOKEN' $BASE/api/verify/poll"
get /api/verify/poll | jqish
cmd "curl -s -X POST $BASE/api/verify/stop"
post /api/verify/stop | jqish

sec "9. /api/config（阈值越界 -> INVALID；改阈值再读回）"
cmd "curl -s -X POST -d '{\"threshold\":0.95}' $BASE/api/config   # 期望 INVALID"
post /api/config '{"threshold":0.95}' | jqish
cmd "curl -s -X POST -d '{\"threshold\":0.62,\"services\":{\"sudo\":false}}' $BASE/api/config"
post /api/config '{"threshold":0.62,"services":{"sudo":false}}' | python3 -c 'import json,sys;c=json.load(sys.stdin);print("threshold=",c["threshold"]," services.sudo=",c["services"]["sudo"]," (顶层与 config 字段都返回完整配置: ", c["threshold"]==c["config"]["threshold"], ")")'
cmd "curl -s -H 'X-FaceUnlock-Token: \$TOKEN' $BASE/api/state   # 读回"
get /api/state | python3 -c 'import json,sys;c=json.load(sys.stdin)["config"];print("state.config.threshold =", c["threshold"], "| services.sudo =", c["services"]["sudo"], "| 磁盘上:", __import__("json").load(open("/tmp/fu-gui-test/config.json"))["threshold"])'
cmd "curl -s -X POST -d '{\"threshold\":0.50,\"services\":{\"sudo\":true}}' $BASE/api/config   # 改回"
post /api/config '{"threshold":0.50,"services":{"sudo":true}}' | jqish

sec "10. /api/pam disable / enable / panic"
cmd "curl -s -X POST -d '{\"action\":\"disable\"}' $BASE/api/pam"
post /api/pam '{"action":"disable"}' | jqish
cmd "curl -s -X POST -d '{\"action\":\"enable\"}' $BASE/api/pam"
post /api/pam '{"action":"enable"}' | jqish
cmd "curl -s -X POST -d '{\"action\":\"panic\"}' $BASE/api/pam"
post /api/pam '{"action":"panic"}' | jqish
get /api/state | python3 -c 'import json,sys;d=json.load(sys.stdin);print("panic 后: pam.enabled =", d["pam"]["enabled"], "| config.enabled =", d["config"]["enabled"])'

sec "11. /api/doctor"
cmd "curl -s -H 'X-FaceUnlock-Token: \$TOKEN' $BASE/api/doctor"
get /api/doctor | jqish

sec "12. /api/preview.mjpg（multipart 流）"
cmd "curl -s -D - -o $WORK/preview.mjpg --max-time 2 \"$BASE/api/preview.mjpg?token=\$TOKEN\" | head -6"
curl -s -D - -o "$WORK/preview.mjpg" --max-time 2 "$BASE/api/preview.mjpg?token=$TOKEN" | head -6
echo "落盘: $(ls -l "$WORK/preview.mjpg" | awk '{print $5" bytes"}')  前 3 帧边界:"
head -c 200 "$WORK/preview.mjpg" | tr -cd "[:print:]\n" | head -5

sec "13. /api/enroll/cancel（并验证无会话时 commit 返回 INVALID 而不是 500）"
cmd "curl -s -X POST $BASE/api/enroll/cancel"
post /api/enroll/cancel | jqish
get /api/enroll/poll | jqish
cmd "curl -s -X POST $BASE/api/enroll/commit   # 无会话 -> INVALID"
post /api/enroll/commit | jqish
cmd "curl -s -X POST -d '{}' $BASE/api/nope   # 未知接口 -> INVALID"
post /api/nope '{}' | jqish

sec "14. /api/face/delete_all"
cmd "curl -s -X POST -d '{\"user\":\"alice\"}' $BASE/api/face/delete_all"
post /api/face/delete_all '{"user":"alice"}' | jqish
get "/api/faces?user=alice" | python3 -c 'import json,sys;print("剩余人脸数:", len(json.load(sys.stdin)["faces"]))'

stop_bridge

sec "15. 无相机（FACEUNLOCK_FAKE_CAMERA=busy）：enroll 必须优雅返回 BUSY，不能 500"
cmd "FACEUNLOCK_FAKE_CAMERA=busy bin/faceunlock-gui-bridge"
start_bridge busy FACEUNLOCK_FAKE_CAMERA=busy
cmd "curl -s -X POST -d '{\"user\":\"alice\",\"label\":\"x\",\"count\":3}' $BASE/api/enroll/start"
post /api/enroll/start '{"user":"alice","label":"x","count":3}' | jqish
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST -H "X-FaceUnlock-Token: $TOKEN" \
     -H 'Content-Type: application/json' -d '{"user":"alice","count":3}' "$BASE/api/enroll/start"
cmd "curl -s -H 'X-FaceUnlock-Token: \$TOKEN' $BASE/api/enroll/poll"
get /api/enroll/poll | jqish
cmd "curl -s -X POST $BASE/api/enroll/cancel"
post /api/enroll/cancel | jqish
stop_bridge

sec "16. 真实摄像头 /dev/video0（不用假帧）：录入 -> 落库 -> 识别测试真实打分"
start_bridge real FACEUNLOCK_FAKE_CAMERA=0
cmd "curl -s -X POST -d '{\"user\":\"alice\",\"label\":\"真机\",\"count\":3}' $BASE/api/enroll/start"
post /api/enroll/start '{"user":"alice","label":"真机","count":3}' | jqish
cmd "curl -s -H 'X-FaceUnlock-Token: \$TOKEN' $BASE/api/enroll/poll   # 反复调用直到 done"
enroll_until_done
cmd "curl -s -X POST $BASE/api/enroll/commit"
post /api/enroll/commit | jqish
get "/api/faces?user=alice" | python3 -c 'import json,sys;d=json.load(sys.stdin);print("真机录入人脸数:",len(d["faces"]),[f["quality"].get("height") for f in d["faces"]])'
cmd "curl -s -X POST -d '{\"user\":\"alice\"}' $BASE/api/verify/start"
post /api/verify/start '{"user":"alice"}' | jqish
for i in 1 2 3; do
  cmd "curl -s -H 'X-FaceUnlock-Token: \$TOKEN' $BASE/api/verify/poll"
  get /api/verify/poll | jqish
  sleep 0.5
done
cmd "curl -s -X POST $BASE/api/verify/stop"
post /api/verify/stop | jqish
post /api/face/delete_all '{"user":"alice"}' >/dev/null
stop_bridge

sec "17. pkexec 被取消（假助手 exit 126）-> code=AUTH_REQUIRED，界面不崩"
cmd "FACEUNLOCK_ADMIN_CMD='python3 $REPO/tools/fake_admin.py serve --exit-auth' bin/faceunlock-gui-bridge"
start_bridge auth FACEUNLOCK_ADMIN_CMD="python3 $REPO/tools/fake_admin.py serve --exit-auth"
cmd "curl -s -H 'X-FaceUnlock-Token: \$TOKEN' '$BASE/api/faces?user=alice'"
get "/api/faces?user=alice" | jqish
cmd "curl -s -H 'X-FaceUnlock-Token: \$TOKEN' $BASE/api/doctor"
get /api/doctor | jqish
stop_bridge

sec "18. 助手进程崩溃 -> 自动重启一次后成功"
rm -f "$WORK/died"
cmd "FACEUNLOCK_ADMIN_CMD='python3 tools/fake_admin.py serve --die-after 2 --die-marker $WORK/died' bin/faceunlock-gui-bridge"
start_bridge restart FACEUNLOCK_ADMIN_CMD="python3 $REPO/tools/fake_admin.py serve --die-after 2 --die-marker $WORK/died"
cmd "curl -s -H 'X-FaceUnlock-Token: \$TOKEN' '$BASE/api/faces?user=alice'   # 第 1 次调用用掉 hello+list_faces 后助手崩溃"
get "/api/faces?user=alice" | jqish
cmd "curl -s -H 'X-FaceUnlock-Token: \$TOKEN' '$BASE/api/faces?user=alice'   # 第 2 次调用触发重启（只重启一次）后成功"
get "/api/faces?user=alice" | jqish
echo; echo "桥接服务 stderr（重启证据）:"; grep -E "已启动特权助手|助手就绪" "$WORK/restart.err" | tail -4
stop_bridge

sec "完成"
echo "所有输出目录: $WORK"
