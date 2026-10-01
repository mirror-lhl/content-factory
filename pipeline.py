#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipeline — 内容生产流水线（编排层）

把六个引擎串成一条命令：

    选题 + brief
        ↓
    ┌───────────────────────────────────────────────┐
    │ ① 图文    content_ops 模板系统（4 套）          │
    │ ② 封面    cover_studio（5 风格 × N 平台）       │
    │ ③ 视频    video_studio + asset_library 自动配图 │
    │ ④ 清单    人可读的发布指引                      │
    └───────────────────────────────────────────────┘
        ↓
    workspace/<id>/output/

设计原则
--------
1. **一次出全平台多风格，让你挑** —— 不替你决定用哪张封面/哪个模板
2. **每个环节可单独重跑** —— `--only video` 只重做视频，改一个字不用全跑
3. **单环节失败不拖垮全局** —— 视频挂了，图文和封面照样产出，最后汇总报错
4. **产出一份人可读的发布清单** —— 明确告诉人：用哪张图、粘到哪、按钮谁点

用法
----
    # 先看计划，不执行
    python3 pipeline.py plan <项目id>

    # 全量生产
    python3 pipeline.py produce <项目id>

    # 只重做视频
    python3 pipeline.py produce <项目id> --only video

    # 指定封面风格与目标平台
    python3 pipeline.py produce <项目id> --styles classic,frame --platforms xiaohongshu,gzh
