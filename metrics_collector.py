#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
metrics_collector — 数据自动采集（只读）

把创作者后台数据看板上的数字**读回本地**，自动填进内容项目，闭合选题反馈回路。

    截图创作者后台 → Vision OCR（带坐标）→ 按行列重组 → 匹配内容项目 → 写回 metrics

⚠️ 边界：**只读不写**
------------------------
本模块只做一件事：读取**你自己账号**的数据看板。
它不发布、不点赞、不评论、不关注、不改任何设置。
读取自己账号的数据是这类自动化里风险最低的一类。

OCR 说明
--------
用 macOS 内置的 **Vision 框架**（`VNRecognizeTextRequest`），无需联网、无需 API key。
实测：后台看板类界面识别准确；**竖排古籍的识别能力未用真实素材验证**。

用法
----
    # 从已有的截图读取
    python3 metrics_collector.py read 截图.png

    # 打开后台截图再读取（用 browser_rpa）
    python3 metrics_collector.py capture --platform xiaohongshu

    # 读取并把数据写回匹配的内容项目
    python3 metrics_collector.py sync 截图.png --ws workspace
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional, Sequence

import content_ops as co


# ═══════════════════════════ OCR ═══════════════════════════

@dataclass
class OcrItem:
    """一段识别出的文字及其归一化坐标（原点在**左下角**，Vision 约定）。"""

    text: str
    x: float
    y: float
    w: float = 0.0
    h: float = 0.0

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


class Ocr:
    """macOS Vision 文字识别封装。"""

    def __init__(self, languages: Sequence[str] = ("zh-Hans", "en-US"),
                 accurate: bool = True):
        try:
            import Vision
        except ImportError:
            raise RuntimeError(
                "需要 pyobjc-framework-Vision：\n"
                "  pip install --target=./rpa_libs pyobjc-framework-Vision")
        import Quartz
        import Foundation
        self._V, self._Q, self._F = Vision, Quartz, Foundation
        self.languages = list(languages)
        self.accurate = accurate

    def read(self, path: str) -> list[OcrItem]:
        V, Q, F = self._V, self._Q, self._F
        url = F.NSURL.fileURLWithPath_(os.path.abspath(path))
        src = Q.CGImageSourceCreateWithURL(url, None)
        if src is None:
            raise RuntimeError(f"无法读取图片: {path}")
        cg = Q.CGImageSourceCreateImageAtIndex(src, 0, None)
        handler = V.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
        req = V.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(
            V.VNRequestTextRecognitionLevelAccurate if self.accurate
            else V.VNRequestTextRecognitionLevelFast)
        req.setRecognitionLanguages_(self.languages)
        req.setUsesLanguageCorrection_(True)
        ok, err = handler.performRequests_error_([req], None)
        if not ok:
            raise RuntimeError(f"OCR 失败: {err}")
        out: list[OcrItem] = []
        for obs in (req.results() or []):
            cands = obs.topCandidates_(1)
            if not cands or not len(cands):
                continue
            bb = obs.boundingBox()
            out.append(OcrItem(
                text=str(cands[0].string()),
                x=float(bb.origin.x), y=float(bb.origin.y),
                w=float(bb.size.width), h=float(bb.size.height)))
        return out


def group_rows(items: Sequence[OcrItem], *, tol: float = 0.018) -> list[list[OcrItem]]:
    """按 y 坐标把 OCR 结果聚合成「行」。

    Vision 返回的是零散文本块，同一行的块 y 很接近。
    先按 y 从**上到下**排序（Vision 的 y 原点在左下，所以要反过来），
    再把 y 差值小于 tol 的归为同一行。
    """
    if not items:
        return []
    ordered = sorted(items, key=lambda i: -i.cy)
    rows: list[list[OcrItem]] = []
    cur: list[OcrItem] = []
    base = None
    for it in ordered:
        if base is None or abs(it.cy - base) <= tol:
            cur.append(it)
            base = it.cy if base is None else base
        else:
            rows.append(sorted(cur, key=lambda i: i.x))
            cur = [it]
            base = it.cy
    if cur:
        rows.append(sorted(cur, key=lambda i: i.x))
    return rows


# ═══════════════════════════ 看板解析 ═══════════════════════════

