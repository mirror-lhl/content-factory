#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
video_studio — 图文成片引擎

把「一段文案 + 一组图片」变成带**配音、字幕、BGM**的成片。
纯 ffmpeg + Pillow + macOS `say`，不需要任何外部服务。

为什么字幕要用 PNG 而不是 drawtext
----------------------------------
本机 ffmpeg 没编译 freetype/libass，`drawtext`/`subtitles`/`ass` 三个滤镜都缺失。
所以走 **Pillow 渲染透明 PNG → ffmpeg overlay** 的路子。
副作用是好事：字体、描边、字距、圆角背景全部可控，比 drawtext 灵活。

管线
----
    每段文案 ──say──→ wav ──ffprobe──→ 时长
       │                                  │
       ├──Pillow──→ 字幕 PNG              │
       │                                  ↓
       └──图片──→ scale/crop ──zoompan(运镜)──→ 段视频 ──┐
                                                        ├─concat─→ 成片
                        BGM ──volume/afade──────────────┘

用法
----
    from video_studio import build_video, Segment

    build_video(
        segments=[
            Segment(text="第一句旁白", image="a.jpg"),
            Segment(text="第二句旁白", image="b.jpg"),
        ],
        out="成片.mp4",
        size=(1080, 1920),      # 竖屏
        voice="Tingting",
        bgm="bgm.mp3",
    )

CLI:
    python3 video_studio.py demo            # 生成一个演示片，验证管线
    python3 video_studio.py build spec.json -o out.mp4
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Optional, Sequence

from PIL import Image, ImageDraw, ImageFont


# ─────────────────────────── 常量 ───────────────────────────

#: 常见画幅规格（宽, 高）
SIZES = {
    "vertical":   (1080, 1920),   # 抖音 / 小红书 / 视频号
    "horizontal": (1920, 1080),   # B站 / YouTube
    "square":     (1080, 1080),   # 小红书方图
    "portrait43": (1080, 1440),   # 4:3
}

#: 候选中文字体（按优先级）
FONT_CANDIDATES = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
]

#: macOS `say` 常见中文音色
ZH_VOICES = ["Tingting", "Binbin", "Shelley", "Eddy", "Flo", "Reed", "Sandy"]


class VideoError(RuntimeError):
    pass


# ─────────────────────────── 工具函数 ───────────────────────────

def _run(cmd: Sequence[str], *, desc: str = "", quiet: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(list(cmd), capture_output=True, text=True)
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-6:]
        raise VideoError(f"{desc or cmd[0]} 失败 (rc={r.returncode}):\n  " + "\n  ".join(tail))
    return r


def find_font() -> str:
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    raise VideoError("找不到可用的中文字体")


#: 字体缓存（字号变化频繁，缓存避免重复加载）
_FCACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(role: str = "serif", size: int = 40) -> ImageFont.FreeTypeFont:
    """按角色取中文字体。

    video_studio 只用一套字体（find_font 解析出来的），role 参数保留
    是为了与 cover_studio 的调用写法一致，将来接入多字体时可直接扩展。
    """
    size = max(int(size), 8)
    key = (role, size)
    if key not in _FCACHE:
        _FCACHE[key] = ImageFont.truetype(find_font(), size)
    return _FCACHE[key]


def probe_duration(path: str) -> float:
    """取媒体时长（秒）。"""
    r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "csv=p=0", path], desc="ffprobe")
    try:
        return float((r.stdout or "0").strip())
    except ValueError:
        return 0.0


def tts(text: str, out_wav: str, *, voice: str = "Tingting",
        rate: int = 180) -> str:
    """用 macOS `say` 合成配音。rate 是词/分钟，中文 160~200 比较自然。"""
    aiff = out_wav + ".aiff"
    _run(["say", "-v", voice, "-r", str(rate), "-o", aiff, text], desc="say")
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
          "-i", aiff, "-ar", "44100", "-ac", "2", out_wav], desc="aiff→wav")
    os.remove(aiff)
    return out_wav


#: 可作为断行点的标点
_BREAK_PUNCT = "，。！？；：、,.!?;:）)】」》…—"


def wrap_cjk(text: str, max_chars: int) -> list[str]:
    """中文智能折行。

    朴素的「每 N 个字切一刀」会产生难看的孤行（末行只剩一两个字）。
    这里做两件事：
      1. **行数确定后让各行长度均衡**（不贪心填满第一行）
      2. **优先在标点后断行**，找不到标点才硬断
    """
    text = text.strip()
    if not text:
        return [""]
    if len(text) <= max_chars:
        return [text]

    n_lines = math.ceil(len(text) / max_chars)
    target = math.ceil(len(text) / n_lines)
    lines: list[str] = []
    i = 0
    while i < len(text):
        remain = len(text) - i
        if remain <= max_chars:          # 剩下的能一行放下
            lines.append(text[i:])
            break
        take = min(target, max_chars)
        # 在目标位置附近 ±5 字窗口里找标点断点（但不留少于 3 字的尾巴）
        chosen = None
        lo = max(2, take - 5)
        hi = min(max_chars, remain - 3)
        for j in range(lo, hi + 1):
            if text[i + j - 1] in _BREAK_PUNCT:
                chosen = j
                break
        if chosen:
            take = chosen
        lines.append(text[i:i + take])
        i += take
    return lines


