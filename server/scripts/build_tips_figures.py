"""把开发者工具抓到的真实界面，做成「使用技巧」的封面与正文配图。

坐标全部来自真实渲染层（截图时同批导出的 ``rects-*.json``），不是肉眼估的，
所以裁切框和绿色标注永远落在真实按钮上；界面改版后重抓重跑一次即可。

抓图脚本见 /tmp/tips-shots/cap2.js（导入面板 / 资料页 / 输入框 / 参考出处 /
技能面板 / 深度思考 / 会员方案），它会把每个元素的真实 rect 一起落盘。

用法（后端目录下）::

    python3 scripts/build_tips_figures.py --src /tmp/tips-shots/out2 --out content/tips/assets

每张正文配图 = 真实界面裁切 + 绿色圈/序号 + 图注条：
不点开也知道该点哪里，读图就能照着做。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

CSS_WIDTH = 430                      # 模拟器逻辑宽度
FONT_PATH = '/System/Library/Fonts/Hiragino Sans GB.ttc'
BRAND = (7, 160, 95)
CAPTION_BG = (244, 247, 246)
CAPTION_TEXT = (46, 58, 70)
BORDER = (228, 233, 231)
RING_PAD = 6                          # 圈比按钮大出来的像素（CSS px）
WIDTH = 750                           # 输出宽度（750 = 小程序 2x 图）


def load_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_PATH, size)


def load_rects(src: Path, shot: str) -> dict[str, Any]:
    path = src / f'rects-{shot}.json'
    if not path.exists():
        return {'boxes': {}, 'lists': {}}
    data = json.loads(path.read_text(encoding='utf-8'))
    data.setdefault('boxes', {})
    data.setdefault('lists', {})
    return data


def pick_box(rects: dict[str, Any], key: str) -> dict[str, int]:
    box = rects['boxes'].get(key)
    if not box:
        raise KeyError(f'缺少元素坐标 {key}（重新抓一次截图）')
    return box


def pick_icon(rects: dict[str, Any], list_key: str, text: str) -> dict[str, int]:
    for item in rects['lists'].get(list_key) or []:
        if item.get('t', '').startswith(text):
            return item
    raise KeyError(f'在 {list_key} 里找不到「{text}」')


def wrap(text: str, font: ImageFont.FreeTypeFont, width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    lines: list[str] = []
    current = ''
    for char in text:
        candidate = current + char
        if draw.textlength(candidate, font=font) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = char
    if current:
        lines.append(current)
    return lines


def crop(image: Image.Image, box: tuple[int, int, int, int], width: int) -> tuple[Image.Image, float]:
    """按 CSS 坐标裁切并等比缩放到目标宽度，返回缩放比例（片内像素 / CSS px）。"""
    scale = image.width / CSS_WIDTH
    left, top, right, bottom = (round(value * scale) for value in box)
    piece = image.crop((left, top, right, bottom))
    height = max(1, round(piece.height * width / piece.width))
    return piece.resize((width, height), Image.LANCZOS), width / (box[2] - box[0])


def to_piece(value: int, origin: int, ratio: float) -> float:
    return (value - origin) * ratio


def draw_ring(piece: Image.Image, crop_box: tuple[int, int, int, int], rect: dict[str, int], ratio: float) -> None:
    draw = ImageDraw.Draw(piece)
    pad = RING_PAD * ratio
    left = to_piece(rect['x'], crop_box[0], ratio) - pad
    top = to_piece(rect['y'], crop_box[1], ratio) - pad
    right = to_piece(rect['x'] + rect['w'], crop_box[0], ratio) + pad
    bottom = to_piece(rect['y'] + rect['h'], crop_box[1], ratio) + pad
    radius = max(10, round(16 * ratio))
    line = max(3, round(3 * ratio))
    draw.rounded_rectangle((left, top, right, bottom), radius=radius, outline=BRAND, width=line)


def draw_dot(piece: Image.Image, crop_box: tuple[int, int, int, int], x: int, y: int, label: str, ratio: float) -> None:
    draw = ImageDraw.Draw(piece)
    radius = max(16, round(17 * ratio))
    cx = to_piece(x, crop_box[0], ratio)
    cy = to_piece(y, crop_box[1], ratio)
    if cx - radius < 0 or cy - radius < 0 or cx + radius > piece.width or cy + radius > piece.height:
        return
    draw.ellipse((cx - radius - 3, cy - radius - 3, cx + radius + 3, cy + radius + 3), fill=(255, 255, 255))
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=BRAND)
    font = load_font(max(18, round(24 * ratio)))
    text = str(label)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text((cx - (right - left) / 2 - left, cy - (bottom - top) / 2 - top), text, font=font, fill=(255, 255, 255))


def draw_caption(piece: Image.Image, caption: str) -> Image.Image:
    font = load_font(25)
    measure = ImageDraw.Draw(piece)
    lines = wrap(caption, font, piece.width - 68, measure)[:3]
    line_height = 37
    bar_height = 24 + line_height * len(lines) + 18
    canvas = Image.new('RGB', (piece.width, piece.height + bar_height), CAPTION_BG)
    canvas.paste(piece, (0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.line((0, piece.height, piece.width, piece.height), fill=BORDER, width=2)
    dot_y = piece.height + 26 + line_height // 2 - 5
    draw.ellipse((28, dot_y, 40, dot_y + 12), fill=BRAND)
    for index, line in enumerate(lines):
        draw.text((54, piece.height + 20 + index * line_height), line, font=font, fill=CAPTION_TEXT)
    return canvas


FIGURES: list[dict[str, Any]] = [
    {
        'out': 'fig-import.png', 'shot': 's1-import', 'crop': (0, 512, 430, 812),
        'caption': '资料在哪就点哪个：1 微信文件、2 本地相册、3 拍照、4 公众号导入。选完等它变成「已整理」。',
        'icons': [('.upload-option', '微信文件', '1'), ('.upload-option', '本地相册', '2'),
                  ('.upload-option', '拍照', '3'), ('.upload-option', '公众号导入', '4')],
    },
    {
        'out': 'tip-organize.png', 'shot': 's2-doc', 'crop': (0, 150, 430, 500),
        'caption': '资料页最上面的「已整理」卡片：标题、摘要、标签、要点都是自动生成的，你只要确认是不是这份文件。',
        'rings': ['.organize-card'],
    },
    {
        'out': 'tip-logic.png', 'shot': 's4-question', 'crop': (0, 690, 430, 900),
        'caption': '输入框左下角的小机器人切换问答逻辑：只查自己的资料，还是让它上网办事。右下角是思考深度。',
        'rings': ['.qa-logic', '.qa-model'],
        'dots': [(49, 806, '1'), (337, 806, '2')],
    },
    {
        'out': 'tip-question.png', 'shot': 's4-question', 'crop': (0, 690, 430, 900),
        'caption': '问题里带上范围（哪份资料）、目标（要结论还是要清单）、格式（表格还是要点），答案基本一次能用。',
        'rings': ['.qa-input'],
        'dots': [(38, 712, '1')],
    },
    {
        'out': 'tip-source.png', 'shot': 's5-sources', 'crop': (0, 296, 430, 560),
        'caption': '答案里的 [1] 对应下面的「参考出处」：点一条就能打开原文核对，判断这句话能不能直接拿去用。',
        'rings': ['.qa-source'],
        'dots': [(397, 381, '1')],
    },
    {
        'out': 'fig-skillpick.png', 'shot': 's6-skills', 'crop': (0, 180, 430, 620),
        'caption': '技能面板：点技能名就选中（再点一下取消），可以同时选多个；选中的技能会一直生效。',
        'dots': [(121, 312, '1'), (48, 428, '2')],
    },
    {
        'out': 'tip-model.png', 'shot': 's9-model-sheet', 'crop': (0, 700, 430, 900),
        'caption': '点输入框右下角图标打开「更多选项」：点一下「深度思考」，变成「自动」就是开了。',
        'rings': ['.option-card'],
        'dots': [(396, 722, '1')],
    },
    {
        'out': 'tip-plan.png', 'shot': 's8-plan', 'crop': (0, 505, 430, 900),
        'caption': '开通会员三步：1 选 Plus 或 Pro，2 选月付 / 季付 / 年付，3 点「微信支付」，付完立刻生效。',
        'rings': ['.tier-tabs', '.plan-options', '.confirm-pay'],
        'dots': [(43, 620, '1'), (43, 689, '2'), (43, 821, '3')],
    },
]

COVERS: list[dict[str, Any]] = [
    {'out': 'cover-upload.png', 'shot': 's1-import', 'crop': (0, 516, 430, 706)},
    {'out': 'cover-organize.png', 'shot': 's2-doc', 'crop': (0, 150, 430, 340)},
    {'out': 'cover-scope.png', 'shot': 's4-question', 'crop': (0, 712, 430, 900)},
    {'out': 'cover-question.png', 'shot': 's4-question', 'crop': (0, 690, 430, 878)},
    {'out': 'cover-source.png', 'shot': 's5-sources', 'crop': (0, 300, 430, 488)},
    {'out': 'cover-skill.png', 'shot': 's6-skills', 'crop': (0, 186, 430, 374)},
    {'out': 'cover-model.png', 'shot': 's9-model-sheet', 'crop': (0, 715, 430, 903)},
    {'out': 'cover-plan.png', 'shot': 's8-plan', 'crop': (0, 520, 430, 708)},
]


def build_figure(src: Path, out: Path, spec: dict[str, Any]) -> str:
    shot = spec['shot']
    path = src / f'{shot}.png'
    if not path.exists():
        return f'! 缺少截图 {path}'
    rects = load_rects(src, shot)
    crop_box = tuple(spec['crop'])
    with Image.open(path) as raw:
        piece, ratio = crop(raw.convert('RGB'), crop_box, WIDTH)

    for key in spec.get('rings') or []:
        draw_ring(piece, crop_box, pick_box(rects, key), ratio)

    for list_key, text, label in spec.get('icons') or []:
        icon = pick_icon(rects, list_key, text)
        draw_dot(piece, crop_box, icon['x'] + icon['w'], icon['y'], label, ratio)

    for x, y, label in spec.get('dots') or []:
        draw_dot(piece, crop_box, x, y, label, ratio)

    canvas = draw_caption(piece, spec['caption'])
    target = out / spec['out']
    canvas.save(target, optimize=True)
    return f"{spec['out']}  {canvas.width}x{canvas.height}  {target.stat().st_size // 1024}KB"


def build_cover(src: Path, out: Path, spec: dict[str, Any]) -> str:
    path = src / f"{spec['shot']}.png"
    if not path.exists():
        return f'! 缺少截图 {path}'
    with Image.open(path) as raw:
        piece, _ = crop(raw.convert('RGB'), tuple(spec['crop']), WIDTH)
    target = out / spec['out']
    piece.save(target, optimize=True)
    return f"{spec['out']}  {piece.width}x{piece.height}  {target.stat().st_size // 1024}KB"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--src', default='/tmp/tips-shots/out2')
    parser.add_argument('--out', default='content/tips/assets')
    args = parser.parse_args()
    src = Path(args.src)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print(f'源截图: {src}\n输出: {out}\n')
    for spec in FIGURES:
        print('配图 ', build_figure(src, out, spec))
    for spec in COVERS:
        print('封面 ', build_cover(src, out, spec))


if __name__ == '__main__':
    main()
