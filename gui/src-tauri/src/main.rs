//! faceunlock 管理界面 —— Tauri 外壳（保持最小职责）。
//!
//! 职责只有四件事：
//!   1. 以子进程方式启动 `faceunlock-gui-bridge`（普通用户身份）；
//!   2. 读它 stdout 的第一行握手 JSON，拿到 {port, token}；
//!   3. 用 `initialization_script` 把 `window.__FACEUNLOCK__ = {port, token}`
//!      注入 webview，之后所有 HTTP 都由前端直接与本地服务通信；
//!   4. 窗口关闭 / 进程退出时杀掉子进程。
//!
//! 不涉及任何特权操作：polkit 授权走 Python 侧的 `faceunlock-admin`。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::sync::Mutex;
use std::time::Duration;

use serde::Deserialize;
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent};

/// 与 Python 侧 gui_bridge 的握手超时（模型不加载，正常 < 1s；留足冷启动余量）
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(20);

const BRIDGE_NAME: &str = "faceunlock-gui-bridge";

#[derive(Debug, Deserialize)]
struct Handshake {
    port: u16,
    token: String,
}

/// 持有 bridge 子进程句柄，保证窗口关闭时能杀掉它。
struct BridgeProc {
    child: Mutex<Option<Child>>,
}

impl BridgeProc {
    fn new(child: Child) -> Self {
        Self { child: Mutex::new(Some(child)) }
    }

    fn kill(&self) {
        if let Ok(mut guard) = self.child.lock() {
            if let Some(mut child) = guard.take() {
                let _ = child.kill();
                let _ = child.wait();
                eprintln!("[faceunlock-gui] bridge 子进程已结束");
            }
        }
    }
}

/// 按优先级查找 bridge 可执行文件。
///
/// 1. 环境变量 `FACEUNLOCK_GUI_BRIDGE`（开发/调试用）
/// 2. 安装路径 `/usr/libexec/faceunlock-gui-bridge`
/// 3. `$FACEUNLOCK_ROOT/bin/...`
/// 4. 从可执行文件与当前工作目录逐级向上找 `<ancestor>/bin/faceunlock-gui-bridge`
///    （开发态 `cargo run` / `target/release/` 都能命中仓库）
fn find_bridge() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("FACEUNLOCK_GUI_BRIDGE") {
        if !p.trim().is_empty() {
            let path = PathBuf::from(&p);
            if path.is_file() {
                return Some(path);
            }
            eprintln!("[faceunlock-gui] FACEUNLOCK_GUI_BRIDGE={} 不是文件，继续查找", p);
        }
    }

    let mut candidates: Vec<PathBuf> = vec![
        PathBuf::from("/usr/libexec").join(BRIDGE_NAME),
        PathBuf::from("/usr/lib/faceunlock/bin").join(BRIDGE_NAME),
    ];

    if let Ok(root) = std::env::var("FACEUNLOCK_ROOT") {
        candidates.push(Path::new(&root).join("bin").join(BRIDGE_NAME));
    }

    let mut bases: Vec<PathBuf> = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            bases.push(dir.to_path_buf());
        }
    }
    if let Ok(cwd) = std::env::current_dir() {
        bases.push(cwd);
    }
    for base in bases {
        let mut cur: Option<&Path> = Some(base.as_path());
        let mut depth = 0;
        while let Some(dir) = cur {
            candidates.push(dir.join("bin").join(BRIDGE_NAME));
            if depth >= 6 {
                break;
            }
            depth += 1;
            cur = dir.parent();
        }
    }

    candidates.into_iter().find(|p| p.is_file())
}

/// 启动 bridge 并读取握手行。返回 (子进程, 握手)。
fn spawn_bridge(path: &Path) -> Result<(Child, Handshake), String> {
    let mut child = Command::new(path)
        .arg("--host")
        .arg("127.0.0.1")
        .arg("--port")
        .arg("0")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("无法启动 {}: {}", path.display(), e))?;

    let stdout = child.stdout.take().ok_or_else(|| "拿不到 bridge stdout".to_string())?;
    let stderr = child.stderr.take().ok_or_else(|| "拿不到 bridge stderr".to_string())?;

    let (tx, rx) = mpsc::channel::<String>();
    // stdout：第一行是握手 JSON，其余（正常情况下不会有）转发到 stderr 便于排障
    std::thread::spawn(move || {
        let mut sent = false;
        for line in BufReader::new(stdout).lines() {
            match line {
                Ok(l) => {
                    if !sent {
                        sent = true;
                        let _ = tx.send(l);
                    } else if !l.trim().is_empty() {
                        eprintln!("[faceunlock-gui][bridge stdout] {}", l);
                    }
                }
                Err(_) => break,
            }
        }
    });
    // stderr 必须持续抽干，否则管道写满会让 bridge 阻塞
    std::thread::spawn(move || {
        for line in BufReader::new(stderr).lines() {
            match line {
                Ok(l) => eprintln!("[faceunlock-gui][bridge] {}", l),
                Err(_) => break,
            }
        }
    });

    match rx.recv_timeout(HANDSHAKE_TIMEOUT) {
        Ok(line) => {
            let hs: Handshake = serde_json::from_str(line.trim())
                .map_err(|e| format!("握手 JSON 解析失败: {e}（原始内容: {line}）"))?;
            Ok((child, hs))
        }
        Err(e) => {
            let _ = child.kill();
            Err(format!("等待 bridge 握手超时/失败: {e}"))
        }
    }
}