# ─────────────────────────── 字幕渲染 ───────────────────────────

@dataclass
class SubtitleStyle:
    font_size: int = 56
    color: tuple = (255, 255, 255, 255)
    stroke_color: tuple = (0, 0, 0, 220)
    stroke_width: int = 5
    max_chars_per_line: int = 18
    line_spacing: int = 18
    box_color: tuple = (0, 0, 0, 140)   # 半透明底衬，提升可读性
    box_padding: int = 24
    box_radius: int = 16
    bottom_margin: int = 320            # 距底部像素（竖屏要避开平台的点赞/评论按钮区）
    top_margin: int = 160               # position=top 时距顶部像素
    #: 字幕位置：bottom / center / top
    position: str = "bottom"
    #: 通栏底带 —— 在整条字幕后面铺一层半透明色带。
    #: 当底图本身有文字（例如竖排古籍页），这是唯一能彻底隔离的做法。
    band: bool = False


def render_subtitle_png(text: str, canvas_w: int, out_png: str,
                        style: SubtitleStyle = SubtitleStyle(),
                        *, canvas_h: Optional[int] = None) -> tuple[str, int, int]:
    """把一句话渲染成**透明 PNG**，返回 (路径, 宽, 高)。

    canvas_h 给定时输出**整画布尺寸**的 PNG，内容按 style.position 摆好位置。
    这样叠加时统一用 `overlay=0:0` 即可 —— 不用在两处重复计算 y 偏移，
    也就不会出现「字幕跑到画面顶部」这类错位。
    """
    font = ImageFont.truetype(find_font(), style.font_size)
    lines = wrap_cjk(text, style.max_chars_per_line)

    tmp = Image.new("RGBA", (10, 10))
    d0 = ImageDraw.Draw(tmp)
    w = 0
    line_h = []
    for ln in lines:
        bb = d0.textbbox((0, 0), ln, font=font, stroke_width=style.stroke_width)
        w = max(w, bb[2] - bb[0])
        line_h.append(bb[3] - bb[1])
    total_h = sum(line_h) + style.line_spacing * (len(lines) - 1)
    pad = style.box_padding
    box_w = int(canvas_w) if style.band else int(w + pad * 2)
    box_h = int(total_h + pad * 2)

    if canvas_h:                                  # 整画布模式
        img = Image.new("RGBA", (int(canvas_w), int(canvas_h)), (0, 0, 0, 0))
        pos = (style.position or "bottom").lower()
        if pos == "top":
            top = int(style.top_margin)
        elif pos == "center":
            top = int((canvas_h - box_h) / 2)
        else:
            top = int(canvas_h - box_h - style.bottom_margin)
        top = max(0, min(top, int(canvas_h) - box_h))
        left = 0 if style.band else int((canvas_w - box_w) / 2)
    else:                                          # 紧贴文字模式
        img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
        top, left = 0, 0

    draw = ImageDraw.Draw(img)
    if style.box_color[3] > 0:
        if style.band:
            draw.rectangle([left, top, left + box_w, top + box_h],
                           fill=style.box_color)
        else:
            draw.rounded_rectangle([left, top, left + box_w - 1, top + box_h - 1],
                                   radius=style.box_radius, fill=style.box_color)

    y = top + pad
    for ln, lh in zip(lines, line_h):
        bb = draw.textbbox((0, 0), ln, font=font, stroke_width=style.stroke_width)
        lw = bb[2] - bb[0]
        cx = left + (box_w - lw) / 2
        draw.text((cx - bb[0], y - bb[1]), ln, font=font, fill=style.color,
                  stroke_width=style.stroke_width, stroke_fill=style.stroke_color)
        y += lh + style.line_spacing

    img.save(out_png)
    return out_png, img.width, img.height


# ─────────────────────────── 图片预处理 ───────────────────────────

def fit_image(src: str, dst: str, size: tuple[int, int],
              *, overscan: float = 1.12) -> str:
    """把图片裁成目标画幅（cover 模式），并放大一点留给运镜。

    overscan 让图片比画布大一圈，zoompan 才有移动/缩放空间。
    """
    W, H = size
    bw, bh = int(W * overscan), int(H * overscan)
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
          "-vf", f"scale={bw}:{bh}:force_original_aspect_ratio=increase,"
                 f"crop={bw}:{bh}",
          "-frames:v", "1", dst], desc="fit_image")
    return dst


