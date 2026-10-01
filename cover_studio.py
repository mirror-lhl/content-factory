#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cover_studio — 封面 / 配图生成引擎

给图文与视频生成**各平台规格**的封面。纯 Pillow，无外部依赖。

设计要点
--------
1. **多风格**：5 套排版模板，覆盖「古典 / 水墨 / 强冲击 / 画框 / 上下分割」
2. **多平台**：一次生成全平台规格（小红书 3:4、公众号 2.35:1、B站 16:9…）
3. **字体优先**：自动探测并优先使用更好的字体（装了霞鹜文楷就自动用）
4. **竖排支持**：国学内容常要竖排，专门实现了从右往左的竖排排版
5. **字号自适应**：标题再长也不会溢出，自动缩到放得下

字体说明
--------
macOS 自带只有 4 个中文字体（宋体 / STHeiti / 冬青黑体），没有楷体。
**强烈建议安装 [霞鹜文楷 LXGW WenKai](https://github.com/lxgw/LxgwWenKai)**
（免费开源，国学类内容首选）。装到 ~/Library/Fonts/ 后本引擎会自动优先使用。

用法
----
    # 单张
    python3 cover_studio.py one --title "我用 AI 给古籍标点，错了 30%" \\
        --platform xiaohongshu --style classic -o cover.png

    # 一次出全平台所有规格
    python3 cover_studio.py all --title "..." --image assets/论语/学而/x.jpg -d out/

    # 列出风格与规格
    python3 cover_studio.py list
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
from dataclasses import dataclass, field
from typing import Optional, Sequence

from PIL import Image, ImageDraw, ImageFilter, ImageFont


# ═══════════════════════════ 字体 ═══════════════════════════

#: 字体角色 → 候选路径（越靠前越优先）
FONT_ROLES: dict[str, list[str]] = {
    # 古典 / 文化感 —— 国学首选
    "serif": [
        os.path.expanduser("~/Library/Fonts/LXGWWenKai-Regular.ttf"),
        os.path.expanduser("~/Library/Fonts/LXGWWenKai-Medium.ttf"),
        "/Library/Fonts/LXGWWenKai-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
    ],
    # 现代 / 冲击力 —— AI、科技感
    "sans": [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
    ],
    # 正文 / 小字
    "body": [
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
    ],
}

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def resolve_font_path(role: str) -> str:
    for p in FONT_ROLES.get(role, FONT_ROLES["sans"]):
        if os.path.exists(p):
            return p
    raise RuntimeError(f"找不到 {role} 角色的可用字体")


def font(role: str, size: int) -> ImageFont.FreeTypeFont:
    size = max(int(size), 8)
    key = (role, size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(resolve_font_path(role), size)
    return _font_cache[key]


def installed_fonts() -> dict[str, str]:
    """当前实际会用到的字体（便于自检）。"""
    out = {}
    for role in FONT_ROLES:
        try:
            out[role] = resolve_font_path(role)
        except RuntimeError:
            out[role] = "（无）"
    return out


# ═══════════════════════════ 平台规格 ═══════════════════════════

PLATFORM_COVERS: dict[str, dict[str, tuple[int, int]]] = {
    "xiaohongshu": {"主图3:4": (1080, 1440), "方图1:1": (1080, 1080)},
    "gzh":         {"首图2.35:1": (900, 383), "次图1:1": (200, 200)},
    "douyin":      {"竖版9:16": (1080, 1920), "主页3:4": (1080, 1440)},
    "bilibili":    {"横版16:9": (1920, 1080)},
    "zhihu":       {"横版16:9": (1920, 1080)},
    "toutiao":     {"横版16:9": (1920, 1080), "三图1:1": (1080, 1080)},
}

PLATFORM_NAME = {
    "xiaohongshu": "小红书", "gzh": "微信公众号", "douyin": "抖音",
    "bilibili": "B站", "zhihu": "知乎", "toutiao": "今日头条",
}


# ═══════════════════════════ 风格 ═══════════════════════════

@dataclass
class Style:
    """一套封面视觉风格。"""

    key: str
    name: str
    desc: str
    layout: str                       # classic / ink / bold / frame / split
    bg: tuple = (243, 238, 226)       # 底色
    bg2: Optional[tuple] = None       # 渐变第二色
    title: tuple = (28, 26, 24)       # 标题色
    title_role: str = "serif"
    accent: tuple = (176, 58, 46)     # 强调色（朱红）
    body: tuple = (110, 104, 96)
    paper: bool = True                # 是否加宣纸纹理
    seal: bool = True                 # 是否画印章
    border: bool = False              # 是否画细线框
    overlay: float = 0.0              # 有底图时的暗化程度 0~1
    blur_bg: bool = False


STYLES: dict[str, Style] = {
    "classic": Style(
        key="classic", name="宣纸古典", layout="classic",
        desc="米色宣纸底 + 宋体 + 朱红印章，国学内容首选",
        bg=(243, 238, 226), bg2=(232, 223, 205),
        title=(30, 26, 22), accent=(168, 52, 42), body=(120, 110, 98),
        paper=True, seal=True, border=True, overlay=0.55,
    ),
    "ink": Style(
        key="ink", name="水墨深色", layout="ink",
        desc="深墨底 + 米白宋体 + 大留白，沉稳有格调",
        bg=(24, 28, 36), bg2=(38, 44, 56),
        title=(240, 236, 226), accent=(198, 160, 92), body=(150, 148, 142),
        title_role="serif", paper=False, seal=True, border=False, overlay=0.45,
    ),
    "bold": Style(
        key="bold", name="强冲击大字", layout="bold",
        desc="高饱和底 + 黑体超大字，适合抖音/小红书抓眼球",
        bg=(198, 48, 40), bg2=(150, 28, 30),
        title=(255, 255, 255), accent=(255, 214, 102), body=(255, 230, 225),
        title_role="sans", paper=False, seal=False, border=False, overlay=0.5,
    ),
    "frame": Style(
        key="frame", name="画框古卷", layout="frame",
        desc="古籍图居中成画框，上下留白，突出原图本身",
        bg=(246, 243, 236), bg2=(236, 231, 220),
        title=(32, 28, 24), accent=(150, 120, 70), body=(130, 122, 110),
        paper=True, seal=False, border=True, overlay=0.0,
    ),
    "split": Style(
        key="split", name="上下分割", layout="split",
        desc="上半底图下半文字，通用性最强",
        bg=(250, 249, 246), bg2=(240, 238, 233),
        title=(26, 24, 22), accent=(176, 58, 46), body=(120, 114, 106),
        paper=False, seal=True, border=False, overlay=0.35,
    ),
}


# ═══════════════════════════ 绘制原语 ═══════════════════════════

def make_background(w: int, h: int, st: Style) -> Image.Image:
    """生成背景：渐变 + 可选宣纸纹理。"""
    img = Image.new("RGB", (w, h), st.bg)
    d = ImageDraw.Draw(img)
    if st.bg2:
        for y in range(h):                       # 竖向渐变
            t = y / max(h - 1, 1)
            c = tuple(int(st.bg[i] + (st.bg2[i] - st.bg[i]) * t) for i in range(3))
            d.line([(0, y), (w, y)], fill=c)
    if st.paper:
        _add_paper_texture(img, w, h)
    return img


def _add_paper_texture(img: Image.Image, w: int, h: int, *, density: float = 0.0016) -> None:
    """撒一层轻微噪点模拟宣纸纤维，再压一个暗角。"""
    random.seed(w * 7919 + h)                    # 同尺寸纹理稳定，便于复现
    px = img.load()
    n = int(w * h * density)
    for _ in range(n):
        x, y = random.randint(0, w - 1), random.randint(0, h - 1)
        r, g, b = px[x, y]
        k = random.choice((-14, -8, 8, 14))
        px[x, y] = (max(0, min(255, r + k)), max(0, min(255, g + k)), max(0, min(255, b + k)))
    # 暗角
    vig = Image.new("L", (w, h), 0)
    vd = ImageDraw.Draw(vig)
    vd.ellipse([-w * 0.25, -h * 0.25, w * 1.25, h * 1.25], fill=255)
    vig = vig.filter(ImageFilter.GaussianBlur(min(w, h) * 0.12))
    dark = Image.new("RGB", (w, h), (0, 0, 0))
    img.paste(Image.composite(img, dark, vig), (0, 0))


def cover_image(img: Image.Image, w: int, h: int, *, overlay: float = 0.0,
                blur: bool = False, align_y: float = 0.5) -> Image.Image:
    """把底图按 cover 模式铺满 (w,h)，可加暗化遮罩与模糊。"""
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(max(w, h) * 0.02))
    iw, ih = img.size
    scale = max(w / iw, h / ih)
    nw, nh = int(iw * scale + 0.5), int(ih * scale + 0.5)
    img = img.resize((nw, nh), Image.LANCZOS)
    left = int((nw - w) / 2)
    top = int((nh - h) * align_y)
    out = img.crop((left, top, left + w, top + h)).convert("RGB")
    if overlay > 0:
        dark = Image.new("RGB", (w, h), (0, 0, 0))
        out = Image.blend(out, dark, overlay)
    return out



def contain_with_blur(img: Image.Image, w: int, h: int, *, overlay: float = 0.0,
                      margin: float = 0.0, border: Optional[tuple] = None) -> Image.Image:
    """**模糊铺底 + 原图完整居中** —— 竖版素材放进横版区域的标准做法。

    直接把竖版图 cover 到横版区域会裁掉大半内容（古籍的竖排文字首当其冲）。
    这里用原图的模糊放大版填满背景，原图则按 contain 完整显示，一点不裁。
    """
    bg = cover_image(img.copy(), w, h, overlay=max(overlay, 0.35), blur=True)
    iw, ih = img.size
    inner_w = int(w * (1 - margin))
    inner_h = int(h * (1 - margin))
    scale = min(inner_w / iw, inner_h / ih)
    nw, nh = max(int(iw * scale), 1), max(int(ih * scale), 1)
    fg = img.resize((nw, nh), Image.LANCZOS)
    x, y = (w - nw) // 2, (h - nh) // 2
    if border:
        ImageDraw.Draw(bg).rectangle([x - 2, y - 2, x + nw + 1, y + nh + 1],
                                     outline=border, width=2)
    bg.paste(fg, (x, y))
    return bg


def wrap_text(text: str, max_chars: int) -> list[str]:
    """中文智能折行：优先在标点断行，各行长度均衡，避免孤行。

    （与 video_studio 同款算法 —— 保证封面和视频字幕的断行观感一致）
    """
    text = (text or "").strip()
    if not text:
        return [""]
    if len(text) <= max_chars:
        return [text]
    punct = "，。！？；：、,.!?;:）)】」》…—"
    n_lines = math.ceil(len(text) / max_chars)
    target = math.ceil(len(text) / n_lines)
    lines: list[str] = []
    i = 0
    while i < len(text):
        remain = len(text) - i
        if remain <= max_chars:
            lines.append(text[i:])
            break
        take = min(target, max_chars)
        lo, hi = max(2, take - 5), min(max_chars, remain - 3)
        for j in range(lo, hi + 1):
            if text[i + j - 1] in punct:
                take = j
                break
        lines.append(text[i:i + take])
        i += take
    return lines


def fit_font_size(text: str, box_w: int, box_h: int, *, role: str,
                  max_size: int, min_size: int = 20,
                  max_chars_per_line: Optional[int] = None,
                  line_spacing: float = 1.35) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """自动选一个能放进 (box_w, box_h) 的字号。标题再长也不会溢出。"""
    size = max_size
    while size >= min_size:
        f = font(role, size)
        mcp = max_chars_per_line or max(int(box_w / (size * 1.02)), 4)
        lines = wrap_text(text, mcp)
        widest = max(f.getbbox(ln)[2] - f.getbbox(ln)[0] for ln in lines)
        total_h = len(lines) * size * line_spacing
        if widest <= box_w and total_h <= box_h:
            return f, lines
        size -= 2
    f = font(role, min_size)
    return f, wrap_text(text, max_chars_per_line or 8)


def draw_lines(draw: ImageDraw.ImageDraw, lines: list[str], font_obj, x: float, y: float,
               *, fill, stroke_fill=None, stroke_width: int = 0,
               line_spacing: float = 1.35, align: str = "left") -> float:
    """画多行文字，返回结束时的 y。align: left/center/right"""
    size = font_obj.size
    step = size * line_spacing
    cy = y
    for ln in lines:
        bbox = font_obj.getbbox(ln)
        lw = bbox[2] - bbox[0]
        dx = x - lw / 2 if align == "center" else (x - lw if align == "right" else x)
        draw.text((dx - bbox[0], cy - bbox[1]), ln, font=font_obj, fill=fill,
                  stroke_width=stroke_width, stroke_fill=stroke_fill)
        cy += step
    return cy


def draw_vertical(draw: ImageDraw.ImageDraw, text: str, x_right: float, y_top: float,
                  font_obj, *, fill, stroke_fill=None, stroke_width: int = 0,
                  max_per_col: int = 10, col_gap: float = 1.18) -> float:
    """竖排文字（从右往左，每列从上往下）。国学封面必备。

    返回最左一列的 x。
    """
    cols = [text[i:i + max_per_col] for i in range(0, len(text), max_per_col)]
    size = font_obj.size
    cx = x_right
    for col in cols:
        cy = y_top
        for ch in col:
            bbox = font_obj.getbbox(ch)
            draw.text((cx - (bbox[2] - bbox[0]), cy - bbox[1]), ch, font=font_obj,
                      fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill)
            cy += size * 1.06
        cx -= size * col_gap
    return cx


def draw_seal(img: Image.Image, xy: tuple[int, int], size: int, text: str,
              *, color: tuple = (176, 58, 46), label_role: str = "serif") -> None:
    """画一个朱文印章（圆角方框 + 竖排印文）。"""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle([2, 2, size - 3, size - 3], radius=int(size * 0.12),
                        outline=color + (235,), width=max(int(size * 0.055), 2))
    f = font(label_role, int(size * 0.30))
    n = min(len(text), 4)
    per = 2 if n > 2 else n
    items = [text[i:i + per] for i in range(0, n, per)]
    step = size * 0.44
    y0 = (size - step * len(items)) / 2 + size * 0.02
    for i, it in enumerate(items):
        bbox = f.getbbox(it)
        w = bbox[2] - bbox[0]
        d.text(((size - w) / 2 - bbox[0], y0 + i * step), it, font=f, fill=color + (245,))
    img.paste(layer, xy, layer)


# ═══════════════════════════ 五种排版 ═══════════════════════════

def render_cover(*, title: str, subtitle: str = "", kicker: str = "",
                 image: Optional[str] = None, style: str = "classic",
                 size: tuple[int, int] = (1080, 1440), out: str = "cover.png",
                 seal_text: str = "国学") -> str:
    """生成一张封面。返回输出路径。"""
    st = STYLES.get(style)
    if st is None:
        raise ValueError(f"未知风格 {style!r}，可用: {sorted(STYLES)}")
    W, H = size
    vertical = H > W * 1.15          # 竖版才考虑竖排

    # ---- 背景 ----
    # frame / split 两种排版会自己贴图，**不能**先把整张画布铺满底图
    if image and os.path.exists(image) and st.layout not in ("frame", "split"):
        base = cover_image(Image.open(image).convert("RGB"), W, H,
                           overlay=st.overlay, blur=st.blur_bg)
    else:
        base = make_background(W, H, st)

    img = base.convert("RGB")
    d = ImageDraw.Draw(img)

    if st.layout == "frame":
        _layout_frame(img, d, st, W, H, title, subtitle, image, seal_text)
    elif st.layout == "split":
        _layout_split(img, st, W, H, title, subtitle, image, seal_text)
    else:
        _layout_text(st, img, d, W, H, title, subtitle, kicker, vertical, seal_text)

    if st.border:
        m = int(min(W, H) * 0.035)
        d.rounded_rectangle([m, m, W - m, H - m], radius=int(min(W, H) * 0.012),
                            outline=st.accent + (), width=max(int(min(W, H) * 0.0035), 2))

    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    img.save(out, quality=95)
    return out


def _layout_text(st: Style, img, d, W: int, H: int, title: str, subtitle: str,
                 kicker: str, vertical: bool, seal_text: str) -> None:
    """classic / ink / bold 三种：以文字为主体的排版。"""
    pad = int(min(W, H) * 0.085)

    # 顶部装饰线 + kicker
    y = pad
    if kicker:
        kf = font("body", int(min(W, H) * 0.028))
        d.text((pad, y), kicker, font=kf, fill=st.accent)
        y += int(min(W, H) * 0.055)
    d.line([(pad, y), (pad + int(min(W, H) * 0.12), y)], fill=st.accent,
           width=max(int(min(W, H) * 0.006), 3))
    y += int(min(W, H) * 0.05)

    # 竖排（仅竖版且风格为古典/水墨）
    if vertical and st.layout in ("classic", "ink") and len(title) <= 16:
        size = int(min(W * 0.11, (H - 2 * pad) / max(len(title) * 1.06, 1)))
        size = max(min(size, int(W * 0.13)), int(min(W, H) * 0.05))
        f = font(st.title_role, size)
        x_right = W - pad
        bottom = draw_vertical(d, title, x_right, y, f, fill=st.title,
                               stroke_fill=st.bg if st.layout == "bold" else None,
                               stroke_width=0, max_per_col=min(len(title), 8),
                               col_gap=1.16)
        if subtitle:
            sf = font("body", int(min(W, H) * 0.030))
            lines = wrap_text(subtitle, max(int((bottom - pad) / (sf.size * 1.02)), 6))
            draw_lines(d, lines, sf, pad, H - pad - len(lines) * sf.size * 1.4,
                       fill=st.body, line_spacing=1.4)
        if st.seal:
            draw_seal(img, (pad, H - pad - int(min(W, H) * 0.16)),
                      int(min(W, H) * 0.14), seal_text, color=st.accent)
        return

    # 横排：标题占中上部，副标题在下方
    box_w = W - 2 * pad
    box_h = int(H * (0.52 if subtitle else 0.60))
    max_size = int(min(W, H) * (0.135 if st.layout == "bold" else 0.115))
    f, lines = fit_font_size(title, box_w, box_h, role=st.title_role,
                             max_size=max_size,
                             max_chars_per_line=(max(int(box_w / (max_size * 0.95)), 5)
                                                 if st.layout == "bold" else None))
    y0 = y + max((box_h - len(lines) * f.size * 1.32) / 2, 0)
    stroke = max(int(f.size * 0.045), 2) if st.layout == "bold" else 0
    end_y = draw_lines(d, lines, f, pad if st.layout != "bold" else W / 2, y0,
                       fill=st.title, stroke_fill=(20, 20, 20),
                       stroke_width=stroke, line_spacing=1.32,
                       align="left" if st.layout != "bold" else "center")

    if subtitle:
        sf = font("body", int(min(W, H) * 0.032))
        slines = wrap_text(subtitle, max(int(box_w / (sf.size * 1.02)), 8))
        sy = min(end_y + int(min(W, H) * 0.045), H - pad - len(slines) * sf.size * 1.45)
        draw_lines(d, slines, sf, pad if st.layout != "bold" else W / 2, sy,
                   fill=st.body, line_spacing=1.45,
                   align="left" if st.layout != "bold" else "center")

    if st.seal:
        draw_seal(img, (W - pad - int(min(W, H) * 0.14),
                        H - pad - int(min(W, H) * 0.14)),
                  int(min(W, H) * 0.13), seal_text, color=st.accent)


def _layout_frame(img, d, st: Style, W: int, H: int, title: str, subtitle: str,
                  image: Optional[str], seal_text: str) -> None:
    """画框：古籍图居中，四周留白，标题在上方。"""
    pad = int(min(W, H) * 0.075)
    tf = font(st.title_role, int(min(W, H) * 0.062))
    tlines = wrap_text(title, max(int((W - 2 * pad) / (tf.size * 1.02)), 8))
    end_y = draw_lines(d, tlines, tf, W / 2, pad, fill=st.title,
                       line_spacing=1.3, align="center")
    y = end_y + int(min(W, H) * 0.035)
    d.line([(W * 0.5 - min(W, H) * 0.07, y), (W * 0.5 + min(W, H) * 0.07, y)],
           fill=st.accent, width=max(int(min(W, H) * 0.005), 2))
    y += int(min(W, H) * 0.045)

    bottom = H - pad - (int(min(W, H) * 0.10) if subtitle else 0)
    avail_h = bottom - y
    if image and os.path.exists(image):
        src = Image.open(image).convert("RGB")
        iw, ih = src.size
        scale = min((W - 2 * pad) / iw, avail_h / ih)
        nw, nh = max(int(iw * scale), 1), max(int(ih * scale), 1)
        thumb = src.resize((nw, nh), Image.LANCZOS)
        px, py = (W - nw) // 2, int(y + (avail_h - nh) / 2)
        d.rectangle([px - 3, py - 3, px + nw + 3, py + nh + 3], outline=st.accent, width=3)
        img.paste(thumb, (px, py))
    if subtitle:
        sf = font("body", int(min(W, H) * 0.030))
        slines = wrap_text(subtitle, max(int((W - 2 * pad) / (sf.size * 1.02)), 8))
        draw_lines(d, slines, sf, W / 2, H - pad - len(slines) * sf.size * 1.4,
                   fill=st.body, line_spacing=1.4, align="center")


def _layout_split(img, st: Style, W: int, H: int, title: str, subtitle: str,
                  image: Optional[str], seal_text: str) -> None:
    """上下分割：上半底图，下半文字。"""
    top_h = int(H * 0.52)
    d = ImageDraw.Draw(img)
    if image and os.path.exists(image):
        # 竖版素材（古籍页）放进横版条带必须 contain，否则竖排文字会被裁掉
        top = contain_with_blur(Image.open(image).convert("RGB"), W, top_h,
                                overlay=st.overlay, margin=0.08)
        img.paste(top, (0, 0))
    else:
        img.paste(make_background(W, top_h, st), (0, 0))
    d.line([(0, top_h), (W, top_h)], fill=st.accent, width=max(int(min(W, H) * 0.006), 3))

    pad = int(min(W, H) * 0.08)
    box_w = W - 2 * pad
    f, lines = fit_font_size(title, box_w, int(H - top_h - 2.2 * pad),
                             role=st.title_role, max_size=int(min(W, H) * 0.095))
    draw_lines(d, lines, f, pad, top_h + pad, fill=st.title, line_spacing=1.32)
    if subtitle:
        sf = font("body", int(min(W, H) * 0.030))
        slines = wrap_text(subtitle, max(int(box_w / (sf.size * 1.02)), 8))
        draw_lines(d, slines, sf, pad, H - pad - len(slines) * sf.size * 1.42,
                   fill=st.body, line_spacing=1.42)
    if st.seal:
        draw_seal(img, (W - pad - int(min(W, H) * 0.12), H - pad - int(min(W, H) * 0.12)),
                  int(min(W, H) * 0.11), seal_text, color=st.accent)


# ═══════════════════════════ 批量 ═══════════════════════════

def render_all_platforms(*, title: str, subtitle: str = "", kicker: str = "",
                         image: Optional[str] = None, style: str = "classic",
                         outdir: str = "covers", seal_text: str = "国学",
                         platforms: Optional[Sequence[str]] = None) -> list[str]:
    """一次生成全平台所有规格。返回生成的文件列表。"""
    os.makedirs(outdir, exist_ok=True)
    made: list[str] = []
    for pf in (platforms or list(PLATFORM_COVERS)):
        for label, size in PLATFORM_COVERS[pf].items():
            fn = f"{pf}_{label.replace(':', '-')}_{style}.png"
            p = os.path.join(outdir, fn)
            small = min(size) < 400          # 小图（如公众号次图）不放太多字
            render_cover(title=title.split("，")[0][:12] if small else title,
                         subtitle="" if small else subtitle,
                         kicker="" if small else kicker,
                         image=image, style=style, size=size, out=p,
                         seal_text=seal_text)
            made.append(p)
    return made


# ═══════════════════════════ CLI ═══════════════════════════

def cmd_list(args) -> int:
    print("可用字体：")
    for role, path in installed_fonts().items():
        print(f"  {role:<7} {path}")
    if "LXGWWenKai" not in installed_fonts().get("serif", ""):
        print("\n💡 未检测到霞鹜文楷。国学内容建议安装：")
        print("   https://github.com/lxgw/LxgwWenKai  → 放到 ~/Library/Fonts/")
    print("\n封面风格：")
    for k, s in STYLES.items():
        print(f"  {k:<9} {s.name:<10} {s.desc}")
    print("\n平台规格：")
    for pf, sizes in PLATFORM_COVERS.items():
        spec = "  ".join(f"{l}={w}×{h}" for l, (w, h) in sizes.items())
        print(f"  {pf:<12} {PLATFORM_NAME.get(pf, pf):<10} {spec}")
    return 0


def cmd_one(args) -> int:
    size = None
    if args.size:
        w, h = args.size.lower().split("x")
        size = (int(w), int(h))
    elif args.platform:
        sizes = PLATFORM_COVERS.get(args.platform)
        if not sizes:
            print(f"未知平台 {args.platform}，可用: {sorted(PLATFORM_COVERS)}")
            return 2
        size = sizes[args.label or list(sizes)[0]]
    size = size or (1080, 1440)
    p = render_cover(title=args.title, subtitle=args.subtitle or "", kicker=args.kicker or "",
                     image=args.image, style=args.style, size=size, out=args.out,
                     seal_text=args.seal)
    print(f"✅ {p}  {size[0]}×{size[1]}  [{STYLES[args.style].name}]")
    return 0


def cmd_all(args) -> int:
    made = render_all_platforms(title=args.title, subtitle=args.subtitle or "",
                                kicker=args.kicker or "", image=args.image,
                                style=args.style, outdir=args.outdir, seal_text=args.seal)
    print(f"✅ 生成 {len(made)} 张封面 → {args.outdir}/")
    for p in made:
        im = Image.open(p)
        print(f"   {os.path.basename(p):<38} {im.size[0]}×{im.size[1]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="cover_studio — 封面生成引擎",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__)
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("list", help="列字体/风格/平台规格").set_defaults(func=cmd_list)

    o = sub.add_parser("one", help="生成单张封面")
    o.add_argument("--title", required=True)
    o.add_argument("--subtitle", default="")
    o.add_argument("--kicker", default="", help="顶部小标签")
    o.add_argument("--image", help="底图路径")
    o.add_argument("--style", default="classic", choices=sorted(STYLES))
    o.add_argument("--platform", choices=sorted(PLATFORM_COVERS))
    o.add_argument("--label", help="平台下的具体规格名")
    o.add_argument("--size", help="自定义尺寸 WxH，如 1080x1440")
    o.add_argument("--seal", default="国学", help="印章文字")
    o.add_argument("-o", "--out", default="cover.png")
    o.set_defaults(func=cmd_one)

    a = sub.add_parser("all", help="生成全平台所有规格")
    a.add_argument("--title", required=True)
    a.add_argument("--subtitle", default="")
    a.add_argument("--kicker", default="")
    a.add_argument("--image")
    a.add_argument("--style", default="classic", choices=sorted(STYLES))
    a.add_argument("--seal", default="国学")
    a.add_argument("-d", "--outdir", default="covers")
    a.set_defaults(func=cmd_all)
    return p


def main(argv: Sequence[str]) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    fn = getattr(args, "func", None)
    if fn is None:
        p.print_help()
        return 2
    return fn(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