/// 构造注入脚本：成功时给 port+token，失败时给错误信息，前端据此显示中文提示。
/// `tab` 为可选深链（--tab faces|verify|settings），前端启动后直接切到该标签。
fn init_script(port: Option<u16>, token: Option<String>, error: Option<String>,
               tab: Option<&str>) -> String {
    match (port, token) {
        (Some(port), Some(token)) => {
            let mut payload = serde_json::json!({
                "port": port,
                "token": token,
                "base": format!("http://127.0.0.1:{port}"),
            });
            if let Some(t) = tab {
                payload["tab"] = serde_json::Value::String(t.to_string());
            }
            format!("window.__FACEUNLOCK__ = {payload};")
        }
        _ => {
            let payload = serde_json::json!({
                "error": error.unwrap_or_else(|| "未知错误".to_string()),
            });
            format!("window.__FACEUNLOCK__ = null; window.__FACEUNLOCK_ERROR__ = {payload};")
        }
    }
}

/// 可选：`--tab verify` 或 `FACEUNLOCK_GUI_TAB=verify` 直接打开某个标签页
/// （桌面快捷方式/菜单「直接打开设置」用得到，不是必需参数）。
fn initial_tab() -> Option<String> {
    const TABS: [&str; 3] = ["faces", "verify", "settings"];
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        let v = if let Some(rest) = a.strip_prefix("--tab=") {
            Some(rest.to_string())
        } else if a == "--tab" {
            args.next()
        } else {
            None
        };
        if let Some(v) = v {
            let v = v.trim().to_lowercase();
            if TABS.contains(&v.as_str()) {
                return Some(v);
            }
            eprintln!("[faceunlock-gui] 忽略未知的 --tab 值: {v}");
        }
    }
    let v = std::env::var("FACEUNLOCK_GUI_TAB").unwrap_or_default();
    let v = v.trim().to_lowercase();
    if TABS.contains(&v.as_str()) {
        return Some(v);
    }
    None
}

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            let tab = initial_tab();
            let (script, proc) = match find_bridge() {
                Some(path) => {
                    eprintln!("[faceunlock-gui] 使用 bridge: {}", path.display());
                    match spawn_bridge(&path) {
                        Ok((child, hs)) => {
                            eprintln!(
                                "[faceunlock-gui] bridge 就绪: port={} pid={:?}",
                                hs.port,
                                child.id()
                            );
                            (init_script(Some(hs.port), Some(hs.token), None, tab.as_deref()), Some(child))
                        }
                        Err(e) => {
                            eprintln!("[faceunlock-gui] 启动 bridge 失败: {e}");
                            (init_script(None, None, Some(e), None), None)
                        }
                    }
                }
                None => {
                    let msg = format!(
                        "找不到 {BRIDGE_NAME}：请安装到 /usr/libexec/，或用 FACEUNLOCK_GUI_BRIDGE 指定路径"
                    );
                    eprintln!("[faceunlock-gui] {msg}");
                    (init_script(None, None, Some(msg), None), None)
                }
            };

            if let Some(child) = proc {
                app.manage(BridgeProc::new(child));
            } else {
                app.manage(BridgeProc::new(
                    Command::new("true")
                        .stdout(Stdio::null())
                        .stderr(Stdio::null())
                        .spawn()
                        .expect("占位子进程"),
                ));
            }

            let window = WebviewWindowBuilder::new(
                app,
                "main",
                WebviewUrl::App("index.html".into()),
            )
            .title("faceunlock · 人脸识别登录管理")
            .inner_size(1180.0, 800.0)
            .min_inner_size(960.0, 640.0)
            .initialization_script(&script)
            .build()?;
            let _ = window.set_focus();
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::Destroyed = event {
                eprintln!("[faceunlock-gui] 窗口已销毁，清理子进程");
                if let Some(state) = window.app_handle().try_state::<BridgeProc>() {
                    state.kill();
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("Tauri 应用初始化失败")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                if let Some(state) = app.try_state::<BridgeProc>() {
                    state.kill();
                }
            }
        });
}