def make_placeholder_image(text: str, dst: str, size: tuple[int, int]) -> str:
    """生成占位图（渐变 + 文字），用于在没有素材时验证管线。"""
    W, H = size
    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(H):                       # 竖向渐变
        t = y / max(H - 1, 1)
        r = int(28 + 40 * t)
        g = int(34 + 22 * t)
        b = int(58 + 70 * t)
        for x in range(W):
            px[x, y] = (r, g, b)
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(find_font(), 120)
    bbox = d.textbbox((0, 0), text, font=f)
    d.text(((W - (bbox[2] - bbox[0])) / 2 - bbox[0],
            (H - (bbox[3] - bbox[1])) / 2 - bbox[1]),
           text, font=f, fill=(255, 255, 255))
    img.save(dst)
    return dst


# ─────────────────────────── 成片 ───────────────────────────

@dataclass
class Segment:
    """一段旁白 + 一张配图。"""

    text: str
    image: Optional[str] = None      # 留空则自动生成占位图
    duration: Optional[float] = None  # 留空则用配音时长
    zoom: str = "in"                  # in / out / none


@dataclass
class BuildSpec:
    segments: list[Segment]
    out: str = "out.mp4"
    size: tuple[int, int] = SIZES["vertical"]
    fps: int = 30
    voice: str = "Tingting"
    rate: int = 180
    bgm: Optional[str] = None
    bgm_volume: float = 0.12
    subtitle: SubtitleStyle = field(default_factory=SubtitleStyle)
    tail_padding: float = 0.35        # 每段末尾留白，避免话赶话
    encoder: str = "h264_videotoolbox"
    #: 视频模板名（见 TEMPLATES）。留空则用 BuildSpec 上的零散参数
    template: str = ""
    #: 片头标题（留空则用第一段文案的前 16 字）
    intro_title: str = ""


#: 运镜类型 → 缩放/平移表达式生成器
MOTIONS = ["in", "out", "left", "right", "up", "down", "none"]


# ═══════════════════════════ 视频模板 ═══════════════════════════

@dataclass
class VideoTemplate:
    """一套成片风格预设。把"要调哪些参数"变成"选一个名字"。"""

    key: str
    name: str
    desc: str
    #: 适合的画幅
    sizes: tuple = ("vertical", "horizontal", "square")
    #: 片头 / 片尾
    intro: bool = True
    intro_dur: float = 3.0
    intro_kicker: str = ""
    outro: bool = False
    outro_dur: float = 2.5
    outro_text: str = "关注我，一起读经典"
    #: 转场
    transition: str = "fade"
    transition_dur: float = 0.45
    #: 运镜（auto = 逐段交替 in/out）
    motion: str = "auto"
    #: 字幕
    subtitle: SubtitleStyle = field(default_factory=SubtitleStyle)
    #: 装饰
    index_badge: bool = False
    progress_bar: bool = False
    #: 视觉
    accent: tuple = (168, 52, 42)
    card_bg: tuple = (243, 238, 226)
    card_ink: tuple = (30, 26, 22)
    font_role: str = "serif"
    bgm_volume: float = 0.12

    def motion_for(self, i: int) -> str:
        if self.motion != "auto":
            return self.motion
        return ("in", "out", "left", "right")[i % 4]


TEMPLATES: dict[str, VideoTemplate] = {
    "guofeng": VideoTemplate(
        key="guofeng", name="国风典雅",
        desc="宣纸底片头 + 慢速运镜 + 通栏字幕 + 朱红装饰 —— 国学内容首选",
        intro=True, intro_dur=3.0, outro=True, outro_text="读经典，慢一点也好",
        transition="fade", transition_dur=0.6, motion="auto",
        # 底带透明度 215：古籍扫描页文字密集，半透明挡不住，字幕会糊在原文里
        subtitle=SubtitleStyle(font_size=56, band=True, box_color=(0, 0, 0, 215),
                               bottom_margin=0, max_chars_per_line=16,
                               stroke_width=6),
        index_badge=False, progress_bar=True,
        accent=(168, 52, 42), card_bg=(243, 238, 226), card_ink=(30, 26, 22),
        font_role="serif", bgm_volume=0.10,
    ),
    "knowledge": VideoTemplate(
        key="knowledge", name="知识口播",
        desc="简介片头 + 硬切 + 底部字幕 + 序号角标 —— 信息密度优先",
        intro=True, intro_dur=2.2, outro=False,
        transition="none", motion="in",
        subtitle=SubtitleStyle(font_size=52, band=False,
                               box_color=(0, 0, 0, 200), bottom_margin=300,
                               max_chars_per_line=18),
        index_badge=True, progress_bar=True,
        accent=(60, 90, 150), card_bg=(255, 255, 255), card_ink=(20, 20, 24),
        font_role="sans", bgm_volume=0.06,
    ),
    "punch": VideoTemplate(
        key="punch", name="快节奏冲击",
        desc="大字片头 + 侧滑转场 + 快速缩放 —— 抖音/小红书抓眼球",
        intro=True, intro_dur=2.0, outro=False,
        transition="slideleft", transition_dur=0.32, motion="in",
        subtitle=SubtitleStyle(font_size=62, band=True, box_color=(0, 0, 0, 190),
                               bottom_margin=0, max_chars_per_line=14,
                               stroke_width=6),
        index_badge=False, progress_bar=True,
        accent=(230, 70, 60), card_bg=(198, 48, 40), card_ink=(255, 255, 255),
        font_role="sans", bgm_volume=0.16,
    ),
    "poetic": VideoTemplate(
        key="poetic", name="留白诗意",
        desc="静止画面 + 缓慢淡入淡出 + 居中字幕 —— 适合原文诵读",
        intro=False, outro=False,
        transition="fadeblack", transition_dur=0.9, motion="none",
        subtitle=SubtitleStyle(font_size=50, band=False,
                               box_color=(0, 0, 0, 0), bottom_margin=380,
                               max_chars_per_line=14),
        index_badge=False, progress_bar=False,
        accent=(150, 120, 70), card_bg=(248, 246, 240), card_ink=(40, 36, 32),
        font_role="serif", bgm_volume=0.14,
    ),
    "plain": VideoTemplate(
        key="plain", name="素片（无片头）",
        desc="不加片头片尾，硬切，只做字幕与配音 —— 最快出片",
        intro=False, outro=False, transition="none", motion="auto",
        subtitle=SubtitleStyle(font_size=56, band=True, box_color=(0, 0, 0, 165),
                               bottom_margin=0, max_chars_per_line=16),
        index_badge=False, progress_bar=False, bgm_volume=0.10,
    ),
}


