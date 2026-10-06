#!/usr/bin/env python3
"""生成 README 用的界面示意图（docs/images/*.png）。

为什么需要这个脚本：真实的界面截图里含有开发者本人的人脸和真实用户名，
不能进入公开仓库（见 PRIVACY.md）。这里用同一套配色与布局渲染**占位内容**，
读者能看清 UI 结构，同时不含任何真实生物特征。

用法: python3 tools/make_doc_images.py
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

# 与 gui/src/style.css 保持一致的调色板
BG = (14, 16, 20)
BG2 = (20, 23, 30)
PANEL = (25, 29, 38)
PANEL2 = (31, 36, 48)
BORDER = (42, 48, 64)
TEXT = (230, 233, 239)
DIM = (152, 162, 179)
ACCENT = (56, 189, 248)
ACCENT_DIM = (14, 165, 233)
OK = (34, 197, 94)
WARN = (245, 158, 11)

CJK = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
CJK_B = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"

W, H = 1400, 880
SIDEBAR = 220


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = CJK_B if bold else CJK
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


F_TITLE = font(26, True)
F_H2 = font(15, True)
F_BODY = font(13)
F_SMALL = font(11)
F_TINY = font(10)
F_BIG = font(46, True)


def text(d: ImageDraw.ImageDraw, xy, s, f, fill=TEXT, anchor="la"):
    d.text(xy, s, font=f, fill=fill, anchor=anchor)


def rounded(d, box, r, fill=None, outline=None, width=1):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def base_shell(title: str, subtitle: str, active: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    """画出侧边栏 + 页头，返回画布供各页面继续绘制。"""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # 侧边栏
    d.rectangle([0, 0, SIDEBAR, H], fill=BG2)
    d.line([SIDEBAR, 0, SIDEBAR, H], fill=BORDER, width=1)

    # 品牌区：圆角方块 + 相机图形
    rounded(d, [22, 26, 60, 64], 10, fill=ACCENT_DIM)
    d.ellipse([32, 38, 46, 52], outline=(4, 20, 31), width=2)
    d.rectangle([37, 34, 41, 38], fill=(4, 20, 31))
    text(d, (72, 32), "faceunlock", F_H2)
    text(d, (72, 50), "人脸识别登录", F_SMALL, DIM)

    d.line([16, 92, SIDEBAR - 16, 92], fill=BORDER, width=1)

    # 导航项
    items = [("manage", "人脸管理"), ("verify", "识别测试"), ("settings", "设置")]
    y = 112
    for key, label in items:
        on = key == active
        if on:
            rounded(d, [14, y, SIDEBAR - 14, y + 42], 9, fill=(22, 35, 46),
                    outline=(30, 75, 99))
        text(d, (34, y + 21), label, F_BODY, (223, 242, 255) if on else DIM, anchor="lm")
        y += 52

    # 左下角状态
    rounded(d, [14, H - 92, SIDEBAR - 14, H - 52], 8, fill=(23, 27, 35), outline=BORDER)
    text(d, (28, H - 72), "后端就绪（未授权特权操作）", F_TINY, (255, 224, 172), anchor="lm")
    text(d, (18, H - 32), "alice · v0.1.0", F_SMALL, DIM)

    # 页头
    text(d, (SIDEBAR + 36, 34), title, F_TITLE)
    text(d, (SIDEBAR + 36, 78), subtitle, F_BODY, DIM)
    return img, d


def anonymized_avatar(size: int, label: str, seed: int) -> Image.Image:
    """合成一个抽象的'人脸占位'缩略图，带检测框与关键点，不含真实人脸。"""
    im = Image.new("RGB", (size, size), (38, 42, 52))
    d = ImageDraw.Draw(im)
    # 背景纹理
    for i in range(0, size, 24):
        d.line([0, i, size, i], fill=(44, 48, 60), width=1)

    cx, cy = size // 2, int(size * 0.52)
    # 头 + 肩
    d.ellipse([cx - size * 0.20, cy - size * 0.30, cx + size * 0.20, cy + size * 0.16],
              fill=(58, 64, 78))
    d.ellipse([cx - size * 0.34, cy + size * 0.12, cx + size * 0.34, cy + size * 0.62],
              fill=(52, 58, 72))
    # 五官占位
    ew = size * 0.045
    for dx in (-size * 0.085, size * 0.085):
        d.ellipse([cx + dx - ew, cy - size * 0.075, cx + dx + ew, cy - size * 0.075 + ew * 2],
                  fill=(80, 88, 104))
    d.arc([cx - size * 0.10, cy + size * 0.01, cx + size * 0.10, cy + size * 0.11],
          200, 340, fill=(80, 88, 104), width=max(2, size // 60))

    # YuNet 风格的绿色检测框 + 5 个关键点
    bx0, by0 = size * 0.24, size * 0.16
    bx1, by1 = size * 0.76, size * 0.84
    d.rectangle([bx0, by0, bx1, by1], outline=(0, 255, 0), width=2)
    for px, py in ((0.40, 0.40), (0.60, 0.40), (0.50, 0.53), (0.42, 0.64), (0.58, 0.64)):
        r = 3
        d.ellipse([bx0 + (bx1 - bx0) * px - r, by0 + (by1 - by0) * py - r,
                   bx0 + (bx1 - bx0) * px + r, by0 + (by1 - by0) * py + r],
                  fill=(0, 220, 0))
    # 框顶文字，模仿 YuNet 的 "人脸 0.98x 285x285"
    text(d, (bx0 + 2, by0 - 12), label, F_TINY, (0, 255, 0))
    return im


def page_manage() -> Image.Image:
    img, d = base_shell("人脸管理", "模板只保存 128 维特征（不可逆）与可选缩略图。管理他人需要管理员授权。",
                        "manage")

    # 右上工具栏
    rounded(d, [W - 470, 34, W - 230, 70], 8, fill=(23, 27, 35), outline=BORDER)
    text(d, (W - 452, 52), "用户  alice", F_BODY, anchor="lm")
    text(d, (W - 250, 52), "▾", F_BODY, DIM, anchor="mm")
    rounded(d, [W - 218, 34, W - 132, 70], 8, fill=(23, 27, 35), outline=BORDER)
    text(d, (W - 175, 52), "刷新", F_BODY, anchor="mm")
    rounded(d, [W - 120, 34, W - 36, 70], 8, fill=ACCENT_DIM)
    text(d, (W - 78, 52), "+ 添加人脸", F_BODY, (4, 20, 31), anchor="mm")

    # 预览开关
    rounded(d, [SIDEBAR + 36, 112, SIDEBAR + 156, 148], 8, fill=(23, 27, 35), outline=BORDER)
    text(d, (SIDEBAR + 96, 130), "开启实时预览", F_BODY, anchor="mm")
    text(d, (SIDEBAR + 170, 130), "MJPEG 预览，同一时刻只允许一个消费者；录入/测试会自动暂停它。",
         F_SMALL, DIM, anchor="lm")

    # 三张人脸卡片
    card_w, thumb_h, gap = 300, 300, 26
    card_h = thumb_h + 210
    x = SIDEBAR + 36
    y = 172
    poses = ["正面", "稍微左转", "稍微右转"]
    for i, label in enumerate(poses):
        rounded(d, [x, y, x + card_w, y + card_h], 12, fill=PANEL, outline=BORDER)
        thumb = anonymized_avatar(thumb_h, f"人脸 0.9{7 - i}x  285x285", i)
        img.paste(thumb, (x, y))
        d.rectangle([x, y, x + card_w, y + thumb_h], outline=BORDER, width=1)

        ty = y + thumb_h + 18
        text(d, (x + 18, ty), label, F_H2)
        ty += 32
        text(d, (x + 18, ty), "创建于 2026-10-06 17:32", F_SMALL, DIM)
        ty += 22
        text(d, (x + 18, ty), "质量: 高度 285px · 清晰度 241", F_SMALL, DIM)
        ty += 18
        text(d, (x + 18, ty), "亮度 150", F_SMALL, DIM)

        by = y + card_h - 52
        rounded(d, [x + 18, by, x + 108, by + 36], 8, fill=(23, 27, 35), outline=BORDER)
        text(d, (x + 63, by + 18), "重命名", F_BODY, anchor="mm")
        rounded(d, [x + 120, by, x + 200, by + 36], 8, fill=(239, 68, 68))
        text(d, (x + 160, by + 18), "删除", F_BODY, (25, 10, 10), anchor="mm")
        x += card_w + gap

    text(d, (SIDEBAR + 36, H - 60),
         "占位示意图：真实截图中的人脸已替换为合成图形，仓库不含任何真实生物特征数据。",
         F_SMALL, (110, 118, 132))
    return img


def page_verify() -> Image.Image:
    img, d = base_shell("识别测试", "边看画面边看相似度，用来确认阈值是否合适。本机实测：本人最低 0.70，冒充者最高 0.14。",
                        "verify")

    # 右上按钮
    rounded(d, [W - 248, 34, W - 150, 70], 8, fill=ACCENT_DIM)
    text(d, (W - 199, 52), "开始测试", F_BODY, (4, 20, 31), anchor="mm")
    rounded(d, [W - 138, 34, W - 36, 70], 8, fill=(23, 27, 35), outline=BORDER)
    text(d, (W - 87, 52), "停止", F_BODY, anchor="mm")

    # 左侧视频区：先画在独立画布上，最后用圆角遮罩贴回主图，
    # 这样人像/肩部无论多大都会被裁在框内（不会被裁切到框外）。
    vx, vy, vw, vh = SIDEBAR + 36, 112, 820, 500
    vid = Image.new("RGB", (vw, vh), (30, 34, 42))
    vd = ImageDraw.Draw(vid)
    # 房间背景的抽象化：窗 + 衣柜
    vd.rectangle([60, 60, 260, 300], fill=(58, 70, 86))
    vd.rectangle([700, 90, 780, 460], fill=(44, 50, 62))
    for i in range(0, vw, 40):
        vd.line([i, 0, i, vh], fill=(34, 38, 47), width=1)

    # 人像占位 + 检测框
    px, py = 320, 132
    vd.ellipse([px - 46, py + 208, px + 220, py + vh + 60], fill=(64, 72, 88))
    vd.ellipse([px, py, px + 174, py + 226], fill=(72, 80, 96))
    vd.rectangle([px + 6, py + 4, px + 168, py + 206], outline=(0, 255, 0), width=3)
    for fx, fy in ((0.36, 0.38), (0.64, 0.38), (0.50, 0.54), (0.40, 0.70), (0.60, 0.70)):
        cx2, cy2 = px + 6 + 162 * fx, py + 4 + 202 * fy
        vd.ellipse([cx2 - 4, cy2 - 4, cx2 + 4, cy2 + 4], fill=(0, 220, 0))
    text(vd, (px + 8, py - 16), "人脸 0.98x  285x285", F_SMALL, (0, 255, 0))

    mask = Image.new("L", (vw, vh), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, vw - 1, vh - 1], radius=12, fill=255)
    img.paste(vid, (vx, vy), mask)

    # 框 + 通过徽标画在主图上（覆盖在视频之上）
    rounded(d, [vx, vy, vx + vw, vy + vh], 12, outline=BORDER)
    rounded(d, [vx + 20, vy + 20, vx + 96, vy + 52], 16, fill=(16, 36, 26), outline=(31, 81, 51))
    text(d, (vx + 58, vy + 36), "通过", F_BODY, (185, 246, 206), anchor="mm")

    # 右侧分数面板
    rx, rw = vx + vw + 26, W - (vx + vw + 26) - 36
    rounded(d, [rx, vy, rx + rw, vy + 230], 12, fill=PANEL, outline=BORDER)
    text(d, (rx + 22, vy + 24), "相似度", F_H2)
    text(d, (rx + 22, vy + 84), "0.830", F_BIG, anchor="lm")
    text(d, (rx + rw - 22, vy + 108), "阈值 0.50", F_SMALL, DIM, anchor="rm")
    # 进度条
    bx, by, bw, bh = rx + 22, vy + 140, rw - 44, 10
    rounded(d, [bx, by, bx + bw, by + bh], 5, fill=(35, 40, 52))
    filled = int(bw * 0.830)
    rounded(d, [bx, by, bx + filled, by + bh], 5, fill=OK)
    rounded(d, [bx, by, bx + int(bw * 0.5), by + bh], 5, fill=ACCENT_DIM)
    # 阈值参考线
    tx = bx + int(bw * 0.5)
    d.line([tx, by - 6, tx, by + bh + 6], fill=TEXT, width=2)
    text(d, (rx + 22, vy + 172), "相似度 0.830 ≥ 阈值 0.50", F_BODY, OK)

    # 说明面板
    rounded(d, [rx, vy + 250, rx + rw, vy + 470], 12, fill=PANEL, outline=BORDER)
    text(d, (rx + 22, vy + 274), "怎么看这个分数", F_H2)
    lines = [
        "· 分数 = 与模板库中最高的余弦相似度。",
        "· 绿色徽标表示 ≥ 阈值，登录会判为通过。",
        "· 若本人分数长期低于阈值，请到",
        "  「人脸管理」重新录入，注意光线与",
        "  清晰度。",
    ]
    ly = vy + 312
    for ln in lines:
        text(d, (rx + 22, ly), ln, F_SMALL, DIM)
        ly += 24

    text(d, (SIDEBAR + 36, H - 60),
         "占位示意图：真实截图中的人脸已替换为合成图形，仓库不含任何真实生物特征数据。",
         F_SMALL, (110, 118, 132))
    return img


def main() -> int:
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "docs", "images")
    os.makedirs(out, exist_ok=True)
    for name, fn in (("gui-manage.png", page_manage), ("gui-verify.png", page_verify)):
        path = os.path.join(out, name)
        fn().save(path, "PNG", optimize=True)
        print(f"已生成 {path} ({os.path.getsize(path) // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
