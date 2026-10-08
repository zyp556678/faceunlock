#!/bin/bash
# 在没有 debhelper / libpam0g-dev 的机器上，手工装配 faceunlock-full 的 deb。
#
# 为什么需要它：目标机（Ubuntu 26.04）没有免密 sudo，装不了 debhelper 与
# libpam0g-dev，而 `make deb`（dpkg-buildpackage → dh）两者都要。这个脚本用
# dpkg-deb 直接装配，等价物如下：
#
#   debian/rules + dh_*                →  packaging/install-tree.sh 生成内容树
#   dh_gencontrol                      →  control 取自**已安装**的 faceunlock-full
#                                         （Depends/shlibs 是本机解析好的真实值），
#                                         只替换 Version / Description / Installed-Size
#   dh_installdeb                      →  手工写 conffiles + 维护者脚本
#   dh_md5sums                         →  手工生成 DEBIAN/md5sums
#   dh_builddeb                        →  dpkg-deb --build
#
# 打包输入（models/vendor/GUI 二进制）都是 .gitignore 里的大体积产物：
#   models/  vendor/  优先用仓库里的，缺了就取已安装系统的那一份
#   gui 二进制        优先用 gui/src-tauri/target/release/，缺了就用 /usr/bin/faceunlock-gui
#
# 用法: tools/build-deb-offline.sh [输出目录]        # 默认输出到仓库根
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")/.."
repo="$PWD"
outdir="${1:-$repo}"
pkg=faceunlock-full
ver=$(dpkg-parsechangelog -SVersion 2>/dev/null || python3 -c 'import re;print(re.search(r"\(([^)]+)\)",open("debian/changelog").readline()).group(1))')
arch=$(dpkg-architecture -qDEB_HOST_MULTIARCH)

log() { printf '\033[1m==> %s\033[0m\n' "$*"; }

# ---------- 1. PAM 开发头文件 ----------
if [ ! -f /usr/include/security/pam_modules.h ]; then
    log "系统缺 libpam0g-dev，改用 apt-get download 解包取头文件"
    cache=/tmp/faceunlock-pamdev
    if [ ! -f "$cache/usr/include/security/pam_modules.h" ]; then
        rm -rf "$cache" /tmp/faceunlock-pamdev-dl
        mkdir -p "$cache" /tmp/faceunlock-pamdev-dl
        ( cd /tmp/faceunlock-pamdev-dl && apt-get download libpam0g-dev >/dev/null 2>&1 )
        deb=$(ls /tmp/faceunlock-pamdev-dl/libpam0g-dev_*.deb | head -1)
        dpkg-deb -x "$deb" "$cache"
    fi
    PAM_CPPFLAGS="-I$cache/usr/include"
else
    PAM_CPPFLAGS=""
fi

# ---------- 2. 编译 PAM 模块 ----------
# 注意用 -l:libpam.so.0 而不是 -lpam：在只解包了 libpam0g-dev 的机器上，
# /usr/lib/.../libpam.so 是一个指向 libpam.so.0 的**悬空**符号链接（目标在
# libpam0g 里，路径不同），链接器找不到它就会**回退到 libpam.a 静态链接** ——
# 结果是模块自带一份 pam_* 符号定义（34 KB、导出了 pam_get_user 等 18 个符号），
# 在 PAM 栈里会造成符号劫持。显式写 soname 可以彻底避开这个陷阱。
log "编译 pam_faceunlock.so"
gcc $PAM_CPPFLAGS -shared -fPIC -O2 -Wall -Wextra \
    -o pam/pam_faceunlock.so pam/pam_faceunlock.c -l:libpam.so.0

# 构建期断言：必须动态依赖 libpam.so.0，且不得自己定义 pam_* 符号
if ! readelf -d pam/pam_faceunlock.so | grep -q 'libpam\.so\.0'; then
    echo "ERROR: pam_faceunlock.so 没有动态依赖 libpam.so.0（可能静态链了 libpam.a）" >&2
    exit 1
fi
if nm -D --defined-only pam/pam_faceunlock.so | grep -q ' T pam_get_user'; then
    echo "ERROR: pam_faceunlock.so 自己定义了 pam_get_user —— 静态链接了 libpam，会在 PAM 栈里劫持符号" >&2
    exit 1
fi
echo "  ✔ 动态链接 libpam.so.0，未导出 pam_* 符号（$(stat -c%s pam/pam_faceunlock.so) 字节）"

# ---------- 3. 准备大体积输入 ----------
if [ ! -f models/face_recognition_sface_2021dec.onnx ]; then
    log "仓库缺 models/，从已安装系统取一份（版本一致）"
    mkdir -p models
    cp -a /usr/share/faceunlock/models/. models/ 2>/dev/null || true
