#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_sample.py — 生成一份**示例素材库**，让项目 clone 下来就能跑通全流程。

为什么需要它
------------
真实古籍扫描件动辄几个 GB，不可能放进仓库。但没有素材，
素材库、选题引擎、封面、视频这整条链都演示不了。

所以这个脚本造一份**结构完全真实**的小型素材库：

    assets/
      示例古籍甲/SOURCE.md            ← 馆藏、索书号、册目（真实格式）
      示例古籍甲/0001/manifest.json   ← 逐页清单 + 许可（真实格式）
      示例古籍甲/0001/images/0001.jpg ← 第 1 页是**校准页**（故意造的）
      示例古籍甲/0001/images/0002.jpg ← 第 2 页起是正文

刻意保留的一个细节
------------------
每册第 1 页造的是**扫描校准靶**（灰阶/色卡那种），正文从第 2 页开始 ——
这和真实的数字化古籍完全一致。项目里的 `asset_library.is_content_page()`
就是靠"暗块占比"把这类页挑出来跳过的，你可以用它验证这个功能。

用法
----
    python3 make_sample.py                    # 默认 3 部书 × 8 页
    python3 make_sample.py --books 5 --pages 12
    python3 make_sample.py --out my_assets
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("需要 Pillow：pip install Pillow", file=sys.stderr)
    sys.exit(1)


#: 示例书目 —— 用真实存在的古籍名，但内容是随机字（不做任何原文转载）
SAMPLE_BOOKS = [
    ("示例古籍甲", "子部·术数类", "３１１－０２７２", "A"),
    ("示例古籍乙", "子部·医家类", "３１１－０４１５", "A"),
    ("示例古籍丙", "经部·易类", "３１１－００８８", "B"),
    ("示例古籍丁", "子部·天文算法类", "３１１－０３３０", "A"),
    ("示例古籍戊", "集部·别集类", "３１１－０５１２", "B"),
]

#: 造字用的常用字池（汉字符号，不构成任何真实文句）
CHARS = ("天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏"
         "阴阳五行乾坤震巽坎离艮兑金木水火土春夏秋冬"
         "君臣父子仁义礼智信温良恭俭让诗书礼乐射御书数")

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "C:/Windows/Fonts/simsun.ttc",
]


def find_font(size: int):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def make_calibration_page(w: int, h: int) -> Image.Image:
    """造一页**扫描校准靶** —— 暗背景 + 灰阶条 + 色块。

    真实数字化的每册首页就长这样。项目靠"暗块占比 > 20%"识别并跳过它们。
    """
    im = Image.new("RGB", (w, h), (38, 38, 40))
    d = ImageDraw.Draw(im)
    # 灰阶条
    steps = 12
    bw = int(w * 0.72 / steps)
    x0, y0 = int(w * 0.12), int(h * 0.30)
    for i in range(steps):
        v = int(255 * i / (steps - 1))
        d.rectangle([x0 + i * bw, y0, x0 + (i + 1) * bw, y0 + int(h * 0.20)],
                    fill=(v, v, v))
    # 色块
    colors = [(200, 60, 60), (60, 160, 90), (60, 90, 200), (210, 180, 60),
              (170, 80, 170), (80, 180, 190)]
    cw = int(w * 0.72 / len(colors))
    y1 = y0 + int(h * 0.24)
    for i, c in enumerate(colors):
        d.rectangle([x0 + i * cw, y1, x0 + (i + 1) * cw, y1 + int(h * 0.16)],
                    fill=c)
    # 角标
    d.text((x0, int(h * 0.12)), "CALIBRATION  TARGET",
           font=find_font(int(h * 0.03)), fill=(190, 190, 190))
    return im