def _zoompan_expr(direction: str, frames: int, size: tuple[int, int],
                  *, fps: int = 30) -> str:
    """Ken Burns 运镜表达式。

    支持 6 种运镜 + 静止：
      in/out         —— 推近 / 拉远
      left/right     —— 左右平移
      up/down        —— 上下平移
      none           —— 静止（留白诗意类模板用）
    平移类在保持 1.15 倍放大的基础上移动取景框，观感比纯缩放更"活"。
    """
    W, H = size
    d = (direction or "in").lower()
    if d == "none":
        return f"zoompan=z=1.001:d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'" \
               f":s={W}x{H}:fps={fps}"
    if d == "out":
        z = "max(1.18-0.0006*on,1.0)"
        return (f"zoompan=z='{z}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                f":s={W}x{H}:fps={fps}")
    if d == "in":
        z = "min(zoom+0.0006,1.18)"
        return (f"zoompan=z='{z}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                f":s={W}x{H}:fps={fps}")
    # 平移类：固定 1.15 倍放大，取景框按时间移动
    Z = 1.15
    x1 = "(iw-iw/zoom)"
    y1 = "(ih-ih/zoom)"
    ex = {"left":  (f"({x1})*(1-on/{max(frames-1,1)})", f"({y1})/2"),
          "right": (f"({x1})*(on/{max(frames-1,1)})", f"({y1})/2"),
          "up":    (f"({x1})/2", f"({y1})*(1-on/{max(frames-1,1)})"),
          "down":  (f"({x1})/2", f"({y1})*(on/{max(frames-1,1)})")}.get(
              d, (f"({x1})/2", f"({y1})/2"))
    return (f"zoompan=z={Z}:d=1:x='{ex[0]}':y='{ex[1]}':s={W}x{H}:fps={fps}")


#: 可选转场（对应 ffmpeg xfade 的 transition 名）
TRANSITIONS = ["fade", "fadeblack", "fadewhite", "slideleft", "slideright",
               "slideup", "wipedown", "wipeleft", "circleopen", "dissolve",
               "smoothleft", "none"]




def timeline_spans(durs: list[float], transition: str, td: float
                   ) -> list[tuple[float, float]]:
    """计算每段在**最终时间轴**上的 [起, 止]。

    转场会让相邻段重叠，所以总时长不是各段之和。
    字幕必须按这个时间轴对齐，否则会错位。

    注意：transition='none' 是硬切，没有重叠（td 视为 0）。
    """
    eff = 0.0 if (not transition or transition == "none") else td
    spans: list[tuple[float, float]] = []
    cum = 0.0
    for i, d in enumerate(durs):
        start = 0.0 if i == 0 else cum - eff
        start = max(start, 0.0)
        spans.append((start, start + d))
        cum = spans[-1][1]
    return spans


def combine_xfade(clips: list[str], out: str, *, transition: str = "fade",
                  duration: float = 0.45, fps: int = 30,
                  encoder: str = "h264_videotoolbox", verbose: bool = True) -> str:
    """把多段视频用 xfade **交叉转场**接起来（视频 + 音频都做过渡）。

    单纯 concat 是硬切，观感生硬。xfade 让相邻两段重叠淡变，
    这是"看起来像剪过"和"看起来像 PPT"的分界线。

    拼接公式：第 i 次拼接的 offset = 前面所有段累计时长 - 转场时长
    """
    if len(clips) == 1:
        shutil.copyfile(clips[0], out)
        return out
    if not transition or transition == "none":
        lf = out + ".concat.txt"
        with open(lf, "w", encoding="utf-8") as f:
            for c in clips:
                f.write(f"file '{c}'\n")
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
              "-f", "concat", "-safe", "0", "-i", lf, "-c", "copy", out],
             desc="硬切拼接")
        os.remove(lf)
        return out

    durs = [probe_duration(c) for c in clips]
    td = max(min(duration, min(durs) * 0.4), 0.1)   # 转场不能超过最短段

    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for c in clips:
        args += ["-i", c]

    vparts, aparts = [], []
    vprev, aprev = "[0:v]", "[0:a]"
    cum = durs[0]
    for i in range(1, len(clips)):
        off = max(cum - td, 0.001)
        vout, aout = f"[v{i}]", f"[a{i}]"
        vparts.append(f"{vprev}[{i}:v]xfade=transition={transition}"
                      f":duration={td:.3f}:offset={off:.3f}{vout}")
        aparts.append(f"{aprev}[{i}:a]acrossfade=d={td:.3f}:c1=tri:c2=tri{aout}")
        vprev, aprev = vout, aout
        cum = cum + durs[i] - td

    fc = ";".join(vparts + aparts)
    args += ["-filter_complex", fc, "-map", vprev, "-map", aprev,
             "-c:v", encoder, "-b:v", "8M", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-r", str(fps), out]
    _run(args, desc=f"转场拼接({transition})")
    if verbose:
        print(f"[video_studio] 转场拼接 {len(clips)} 段 · {transition} · "
              f"过渡 {td:.2f}s", file=sys.stderr)
    return out



# ═══════════════════════════ 卡片与装饰 ═══════════════════════════

def render_card_image(text: str, out_png: str, size: tuple[int, int], *,
                      kicker: str = "", tpl: Optional[VideoTemplate] = None,
                      big: bool = True) -> str:
    """渲染片头/片尾卡片（纯色渐变 + 标题 + 强调线 + 印章感方块）。"""
    W, H = size
    t = tpl or TEMPLATES["guofeng"]
    img = Image.new("RGB", (W, H), t.card_bg)
    d = ImageDraw.Draw(img)

    # 竖向渐变，让卡片不呆板
    base = t.card_bg
    end = tuple(max(0, c - 14) for c in base)
    for y in range(H):
        k = y / max(H - 1, 1)
        d.line([(0, y), (W, y)],
               fill=tuple(int(base[i] + (end[i] - base[i]) * k) for i in range(3)))

    fsize = int(min(W, H) * (0.105 if big else 0.075))
    f = font(t.font_role, fsize)
    lines = wrap_cjk(text, max(int((W * 0.78) / (fsize * 1.02)), 6))
    total_h = len(lines) * fsize * 1.35
    y0 = (H - total_h) / 2 - (H * 0.03 if kicker else 0)

    if kicker:
        kf = font(t.font_role, int(min(W, H) * 0.030))
        kw = kf.getbbox(kicker)[2] - kf.getbbox(kicker)[0]
        d.text(((W - kw) / 2, y0 - min(W, H) * 0.085), kicker, font=kf,
               fill=t.accent)

    cy = y0
    for ln in lines:
        bb = f.getbbox(ln)
        lw = bb[2] - bb[0]
        d.text(((W - lw) / 2 - bb[0], cy - bb[1]), ln, font=f, fill=t.card_ink)
        cy += fsize * 1.35

    # 强调线
    lw2 = min(W, H) * 0.09
    ly = cy + min(W, H) * 0.045
    d.line([(W / 2 - lw2, ly), (W / 2 + lw2, ly)], fill=t.accent,
           width=max(int(min(W, H) * 0.006), 3))

    # 右下角小方印（用纯色块，避免依赖 cover_studio 的印章实现）
    sq = int(min(W, H) * 0.055)
    m = int(min(W, H) * 0.075)
    d.rectangle([W - m - sq, H - m - sq, W - m, H - m],
                outline=t.accent, width=max(int(sq * 0.10), 2))
    img.save(out_png)
    return out_png


def render_decoration_png(idx: int, total: int, out_png: str,
                          size: tuple[int, int], tpl: VideoTemplate) -> Optional[str]:
    """渲染装饰层：序号角标 + 进度条（合成一张透明 PNG，一次 overlay）。"""
    if not tpl.index_badge and not tpl.progress_bar:
        return None
    W, H = size
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    if tpl.index_badge:
        r = int(min(W, H) * 0.032)
        pad = int(min(W, H) * 0.05)
        cx, cy = pad + r, pad + r
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=tpl.accent + (230,))
        f = font(tpl.font_role, int(r * 1.15))
        s = str(idx)
        bb = f.getbbox(s)
        d.text((cx - (bb[2] - bb[0]) / 2 - bb[0], cy - (bb[3] - bb[1]) / 2 - bb[1]),
               s, font=f, fill=(255, 255, 255, 255))

    if tpl.progress_bar:
        bh = max(int(min(W, H) * 0.0055), 4)
        y = H - bh
        d.rectangle([0, y, W, H], fill=(0, 0, 0, 70))
        done = int(W * (idx / max(total, 1)))
        if done > 0:
            d.rectangle([0, y, done, H], fill=tpl.accent + (235,))

    img.save(out_png)
    return out_png