fi
if [ ! -d vendor/cv2 ]; then
    log "仓库缺 vendor/，从已安装系统取一份"
    mkdir -p vendor
    cp -a /usr/lib/faceunlock/vendor/. vendor/
fi
gui_bin=gui/src-tauri/target/release/faceunlock-gui
if [ ! -x "$gui_bin" ]; then
    log "没有 Tauri 产物，复用已安装的 /usr/bin/faceunlock-gui"
    mkdir -p "$(dirname "$gui_bin")"
    cp -a /usr/bin/faceunlock-gui "$gui_bin"
fi

# ---------- 4. 生成内容树 ----------
stage=$(mktemp -d /tmp/faceunlock-deb-XXXXXX)
trap 'rm -rf "$stage"' EXIT
log "生成内容树（install-tree.sh all）→ $stage/tree"
bash packaging/install-tree.sh all "$stage/tree"

DEBIAN="$stage/tree/DEBIAN"
mkdir -p "$DEBIAN"
install -m 0755 debian/$pkg.postinst "$DEBIAN/postinst"
install -m 0755 debian/$pkg.prerm    "$DEBIAN/prerm"
install -m 0755 debian/$pkg.postrm   "$DEBIAN/postrm"

# ---------- 4b. 文档与手册页 ----------
# 等价 dh_installman + dh_installdocs + dh_installchangelogs + dh_compress。
# 这一块**必须**有：新包若少了旧包已有的文件（比如手册页），dpkg 升级时会把它删掉。
log "安装 man page / docs / copyright / changelog"
python3 - "$stage/tree" "$pkg" <<'PY'
import gzip, os, shutil, sys

tree, pkg = sys.argv[1], sys.argv[2]

def gz(src, dst, mode=0o644):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(src, "rb") as fi, gzip.GzipFile(dst, "wb", compresslevel=9, mtime=0) as fo:
        shutil.copyfileobj(fi, fo)
    os.chmod(dst, mode)

# 手册页：段号必须从 .TH 行读（docs/faceunlock-gui.8 的 .TH 是第 1 节，最终落在 man1）
for line in open(f"debian/{pkg}.manpages", encoding="utf-8"):
    src = line.strip()
    if not src or src.startswith("#"):
        continue
    head = open(src, encoding="utf-8", errors="replace").readline().split()
    # .TH 里的名字是大写（FACEUNLOCK），dh_installman 会落成小写文件名
    name = (head[1] if len(head) > 1 else os.path.basename(src).rsplit(".", 1)[0]).lower()
    sect = head[2] if len(head) > 2 else src.rsplit(".", 1)[1]
    gz(src, f"{tree}/usr/share/man/man{sect}/{name}.{sect}.gz")

# 文档（dh_compress 会把 /usr/share/doc 下的文本文件压成 .gz）
if os.path.exists(f"debian/{pkg}.docs"):
    for line in open(f"debian/{pkg}.docs", encoding="utf-8"):
        src = line.strip()
        if src:
            gz(src, f"{tree}/usr/share/doc/{pkg}/{os.path.basename(src)}.gz")

# copyright
os.makedirs(f"{tree}/usr/share/doc/{pkg}", exist_ok=True)
shutil.copyfile("debian/copyright", f"{tree}/usr/share/doc/{pkg}/copyright")
os.chmod(f"{tree}/usr/share/doc/{pkg}/copyright", 0o644)

# changelog：本包是 3.0 (native)，debian/changelog 就是上游 changelog（dh 也这么装）
gz("debian/changelog", f"{tree}/usr/share/doc/{pkg}/changelog.gz")

# lintian overrides（等价 dh_lintian）
ov = f"debian/{pkg}.lintian-overrides"
if os.path.exists(ov):
    os.makedirs(f"{tree}/usr/share/lintian/overrides", exist_ok=True)
    shutil.copyfile(ov, f"{tree}/usr/share/lintian/overrides/{pkg}")
    os.chmod(f"{tree}/usr/share/lintian/overrides/{pkg}", 0o644)
PY

# conffiles：dpkg 视为配置文件的东西。Docker 上没有 debhelper 时最容易漏掉的就是它，
# 漏了会导致升级时把用户的配置/开关直接覆盖掉。
cat > "$DEBIAN/conffiles" <<'EOF'
/etc/faceunlock/config.json
/etc/systemd/system/polkit-agent-helper@.service.d/10-faceunlock-camera.conf
EOF

# ---------- 5. control ----------
log "装配 control（基数取自已安装的 $pkg，Version=$ver）"
python3 - "$DEBIAN/control" "$pkg" "$ver" "$stage/tree" <<'PY'
import subprocess, sys, os

