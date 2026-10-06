#!/bin/bash
# 安装 Tauri v2 构建工具链（Ubuntu 24.04 + 国内镜像）
# 说明: noble 自带的 rustc 1.75 低于 Tauri v2 要求的 1.77.2+，故必须走 rustup。
#       static.rust-lang.org 与 crates.io 直连在本机不可用，统一走 rsproxy.cn 镜像。
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "===== 1/4 apt 构建依赖 ====="
apt-get install -y -qq \
  libwebkit2gtk-4.1-dev libgtk-3-dev libsoup-3.0-dev \
  libjavascriptcoregtk-4.1-dev librsvg2-dev patchelf \
  libayatana-appindicator3-dev file wget
echo "apt deps OK"

echo "===== 2/4 rustup ====="
export RUSTUP_HOME=/root/.rustup CARGO_HOME=/root/.cargo
export RUSTUP_DIST_SERVER=https://rsproxy.cn
export RUSTUP_UPDATE_ROOT=https://rsproxy.cn/rustup
export PATH="$CARGO_HOME/bin:$PATH"

if ! command -v rustup >/dev/null 2>&1; then
  if apt-get install -y -qq rustup >/dev/null 2>&1 && command -v rustup >/dev/null 2>&1; then
    echo "rustup 来自 apt"
  else
    echo "apt 无 rustup，改用 rsproxy 官方脚本"
    curl -sSf https://rsproxy.cn/rustup-init.sh | sh -s -- -y --profile minimal --default-toolchain stable
  fi
fi
rustup set profile minimal || true
rustup default stable
echo "rustup OK"

echo "===== 3/4 cargo 镜像配置 ====="
mkdir -p "$CARGO_HOME"
if [ ! -f "$CARGO_HOME/config.toml" ]; then
cat > "$CARGO_HOME/config.toml" <<'EOF'
[source.crates-io]
replace-with = 'rsproxy-sparse'

[source.rsproxy-sparse]
registry = "sparse+https://rsproxy.cn/index/"

[registries.rsproxy]
index = "https://rsproxy.cn/crates.io-index"

[net]
git-fetch-with-cli = true
EOF
fi
echo "cargo mirror OK"

echo "===== 4/4 版本与连通性验证 ====="
rustc --version
cargo --version
echo "--- 用真实 crate 验证 crates.io 镜像 ---"
tmp=$(mktemp -d)
cd "$tmp"
cargo init --name probe -q 2>/dev/null || cargo new probe -q
cd probe 2>/dev/null || cd "$tmp"
cargo add serde -q 2>&1 | tail -2 || true
timeout 180 cargo fetch 2>&1 | tail -5
echo "cargo fetch exit=$?"
cd /; rm -rf "$tmp"
echo "===== 工具链就绪 ====="