#: 各平台的列名别名（不同平台后台叫法不一样）
COLUMN_ALIASES: dict[str, list[str]] = {
    "views":     ["浏览量", "观看量", "播放量", "阅读量", "曝光量", "展现量", "阅读", "播放", "浏览", "观看"],
    "likes":     ["点赞量", "点赞数", "点赞", "赞"],
    "comments":  ["评论量", "评论数", "评论"],
    "favorites": ["收藏量", "收藏数", "收藏"],
    "shares":    ["分享量", "分享数", "分享", "转发"],
}

#: 平台 → 后台数据看板地址
PLATFORM_DASHBOARDS: dict[str, dict] = {
    "xiaohongshu": {
        "name": "小红书",
        "url": "https://creator.xiaohongshu.com/new/note-manager",
        "note": "笔记管理页含每篇笔记的数据",
    },
    "douyin": {
        "name": "抖音",
        "url": "https://creator.douyin.com/creator-micro/content/manage",
        "note": "作品管理页含播放/点赞/评论",
    },
    "bilibili": {
        "name": "B站",
        "url": "https://member.bilibili.com/platform/upload-manager/article",
        "note": "稿件管理页含播放/点赞/评论",
    },
    "gzh": {
        "name": "微信公众号",
        "url": "https://mp.weixin.qq.com/",
        "note": "后台「内容与互动 → 已发表」含阅读/点赞/在看",
    },
    "zhihu": {
        "name": "知乎",
        "url": "https://www.zhihu.com/creator/manage/creation/all",
        "note": "创作中心内容管理页",
    },
    "toutiao": {
        "name": "今日头条",
        "url": "https://mp.toutiao.com/profile_v4/graphic/articles",
        "note": "内容管理页含展现/阅读/点赞",
    },
}


@dataclass
class MetricRow:
    """从看板里读出的一行数据。"""

    title: str
    views: int = 0
    likes: int = 0
    comments: int = 0
    favorites: int = 0
    shares: int = 0
    raw: list[str] = field(default_factory=list)

    def summary(self) -> str:
        bits = [f"浏览 {self.views}"]
        for k, v in (("赞", self.likes), ("评", self.comments),
                     ("藏", self.favorites), ("分享", self.shares)):
            if v:
                bits.append(f"{k} {v}")
        return " · ".join(bits)


def _num(s: str) -> Optional[int]:
    """把看板上的数字串解析成整数。

    要处理 OCR 的一个典型误识：**千分位逗号经常被读成句点** ——
    "12,400" → "12.400"。如果按小数解析就变成 12，静默污染数据。

    规则：
      · `12.400` / `1,204` / `1.234.567` → 末段恰 3 位数字 ⇒ 当千分位
      · `12.4` / `3.5` → 末段 1~2 位 ⇒ 当真正的小数
      · 带 `万/w/k/千` 单位的一律按倍数还原
    """
    s = (s or "").strip().replace(" ", "").replace("\u00a0", "")
    if not s:
        return None

    def apply_unit(v: float, unit: Optional[str]) -> int:
        if unit in ("万", "w", "W"):
            v *= 10000
        elif unit in ("k", "K", "千"):
            v *= 1000
        return int(round(v))

    # ① 纯千分位形式（可多段）：1,204 / 12.400 / 1.234.567
    m = re.fullmatch(r"(\d{1,3}(?:[.,]\d{3})+)(万|w|W|k|K|千)?", s)
    if m:
        return apply_unit(int(m.group(1).replace(",", "").replace(".", "")), m.group(2))

    # ② 普通整数或小数（可带单位）
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(万|w|W|k|K|千)?", s)
    if m:
        try:
            return apply_unit(float(m.group(1)), m.group(2))
        except ValueError:
            return None
    return None


def _which_column(text: str) -> Optional[str]:
    for col, names in COLUMN_ALIASES.items():
        for n in names:
            if n in text:
                return col
    return None


