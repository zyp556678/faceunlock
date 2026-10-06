# faceunlock GUI 接口规范（冻结版 v1）

> 这是 Tauri 前端与 Python 后端之间的**唯一契约**。改动必须同时更新
> `faceunlock/gui_bridge.py`、本文件与前端代码。

## 0. 进程拓扑

```
┌──────────────────────────────┐
│ Tauri 应用 (Rust + WebView)   │  普通用户身份运行
│  · 只负责开窗 / 加载 UI        │
│  · 启动 faceunlock-gui-bridge │
│  · 读取握手行，把 port+token   │
│    注入 webview (init script) │
└───────────────┬──────────────┘
                │ 子进程 stdin/stdout
┌───────────────▼──────────────┐
│ faceunlock-gui-bridge (Py)    │  普通用户身份运行
│  · 127.0.0.1:<随机端口> HTTP  │
│  · 摄像头采集 / 预览 / 识别    │
│  · 特权操作转发给 admin 助手   │
└───────┬──────────────┬───────┘
        │ pkexec       │ 直接调用
┌───────▼────────┐  ┌──▼─────────────────┐
│ faceunlock-    │  │ faceunlock-capture │
│ admin (root)   │  │ (普通用户, 摄像头)  │
│ 模板库读写      │  │ YuNet+SFace 推理    │
│ PAM 开关       │  │                    │
└────────────────┘  └────────────────────┘
```

**权限模型**
* 模板库 `/var/lib/faceunlock` 为 `root:root 0700`，只有 root 助手能读写。
* 普通用户只能管理**自己**的人脸；管理他人需要调用者在 `sudo` 组（polkit 会先弹密码）。
* 前端永远拿不到 root 能力：所有特权动作都经 `faceunlock-admin` 校验。

## 1. 握手

`faceunlock-gui-bridge` 启动后在 **stdout 打印一行 JSON** 并 flush：

```json
{"event":"ready","port":38271,"token":"<43字符 urlsafe>","pid":12345}
```

Rust 侧读取该行，然后向 webview 注入：

```js
window.__FACEUNLOCK__ = { port: 38271, token: "<token>" };
```

后续所有 HTTP 请求都必须带 `X-FaceUnlock-Token: <token>`，否则返回 `401`。
服务只监听 `127.0.0.1`，端口随机，进程退出即失效。

## 2. HTTP API

所有响应 `Content-Type: application/json`。统一错误体：

```json
{"ok": false, "error": "人类可读的错误", "code": "AUTH_REQUIRED"}
```

`code` 取值：`AUTH_REQUIRED`（需要 polkit 授权且被取消/失败）、`BUSY`（摄像头被占用）、
`NO_FACE`、`NOT_ENROLLED`、`INVALID`、`INTERNAL`。