dst, pkg, ver, tree = sys.argv[1:5]
KEEP = ["Package", "Source", "Section", "Priority", "Architecture", "Maintainer",
        "Homepage", "Depends", "Pre-Depends", "Recommends", "Suggests",
        "Conflicts", "Breaks", "Replaces", "Provides"]

raw = subprocess.run(["dpkg-query", "-s", pkg], capture_output=True, text=True).stdout
fields, order = {}, []
for line in raw.splitlines():
    if not line.strip():
        continue
    if line[0] in " \t":            # 续行（Description 用，这里不取）
        continue
    k, _, v = line.partition(":")
    if k in KEEP and k not in fields:
        fields[k] = v.strip()
        order.append(k)

# Provides 里带版本号（debian/control 写的是 ${binary:Version}，已安装包里是解析后的
# 旧版本号）。整体版本升级时必须一起替换，否则会声明 Provides: faceunlock (= 0.1.1)
# 却自称 0.1.2。
installed_ver = ""
for line in raw.splitlines():
    if line.startswith("Version:"):
        installed_ver = line.split(":", 1)[1].strip()
        break
if installed_ver and installed_ver != ver and "Provides" in fields:
    fields["Provides"] = fields["Provides"].replace(f"(= {installed_ver})", f"(= {ver})")

# Description 从 debian/control 里取（权威文本，含本次改动说明）
desc, in_pkg, in_desc = [], False, False
for line in open("debian/control", encoding="utf-8"):
    if line.startswith("Package:"):
        in_pkg = line.split(":", 1)[1].strip() == pkg
        in_desc = False
        continue
    if in_pkg and line.startswith("Description:"):
        in_desc = True
        desc.append(line.rstrip("\n"))
        continue
    if in_desc:
        if line.startswith(" ") or line.startswith("\t"):
            desc.append(line.rstrip("\n"))
        else:
            in_desc = False

# Installed-Size（KiB，不含 DEBIAN）
total = 0
for root, _dirs, files in os.walk(tree):
    if "/DEBIAN" in root:
        continue
    for f in files:
        p = os.path.join(root, f)
        if not os.path.islink(p):
            total += os.path.getsize(p)
out = [f"{k}: {fields[k]}" for k in order]
out.append(f"Version: {ver}")
out.append(f"Installed-Size: {max(1, total // 1024)}")
out.append(f"Description: {desc[0].split(':', 1)[1].strip()}")
out.extend(desc[1:])   # 续行必须保留前导空格（Debian control 格式）
open(dst, "w", encoding="utf-8").write("\n".join(out) + "\n")
print("\n".join(out[:4] + ["..."]))
PY

# ---------- 6. md5sums ----------
( cd "$stage/tree" && find . -path ./DEBIAN -prune -o -type f -print0 \
    | xargs -0 -r md5sum | sed 's|\./||' > DEBIAN/md5sums )

# ---------- 7. 打包 ----------
mkdir -p "$outdir"
out="$outdir/${pkg}_${ver}_amd64.deb"
log "打包 → $out"
dpkg-deb --build --root-owner-group -Zxz "$stage/tree" "$out"
ls -lh "$out"
dpkg-deb -I "$out" | sed -n '1,25p'

# ---------- 8. 与已安装包对比文件清单 ----------
# 最危险的低级错误：新包比旧包少文件 —— dpkg 升级时会把"新包没有的路径"当成被删除，
# 于是手册页、doc 之类会在升级后凭空消失。这里硬性拦住。
if dpkg -s "$pkg" >/dev/null 2>&1; then
    log "校验：新包是否覆盖了已安装 $pkg 的全部文件"
    python3 - "$out" "$pkg" <<'PY'
import os, subprocess, sys

deb, pkg = sys.argv[1], sys.argv[2]
lst = subprocess.run(["dpkg-deb", "-c", deb], capture_output=True, text=True).stdout
new = set()
for line in lst.splitlines():
    if not line.strip():
        continue
    path = line.split(maxsplit=5)[-1]
    if path.endswith("/"):
        continue
    new.add("/" + path[2:] if path.startswith("./") else path)
# dpkg -L 的目录项不带斜杠，用主机上的真实类型过滤掉目录
old = {p for p in subprocess.run(["dpkg", "-L", pkg], capture_output=True, text=True).stdout.split()
       if not p.endswith("/") and not os.path.isdir(p)}
missing = sorted(old - new)
extra = sorted(new - old)
print(f"  已安装包 {len(old)} 个文件；新包 {len(new)} 个")
if missing:
    print("  ❌ 新包缺少以下文件（升级会把这些删掉）：")
    for m in missing:
        print("     -", m)
    sys.exit(1)
print("  ✅ 已安装包的全部文件都在新包里")
for e in extra:
    print("     + 本次新增:", e)
PY
fi