def parse_dashboard(items: Sequence[OcrItem], *,
                    row_tol: float = 0.018) -> list[MetricRow]:
    """从 OCR 结果里解析出「每篇内容 → 各指标」。

    策略：
    1. 先找**表头行**（含"浏览量/点赞/收藏"等），记下每列的 x 中心
    2. 之后的每行，把数字按 x 就近分配到对应列
    3. 行里最长的非数字文本当作标题

    比"假定列顺序"稳 —— 平台改版换列序也不会错位。
    """
    rows = group_rows(items, tol=row_tol)
    if not rows:
        return []

    # ① 找表头
    header_idx, col_x = -1, {}
    for i, r in enumerate(rows):
        found = {}
        for it in r:
            c = _which_column(it.text)
            if c and c not in found:
                found[c] = it.cx
        if len(found) >= 2:                    # 至少认出两列才算表头
            header_idx, col_x = i, found
            break

    out: list[MetricRow] = []
    if header_idx < 0:
        return out                          # 没表头就不瞎猜

    for r in rows[header_idx + 1:]:
        nums: list[tuple[float, int]] = []
        texts: list[str] = []
        for it in r:
            v = _num(it.text)
            if v is not None and len(it.text) <= 10:
                nums.append((it.cx, v))
            else:
                texts.append(it.text)
        if not nums:
            continue
        # 标题取最长的非数字文本
        title = max(texts, key=len).strip() if texts else ""
        if not title or len(title) < 2:
            continue
        mr = MetricRow(title=title, raw=[it.text for it in r])
        for cx, v in nums:
            # 就近分配到列
            best, bestd = None, 1e9
            for col, hx in col_x.items():
                d = abs(cx - hx)
                if d < bestd:
                    best, bestd = col, d
            if best and bestd < 0.25:          # 太远的丢弃，避免错配
                if getattr(mr, best, 0) == 0:
                    setattr(mr, best, v)
        if mr.views or mr.likes or mr.favorites or mr.comments:
            out.append(mr)
    return out


# ═══════════════════════════ 匹配与写回 ═══════════════════════════

def _norm(s: str) -> str:
    return re.sub(r"[\s，。！？、：；「」『』“”\"'()（）\[\]【】·\-—_.]", "", (s or "")).lower()


def match_project(row: MetricRow, projects: Sequence[co.ContentProject],
                  *, threshold: float = 0.55) -> Optional[co.ContentProject]:
    """把看板上的标题匹配到本地内容项目。

    用字符级 Jaccard 相似度 + 包含关系。平台展示的标题常被截断，
    所以"一个是另一个的前缀"要判为命中。
    """
    a = _norm(row.title)
    if not a:
        return None
    best, best_score = None, 0.0
    for p in projects:
        for cand in (p.title, p.angle):
            b = _norm(cand)
            if not b:
                continue
            if a == b or a in b or b in a:
                score = min(len(a), len(b)) / max(len(a), len(b))
                score = max(score, 0.9)        # 包含关系给高分
            else:
                sa, sb = set(a), set(b)
                score = len(sa & sb) / len(sa | sb) if (sa | sb) else 0.0
            if score > best_score:
                best, best_score = p, score
    return best if best_score >= threshold else None


def sync(rows: Sequence[MetricRow], ws: co.Workspace, *, platform: str = "",
         dry_run: bool = False) -> dict:
    """把读到的数据写回匹配的内容项目。"""
    projects = [p for p in ws.all()
                if not platform or p.platform == platform]
    matched, unmatched = [], []
    for r in rows:
        p = match_project(r, projects)
        if p is None:
            unmatched.append(r)
            continue
        entry = {"ts": time.strftime("%F %T"), "source": "auto-ocr",
                 "views": r.views, "likes": r.likes, "comments": r.comments,
                 "favorites": r.favorites, "shares": r.shares,
                 "raw_title": r.title}
        p.metrics.setdefault("records", []).append(entry)
        # 推进状态
        try:
            if p.stage_enum == co.Stage.PACKAGED:
                co.advance(p, co.Stage.PUBLISHED, "自动采集到数据")
            if p.stage_enum == co.Stage.PUBLISHED:
                co.advance(p, co.Stage.MEASURED, "自动采集")
        except Exception:
            pass
        if not dry_run:
            ws.save(p)
        matched.append((r, p))
    return {"matched": matched, "unmatched": unmatched,
            "projects": projects}


# ═══════════════════════════ CLI ═══════════════════════════

def _print_rows(rows: Sequence[MetricRow]) -> None:
    if not rows:
        print("  没解析出数据行。可能原因：")
        print("   · 截图里没有「浏览量/点赞/收藏」这类表头")
        print("   · 截图区域不对（要包含表头和数据行）")
        print("   · 页面还没加载完就截了图")
        return
    print(f"  {'标题':<34} {'浏览':>8} {'赞':>7} {'评':>6} {'藏':>7} {'分享':>6}")
    print("  " + "-" * 74)
    for r in rows:
        print(f"  {r.title[:32]:<34} {r.views:>8} {r.likes:>7} "
              f"{r.comments:>6} {r.favorites:>7} {r.shares:>6}")