def overlay_timed_subtitles(base: str, out: str, items: list[tuple[str, float, float]],
                            *, deco: list[tuple[str, float, float]] | None = None,
                            encoder: str = "h264_videotoolbox",
                            verbose: bool = True) -> str:
    """把字幕/装饰**按绝对时间区间**叠到已拼好的视频上。

    为什么要单独一遍：`xfade` 会把相邻两段的**所有图层**一起交叉淡变，
    字幕也会重影 —— 屏幕上同时出现两句话，观感很糟。
    正确做法是转场只作用于画面，**字幕硬切**。

    items: [(png 路径, 开始秒, 结束秒), ...]
    """
    if not items and not deco:
        shutil.copyfile(base, out)
        return out
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", base]
    idx = 1
    parts, last = [], "0:v"
    for png, t0, t1 in list(items) + list(deco or []):
        args += ["-i", png]
        outl = f"[o{idx}]"
        parts.append(f"[{last}][{idx}:v]overlay=0:0:"
                     f"enable='between(t,{t0:.3f},{t1:.3f})'{outl}")
        last = f"o{idx}"
        idx += 1
    parts.append(f"[{last}]format=yuv420p[vout]")
    args += ["-filter_complex", ";".join(parts),
             "-map", "[vout]", "-map", "0:a",
             "-c:v", encoder, "-b:v", "8M", "-pix_fmt", "yuv420p",
             "-c:a", "copy", out]
    _run(args, desc="叠加字幕")
    if verbose:
        print(f"[video_studio] 字幕按时间轴硬切叠加 {len(items)} 条", file=sys.stderr)
    return out