### 2.1 状态与列表

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state` | 全局状态 |
| GET | `/api/doctor` | 自检结果 |
| GET | `/api/faces?user=<u>` | 某人脸列表 |

`GET /api/state` →
```json
{
  "ok": true,
  "version": "0.1.0",
  "current_user": "alice",
  "can_manage_others": false,
  "privileged": true,
  "config": {
    "enabled": true, "threshold": 0.5, "required_frames": 2, "window_frames": 3,
    "timeout_ms": 4000, "no_face_timeout_ms": 1000,
    "services": {"gdm-password": true, "sudo": true, "su": true, "polkit-1": true,
                 "login": false, "sudo-i": true},
    "save_thumbnails": true, "camera": {"device": 0}
  },
  "pam": {"profile_installed": true, "enabled": false, "file": "/etc/pam.d/common-auth"},
  "users": [{"user": "alice", "uid": 1000, "face_count": 2}]
}
```

`GET /api/faces?user=alice` →
```json
{"ok": true, "user": "alice", "faces": [
  {"id": "a1b2c3d4", "label": "正面", "created": 1760000000,
   "quality": {"height": 265.0, "sharpness": 180.2, "brightness": 120.5},
   "thumb": "<base64 jpeg，无缩略图时为 null>"}
]}
```

`GET /api/doctor` →
```json
{"ok": true, "checks": [
  {"name": "摄像头", "status": "ok",   "detail": "/dev/video0 1280x720 MJPG"},
  {"name": "模型",   "status": "ok",   "detail": "YuNet+SFace 已加载"},
  {"name": "模板库", "status": "ok",   "detail": "/var/lib/faceunlock"},
  {"name": "PAM",    "status": "warn", "detail": "faceunlock profile 未启用"}
]}
```
`status` ∈ `ok | warn | fail`。

### 2.2 录入（增）

| 方法 | 路径 | 请求体 |
|---|---|---|
| POST | `/api/enroll/start` | `{"user":"alice","label":"正面","count":5}` |
| GET | `/api/enroll/poll` | – |
| POST | `/api/enroll/commit` | – |
| POST | `/api/enroll/cancel` | – |

`/api/enroll/poll` →
```json
{
  "ok": true, "state": "running",
  "session": {
    "user": "alice", "label": "正面",
    "captured": 3, "required": 5,
    "frame": "<base64 jpeg 预览>",
    "box": [620, 393, 214, 265],
    "message": "很好，请保持",
    "hint": "稍微靠近一点"
  }
}
```
`state` ∈ `idle | running | done | error`。
* 前端以 100~200ms 间隔轮询，把 `frame` 画到 canvas，`box` 用来画框。
* 采满 `required` 张后 `state` 变为 `done`，此时调用 `/api/enroll/commit` 落库。
* 每次成功采集都会略微改变拍摄提示（正面/左转/右转/抬头/低头），以提升模板多样性。

`/api/enroll/commit` → `{"ok": true, "added": 5, "ids": ["...","..."]}`
* 落库需要特权：内部通过 `pkexec faceunlock-admin` 完成，**首次会弹系统密码框**。
* 用户取消 polkit 授权 → `{"ok": false, "code": "AUTH_REQUIRED"}`。

### 2.3 改 / 删

| 方法 | 路径 | 请求体 |
|---|---|---|
| POST | `/api/face/rename` | `{"user":"u","id":"a1b2c3d4","label":"新名字"}` |
| POST | `/api/face/delete` | `{"user":"u","id":"a1b2c3d4"}` |
| POST | `/api/face/delete_all` | `{"user":"u"}` |

均返回 `{"ok": true}`。

### 2.4 识别测试

| 方法 | 路径 | 请求体 |
|---|---|---|
| POST | `/api/verify/start` | `{"user":"u"}` |
| GET | `/api/verify/poll` | – |
| POST | `/api/verify/stop` | – |

`/api/verify/poll` →
```json
{"ok": true, "state": "running", "score": 0.812, "passed": true,
 "threshold": 0.5, "frame": "<base64 jpeg>", "box": [620,393,214,265],
 "message": "相似度 0.812 ≥ 阈值 0.50"}
```
`state` ∈ `idle | running | stopped`。用于让用户边看画面边看分数，验证阈值是否合适。

### 2.5 配置与 PAM

| 方法 | 路径 | 请求体 |
|---|---|---|
| POST | `/api/config` | `{"threshold":0.55,"services":{"sudo":false},...}` 只传要改的字段 |
| POST | `/api/pam` | `{"action":"enable"｜"disable"｜"panic"}` |

`/api/config` 返回更新后的完整 config（同 `/api/state` 里的结构）。
阈值合法范围 `0.20 ~ 0.90`；越界返回 `code=INVALID`。

`/api/pam` 的 `panic` = 关闭总开关 + 从 PAM 栈移除，用于"一键还原"。
返回 `{"ok": true, "pam": {...}}`。

### 2.6 原始预览流

`GET /api/preview.mjpg` → `multipart/x-mixed-replace; boundary=frame` 的 MJPEG 流。
用于录入/识别之外的常驻小窗预览。前端 `<img src="/api/preview.mjpg?token=...">` 即可。
同一时刻只允许一个预览消费者。

## 3. 前端页面结构（建议）

单页，左侧导航三个标签：

1. **人脸管理**：顶部用户选择器（无权限管理他人时只显示自己）；
   人脸卡片网格（缩略图 + 标签 + 创建时间 + 改/删按钮）；右上"添加人脸"按钮打开录入对话框。
2. **识别测试**：大画面 + 实时相似度分数条 + 阈值参考线 + 通过/不通过徽标。
3. **设置**：总开关；场景开关（登录/锁屏、sudo、su、pkexec 管理员弹窗）；
   阈值滑块（带"本机实测：本人最低 0.70，冒充者最高 0.14"的说明）；
   多帧投票参数；`一键停用（panic）` 红色按钮；自检结果表格。

所有破坏性操作需二次确认。中文界面。

## 4. 错误与边界

* 摄像头被占用 → `code=BUSY`，界面提示"摄像头被其它程序占用，请关闭视频会议/浏览器后重试"。
* 未录入任何人脸时，"识别测试"应提示先去录入。
* 无 polkit 授权时，特权操作按钮应显示"需要管理员授权"而不是报错崩溃。
* 轮询接口在会话不存在时返回 `{"ok": true, "state": "idle"}`，不要返回错误。