"""

from __future__ import annotations

import argparse
import html as _html
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from typing import Optional, Sequence

import content_ops as co
from asset_library import AssetLibrary, AssetError


# ═══════════════════════════ 配置 ═══════════════════════════

#: 目标平台 → 该平台用哪几种封面风格最合适
PLATFORM_STYLES = {
    "xiaohongshu": ["classic", "frame", "bold"],
    "gzh":         ["classic", "ink"],
    "zhihu":       ["classic", "ink"],
    "toutiao":     ["classic", "bold"],
    "douyin":      ["bold", "split"],
    "bilibili":    ["classic", "bold"],
}

#: 需要底图的封面风格（其余是纯文字排版）
STYLES_NEED_IMAGE = {"frame", "split"}

DEFAULT_PLATFORMS = ["xiaohongshu", "gzh", "zhihu", "bilibili"]


@dataclass
class Options:
    article: bool = True
    covers: bool = True
    video: bool = True
    manifest: bool = True
    styles: list[str] = field(default_factory=list)      # 空 = 按平台推荐
    platforms: list[str] = field(default_factory=list)   # 空 = DEFAULT_PLATFORMS
    template: str = ""
    voice: str = "Tingting"
    rate: int = 180
    subtitle_position: str = "bottom"
    subtitle_band: bool = True          # 古籍素材建议开
    video_template: str = ""            # 视频模板（见 video_studio.TEMPLATES）
    skip_qa: bool = False               # 跳过出厂质检（不建议）
    force: bool = False                 # 已有产物也重做


@dataclass
class Result:
    produced: dict[str, list[str]] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ═══════════════════════════ 工具 ═══════════════════════════

def _log(msg: str, indent: int = 1) -> None:
    print("  " * indent + msg, flush=True)


def _step(title: str):
    """计时上下文管理器。"""
    class _T:
        def __enter__(self):
            self.t0 = time.time()
            print(f"\n▶ {title}", flush=True)
            return self

        def __exit__(self, *a):
            print(f"  ⏱  {time.time() - self.t0:.1f}s", flush=True)
            return False
    return _T()


def adapt_title(title: str, platform: str) -> tuple[str, Optional[str]]:
    """把标题裁到平台上限。返回 (标题, 警告或 None)。

    优先在标点处截断，而不是硬切 —— 硬切出来的标题很别扭。
    """
    meta = co.PLATFORMS.get(platform, {})
    limit = meta.get("title_max", 100)
    title = (title or "").strip()
    if len(title) <= limit:
        return title, None
    cut = -1
    for i in range(min(limit, len(title)) - 1, max(limit - 12, 0), -1):
        if title[i] in "，。！？；：、,.!?;:）)":
            cut = i + 1
            break
    if cut < limit * 0.5:                 # 标点太靠前，退回硬切
        cut = limit
    final = title[:cut].rstrip("，,。.、")   # rstrip 会再删掉尾部标点，长度以实际为准
    return final, f"原标题 {len(title)} 字，已裁到 {len(final)} 字（上限 {limit}）"


def suggested_asset(proj, lib: Optional[AssetLibrary], *, n_used: int = 0):
    """为封面/背景挑一张古籍素材。按选题 + 钩子 + 要点一起匹配。"""
    if lib is None or not lib.assets:
        return None
    query = " ".join([proj.angle or "", proj.brief.hook or "",
                      " ".join(proj.brief.key_points[:3])]).strip()
    hits = lib.suggest(query, n=1) if query else []
    return lib.resolve(hits[0].id) if hits else None


# ═══════════════════════════ 各环节 ═══════════════════════════

def stage_article(proj: co.ContentProject, outdir: str, opts: Options,
                  res: Result) -> None:
    tpl = opts.template or co.PLATFORM_TEMPLATE.get(proj.platform, "classic")
    html = co.render_article_html(proj, tpl)
    ap = os.path.join(outdir, "article.html")
    with open(ap, "w", encoding="utf-8") as f:
        f.write(html)

    # 纯文本版（方便贴到不支持富文本的地方）
    plain = re.sub(r"<br\s*/?>", "\n", html)
    plain = re.sub(r"</(p|h1|h2|blockquote|div)>", "\n", plain)
    plain = _html.unescape(re.sub(r"<[^>]+>", "", plain))
    plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
    tp = os.path.join(outdir, "article.txt")
    with open(tp, "w", encoding="utf-8") as f:
        f.write(plain)

    chars = len(re.sub(r"<[^>]+>", "", html))
    proj.article = {"html_path": ap, "txt_path": tp, "chars": chars,
                    "template": tpl, "template_name": co.ARTICLE_TEMPLATES[tpl]["name"],
                    "rendered_at": time.strftime("%F %T")}
    _log(f"✅ 图文 article.html（{co.ARTICLE_TEMPLATES[tpl]['name']}，{chars} 字）")
    _log(f"   纯文本 article.txt")
    res.produced.setdefault("article", []).extend([ap, tp])


def stage_covers(proj: co.ContentProject, outdir: str, opts: Options,
                 res: Result, lib: Optional[AssetLibrary]) -> None:
    import cover_studio as cs

    platforms = opts.platforms or DEFAULT_PLATFORMS
    coverdir = os.path.join(outdir, "covers")
    os.makedirs(coverdir, exist_ok=True)

    img = suggested_asset(proj, lib)
    if img:
        _log(f"底图素材: {os.path.relpath(img, os.path.dirname(outdir))}")

    made: list[tuple[str, str, tuple[int, int]]] = []
    for pf in platforms:
        specs = cs.PLATFORM_COVERS.get(pf)
        if not specs:
            _log(f"⚠️ 跳过未知平台 {pf}")
            continue
        styles = opts.styles or PLATFORM_STYLES.get(pf, ["classic"])
        for style in styles:
            if style not in cs.STYLES:
                continue
            # 需要底图但没素材时，自动换成纯文字风格，不让它失败
            use_style = style
            if style in STYLES_NEED_IMAGE and not img:
                use_style = "classic"
            st = cs.STYLES[use_style]
            for label, size in specs.items():
                fn = f"{pf}_{label.replace(':', '-')}_{use_style}.png"
                path = os.path.join(coverdir, fn)
                small = min(size) < 400
                try:
                    cs.render_cover(
                        title=(proj.title or proj.angle).split("，")[0][:12] if small
                              else (proj.title or proj.angle),
                        subtitle="" if small else (proj.brief.hook or ""),
                        kicker=f"{proj.niche} · 原创" if not small else "",
                        image=img if use_style in STYLES_NEED_IMAGE else None,
                        style=use_style, size=size, out=path,
                        seal_text=(proj.niche[:2] or "国学"))
                    made.append((pf, use_style, size))
                    res.produced.setdefault("covers", []).append(path)
                except Exception as e:
                    res.errors.append((f"cover:{pf}/{use_style}", str(e)))

    by_style: dict[str, int] = {}
    for _pf, s, _sz in made:
        by_style[s] = by_style.get(s, 0) + 1
    _log(f"✅ 封面 {len(made)} 张 · {len(platforms)} 个平台 · "
         f"风格 {dict(by_style)}")
    proj.covers = {"dir": coverdir, "count": len(made),
                   "styles": sorted(by_style), "platforms": platforms,
                   "items": [os.path.basename(p) for p in res.produced.get("covers", [])],
                   "rendered_at": time.strftime("%F %T")}


def stage_video(proj: co.ContentProject, outdir: str, opts: Options,
                res: Result, lib: Optional[AssetLibrary]) -> None:
    from video_studio import (BuildSpec, Segment, SIZES, SubtitleStyle,
                              build_video, probe_duration)

    # 视频分句：先按句号切「经历」，再补上要点
    lines: list[str] = []
    personal = (proj.brief.personal or "").strip()
    if personal:
        for part in re.split(r"(?<=[。！？!?])", personal):
            if part.strip():
                lines.append(part.strip())
    lines += [k for k in proj.brief.key_points if k.strip()]
    if not lines:
        res.skipped.append("video（brief 里没有可用的分句文案）")
        _log("⏭ 跳过视频：brief 里没有可用文案")
        return

    # 配图优先级：① brief 里**显式指定**的素材 → ② 素材库语义匹配 → ③ 占位图
    #
    # 为什么显式优先：AI 实战类文案（讲向量检索、OCR）不含古籍术语，
    # 语义匹配找不到对应书，会静默回退到"最新导入"，配出无关的图。
    # 作者既然在 brief 里点了名，就按他点的用。
    explicit = [p for p in (proj.brief.assets or []) if os.path.exists(p)]
    if explicit:
        _log(f"配图用 brief 指定的 {len(explicit)} 个素材")
    segs = []
    for i, text in enumerate(lines):
        image = None
        if explicit:
            image = explicit[i % len(explicit)]
        elif lib is not None:
            hits = lib.suggest(text, n=1)
            if hits:
                image = lib.resolve(hits[0].id)
        segs.append(Segment(text=text, image=image, zoom="in" if i % 2 == 0 else "out"))

    style = SubtitleStyle(position=opts.subtitle_position, band=opts.subtitle_band)

    # **按目标平台需要的画幅分组渲染** —— 横屏平台和竖屏平台拿到的视频不一样。
    # 只渲染一次然后到处用，会导致 B站/知乎 收到竖版视频（真实产品缺陷）。
    platforms = opts.platforms or DEFAULT_PLATFORMS
    wanted: dict[str, list[str]] = {}
    for pf in platforms:
        sk = co.PLATFORMS.get(pf, {}).get("video_size", "vertical")
        wanted.setdefault(sk, []).append(pf)
    if not wanted:
        wanted = {"vertical": []}

    multi = len(wanted) > 1
    videos: dict[str, dict] = {}
    for size_key, pfs in wanted.items():
        size = SIZES.get(size_key, SIZES["vertical"])
        fn = f"video_{size_key}.mp4" if multi else "video.mp4"
        vp = os.path.join(outdir, fn)
        tpl_note = ""
        if opts.video_template:
            try:
                from video_studio import TEMPLATES as _VT
                tpl_note = f" · 模板「{_VT[opts.video_template].name}」"
            except Exception:
                pass
        _log(f"渲染 {len(segs)} 段 · {size[0]}×{size[1]} · "
             f"配音 {opts.voice}{tpl_note} → {fn}"
             + (f"（给 {','.join(co.PLATFORM_NAME_ZH.get(x, x) for x in pfs)}）" if pfs else ""))
        build_video(BuildSpec(segments=segs, out=vp, size=size, voice=opts.voice,
                              rate=opts.rate, subtitle=style,
                              template=opts.video_template,
                              intro_title=(proj.title or proj.angle or "")[:16]),
                    # 打开 verbose —— 逐段进度打到 stderr，界面才能看到"在动"。
                    # 之前写死 False，视频渲染这 60 秒日志完全静止，看着像卡死。
                    verbose=True)
        dur = probe_duration(vp)
        videos[size_key] = {"path": vp, "file": fn, "segments": len(segs),
                            "size": f"{size[0]}x{size[1]}", "duration": round(dur, 1),
                            "platforms": pfs, "rendered_at": time.strftime("%F %T")}
        res.produced.setdefault("video", []).append(vp)
        _log(f"✅ {fn}（{len(segs)} 段 · {dur:.1f}s · {size[0]}×{size[1]}）")

    proj.video = {"variants": videos, "primary": next(iter(videos)),
                  "rendered_at": time.strftime("%F %T")}
    # 兼容旧字段（单画幅时）
    first = next(iter(videos.values()))
    proj.video.update({"path": first["path"], "segments": first["segments"],
                       "size": first["size"], "duration": first["duration"]})


def stage_manifest(proj: co.ContentProject, outdir: str, opts: Options,
                   res: Result) -> None:
    """生成**人可读的发布清单** —— 明确告诉人该做什么。"""
    platforms = opts.platforms or DEFAULT_PLATFORMS
    covers = res.produced.get("covers", [])
    L: list[str] = []
    L.append(f"# 发布清单 · {proj.title or proj.angle}\n")
    L.append(f"> 项目 `{proj.id}` · 领域 {proj.niche} · 生成于 {time.strftime('%F %T')}\n")
    L.append("**发布按钮由你点。** 本工具只准备内容，不触碰任何平台。\n")
    L.append("---\n")

    for pf in platforms:
        meta = co.PLATFORMS.get(pf, {})
        name = meta.get("name", pf)
        L.append(f"## {name}\n")
        adapted, warn = adapt_title(proj.title or proj.angle, pf)
        L.append(f"- **标题**（{len(adapted)}/{meta.get('title_max','?')} 字）："
                 f"`{adapted}`")
        if warn:
            L.append(f"  - ⚠️ {warn}")
        L.append(f"- **正文**：打开 `article.html` 全选复制，或直接粘贴"
                 f"（`article.txt` 是纯文本版）")
        pfc = [os.path.basename(c) for c in covers if os.path.basename(c).startswith(pf + "_")]
        if pfc:
            L.append(f"- **封面**（挑一张）：")
            for c in pfc:
                L.append(f"  - `covers/{c}`")
        vinfo = None
        for v in (proj.video.get("variants") or {}).values():
            if pf in (v.get("platforms") or []):
                vinfo = v
                break
        if vinfo is None and proj.video.get("path"):
            vinfo = {"file": os.path.basename(proj.video["path"]),
                     "size": proj.video.get("size"), "duration": proj.video.get("duration")}
        if vinfo:
            L.append(f"- **视频**：`{vinfo['file']}`（{vinfo.get('size')} · "
                     f"{vinfo.get('duration')}s）")
        L.append(f"- **操作**：粘贴正文 → 上传封面/视频 → 填标题 → "
                 f"**你点发布**")
        L.append("")

    L.append("---\n")
    L.append("## 全平台通用提醒\n")
    L.append("- 内容由**你本人创作**，工具只负责排版与素材（平台要求真实创作者身份）")
    L.append("- 发布节奏放慢，不要批量连续发（各平台明文限制）")
    L.append("- 发布后跑一次数据回填，供下一轮选题参考：")
    L.append(f"  ```\npython3 content_ops.py metrics {proj.id} --views N --likes N\n  ```")

    mp = os.path.join(outdir, "发布清单.md")
    with open(mp, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    _log(f"✅ 发布清单 发布清单.md")
    res.produced.setdefault("manifest", []).append(mp)

    # 机器可读版
    bundle = {
        "id": proj.id, "title": proj.title or proj.angle, "niche": proj.niche,
        "platforms": [
            {"key": pf, "name": co.PLATFORMS.get(pf, {}).get("name", pf),
             "title": adapt_title(proj.title or proj.angle, pf)[0],
             "title_max": co.PLATFORMS.get(pf, {}).get("title_max"),
             "covers": [os.path.basename(c) for c in covers
                        if os.path.basename(c).startswith(pf + "_")]}
            for pf in platforms
        ],
        "article": {"html": "article.html", "txt": "article.txt"},
        "video": proj.video,
        "video_variants": {k: v.get("file") for k, v in
                           (proj.video.get("variants") or {}).items()},
        "covers": [os.path.basename(c) for c in covers],
        "generated_at": time.strftime("%F %T"),
        "note": "发布由人工完成，本工具不触碰平台",
    }
    bp = os.path.join(outdir, "bundle.json")
    with open(bp, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, indent=2)
    res.produced.setdefault("manifest", []).append(bp)


# ═══════════════════════════ 编排 ═══════════════════════════

def produce(proj: co.ContentProject, ws: co.Workspace, opts: Options) -> Result:
    outdir = os.path.join(ws.dir_of(proj.id), "output")
    os.makedirs(outdir, exist_ok=True)
    res = Result()

    if not proj.angle:
        raise co.OpsError("还没选题。先跑：content_ops.py topic <id> --set '你的角度'")

    # ── SOP 出厂质检：命中硬红线直接拦下 ──
    if not opts.skip_qa:
        try:
            import sop as _sop
            rep = _sop.check_project(proj, ws=ws)
            if rep.red:
                print(f"\n{'=' * 58}")
                print(f"  🔴 质检未通过：{len(rep.red)} 处硬红线")
                for i in rep.red[:8]:
                    print(f"     · [{i.where}] 命中「{i.word}」")
                print(f"\n  这些词会直接触发平台审核，生产出来也发不出去。")
                print(f"  改完再跑，或用 --skip-qa 强制跳过（不建议）")
                print(f"{'=' * 58}\n")
                raise co.OpsError("质检未通过，已中止生产")
            if rep.amber:
                print(f"  🟡 质检提示：{len(rep.amber)} 处易触发审核（不拦截，建议改）")
                for i in rep.amber[:4]:
                    print(f"     · [{i.where}] 「{i.word}」→ {i.advice}")
            else:
                print("  ✅ 质检通过（无触发词）")
        except ImportError:
            pass
    if not (proj.brief.key_points or proj.brief.personal):
        raise co.OpsError(
            "brief 是空的 —— 工具不会替你编内容。\n"
            "先跑：content_ops.py brief <id> --file brief.md")

    lib = None
    try:
        lib = AssetLibrary("assets")
        if not lib.assets:
            _log("（素材库为空，封面将用纯文字风格、视频用占位图）")
    except Exception as e:
        _log(f"⚠️ 素材库不可用（{e}），继续但不自动配图")

    print(f"\n{'=' * 58}")
    print(f"  内容生产 · {proj.title or proj.angle}")
    print(f"  平台: {', '.join(opts.platforms or DEFAULT_PLATFORMS)}")
    print(f"{'=' * 58}")

    if opts.article:
        with _step("① 图文"):
            try:
                stage_article(proj, outdir, opts, res)
            except Exception as e:
                res.errors.append(("article", str(e)))
                _log(f"❌ 图文失败: {e}")
    else:
        res.skipped.append("article")

    if opts.covers:
        with _step("② 封面"):
            try:
                stage_covers(proj, outdir, opts, res, lib)
            except Exception as e:
                res.errors.append(("covers", str(e)))
                _log(f"❌ 封面失败: {e}")
    else:
        res.skipped.append("covers")

    if opts.video:
        with _step("③ 视频"):
            try:
                stage_video(proj, outdir, opts, res, lib)
            except Exception as e:
                res.errors.append(("video", str(e)))
                _log(f"❌ 视频失败: {e}")
    else:
        res.skipped.append("video")

    if opts.manifest:
        with _step("④ 发布清单"):
            try:
                stage_manifest(proj, outdir, opts, res)
            except Exception as e:
                res.errors.append(("manifest", str(e)))
                _log(f"❌ 清单失败: {e}")
    else:
        res.skipped.append("manifest")

    # 推进状态
    if res.produced and proj.stage_enum == co.Stage.TOPIC_SELECTED:
        co.advance(proj, co.Stage.CREATED, "流水线生产")
    if proj.stage_enum == co.Stage.CREATED:
        co.advance(proj, co.Stage.PACKAGED, "流水线打包")
    ws.save(proj)

    # 汇总
    print(f"\n{'=' * 58}")
    total = sum(len(v) for v in res.produced.values())
    print(f"  产出 {total} 个文件 → {os.path.relpath(outdir)}")
    for k in ("article", "covers", "video", "manifest"):
        if k in res.produced:
            print(f"    {k:<9} {len(res.produced[k])} 个")
    if res.skipped:
        print(f"    ⏭ 跳过: {', '.join(res.skipped)}")
    if res.errors:
        print(f"\n  ❌ {len(res.errors)} 个环节失败（其余产物仍可用）:")
        for k, v in res.errors:
            print(f"    {k}: {v[:110]}")
    print(f"{'=' * 58}\n")

    # SOP 第⑤步「出成品」自动通关。
    # 放在这里（内核）而不是 Web 服务层 —— 否则命令行生产完 SOP 不推进，
    # 只有网页路径能推进，两条路径行为就不一致了。
    try:
        import sop as _sop
        if res.ok and _sop.next_key(proj) == "produce":
            _ok, _issues = _sop.submit_step(proj, "produce", {})
            ws.save(proj)
            print("  ✅ SOP 第⑤步自动通过 → 下一步：⑥ 出厂质检\n" if _ok
                  else f"  第⑤步未通过：{_issues}")
    except Exception as _e:
        print(f"  (SOP 自动推进跳过: {_e})")
    return res


def cmd_plan(ws, args) -> int:
    proj = ws.load(args.id)
    platforms = args.platforms or DEFAULT_PLATFORMS
    print(f"\n项目 {proj.id} · {proj.niche}")
    print(f"选题: {proj.angle or '（未定）'}\n")
    print(f"{'平台':<12} {'标题上限':<9} {'裁后标题':<34} 封面风格")
    print("-" * 92)
    for pf in platforms:
        adapted, _w = adapt_title(proj.title or proj.angle, pf)
        meta = co.PLATFORMS.get(pf, {})
        styles = args.styles or PLATFORM_STYLES.get(pf, ["classic"])
        n_cov = len(styles) * len(_cover_specs(pf))
        print(f"{meta.get('name', pf):<12} {meta.get('title_max','?'):<9} "
              f"{adapted[:32]:<34} {','.join(styles)} ({n_cov} 张)")
    print(f"\n产出目录: {os.path.join(ws.dir_of(proj.id), 'output')}/")
    print("  ① article.html / article.txt   （按平台模板）")
    print("  ② covers/                       （多风格，你挑）")
    vt = getattr(args, "video_template", "") or ""
    tn = ""
    if vt:
        try:
            from video_studio import TEMPLATES as _VT
            tn = f" · 模板「{_VT[vt].name}」"
        except Exception:
            pass
    print(f"  ③ video_*.mp4                   （自动配图 + 配音字幕{tn}）")
    print("  ④ 发布清单.md / bundle.json      （人可读 + 机器可读）")
    print("\n去掉 --plan 即执行。")
    return 0


def _cover_specs(pf: str) -> dict:
    import cover_studio as cs
    return cs.PLATFORM_COVERS.get(pf, {})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="pipeline — 内容生产流水线",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__)
    p.add_argument("--ws", default="workspace")
    sub = p.add_subparsers(dest="cmd")

    def add_opts(sp):
        sp.add_argument("id")
        sp.add_argument("--only", action="append", choices=["article", "covers", "video", "manifest"],
                        help="只做指定环节，可重复")
        sp.add_argument("--styles", help="封面风格，逗号分隔，如 classic,frame")
        sp.add_argument("--platforms", help="目标平台，逗号分隔")
        sp.add_argument("--template", default="", help="图文模板")
        sp.add_argument("--voice", default="Tingting")
        sp.add_argument("--rate", type=int, default=180)
        sp.add_argument("--subtitle-position", default="bottom",
                        choices=["bottom", "center", "top"])
        sp.add_argument("--no-band", action="store_true", help="关闭通栏底带")
        try:
            from video_studio import TEMPLATES as _VT
            sp.add_argument("--video-template", default="", choices=[""] + sorted(_VT),
                            help="视频模板（留空=不加片头片尾的素片）")
        except Exception:
            sp.add_argument("--video-template", default="")
        sp.add_argument("--skip-qa", action="store_true",
                        help="跳过出厂质检（命中硬红线时强行生产）")
        return sp

    add_opts(sub.add_parser("plan", help="预览计划，不执行")).set_defaults(func=cmd_plan)

    def _run(ws, args) -> int:
        proj = ws.load(args.id)
        only = set(args.only) if args.only else None
        opts = Options(
            article=(only is None or "article" in only),
            covers=(only is None or "covers" in only),
            video=(only is None or "video" in only),
            manifest=(only is None or "manifest" in only),
            styles=[s.strip() for s in args.styles.split(",")] if args.styles else [],
            platforms=[s.strip() for s in args.platforms.split(",")] if args.platforms else [],
            template=args.template, voice=args.voice, rate=args.rate,
            subtitle_position=args.subtitle_position,
            subtitle_band=not args.no_band,
            video_template=getattr(args, "video_template", "") or "",
            skip_qa=getattr(args, "skip_qa", False),
        )
        return 0 if produce(proj, ws, opts).ok else 1

    add_opts(sub.add_parser("produce", help="执行生产")).set_defaults(func=_run)
    return p


def main(argv: Sequence[str]) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    fn = getattr(args, "func", None)
    if fn is None:
        p.print_help()
        return 2
    try:
        return fn(co.Workspace(args.ws), args)
    except co.OpsError as e:
        print(f"\n❌ {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