def _silent_audio(dur: float, out_wav: str, *, ar: int = 44100) -> str:
    """生成一段静音，给片头/片尾配一条音轨（转场需要两段都有音频）。"""
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
          "-f", "lavfi", "-i", f"anullsrc=r={ar}:cl=stereo",
          "-t", f"{dur:.3f}", out_wav], desc="静音轨")
    return out_wav


def _render_clip(bg_png: str, wav: str, out: str, *, size: tuple[int, int],
                 dur: float, fps: int, motion: str, subtitle_png: Optional[str],
                 subtitle: SubtitleStyle, deco_png: Optional[str],
                 encoder: str, tag: str) -> str:
    """把「底图 + 配音 + 字幕 + 装饰」合成一段视频。

    滤镜链：底图 → 放大裁切 → 运镜 → 叠字幕 → 叠装饰
    多个 overlay 在同一次 ffmpeg 调用里链式完成，不落中间文件。
    """
    W, H = size
    over = 1.12
    bw, bh = int(W * over), int(H * over)
    parts = [f"[0:v]scale={bw}:{bh}:force_original_aspect_ratio=increase,"
             f"crop={bw}:{bh},{_zoompan_expr(motion, max(int(dur*fps), 1), size, fps=fps)}[bg]"]
    inputs = ["-loop", "1", "-framerate", str(fps), "-i", bg_png]
    last = "bg"
    nidx = 1
    if subtitle_png:
        inputs += ["-i", subtitle_png]
        y_expr = {"bottom": f"H-h-{subtitle.bottom_margin}",
                  "top": f"{subtitle.top_margin}",
                  "center": "(H-h)/2"}.get((subtitle.position or "bottom").lower(),
                                           f"H-h-{subtitle.bottom_margin}")
        parts.append(f"[{last}][{nidx}:v]overlay=(W-w)/2:{y_expr}[s{nidx}]")
        last = f"s{nidx}"
        nidx += 1
    if deco_png:
        inputs += ["-i", deco_png]
        parts.append(f"[{last}][{nidx}:v]overlay=0:0[d{nidx}]")
        last = f"d{nidx}"
        nidx += 1
    parts.append(f"[{last}]format=yuv420p[vout]")

    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + inputs +
         ["-i", wav, "-filter_complex", ";".join(parts),
          "-map", "[vout]", "-map", f"{nidx}:a",
          "-t", f"{dur:.3f}",
          "-c:v", encoder, "-b:v", "8M", "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
          out], desc=f"渲染 {tag}")
    return out