def cmd_read(args) -> int:
    ocr = Ocr()
    items = ocr.read(args.image)
    print(f"OCR 识别 {len(items)} 段文字")
    rows = parse_dashboard(items)
    _print_rows(rows)
    if args.verbose:
        print("\n原始 OCR 文本：")
        for it in sorted(items, key=lambda i: (-i.cy, i.x))[:60]:
            print(f"  y={it.cy:.3f} x={it.cx:.3f}  「{it.text}」")
    return 0


def cmd_sync(args) -> int:
    ocr = Ocr()
    items = ocr.read(args.image)
    rows = parse_dashboard(items)
    print(f"OCR {len(items)} 段 → 解析出 {len(rows)} 行数据")
    if not rows:
        _print_rows(rows)
        return 1
    _print_rows(rows)
    ws = co.Workspace(args.ws)
    r = sync(rows, ws, platform=args.platform, dry_run=args.dry_run)
    print(f"\n匹配结果（本地共 {len(r['projects'])} 个项目候选）:")
    for row, p in r["matched"]:
        mark = "[dry-run]" if args.dry_run else "✅"
        print(f"  {mark} 「{row.title[:26]}」 → {p.id}  {p.title[:26]}")
        print(f"      {row.summary()}")
    if r["unmatched"]:
        print(f"\n未匹配 {len(r['unmatched'])} 条（本地没有对应项目，或标题差异太大）:")
        for row in r["unmatched"]:
            print(f"  · 「{row.title[:40]}」")
    if not args.dry_run and r["matched"]:
        print(f"\n✅ 已写回 {len(r['matched'])} 个项目。")
        print("   下一步：python3 topic_engine.py perf   # 看结构表现")
    return 0


def cmd_capture(args) -> int:
    """打开后台看板 → 截图 → 解析。用 browser_rpa 做导航。"""
    dash = PLATFORM_DASHBOARDS.get(args.platform)
    if not dash:
        print(f"未知平台 {args.platform}，可用: {sorted(PLATFORM_DASHBOARDS)}")
        return 2
    print(f"平台: {dash['name']}")
    print(f"看板: {dash['url']}")
    print(f"说明: {dash['note']}")
    out = args.out or f"dashboard_{args.platform}.png"
    print(f"\n将新开标签页打开上述地址，然后截图到 {out}")
    print("（首次需要你手动登录；登录后我会截图）\n")
    try:
        from browser_rpa import BrowserSession, load_or_init
    except ImportError:
        print("需要 browser_rpa.py 在同目录")
        return 2
    prof = load_or_init(args.profile_dir, args.platform, dash["url"])
    s = BrowserSession(profile_dir=args.profile_dir)
    s.goto(dash["url"])
    s.wait_login(prof, timeout=args.login_timeout)
    print("\n请把页面滚动到能看到「表头 + 数据行」的位置，然后按回车截图…")
    try:
        input()
    except EOFError:
        pass
    cap = s.shot(prof, name=f"dash_{args.platform}")
    import shutil
    shutil.copyfile(cap.path, out)
    print(f"已截图: {out}")
    print(f"\n下一步: python3 metrics_collector.py sync {out} --platform {args.platform}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="metrics_collector — 数据自动采集（只读）",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__)
    p.add_argument("--ws", default="workspace")
    p.add_argument("--profile-dir", default="browser_profiles")
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("read", help="从截图读取数据（只看不写）")
    r.add_argument("image")
    r.add_argument("-v", "--verbose", action="store_true", help="打印原始 OCR 文本")
    r.set_defaults(func=cmd_read)

    y = sub.add_parser("sync", help="读取并写回内容项目")
    y.add_argument("image")
    y.add_argument("--platform", default="")
    y.add_argument("--dry-run", action="store_true", help="只匹配不写回")
    y.set_defaults(func=cmd_sync)

    c = sub.add_parser("capture", help="打开后台截图（需人工登录）")
    c.add_argument("--platform", required=True, choices=sorted(PLATFORM_DASHBOARDS))
    c.add_argument("-o", "--out")
    c.add_argument("--login-timeout", type=float, default=300.0)
    c.set_defaults(func=cmd_capture)

    sub.add_parser("platforms", help="列出支持的平台看板").set_defaults(
        func=lambda a: (print("支持的后台看板：\n"), [
            print(f"  {k:<12} {v['name']:<10} {v['url']}\n               {v['note']}")
            for k, v in PLATFORM_DASHBOARDS.items()], 0)[-1])
    return p


def main(argv: Sequence[str]) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    fn = getattr(args, "func", None)
    if fn is None:
        p.print_help()
        return 2
    try:
        return fn(args)
    except Exception as e:
        print(f"\n❌ {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
