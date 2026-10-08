# faceunlock —— 开发态构建入口
#
# 打 deb 请直接用 dpkg-buildpackage（见 debian/rules），本 Makefile 只负责
# 「改完代码立刻验证」这一条快路径。
#
#   make            编译 PAM 模块
#   make check      语法检查 + 安全属性单元测试 + 无相机冒烟测试
#   make acceptance 完整验收套件（需要 root，31 项）
#   make deb        打三个 deb
#   make clean      清理构建产物

CC      ?= cc
CFLAGS  ?= -O2 -Wall -Wextra
PAM_SO   = pam/pam_faceunlock.so
PAM_SRC  = pam/pam_faceunlock.c

PY      ?= python3
PYFILES  = $(wildcard faceunlock/*.py) $(wildcard bin/*) $(wildcard tools/*.py)

.PHONY: all check acceptance deb clean deps doctor help

all: $(PAM_SO)

$(PAM_SO): $(PAM_SRC)
	# 用 -l:libpam.so.0 而不是 -lpam：libpam.so 这个开发符号链接一旦缺失/悬空
	# （例如只解包了 libpam0g-dev 的场景），链接器会静默回退到 libpam.a 静态链接，
	# 让模块自带一份 pam_* 符号定义 —— 在 PAM 栈里是符号劫持。显式 soname 免疫。
	$(CC) $(CPPFLAGS) $(CFLAGS) -shared -fPIC -o $@ $< -l:libpam.so.0
	@echo "→ $@ 已构建"

# ---------------- 验证 ----------------

# 语法检查：bin/ 下的脚本没有 .py 后缀，单独列出
check: $(PAM_SO)
	$(PY) -m py_compile faceunlock/*.py tools/*.py
	@for f in bin/faceunlock bin/faceunlock-auth bin/faceunlock-admin bin/faceunlock-gui-bridge; do \
		$(PY) -m py_compile "$$f" || exit 1; \
	done
	@echo "→ Python 语法检查通过"
	# 守护「PAM 助手绝不写 stdout」这条不变量（README 技术选型第 8 条）
	@if grep -n 'print(f\?"' bin/faceunlock-auth | grep -v 'file=sys.stderr' | grep -q .; then \
		echo "❌ bin/faceunlock-auth 存在未指定 stderr 的 print，会污染 polkit 协议通道"; \
		grep -n 'print(' bin/faceunlock-auth; exit 1; \
	fi
	@if ! grep -q 'STDOUT_FILENO' $(PAM_SRC); then \
		echo "❌ $(PAM_SRC) 缺少 stdout 重定向（polkit 场景会死锁）"; exit 1; \
	fi
	@echo "→ stdout 不变量检查通过"
	# 守护「模块必须动态依赖 libpam，且不得自带 pam_* 符号定义」：
	# 静态链进 libpam.a 会在 PAM 栈里劫持 pam_get_user/pam_get_item 等符号。
	@if command -v readelf >/dev/null 2>&1 && command -v nm >/dev/null 2>&1; then \
		readelf -d $(PAM_SO) | grep -q 'libpam\.so\.0' || { \
			echo "❌ $(PAM_SO) 没有动态依赖 libpam.so.0（静态链了 libpam.a？）"; exit 1; }; \
		nm -D --defined-only $(PAM_SO) | grep -q ' T pam_get_user' && { \
			echo "❌ $(PAM_SO) 自带 pam_get_user 定义（静态链了 libpam.a，会劫持符号）"; exit 1; }; \
		echo "→ libpam 动态链接检查通过"; \
	else \
		echo "→ 跳过 libpam 链接检查（缺少 readelf/nm）"; \
	fi
	$(PY) tools/selftest.py

# 无摄像头也能跑：用合成帧 + test_output/smoke_feats.npy
smoke:
	FACEUNLOCK_FAKE_CAMERA=1 $(PY) tools/smoke_test.py

acceptance: $(PAM_SO)
	sudo tools/acceptance.sh

# ---------------- 打包 / 依赖 ----------------

deps:
	scripts/fetch-deps.sh

deb: $(PAM_SO)
	dpkg-buildpackage -b -us -uc
	@echo "→ 产物在上一级目录"

doctor: $(PAM_SO)
	$(PY) bin/faceunlock doctor

clean:
	rm -f $(PAM_SO)
	find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete 2>/dev/null || true
	@echo "→ 已清理"

help:
	@grep -E '^[a-zA-Z_-]+:' $(MAKEFILE_LIST) | cut -d: -f1 | sort -u | tr '\n' ' '
	@echo
