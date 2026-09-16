#!/usr/bin/env python3
"""生成「使用技巧」文档配图。

设计约定
--------
- 所有插画由本脚本生成，不引用任何第三方素材，避免版权风险。
- 输出两份：``content/tips/assets/src/<name>.svg``（可编辑源文件）
  与 ``content/tips/assets/<name>.png``（上线渲染用，2 倍图）。
- SVG -> PNG 转换依赖 macOS 自带的 ``qlmanage``；PNG 已随仓库提交，
  生产服务器只需要托管 PNG，不需要安装任何图形库。

用法::

    python3 scripts/build_tips_assets.py            # 全部重建
    python3 scripts/build_tips_assets.py tip-upload # 只重建指定几张

改图流程：编辑本脚本里的插画函数 -> 重新执行 -> 提交 assets 目录。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "content" / "tips" / "assets" / "src"
OUT_DIR = ROOT / "content" / "tips" / "assets"

# 与小程序端一致的品牌色。
BG = "#f2f7f5"
CARD = "#ffffff"
CARD_EDGE = "#e2ebe7"
SKEL = "#e4ebe8"
SKEL_DARK = "#d3dcd8"
ACCENT = "#079c5a"
ACCENT_SOFT = "#e7f6ef"
ACCENT_EDGE = "#c2e4d4"
BLUE = "#3a7caa"
BLUE_SOFT = "#e9f3fa"
AMBER = "#c08b23"
AMBER_SOFT = "#fbf3e0"
PURPLE = "#6d5bd0"
PURPLE_SOFT = "#efecfb"
INK = "#25313c"

SCALE = 2  # SVG 逻辑尺寸 -> PNG 像素倍率


def rrect(x, y, w, h, r, fill, stroke=None, sw=2, opacity=None):
    extra = f' opacity="{opacity}"' if opacity is not None else ""
    line = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" ry="{r}" fill="{fill}"{line}{extra}/>'


def circle(cx, cy, r, fill, stroke=None, sw=2, opacity=None):
    extra = f' opacity="{opacity}"' if opacity is not None else ""
    line = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
    return f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}"{line}{extra}/>'


def line(x1, y1, x2, y2, stroke=SKEL, sw=6, cap="round"):
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" stroke-width="{sw}" stroke-linecap="{cap}"/>'


def path(d, fill="none", stroke=None, sw=6, cap="round", join="round"):
    line_attr = f' stroke="{stroke}" stroke-width="{sw}" stroke-linecap="{cap}" stroke-linejoin="{join}"' if stroke else ""
    return f'<path d="{d}" fill="{fill}"{line_attr}/>'


def text_lines(x, y, widths, gap=24, h=12, fill=SKEL):
    """用圆角条模拟正文行，避免插画里出现真实文案。"""
    out = []
    for i, w in enumerate(widths):
        out.append(rrect(x, y + i * (h + gap - h + 12), w, h, h / 2, fill))
    return "".join(out)


def doc_glyph(x, y, size, tint, soft):
    """文档小图标：折叠角 + 三条线。"""
    w = size * 0.78
    h = size
    fold = size * 0.32
    body = (
        f'<path d="M{x + 8} {y} H{x + w - fold} L{x + w} {y + fold} V{x + h - 8} '
        f'A8 8 0 0 1 {x + w - 8} {y + h} H{x + 8} A8 8 0 0 1 {x} {y + h - 8} '
        f'V{y + 8} A8 8 0 0 1 {x + 8} {y} Z" fill="{soft}" stroke="{tint}" stroke-width="2.4" stroke-linejoin="round"/>'
        f'<path d="M{x + w - fold} {y} V{y + fold} H{x + w}" fill="{soft}" stroke="{tint}" stroke-width="2.4" stroke-linejoin="round"/>'
    )
    return body


def image_glyph(cx, cy, size, tint):
    w = size
    h = size * 0.76
    x = cx - w / 2
    y = cy - h / 2
    return (
        rrect(x, y, w, h, 6, "none", tint, 2.4)
        + circle(x + w * 0.28, y + h * 0.32, size * 0.07, tint)
        + path(f"M{x + 5} {y + h - 4} l{w * 0.3} {-h * 0.44} l{w * 0.24} {h * 0.3} l{w * 0.16} {-h * 0.2} l{w * 0.25} {h * 0.34} z", fill=tint)
    )


def camera_glyph(cx, cy, size, tint):
    w = size
    h = size * 0.72
    x = cx - w / 2
    y = cy - h / 2
    return (
        rrect(x, y + h * 0.14, w, h * 0.86, 7, "none", tint, 2.4)
        + rrect(x + w * 0.3, y, w * 0.34, h * 0.2, 4, tint)
        + circle(cx, y + h * 0.6, size * 0.17, "none", tint, 2.4)
    )


def check_badge(cx, cy, r, fill=ACCENT):
    return circle(cx, cy, r, fill) + path(
        f"M{cx - r * 0.42} {cy + r * 0.04} l{r * 0.3} {r * 0.32} l{r * 0.56} {-r * 0.62}",
        stroke="#ffffff",
        sw=r * 0.26,
    )


def arrow(x1, y, x2, tint=ACCENT_EDGE, sw=5):
    mid = (x1 + x2) / 2
    return (
        path(f"M{x1} {y} H{mid}", stroke=tint, sw=sw)
        + path(f"M{x2 - 16} {y - 13} L{x2} {y} L{x2 - 16} {y + 13}", stroke=tint, sw=sw)
    )




def wrap(inner, w=720, h=480, bg=BG):
    """把画布补成正方形再交给 qlmanage，裁剪后即为 w:h 比例。"""
    side = max(w, h)
    dx = (side - w) // 2
    dy = (side - h) // 2
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{side}" height="{side}" viewBox="0 0 {side} {side}">'
        f'{rrect(0, 0, side, side, 0, bg)}'
        f'<g transform="translate({dx} {dy})">{rrect(0, 0, w, h, 0, bg)}{inner}</g>'
        f'</svg>'
    )


# --------------------------------------------------------------------------
# 插画
# --------------------------------------------------------------------------

def cover_upload():
    """把资料交给我：三种入口汇入一张资料卡。"""
    p = []
    sources = [
        (96, "docs", ACCENT, ACCENT_SOFT),
        (192, "album", BLUE, BLUE_SOFT),
        (288, "camera", AMBER, AMBER_SOFT),
    ]
    for y, kind, tint, soft in sources:
        p.append(rrect(48, y - 34, 214, 68, 18, CARD, CARD_EDGE, 2))
        if kind == "docs":
            p.append(doc_glyph(72, y - 20, 40, tint, soft))
        elif kind == "album":
            p.append(image_glyph(92, y, 44, tint))
        else:
            p.append(camera_glyph(92, y, 46, tint))
        p.append(rrect(126, y - 15, 106, 11, 5, SKEL_DARK))
        p.append(rrect(126, y + 6, 72, 9, 4, SKEL))
    p.append(arrow(276, 152, 330))
    p.append(arrow(276, 260, 330))
    p.append(arrow(276, 368, 330))
    p.append(rrect(340, 78, 332, 324, 30, CARD, CARD_EDGE, 2))
    p.append(rrect(340, 78, 332, 60, 30, ACCENT_SOFT))
    p.append(rrect(340, 108, 332, 30, 0, ACCENT_SOFT))
    p.append(circle(376, 108, 15, ACCENT))
    p.append(rrect(404, 101, 120, 13, 6, ACCENT_EDGE))
    p.append(text_lines(376, 168, [244, 258, 228, 250, 186], gap=14, h=13))
    p.append(rrect(376, 330, 96, 34, 17, ACCENT_SOFT, ACCENT_EDGE, 2))
    p.append(rrect(484, 330, 78, 34, 17, SKEL))
    p.append(check_badge(628, 108, 26))
    return "".join(p)


def cover_organize():
    """上传后自动整理：原文 -> 标题/摘要/标签/要点。"""
    p = []
    p.append(rrect(40, 92, 268, 300, 26, CARD, CARD_EDGE, 2))
    p.append(rrect(68, 124, 150, 14, 7, SKEL_DARK))
    p.append(text_lines(68, 168, [212, 198, 216, 176, 206, 148], gap=12, h=11))
    p.append(rrect(68, 336, 92, 28, 14, SKEL))
    p.append(arrow(322, 242, 388, ACCENT_EDGE))
    p.append(rrect(404, 92, 276, 300, 26, CARD, CARD_EDGE, 2))
    p.append(rrect(404, 92, 276, 76, 26, ACCENT_SOFT))
    p.append(rrect(404, 142, 276, 26, 0, ACCENT_SOFT))
    p.append(rrect(430, 122, 138, 16, 8, ACCENT_EDGE))
    p.append(rrect(430, 148, 92, 11, 5, SKEL))
    rows = [(206, ACCENT_SOFT, ACCENT), (252, BLUE_SOFT, BLUE), (298, AMBER_SOFT, AMBER)]
    for y, soft, tint in rows:
        p.append(rrect(430, y, 224, 38, 19, soft))
        p.append(circle(452, y + 19, 7, tint))
        p.append(rrect(472, y + 13, 116, 12, 6, SKEL))
    p.append(rrect(430, 350, 132, 22, 11, SKEL))
    return "".join(p)


def cover_scope():
    """知识库问答 / 全网问答：两个模式并列。"""
    p = []
    p.append(rrect(56, 108, 296, 264, 26, CARD, CARD_EDGE, 2))
    p.append(rrect(56, 108, 296, 64, 26, ACCENT_SOFT))
    p.append(rrect(56, 142, 296, 30, 0, ACCENT_SOFT))
    p.append(folder_shape(88, 122, 40, ACCENT))
    p.append(rrect(142, 132, 118, 15, 7, ACCENT_EDGE))
    p.append(text_lines(88, 208, [232, 214, 240, 168], gap=14, h=12))
    p.append(rrect(88, 322, 96, 26, 13, ACCENT_SOFT, ACCENT_EDGE, 2))
    p.append(rrect(196, 322, 78, 26, 13, SKEL))

    p.append(rrect(368, 108, 296, 264, 26, CARD, CARD_EDGE, 2))
    p.append(rrect(368, 108, 296, 64, 26, BLUE_SOFT))
    p.append(rrect(368, 142, 296, 30, 0, BLUE_SOFT))
    p.append(globe_shape(400, 140, 36, BLUE))
    p.append(rrect(452, 132, 118, 15, 7, "#c3dcec"))
    p.append(text_lines(400, 208, [232, 214, 240, 168], gap=14, h=12))
    p.append(rrect(400, 322, 96, 26, 13, BLUE_SOFT, "#c3dcec", 2))
    p.append(rrect(508, 322, 78, 26, 13, SKEL))
    return "".join(p)


def cover_question():
    """把问题说清楚：输入条 + 结构建议。"""
    p = []
    p.append(rrect(60, 84, 600, 96, 30, CARD, CARD_EDGE, 2))
    p.append(rrect(96, 118, 300, 14, 7, SKEL_DARK))
    p.append(rrect(96, 142, 190, 11, 5, SKEL))
    p.append(circle(604, 132, 26, ACCENT))
    p.append(path("M596 122 L616 132 L596 142", fill="none", stroke="#ffffff", sw=5))
    hints = [
        (228, ACCENT_SOFT, ACCENT, [300, 236]),
        (308, BLUE_SOFT, BLUE, [268, 316]),
        (388, AMBER_SOFT, AMBER, [322, 210]),
    ]
    for y, soft, tint, widths in hints:
        p.append(rrect(60, y - 30, 600, 0, 0, "none"))
        p.append(circle(92, y + 6, 18, soft))
        p.append(path(f"M84 {y + 6} l6 6 l12 -13", stroke=tint, sw=3.6))
        p.append(rrect(126, y, widths[0], 13, 6, SKEL_DARK))
        p.append(rrect(126, y + 22, widths[1], 11, 5, SKEL))
    return "".join(p)


def cover_source():
    """回答可溯源：回答卡 + 引线指向原文。"""
    p = []
    p.append(rrect(44, 96, 340, 288, 26, CARD, CARD_EDGE, 2))
    p.append(circle(84, 140, 22, ACCENT_SOFT))
    p.append(rrect(86, 132, 20, 16, 4, ACCENT, opacity="0.55"))
    p.append(text_lines(124, 128, [222, 206], gap=14, h=12))
    p.append(text_lines(76, 190, [268, 248, 272, 214], gap=14, h=12))
    p.append(rrect(76, 306, 128, 36, 18, ACCENT_SOFT, ACCENT_EDGE, 2))
    p.append(rrect(94, 320, 92, 10, 5, ACCENT_EDGE))
    p.append(arrow(400, 240, 452, ACCENT_EDGE))
    p.append(rrect(468, 96, 208, 288, 26, CARD, CARD_EDGE, 2))
    p.append(rrect(496, 128, 152, 13, 6, SKEL_DARK))
    p.append(text_lines(496, 170, [152, 138, 160, 120, 148, 110], gap=12, h=11))
    p.append(rrect(496, 320, 84, 24, 12, SKEL))
    p.append(check_badge(660, 128, 20, ACCENT))
    return "".join(p)


def cover_skill():
    """技能：可选技能网格，其中一项被选中。"""
    p = []
    tiles = [
        (56, 108, ACCENT, ACCENT_SOFT),
        (256, 108, BLUE, BLUE_SOFT),
        (456, 108, PURPLE, PURPLE_SOFT),
        (56, 268, AMBER, AMBER_SOFT),
        (256, 268, ACCENT, ACCENT_SOFT),
        (456, 268, BLUE, BLUE_SOFT),
    ]
    for i, (x, y, tint, soft) in enumerate(tiles):
        selected = i == 1
        edge = ACCENT if selected else CARD_EDGE
        p.append(rrect(x, y, 168, 128, 24, CARD, edge, 3 if selected else 2))
        p.append(rrect(x + 22, y + 24, 44, 44, 14, soft))
        p.append(circle(x + 44, y + 46, 9, tint))
        p.append(rrect(x + 22, y + 84, 104, 12, 6, SKEL_DARK))
        p.append(rrect(x + 22, y + 104, 68, 9, 4, SKEL))
        if selected:
            p.append(check_badge(x + 168 - 22, y + 22, 20))
    return "".join(p)


def cover_model():
    """模型与深度思考：快速 / 深度两张卡。"""
    p = []
    p.append(rrect(48, 112, 288, 256, 26, CARD, CARD_EDGE, 2))
    p.append(rrect(48, 112, 288, 96, 26, ACCENT_SOFT))
    p.append(rrect(48, 168, 288, 40, 0, ACCENT_SOFT))
    p.append(path("M150 136 L182 136 L162 166 L192 166 L146 214 L158 176 L132 176 Z", fill=ACCENT))
    p.append(rrect(228, 152, 74, 14, 7, ACCENT_EDGE))
    p.append(text_lines(84, 244, [216, 196, 168], gap=14, h=12))
    p.append(rrect(84, 322, 88, 26, 13, ACCENT_SOFT, ACCENT_EDGE, 2))

    p.append(rrect(384, 112, 288, 256, 26, CARD, CARD_EDGE, 2))
    p.append(rrect(384, 112, 288, 96, 26, PURPLE_SOFT))
    p.append(rrect(384, 168, 288, 40, 0, PURPLE_SOFT))
    p.append(brain_shape(522, 160, 40, PURPLE))
    p.append(rrect(580, 152, 60, 14, 7, "#cfc7f2"))
    p.append(text_lines(420, 244, [216, 196, 168], gap=14, h=12))
    p.append(rrect(420, 322, 88, 26, 13, PURPLE_SOFT, "#cfc7f2", 2))
    p.append(rrect(516, 322, 62, 26, 13, SKEL))
    return "".join(p)


def cover_plan():
    """容量与会员：容量条 + 三档阶梯。"""
    p = []
    p.append(rrect(60, 92, 600, 120, 26, CARD, CARD_EDGE, 2))
    p.append(rrect(96, 126, 158, 14, 7, SKEL_DARK))
    p.append(rrect(96, 156, 528, 22, 11, SKEL))
    p.append(rrect(96, 156, 214, 22, 11, ACCENT))
    p.append(rrect(596, 122, 28, 22, 11, ACCENT_SOFT))
    baseline = 442
    tiers = [
        (56, 176, 118, SKEL_DARK, 2),
        (252, 176, 158, ACCENT_EDGE, 3),
        (448, 212, 198, ACCENT, 4),
    ]
    for x, w, h, tint, rows in tiers:
        solid = tint == ACCENT
        top = baseline - h
        p.append(rrect(x, top, w, h, 24, tint if solid else CARD, ACCENT if solid else CARD_EDGE, 3 if solid else 2))
        p.append(rrect(x + 26, top + 26, 82, 15, 7, "#ffffff" if solid else SKEL_DARK))
        for i in range(rows):
            p.append(rrect(x + 26, top + 62 + i * 26, (w - 52) - i * 14, 10, 5, "#cdeedd" if solid else SKEL))
    p.append(check_badge(636, 252, 26))
    return "".join(p)


def figure_import():
    """导入弹窗示意：四个入口。"""
    p = []
    p.append(rrect(56, 96, 608, 288, 30, CARD, CARD_EDGE, 2))
    p.append(rrect(56, 96, 608, 72, 30, CARD))
    p.append(rrect(96, 128, 148, 16, 8, SKEL_DARK))
    p.append(circle(612, 132, 20, SKEL))
    items = [(96, ACCENT, ACCENT_SOFT), (236, BLUE, BLUE_SOFT), (376, AMBER, AMBER_SOFT), (516, PURPLE, PURPLE_SOFT)]
    for x, tint, soft in items:
        p.append(rrect(x, 208, 120, 120, 24, soft))
        p.append(rrect(x + 30, 240, 60, 48, 12, CARD, tint, 2.4))
        p.append(rrect(x + 24, 306, 72, 12, 6, CARD))
    return "".join(p)


def figure_modeswitch():
    """输入框里的模式开关位置示意。"""
    p = []
    p.append(rrect(56, 160, 608, 156, 36, CARD, CARD_EDGE, 2))
    p.append(rrect(92, 200, 300, 14, 7, SKEL_DARK))
    p.append(rrect(92, 228, 210, 12, 6, SKEL))
    p.append(rrect(92, 268, 168, 30, 15, ACCENT_SOFT, ACCENT_EDGE, 2))
    p.append(circle(112, 283, 8, ACCENT))
    p.append(rrect(130, 277, 104, 12, 6, ACCENT_EDGE))
    p.append(circle(592, 238, 30, ACCENT))
    p.append(path("M584 228 L604 238 L584 248", fill="none", stroke="#ffffff", sw=5))
    p.append(path("M360 240 C420 240 430 130 470 130", stroke=ACCENT_EDGE, sw=4))
    p.append(path("M456 124 L470 130 L456 136", fill="none", stroke=ACCENT_EDGE, sw=4))
    p.append(rrect(470, 104, 186, 52, 16, ACCENT_SOFT, ACCENT_EDGE, 2))
    p.append(circle(496, 130, 8, ACCENT))
    p.append(rrect(514, 124, 118, 12, 6, ACCENT_EDGE))
    return "".join(p)


def figure_skillpick():
    """技能选择器：列表 + 已选技能注入。"""
    p = []
    p.append(rrect(56, 72, 608, 336, 30, CARD, CARD_EDGE, 2))
    p.append(rrect(96, 108, 160, 16, 8, SKEL_DARK))
    rows = [(160, ACCENT, ACCENT_SOFT, True), (238, BLUE, BLUE_SOFT, False), (316, PURPLE, PURPLE_SOFT, False)]
    for y, tint, soft, on in rows:
        p.append(rrect(96, y, 528, 64, 18, soft if on else "#f7f9f8"))
        p.append(rrect(118, y + 14, 36, 36, 12, CARD if on else SKEL))
        p.append(circle(136, y + 32, 8, tint))
        p.append(rrect(174, y + 18, 176, 12, 6, SKEL_DARK if on else SKEL))
        p.append(rrect(174, y + 38, 118, 9, 4, SKEL))
        if on:
            p.append(check_badge(592, y + 32, 18))
    return "".join(p)


def figure_sourcechip():
    """回答下方的出处标签。"""
    p = []
    p.append(rrect(56, 96, 608, 288, 30, CARD, CARD_EDGE, 2))
    p.append(text_lines(96, 136, [420, 468, 396], gap=14, h=13))
    chips = [(96, ACCENT_SOFT, ACCENT_EDGE, 188), (300, BLUE_SOFT, "#c3dcec", 164), (480, AMBER_SOFT, "#efdcb4", 148)]
    for x, soft, edge, w in chips:
        p.append(rrect(x, 236, w, 54, 18, soft, edge, 2))
        p.append(rrect(x + 18, 250, 18, 26, 5, edge))
        p.append(rrect(x + 48, 252, w - 78, 11, 5, edge))
        p.append(rrect(x + 48, 270, (w - 78) * 0.6, 9, 4, edge))
    p.append(rrect(96, 322, 148, 34, 17, "#f6f8f7"))
    return "".join(p)


def folder_shape(x, y, size, tint):
    w = size
    h = size * 0.82
    return (
        path(
            f"M{x} {y + h * 0.24} a6 6 0 0 1 6 -6 h{w * 0.3} l{w * 0.14} {h * 0.16} h{w * 0.44} "
            f"a6 6 0 0 1 6 6 v{h * 0.56} a6 6 0 0 1 -6 6 h{w - 12} a6 6 0 0 1 -6 -6 z",
            fill="none",
            stroke=tint,
            sw=2.6,
        )
    )


def globe_shape(cx, cy, r, tint):
    return (
        circle(cx, cy, r, "none", tint, 2.6)
        + f'<ellipse cx="{cx}" cy="{cy}" rx="{r * 0.46}" ry="{r}" fill="none" stroke="{tint}" stroke-width="2.6"/>'
        + f'<line x1="{cx - r}" y1="{cy}" x2="{cx + r}" y2="{cy}" stroke="{tint}" stroke-width="2.6"/>'
    )


def brain_shape(cx, cy, r, tint):
    return (
        circle(cx, cy, r * 0.92, "none", tint, 2.8)
        + path(f"M{cx - r * 0.42} {cy - r * 0.3} a{r * 0.3} {r * 0.3} 0 0 1 {r * 0.42} {-r * 0.14}", stroke=tint, sw=2.6)
        + path(f"M{cx - r * 0.42} {cy + r * 0.3} a{r * 0.3} {r * 0.3} 0 0 0 {r * 0.42} {r * 0.14}", stroke=tint, sw=2.6)
        + path(f"M{cx + r * 0.34} {cy - r * 0.4} a{r * 0.34} {r * 0.34} 0 0 1 0 {r * 0.8}", stroke=tint, sw=2.6)
        + circle(cx - r * 0.06, cy, r * 0.13, tint)
    )


ILLUSTRATIONS = {
    "tip-upload": (cover_upload, 720, 480),
    "tip-organize": (cover_organize, 720, 480),
    "tip-scope": (cover_scope, 720, 480),
    "tip-question": (cover_question, 720, 480),
    "tip-source": (cover_source, 720, 480),
    "tip-skill": (cover_skill, 720, 480),
    "tip-model": (cover_model, 720, 480),
    "tip-plan": (cover_plan, 720, 480),
    "fig-import": (figure_import, 720, 480),
    "fig-modeswitch": (figure_modeswitch, 720, 480),
    "fig-skillpick": (figure_skillpick, 720, 480),
    "fig-sourcechip": (figure_sourcechip, 720, 480),
}


def build(names: list[str]) -> int:
    SRC_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    converter = shutil.which("qlmanage")
    failed = 0
    for name in names:
        builder, w, h = ILLUSTRATIONS[name]
        svg = wrap(builder(), w, h)
        svg_path = SRC_DIR / f"{name}.svg"
        svg_path.write_text(svg, encoding="utf-8")
        if not converter:
            print(f"[warn] {name}: 未找到 qlmanage，已输出 SVG，跳过 PNG")
            failed += 1
            continue
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [converter, "-t", "-s", str(max(w, h) * SCALE), "-o", tmp, str(svg_path)],
                capture_output=True,
                text=True,
            )
            produced = Path(tmp) / f"{name}.svg.png"
            if result.returncode != 0 or not produced.exists():
                print(f"[error] {name}: PNG 转换失败 {result.stderr.strip()[:200]}")
                failed += 1
                continue
            target = OUT_DIR / f"{name}.png"
            # qlmanage 只输出正方形缩略图，这里居中裁回目标比例。
            cropper = shutil.which("sips")
            if cropper:
                subprocess.run(
                    [cropper, "-c", str(h * SCALE), str(w * SCALE), str(produced), "--out", str(target)],
                    capture_output=True,
                    text=True,
                )
            if not target.exists():
                shutil.move(str(produced), str(target))
            print(f"[ok] {name}: {target.relative_to(ROOT)} ({target.stat().st_size // 1024} KB)")
    return failed


if __name__ == "__main__":
    requested = sys.argv[1:] or list(ILLUSTRATIONS)
    unknown = [n for n in requested if n not in ILLUSTRATIONS]
    if unknown:
        print(f"未知插画：{', '.join(unknown)}")
        print(f"可用：{', '.join(ILLUSTRATIONS)}")
        raise SystemExit(2)
    raise SystemExit(1 if build(requested) else 0)