def build_video(spec: BuildSpec, *, keep_temp: bool = False,
                verbose: bool = True) -> str:
    """把 BuildSpec 渲染成 MP4，返回输出路径。

    流程：
        片头卡片 → 逐段(配图+配音+字幕+装饰) → 片尾卡片
            ↓ 转场拼接（xfade 视频 + acrossfade 音频）
            ↓ 混入 BGM
          成片
    """
    tpl = TEMPLATES.get(spec.template) if spec.template else None
    sub_style = tpl.subtitle if tpl else spec.subtitle
    W, H = spec.size
    fps = spec.fps

    work = tempfile.mkdtemp(prefix="video_studio_")
    if verbose:
        print(f"[video_studio] 临时目录 {work}", file=sys.stderr)
        if tpl:
            print(f"[video_studio] 模板「{tpl.name}」· 片头={tpl.intro} · "
                  f"转场={tpl.transition} · 运镜={tpl.motion}", file=sys.stderr)

    clips: list[str] = []
    clip_durs: list[float] = []
    #: 片段序号 → 该片段的字幕/装饰 PNG（转场后再按绝对时间叠上去）
    overlays: dict[int, tuple[Optional[str], Optional[str]]] = {}
    try:
        n_seg = len(spec.segments)
        title = spec.intro_title or (spec.segments[0].text[:16] if spec.segments else "")

        # ① 片头
        if tpl and tpl.intro:
            card = render_card_image(title, os.path.join(work, "intro.png"),
                                     spec.size, kicker=tpl.intro_kicker or "原创",
                                     tpl=tpl, big=True)
            wav = _silent_audio(tpl.intro_dur, os.path.join(work, "intro.wav"))
            clips.append(_render_clip(card, wav, os.path.join(work, "intro.mp4"),
                                      size=spec.size, dur=tpl.intro_dur, fps=fps,
                                      motion="in", subtitle_png=None,
                                      subtitle=sub_style, deco_png=None,
                                      encoder=spec.encoder, tag="片头"))
            clip_durs.append(tpl.intro_dur)
            if verbose:
                print(f"[video_studio] 片头 {tpl.intro_dur:.1f}s", file=sys.stderr)

        # ② 正文各段
        for i, seg in enumerate(spec.segments):
            tag = f"seg{i:02d}"
            wav = os.path.join(work, f"{tag}.wav")
            tts(seg.text, wav, voice=spec.voice, rate=spec.rate)
            dur = seg.duration or (probe_duration(wav) + spec.tail_padding)

            src = seg.image
            if not src or not os.path.exists(src):
                src = make_placeholder_image(f"{i+1}",
                                             os.path.join(work, f"{tag}_ph.png"),
                                             spec.size)
            fitted = fit_image(src, os.path.join(work, f"{tag}_fit.png"), spec.size)

            sub_png = render_subtitle_png(seg.text, W,
                                          os.path.join(work, f"{tag}_sub.png"),
                                          sub_style, canvas_h=H)[0]
            motion = tpl.motion_for(i) if tpl else (seg.zoom or "in")
            deco = (render_decoration_png(i + 1, n_seg, os.path.join(work, f"{tag}_deco.png"),
                                          spec.size, tpl) if tpl else None)

            # **只渲染画面** —— 字幕留到转场之后按绝对时间硬切叠上去
            clips.append(_render_clip(fitted, wav, os.path.join(work, f"{tag}.mp4"),
                                      size=spec.size, dur=dur, fps=fps, motion=motion,
                                      subtitle_png=None, subtitle=sub_style,
                                      deco_png=None, encoder=spec.encoder, tag=f"第 {i+1} 段"))
            clip_durs.append(dur)
            overlays[len(clips) - 1] = (sub_png, deco)
            if verbose:
                print(f"[video_studio] 第 {i+1}/{n_seg} 段 {dur:.2f}s · {motion} "
                      f"← {seg.text[:18]}…", file=sys.stderr)

        # ③ 片尾
        if tpl and tpl.outro:
            card = render_card_image(tpl.outro_text, os.path.join(work, "outro.png"),
                                     spec.size, tpl=tpl, big=False)
            wav = _silent_audio(tpl.outro_dur, os.path.join(work, "outro.wav"))
            clips.append(_render_clip(card, wav, os.path.join(work, "outro.mp4"),
                                      size=spec.size, dur=tpl.outro_dur, fps=fps,
                                      motion="none", subtitle_png=None,
                                      subtitle=sub_style, deco_png=None,
                                      encoder=spec.encoder, tag="片尾"))
            clip_durs.append(tpl.outro_dur)

        if not clips:
            raise VideoError("没有可渲染的片段")

        # ④ 转场拼接（只拼画面）
        if verbose:
            print(f"[video_studio] 开始转场拼接 {len(clips)} 段"
                  f"（ffmpeg 一次调用，稍等）…", file=sys.stderr)
        trans = (tpl.transition if tpl else "fade")
        tdx = (tpl.transition_dur if tpl else 0.35)
        spans = timeline_spans(clip_durs, trans, tdx)   # 字幕按这个时间轴对齐
        joined = os.path.join(work, "joined.mp4")
        combine_xfade(clips, joined, transition=trans, duration=tdx,
                      fps=fps, encoder=spec.encoder, verbose=verbose)

        # ⑤ 字幕/装饰按**绝对时间**硬切叠加 —— 避免转场时两句话重影
        # xfade 让相邻段的**时间区间本身重叠**，所以字幕窗口不能直接用 [t0,t1]
        # —— 那样过渡期间两句话会同时显示（屏幕上出现重影）。
        # 正确做法：字幕窗口**互斥**，在转场中点交接：
        #   段 i 的字幕从（本段起点 + td/2）显示到（下一段起点 + td/2）
        sub_items: list[tuple[str, float, float]] = []
        deco_items: list[tuple[str, float, float]] = []
        for ci in sorted(overlays):
            if ci >= len(spans):
                continue
            sp, dp = overlays[ci]
            t0, t1 = spans[ci]
            s_start = t0 + (tdx / 2 if ci > 0 else 0.05)
            if ci + 1 < len(spans):
                s_end = min(spans[ci + 1][0] + tdx / 2, t1)
            else:
                s_end = t1 - 0.05
            if s_end <= s_start:
                s_end = s_start + 0.3
            if sp:
                sub_items.append((sp, s_start, s_end))
            if dp:
                deco_items.append((dp, s_start, s_end))
        if verbose:
            print(f"[video_studio] 叠加 {len(sub_items)} 条字幕…", file=sys.stderr)
        timed = os.path.join(work, "timed.mp4")
        overlay_timed_subtitles(joined, timed, sub_items, deco=deco_items,
                                encoder=spec.encoder, verbose=verbose)
        joined = timed

        # ⑥ 混 BGM
        if verbose:
            print("[video_studio] 混 BGM…", file=sys.stderr)
        bgm_vol = tpl.bgm_volume if tpl else spec.bgm_volume
        if spec.bgm and os.path.exists(spec.bgm):
            total = probe_duration(joined)
            _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                  "-i", joined, "-stream_loop", "-1", "-i", spec.bgm,
                  "-filter_complex",
                  f"[1:a]volume={bgm_vol},atrim=0:{total:.3f},"
                  f"afade=t=out:st={max(total-1.5,0):.3f}:d=1.5[bgm];"
                  f"[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=0[a]",
                  "-map", "0:v", "-map", "[a]",
                  "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                  spec.out], desc="混入 BGM")
        else:
            shutil.copyfile(joined, spec.out)

        if verbose:
            d = probe_duration(spec.out)
            print(f"[video_studio] ✅ 成片 {spec.out}  {W}×{H}  {d:.1f}s"
                  + (f"  [{tpl.name}]" if tpl else ""), file=sys.stderr)
        return spec.out
    finally:
        if not keep_temp:
            shutil.rmtree(work, ignore_errors=True)