def make_content_page(w: int, h: int, rng: random.Random) -> Image.Image:
    """造一页**仿古籍正文** —— 米黄纸底 + 竖排字 + 版框 + 界行。"""
    paper = (rng.randint(226, 240), rng.randint(218, 232), rng.randint(198, 214))
    im = Image.new("RGB", (w, h), paper)
    d = ImageDraw.Draw(im)

    # 版框
    m = int(w * 0.06)
    d.rectangle([m, m, w - m, h - m], outline=(140, 120, 96), width=2)

    fs = int(w * 0.030)
    font = find_font(fs)
    cols = 9
    col_w = (w - 2 * m) // cols
    rows = int((h - 2 * m) / (fs * 1.16))

    for c in range(cols):
        cx = w - m - (c + 1) * col_w + col_w // 2      # 从右往左
        d.line([cx + col_w // 2, m, cx + col_w // 2, h - m],
               fill=(196, 176, 150), width=1)          # 界行
        for r in range(rows):
            if rng.random() < 0.12:
                continue
            ch = rng.choice(CHARS)
            d.text((cx - fs // 2, m + int(fs * 0.25) + r * int(fs * 1.16)),
                   ch, font=font, fill=(30, 28, 26))

    # 藏书印
    sx, sy, ss = w - m - int(w * 0.14), h - m - int(h * 0.10), int(w * 0.055)
    d.rectangle([sx, sy, sx + ss, sy + ss], outline=(178, 42, 38), width=3)
    d.text((sx + ss * 0.28, sy + ss * 0.28), "藏", font=find_font(int(ss * 0.5)),
           fill=(178, 42, 38))
    return im


def write_source_md(path: str, title: str, kind: str, call_no: str,
                    grade: str, pages: int) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"""# {title}

| 项 | 值 |
| --- | --- |
| 馆藏原题 | {title} |
| 请求番号 | {call_no} |
| 收藏 | 示例馆藏机构 |
| 已取册数 | 1 |
| 已取页数 | {pages} |
| 底本等级 | {grade} —— 示例数据 |

## 对应的文档

- `{title}` —— 精确底本

## 册目

| 册号 | 题名 | 页数 |
| --- | --- | --- |
| 0001 | {title} 上 | {pages} |

> ⚠️ 本文件由 `make_sample.py` 生成，**仅用于演示项目功能**，
> 文字内容为随机字符，不含任何真实古籍原文。
""")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成示例素材库")
    ap.add_argument("--out", default="assets", help="输出目录（默认 assets）")
    ap.add_argument("--books", type=int, default=3, help="生成几部书")
    ap.add_argument("--pages", type=int, default=8, help="每册几页")
    ap.add_argument("--width", type=int, default=1200)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)

    rng = random.Random(args.seed)
    n_books = min(args.books, len(SAMPLE_BOOKS))
    total = 0

    print(f"正在生成示例素材库 → {args.out}/")
    for i in range(n_books):
        title, kind, call_no, grade = SAMPLE_BOOKS[i]
        bdir = os.path.join(args.out, title)
        vdir = os.path.join(bdir, "0001")
        idir = os.path.join(vdir, "images")
        os.makedirs(idir, exist_ok=True)

        write_source_md(os.path.join(bdir, "SOURCE.md"), title, kind,
                        call_no, grade, args.pages)

        pages = []
        for p in range(1, args.pages + 1):
            seq = f"{p:04d}"
            im = (make_calibration_page(args.width, args.height) if p == 1
                  else make_content_page(args.width, args.height, rng))
            fn = f"{seq}.jpg"
            im.save(os.path.join(idir, fn), quality=88)
            pages.append({
                "seq": seq, "file": fn,
                "url": f"https://example.invalid/{title}/{seq}",
                "bytes": os.path.getsize(os.path.join(idir, fn)),
                "status": "ok",
            })
            total += 1

        with open(os.path.join(vdir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({
                "source": "示例馆藏机构デジタルアーカイブ",
                "source_url": "https://example.invalid/",
                "license": "示例数据，可自由使用",
                "license_url": "",
                "aip_id": f"sample-{i + 1:03d}",
                "label": f"{call_no}/{i + 1:04d}",
                "image_mode": "iiif",
                "retrieved_at": time.strftime("%F"),
                "page_count": args.pages,
                "pages": pages,
            }, f, ensure_ascii=False, indent=2)
        print(f"  ✅ {title}  {args.pages} 页")

    print(f"\n完成：{n_books} 部书 · {total} 页 → {args.out}/")
    print("""
下一步：
    python3 asset_library.py import-collection --root %s
    python3 app_server.py
""" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