# ─────────────────────────── CLI ───────────────────────────

def _demo(out: str = "demo_成片.mp4") -> int:
    """生成一个演示片，端到端验证管线。"""
    script = [
        ("很多人做内容，卡在第一步：不知道写什么。", "选题"),
        ("其实选题不该靠灵感，而该靠方法。", "方法"),
        ("先看平台上什么内容在被反复消费。", "观察"),
        ("再找你自己的经历里，能对上这个需求的部分。", "结合"),
        ("最后才是动笔。", "动笔"),
    ]
    segs = [Segment(text=t, image=None, zoom=("in" if i % 2 == 0 else "out"))
            for i, (t, _) in enumerate(script)]
    spec = BuildSpec(segments=segs, out=out, size=SIZES["vertical"],
                     voice="Tingting", rate=180)
    print(f"渲染演示片 → {out}")
    build_video(spec)
    return 0


def _build_from_json(path: str, out: Optional[str]) -> int:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    segs = [Segment(**s) for s in data["segments"]]
    size_key = data.get("size", "vertical")
    size = SIZES.get(size_key, SIZES["vertical"]) if isinstance(size_key, str) \
        else tuple(size_key)
    st = SubtitleStyle(**data["subtitle"]) if "subtitle" in data else SubtitleStyle()
    spec = BuildSpec(segments=segs, out=out or data.get("out", "out.mp4"),
                     size=size, voice=data.get("voice", "Tingting"),
                     rate=data.get("rate", 180), bgm=data.get("bgm"),
                     subtitle=st)
    build_video(spec)
    return 0


def main(argv: Sequence[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "demo":
        return _demo(argv[1] if len(argv) > 1 else "demo_成片.mp4")
    if argv[0] == "build":
        out = None
        if "-o" in argv:
            i = argv.index("-o")
            out = argv[i + 1]
        return _build_from_json(argv[1], out)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
