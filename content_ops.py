#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
content_ops — 自媒体运营助手 · 产品主流程骨架

把「账号数据 → 选题 → 创作 → 成品分发 → 数据回流」串成一条有状态的流水线。
每个阶段可插拔，产物落盘，状态可追溯。

产品定位（重要）
----------------
本骨架的发布阶段**默认只产出可人工分发的成品**：
    · 富文本写进剪贴板 —— 你粘贴到公众号/知乎/头条编辑器
    · 图片/视频/文案导出到目录 —— 你手动上传
这是 秀米 / 135编辑器 / 壹伴 等成熟商业工具验证过的合规范式：
**自动化边界停在"准备内容"，从不碰平台。**

浏览器填草稿属于可选实验模块（`--with-rpa`），默认关闭，
因为浏览器自动化仍属平台协议禁止的"第三方工具接入"。

状态机
------
    draft → topic_selected → created → packaged → published → measured
      ↑__________|______________|__________|___________|
                    （都可回退重做）

用法
----
    python3 content_ops.py new --niche "AI×国学" --platform xiaohongshu
    python3 content_ops.py list
    python3 content_ops.py topic <id> --set "AI 给古籍标点错了 30%"
    python3 content_ops.py brief <id> --file brief.md
    python3 content_ops.py make  <id> --type article
    python3 content_ops.py make  <id> --type video
    python3 content_ops.py package <id> [--clipboard]
    python3 content_ops.py metrics <id> --views 12000 --likes 340
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
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional, Sequence


# ═══════════════════════════ 状态机 ═══════════════════════════

class Stage(str, Enum):
    DRAFT = "draft"                    # 刚建立，还没选角度
    TOPIC_SELECTED = "topic_selected"  # 角度已定 + 已填 brief
    CREATED = "created"                # 图文/视频成品已生成
    PACKAGED = "packaged"              # 已打包成可分发成品
    PUBLISHED = "published"            # 人工发布后回填
    MEASURED = "measured"              # 数据已回流


#: 允许的状态流转（含回退重做）
TRANSITIONS: dict[Stage, set[Stage]] = {
    Stage.DRAFT:          {Stage.TOPIC_SELECTED},
    Stage.TOPIC_SELECTED: {Stage.CREATED, Stage.DRAFT},
    Stage.CREATED:        {Stage.PACKAGED, Stage.TOPIC_SELECTED},
    Stage.PACKAGED:       {Stage.PUBLISHED, Stage.CREATED},
    Stage.PUBLISHED:      {Stage.MEASURED, Stage.PACKAGED},
    Stage.MEASURED:       {Stage.PUBLISHED},
}

STAGE_LABEL = {
    Stage.DRAFT: "① 待选题",
    Stage.TOPIC_SELECTED: "② 已选题",
    Stage.CREATED: "③ 已创作",
    Stage.PACKAGED: "④ 待分发",
    Stage.PUBLISHED: "⑤ 已发布",
    Stage.MEASURED: "⑥ 有数据",
}


class OpsError(RuntimeError):
    pass


def advance(proj: "ContentProject", to: Stage, note: str = "") -> None:
    """推进状态，非法流转直接报错（防止流程走乱）。"""
    cur = Stage(proj.stage)
    if to == cur:
        return
    if to not in TRANSITIONS[cur]:
        raise OpsError(
            f"非法状态流转：{cur.value} → {to.value}\n"
            f"  当前阶段允许流向: {sorted(s.value for s in TRANSITIONS[cur])}")
    proj.stage = to.value
    proj.history.append({
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "from": cur.value, "to": to.value, "note": note,
    })


# ═══════════════════════════ 数据模型 ═══════════════════════════

#: 平台规格（画幅、字数、标题长度等）
PLATFORM_NAME_ZH = {"gzh": "微信公众号", "zhihu": "知乎", "toutiao": "今日头条",
                    "xiaohongshu": "小红书", "douyin": "抖音", "bilibili": "B站"}

PLATFORMS: dict[str, dict[str, Any]] = {
    "gzh":        {"name": "微信公众号", "kind": "article", "video_size": "horizontal",
                   "title_max": 64, "body_max": 20000},
    "zhihu":      {"name": "知乎",       "kind": "article", "video_size": "horizontal",
                   "title_max": 100, "body_max": 40000},
    "toutiao":    {"name": "今日头条",   "kind": "article", "video_size": "horizontal",
                   "title_max": 30, "body_max": 50000},
    "xiaohongshu":{"name": "小红书",     "kind": "article", "video_size": "vertical",
                   "title_max": 20, "body_max": 1000},
    "douyin":     {"name": "抖音",       "kind": "video",   "video_size": "vertical",
                   "title_max": 30, "body_max": 1000},
    "bilibili":   {"name": "B站",        "kind": "video",   "video_size": "horizontal",
                   "title_max": 80, "body_max": 250},
}


@dataclass
class Brief:
    """**由人提供的**内容要点 —— 这是内容的灵魂，不由工具生成。"""

    hook: str = ""                     # 一句话钩子
    key_points: list[str] = field(default_factory=list)   # 核心要点
    personal: str = ""                 # 个人经历 / 实测数据 / 具体案例
    assets: list[str] = field(default_factory=list)       # 素材文件路径
    notes: str = ""


@dataclass
class ContentProject:
    """一条内容的全生命周期。"""

    id: str
    niche: str
    platform: str
    title: str = ""
    angle: str = ""                    # 选定的选题角度
    pattern: str = ""                  # 选题结构（反差/溯源/误读/实测/翻车/方法）
    stage: str = Stage.DRAFT.value
    brief: Brief = field(default_factory=Brief)
    article: dict = field(default_factory=dict)   # 图文成品信息
    video: dict = field(default_factory=dict)     # 视频成品信息
    covers: dict = field(default_factory=dict)    # 封面成品信息
    #: SOP 创作流程的每步状态：{step_key: {done, data, checked_at, issues}}
    #: 这是「流程驱动」的载体 —— 选题建项目时就初始化，之后逐步推进
    sop_steps: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)   # 数据表现
    published_url: str = ""
    history: list[dict] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))

    # ---- 便利属性 ----
    @property
    def platform_meta(self) -> dict:
        return PLATFORMS.get(self.platform, {"name": self.platform, "kind": "article",
                                             "video_size": "vertical",
                                             "title_max": 100, "body_max": 10000})

    @property
    def stage_enum(self) -> Stage:
        return Stage(self.stage)

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @staticmethod
    def from_dict(d: dict) -> "ContentProject":
        d = dict(d)
        d["brief"] = Brief(**d.get("brief", {}))
        return ContentProject(**d)


# ═══════════════════════════ 工作区 ═══════════════════════════

class Workspace:
    """管理一组 ContentProject，落盘为 JSON。"""

    def __init__(self, root: str = "workspace"):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def dir_of(self, pid: str) -> str:
        return os.path.join(self.root, pid)

    def path_of(self, pid: str) -> str:
        return os.path.join(self.dir_of(pid), "project.json")

    def new(self, niche: str, platform: str) -> ContentProject:
        pid = time.strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:6]
        proj = ContentProject(id=pid, niche=niche, platform=platform)
        proj.history.append({"ts": proj.created_at, "from": "", "to": proj.stage,
                             "note": f"新建项目 · {niche} · {platform}"})
        self.save(proj)
        for sub in ("assets", "output"):
            os.makedirs(os.path.join(self.dir_of(pid), sub), exist_ok=True)
        return proj

    def save(self, proj: ContentProject) -> None:
        os.makedirs(self.dir_of(proj.id), exist_ok=True)
        with open(self.path_of(proj.id), "w", encoding="utf-8") as f:
            json.dump(proj.to_dict(), f, ensure_ascii=False, indent=2)

    def load(self, pid: str) -> ContentProject:
        p = self.path_of(pid)
        if not os.path.exists(p):
            # 支持用 id 前缀匹配
            cands = [d for d in os.listdir(self.root)
                     if d.startswith(pid) and os.path.isdir(os.path.join(self.root, d))]
            if len(cands) == 1:
                p = self.path_of(cands[0])
            elif len(cands) > 1:
                raise OpsError(f"id 前缀 {pid!r} 匹配到多个: {cands}")
            else:
                raise OpsError(f"找不到项目 {pid!r}")
        with open(p, encoding="utf-8") as f:
            return ContentProject.from_dict(json.load(f))

    def all(self) -> list[ContentProject]:
        out = []
        for d in sorted(os.listdir(self.root)):
            if d.startswith("."):          # 回收站等隐藏目录不算项目
                continue
            p = self.path_of(d)
            if os.path.exists(p):
                try:
                    out.append(self.load(d))
                except Exception:
                    pass
        return out

    # ───────── 删除 / 回收站 ─────────

    @property
    def trash_root(self) -> str:
        return os.path.join(self.root, ".trash")

    def folder_stats(self, pid: str) -> dict:
        """统计一个项目目录里有什么 —— 删之前给用户看清楚。"""
        d = self.dir_of(pid)
        files, size = 0, 0
        for r, _ds, fs in os.walk(d):
            for f in fs:
                fp = os.path.join(r, f)
                files += 1
                try:
                    size += os.path.getsize(fp)
                except OSError:
                    pass
        return {"files": files, "bytes": size, "path": d}

    def delete(self, pid: str, *, hard: bool = False) -> dict:
        """删除一个创作项目（含图文、封面、视频等全部产出）。

        默认是**移入回收站**而不是真删 —— 误点一次不至于丢掉整个作品。
        hard=True 才真正抹掉。
        """
        # 安全检查：必须严格是 workspace/<pid>，不能是别的路径
        d = os.path.abspath(self.dir_of(pid))
        root = os.path.abspath(self.root)
        if os.path.dirname(d) != root:
            raise OpsError(f"拒绝删除：路径不在工作区内（{d}）")
        if not os.path.isdir(d):
            raise OpsError(f"项目不存在：{pid}")
        st = self.folder_stats(pid)
        if hard:
            shutil.rmtree(d)
            return {**st, "mode": "hard"}
        os.makedirs(self.trash_root, exist_ok=True)
        dest = os.path.join(self.trash_root, f"{pid}__{time.strftime('%Y%m%d%H%M%S')}")
        shutil.move(d, dest)
        return {**st, "mode": "trash", "dest": dest}

    def trash(self) -> list[dict]:
        if not os.path.isdir(self.trash_root):
            return []
        out = []
        for name in sorted(os.listdir(self.trash_root), reverse=True):
            d = os.path.join(self.trash_root, name)
            if not os.path.isdir(d):
                continue
            files, size = 0, 0
            for r, _ds, fs in os.walk(d):
                for f in fs:
                    files += 1
                    try:
                        size += os.path.getsize(os.path.join(r, f))
                    except OSError:
                        pass
            out.append({"name": name, "pid": name.split("__")[0],
                        "files": files, "bytes": size,
                        "deleted_at": time.strftime(
                            "%F %T", time.localtime(os.path.getmtime(d)))})
        return out

    def restore(self, name: str) -> str:
        """从回收站恢复。"""
        src = os.path.join(self.trash_root, name)
        if not os.path.isdir(src):
            raise OpsError(f"回收站里没有：{name}")
        pid = name.split("__")[0]
        dest = self.dir_of(pid)
        if os.path.exists(dest):
            dest = f"{dest}_restored_{time.strftime('%H%M%S')}"
        shutil.move(src, dest)
        return dest

    def empty_trash(self) -> dict:
        n, size = 0, 0
        for t in self.trash():
            n += 1
            size += t["bytes"]
        if os.path.isdir(self.trash_root):
            shutil.rmtree(self.trash_root)
        return {"count": n, "bytes": size}


# ═══════════════════════════ 阶段一：选题 ═══════════════════════════

def cmd_topic(ws: Workspace, args) -> int:
    """选定选题角度并写入。候选角度由调研或人工提供。"""
    pid = args.id
    proj = ws.load(pid)
    if args.set:
        proj.angle = args.set
        if not proj.title:
            proj.title = args.set
        if proj.stage_enum == Stage.DRAFT:
            advance(proj, Stage.TOPIC_SELECTED, f"选题: {args.set[:30]}")
        ws.save(proj)
        print(f"✅ 选题已定: {proj.angle}")
        print(f"   阶段: {STAGE_LABEL[proj.stage_enum]}")
        print("\n下一步：补充 brief（内容要点必须由你提供）")
        print(f"   python3 content_ops.py brief {proj.id} --file brief.md")
        return 0

    print(f"项目 {proj.id} · {proj.niche} · {proj.platform_meta['name']}")
    print(f"当前选题: {proj.angle or '（未定）'}")
    print("\n用 --set 写入选定的角度。")
    return 0


def cmd_brief(ws: Workspace, args) -> int:
    """写入 brief —— **这一步是内容能否立住的关键，必须由人提供**。"""
    pid = args.id
    proj = ws.load(pid)
    text = ""
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
    elif args.text:
        text = args.text
    else:
        print("需要 --file 或 --text 提供内容要点")
        return 2

    b = Brief(**proj.brief.__dict__)
    # 支持简单的分节标记
    for m in re.finditer(r"^##\s*(钩子|要点|经历|素材|备注)\s*$", text, re.M):
        pass
    sections = re.split(r"^##\s*(钩子|要点|经历|素材|备注)\s*$", text, flags=re.M)
    if len(sections) > 1:
        for i in range(1, len(sections), 2):
            key, body = sections[i], sections[i + 1].strip()
            if key == "钩子":
                b.hook = body
            elif key == "要点":
                b.key_points = [l.strip("-• \t") for l in body.splitlines() if l.strip()]
            elif key == "经历":
                b.personal = body
            elif key == "素材":
                b.assets = [l.strip() for l in body.splitlines() if l.strip()]
            elif key == "备注":
                b.notes = body
    else:
        b.personal = text.strip()
    proj.brief = b
    if proj.stage_enum == Stage.DRAFT and proj.angle:
        advance(proj, Stage.TOPIC_SELECTED, "已补 brief")
    ws.save(proj)

    print("✅ brief 已写入")
    print(f"   钩子   : {b.hook[:50] or '（空）'}")
    print(f"   要点   : {len(b.key_points)} 条")
    print(f"   个人经历: {'有' if b.personal else '（空）⚠️'} {len(b.personal)} 字")
    print(f"   素材   : {len(b.assets)} 个")
    if not b.personal and not b.key_points:
        print("\n⚠️ 没有任何个人内容 —— 这样产出的是水文，平台会降权。")
        print("   请补充你的真实经历、实测数据或具体案例。")
    return 0


# ═══════════════════════════ 阶段二：创作 ═══════════════════════════

#: 图文排版模板 —— 全部内联样式（平台编辑器不支持外部 CSS，秀米同款做法）
#: 每个模板给出 h1 / h2 / p / quote / kicker 的样式，以及是否加分隔线与编号。
ARTICLE_TEMPLATES: dict[str, dict] = {
    "classic": {
        "name": "典雅",
        "desc": "宋体感、朱红左边框小标题，适合公众号 / 知乎长文",
        "font": "'Songti SC','PingFang SC',-apple-system,sans-serif",
        "h1": "font-size:22px;font-weight:700;line-height:1.5;margin:0 0 18px;color:#1a1a1a",
        "h2": ("font-size:18px;font-weight:700;line-height:1.6;margin:30px 0 14px;"
               "color:#1a1a1a;border-left:4px solid #c0392b;padding-left:10px"),
        "p": ("font-size:16px;line-height:1.9;margin:0 0 18px;color:#333;"
              "letter-spacing:.4px;text-align:justify"),
        "quote": ("font-size:15px;line-height:1.85;margin:0 0 20px;padding:14px 16px;"
                  "color:#555;background:#f7f5f0;border-radius:6px;"
                  "border-left:3px solid #c0392b"),
        "kicker": "font-size:13px;color:#c0392b;letter-spacing:2px;margin:0 0 10px",
        "numbered": True, "divider": False,
    },
    "minimal": {
        "name": "简洁",
        "desc": "现代无衬线、克制留白，适合知乎 / 头条",
        "font": "-apple-system,'PingFang SC','Hiragino Sans GB',sans-serif",
        "h1": "font-size:21px;font-weight:600;line-height:1.5;margin:0 0 20px;color:#111",
        "h2": "font-size:17px;font-weight:600;line-height:1.6;margin:28px 0 12px;color:#111",
        "p": "font-size:16px;line-height:1.85;margin:0 0 16px;color:#333",
        "quote": ("font-size:15px;line-height:1.8;margin:0 0 18px;padding:12px 14px;"
                  "color:#666;background:#f8f8f8;border-radius:4px"),
        "kicker": "font-size:13px;color:#999;margin:0 0 10px",
        "numbered": False, "divider": True,
    },
    "card": {
        "name": "卡片",
        "desc": "短段落 + 圆角卡片，适合小红书 / 视频号文案",
        "font": "-apple-system,'PingFang SC',sans-serif",
        "h1": "font-size:19px;font-weight:700;line-height:1.5;margin:0 0 16px;color:#1a1a1a",
        "h2": ("font-size:16px;font-weight:700;line-height:1.6;margin:0 0 10px;"
               "color:#1a1a1a"),
        "p": "font-size:15px;line-height:1.8;margin:0 0 14px;color:#333",
        "quote": ("font-size:15px;line-height:1.75;margin:0 0 16px;padding:14px 16px;"
                  "color:#444;background:#fff5f3;border-radius:12px"),
        "kicker": "font-size:13px;color:#ff6b5b;margin:0 0 10px",
        "numbered": False, "divider": False,
        "section_box": ("padding:16px 18px;margin:0 0 16px;background:#fafafa;"
                        "border-radius:12px"),
    },
    "dense": {
        "name": "密排",
        "desc": "信息密度高、小字号紧凑，适合技术长文",
        "font": "-apple-system,'PingFang SC',sans-serif",
        "h1": "font-size:20px;font-weight:700;line-height:1.45;margin:0 0 14px;color:#111",
        "h2": "font-size:16px;font-weight:700;line-height:1.5;margin:22px 0 10px;color:#111",
        "p": "font-size:15px;line-height:1.75;margin:0 0 14px;color:#333",
        "quote": ("font-size:14px;line-height:1.7;margin:0 0 14px;padding:10px 12px;"
                  "color:#555;background:#f4f5f7;border-radius:4px"),
        "kicker": "font-size:12px;color:#888;margin:0 0 8px",
        "numbered": True, "divider": True,
    },
}

#: 平台 → 默认模板
PLATFORM_TEMPLATE = {
    "gzh": "classic", "zhihu": "minimal", "toutiao": "dense",
    "xiaohongshu": "card", "douyin": "card", "bilibili": "minimal",
}


def render_article_html(proj: ContentProject, template: str = "") -> str:
    """把 brief 渲染成**可直接粘贴**的富文本 HTML。

    分工：结构、排版、措辞由这里处理；
    **观点、经历、数据来自 proj.brief**，不由工具编造。
    """
    tpl_key = template or PLATFORM_TEMPLATE.get(proj.platform, "classic")
    tpl = ARTICLE_TEMPLATES.get(tpl_key)
    if tpl is None:
        raise OpsError(f"未知模板 {tpl_key!r}，可用: {sorted(ARTICLE_TEMPLATES)}")
    b = proj.brief
    meta = proj.platform_meta
    # 开头声明编码：这份 HTML 可能被**直接双击打开**（file://），
    # 那时没有 HTTP 头，浏览器会按系统默认编码猜，中文整篇乱码。
    parts: list[str] = ['<meta charset="utf-8">',
                        '<div style="max-width:680px;margin:0 auto;'
                        'font-family:' + tpl["font"] + '">']

    title = proj.title or proj.angle
    if len(title) > meta["title_max"]:
        print(f"⚠️ 标题 {len(title)} 字超过 {meta['name']} 上限 {meta['title_max']} 字",
              file=sys.stderr)
    if b.hook:
        parts.append(f'<p style="{tpl["kicker"]}">{_html.escape(proj.niche or "原创")}</p>')
    parts.append(f'<h1 style="{tpl["h1"]}">{_html.escape(title)}</h1>')
    if b.hook:
        parts.append(f'<blockquote style="{tpl["quote"]}">{_html.escape(b.hook)}</blockquote>')
    if tpl.get("divider"):
        parts.append('<hr style="border:none;border-top:1px solid #e8e8e8;margin:22px 0">')

    if b.personal:
        for para in b.personal.split("\n\n"):
            if para.strip():
                parts.append(f'<p style="{tpl["p"]}">{_html.escape(para.strip())}</p>')

    for i, kp in enumerate(b.key_points, 1):
        head = f"{i}. {kp}" if tpl.get("numbered") else _html.escape(kp)
        if tpl.get("section_box"):
            parts.append(f'<div style="{tpl["section_box"]}">')
            parts.append(f'<h2 style="{tpl["h2"]}">{head}</h2>')
            parts.append(f'<p style="{tpl["p"]};margin-bottom:0">［此处展开 —— '
                         f'需要你的具体案例或数据］</p></div>')
        else:
            parts.append(f'<h2 style="{tpl["h2"]}">{head}</h2>')
            parts.append(f'<p style="{tpl["p"]}">［此处展开这一条 —— '
                         f'需要你的具体案例或数据］</p>')

    if b.notes:
        parts.append(f'<p style="{tpl["p"]}">{_html.escape(b.notes)}</p>')
    parts.append('</div>')
    return "\n".join(parts)


def cmd_make(ws: Workspace, args) -> int:
    """生成图文或视频成品。"""
    pid = args.id
    proj = ws.load(pid)
    outdir = os.path.join(ws.dir_of(proj.id), "output")
    os.makedirs(outdir, exist_ok=True)

    if not proj.angle:
        raise OpsError("还没选题。先跑 topic --set '你的角度'")
    if not (proj.brief.key_points or proj.brief.personal):
        raise OpsError(
            "brief 是空的 —— 工具不会替你编内容。\n"
            "先跑：content_ops.py brief <id> --file brief.md")

    if args.type == "article":
        tpl_key = getattr(args, "template", "") or PLATFORM_TEMPLATE.get(proj.platform, "classic")
        html = render_article_html(proj, tpl_key)
        ap = os.path.join(outdir, "article.html")
        with open(ap, "w", encoding="utf-8") as f:
            f.write(html)
        proj.article = {"html_path": ap, "rendered_at": time.strftime("%F %T"),
                        "chars": len(re.sub(r"<[^>]+>", "", html)),
                        "template": tpl_key, "template_name": ARTICLE_TEMPLATES[tpl_key]["name"]}
        print(f"✅ 图文已生成: {ap}")
        print(f"   模板: {ARTICLE_TEMPLATES[tpl_key]['name']}（{tpl_key}）")
        print(f"   正文约 {proj.article['chars']} 字")
    elif args.type == "video":
        try:
            from video_studio import BuildSpec, Segment, SIZES, build_video
        except ImportError:
            raise OpsError("找不到 video_studio.py（需要放在同一目录）")
        lines = [l for l in (proj.brief.personal or "").split("\n") if l.strip()]
        lines += proj.brief.key_points
        if not lines:
            raise OpsError("视频需要分句文案（brief 的要点/经历）")
        segs = [Segment(text=l.strip(), image=(proj.brief.assets[i]
                                               if i < len(proj.brief.assets) else None),
                        zoom="in" if i % 2 == 0 else "out")
                for i, l in enumerate(lines)]
        vp = os.path.join(outdir, "video.mp4")
        size = SIZES.get(proj.platform_meta.get("video_size", "vertical"),
                         SIZES["vertical"])
        build_video(BuildSpec(segments=segs, out=vp, size=size,
                              voice=args.voice, rate=args.rate))
        proj.video = {"path": vp, "segments": len(segs), "size": f"{size[0]}x{size[1]}",
                      "rendered_at": time.strftime("%F %T")}
        print(f"✅ 视频已生成: {vp}")
        print(f"   {len(segs)} 段 · {size[0]}×{size[1]}")
    else:
        raise OpsError(f"未知类型 {args.type}")

    if proj.stage_enum == Stage.TOPIC_SELECTED:
        advance(proj, Stage.CREATED, f"生成 {args.type}")
    ws.save(proj)
    return 0


# ═══════════════════════════ 阶段三：分发打包 ═══════════════════════════

def _to_clipboard_rich(html: str, plain: str) -> bool:
    """把富文本写进系统剪贴板（秀米/壹伴同款技术）。

    这是**合规分发**的核心：我们只准备内容，用户手动粘贴到平台编辑器。
    """
    try:
        from AppKit import (NSPasteboard, NSPasteboardTypeHTML,
                            NSPasteboardTypeString)
    except Exception:              # 没装 pyobjc 也能用，只是少了「复制富文本」
        return False
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    ok = bool(pb.setString_forType_(html, NSPasteboardTypeHTML))
    pb.setString_forType_(plain, NSPasteboardTypeString)
    return ok


def cmd_package(ws: Workspace, args) -> int:
    """把成品打成"可以直接分发"的形态。"""
    pid = args.id
    proj = ws.load(pid)
    outdir = os.path.join(ws.dir_of(proj.id), "output")
    bundle: dict[str, Any] = {"id": proj.id, "platform": proj.platform_meta["name"],
                              "title": proj.title or proj.angle, "files": []}

    if proj.article.get("html_path"):
        bundle["files"].append(proj.article["html_path"])
    if proj.video.get("path"):
        bundle["files"].append(proj.video["path"])

    # 同目录下所有素材也一并列出（供手动上传）
    if os.path.isdir(outdir):
        for f in sorted(os.listdir(outdir)):
            fp = os.path.join(outdir, f)
            if os.path.isfile(fp) and fp not in bundle["files"]:
                bundle["files"].append(fp)

    bp = os.path.join(outdir, "bundle.json")
    with open(bp, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, indent=2)

    print(f"📦 分发包（{bundle['platform']}）")
    print(f"   标题: {bundle['title']}")
    for f in bundle["files"]:
        print(f"   · {os.path.relpath(f, ws.dir_of(proj.id))}")

    if args.clipboard:
        if not proj.article.get("html_path"):
            print("\n⚠️ 还没有图文成品，跳过剪贴板写入")
        else:
            with open(proj.article["html_path"], encoding="utf-8") as f:
                html = f.read()
            plain = re.sub(r"<br\s*/?>", "\n", html)
            plain = re.sub(r"</(p|h1|h2|blockquote|div)>", "\n", plain)
            plain = _html.unescape(re.sub(r"<[^>]+>", "", plain)).strip()
            if _to_clipboard_rich(html, plain):
                print("\n📋 富文本已写进剪贴板")
                print("   → 打开平台编辑器，直接 ⌘V 粘贴（样式会保留）")
            else:
                print("\n⚠️ 写剪贴板失败（需要 pyobjc-framework-Cocoa）")

    print("\n" + "─" * 56)
    print("  分发方式：手动粘贴 / 手动上传 —— **发布按钮由你点**")
    print("  这样工具不触碰平台，规避第三方工具条款")
    print("─" * 56)

    if proj.stage_enum == Stage.CREATED:
        advance(proj, Stage.PACKAGED, "已打包")
    ws.save(proj)
    return 0


# ═══════════════════════════ 阶段四：数据回流 ═══════════════════════════

def cmd_metrics(ws: Workspace, args) -> int:
    """回填发布后的数据表现 —— 这是选题引擎的养料。"""
    pid = args.id
    proj = ws.load(pid)
    entry = {
        "ts": time.strftime("%F %T"),
        "views": args.views, "likes": args.likes,
        "comments": args.comments, "favorites": args.favorites,
        "url": args.url or proj.published_url,
    }
    proj.metrics.setdefault("records", []).append(entry)
    if args.url:
        proj.published_url = args.url
    if proj.stage_enum == Stage.PACKAGED:
        advance(proj, Stage.PUBLISHED, "已发布")
    if proj.stage_enum == Stage.PUBLISHED:
        advance(proj, Stage.MEASURED, "数据已回流")
    ws.save(proj)
    print(f"✅ 已记录: 浏览 {entry['views']} · 赞 {entry['likes']} · "
          f"评 {entry['comments']} · 藏 {entry['favorites']}")
    return 0


def cmd_insight(ws: Workspace, args) -> int:
    """从已回流的数据里找规律 —— 给下一轮选题提供依据。"""
    pros = [p for p in ws.all() if p.metrics.get("records")]
    if not pros:
        print("还没有数据。用 metrics 命令回填发布后的表现。")
        return 0
    rows = []
    for p in pros:
        rec = p.metrics["records"][-1]
        rows.append((rec.get("views", 0), rec.get("likes", 0),
                     rec.get("favorites", 0), p.angle or p.title, p.platform))
    rows.sort(reverse=True)
    print(f"{'浏览':>8} {'赞':>6} {'藏':>6}  {'互动率':>7}  选题")
    print("-" * 78)
    for v, l, f, ang, pf in rows:
        rate = (l + f) / v * 100 if v else 0
        print(f"{v:>8} {l:>6} {f:>6}  {rate:>6.1f}%  {ang[:36]}  [{pf}]")
    print()
    best = rows[0]
    print(f"💡 表现最好的：《{best[3]}》")
    print("   → 下一轮选题往这个方向多找几个同类角度")
    return 0


# ═══════════════════════════ CLI ═══════════════════════════

def cmd_new(ws: Workspace, args) -> int:
    proj = ws.new(args.niche, args.platform)
    meta = proj.platform_meta
    print(f"✅ 新项目 {proj.id}")
    print(f"   领域: {proj.niche}")
    print(f"   平台: {meta['name']}（{meta['kind']}，标题上限 {meta['title_max']} 字）")
    print(f"   目录: {ws.dir_of(proj.id)}")
    print(f"\n下一步：python3 content_ops.py topic {proj.id} --set '你的选题角度'")
    return 0


def cmd_list(ws: Workspace, args) -> int:
    pros = ws.all()
    if not pros:
        print("还没有项目。用 new 创建。")
        return 0
    print(f"{'ID':<18} {'阶段':<12} {'平台':<10} 选题")
    print("-" * 84)
    for p in pros:
        print(f"{p.id:<18} {STAGE_LABEL[p.stage_enum]:<12} "
              f"{p.platform_meta['name']:<10} {(p.angle or p.title or '（未定）')[:38]}")
    return 0


def cmd_show(ws: Workspace, args) -> int:
    p = ws.load(args.id)
    print(f"项目 {p.id} · {p.niche} · {p.platform_meta['name']}")
    print(f"阶段: {STAGE_LABEL[p.stage_enum]}  创建于 {p.created_at}")
    print(f"选题: {p.angle or '（未定）'}"
          + (f"  [{p.pattern}]" if p.pattern else ""))
    print(f"标题: {p.title or '（未定）'}")
    b = p.brief
    print(f"\n【brief】钩子: {b.hook[:60] or '（空）'}")
    print(f"  要点 {len(b.key_points)} 条 · 个人内容 {len(b.personal)} 字 · 素材 {len(b.assets)} 个")
    if b.key_points:
        for k in b.key_points:
            print(f"    · {k}")
    print(f"\n【成品】图文: {'✅' if p.article else '—'}  "
          f"视频: {'✅' if p.video else '—'}  "
          f"封面: {p.covers.get('count', 0) if p.covers else '—'}")
    if p.covers:
        print(f"        风格 {p.covers.get('styles')} · 平台 {p.covers.get('platforms')}")
    if p.metrics.get("records"):
        r = p.metrics["records"][-1]
        print(f"【数据】浏览 {r.get('views')} · 赞 {r.get('likes')} · 藏 {r.get('favorites')}")
    print(f"\n【流转记录】")
    for h in p.history:
        print(f"  {h['ts']}  {h['from'] or '—':<16} → {h['to']:<16} {h.get('note','')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="自媒体运营助手 · 产品主流程骨架",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__)
    p.add_argument("--ws", default="workspace", help="工作区目录")
    sub = p.add_subparsers(dest="cmd")

    n = sub.add_parser("new", help="新建内容项目")
    n.add_argument("--niche", required=True)
    n.add_argument("--platform", default="gzh", choices=sorted(PLATFORMS))
    n.set_defaults(func=cmd_new)

    sub.add_parser("list", help="列出所有项目").set_defaults(func=cmd_list)

    s = sub.add_parser("show", help="查看项目详情")
    s.add_argument("id")
    s.set_defaults(func=cmd_show)

    t = sub.add_parser("topic", help="选题")
    t.add_argument("id")
    t.add_argument("--set", help="写入选定角度")
    t.set_defaults(func=cmd_topic)

    b = sub.add_parser("brief", help="写入内容要点（必须由人提供）")
    b.add_argument("id")
    b.add_argument("--file")
    b.add_argument("--text")
    b.set_defaults(func=cmd_brief)

    m = sub.add_parser("make", help="生成图文/视频")
    m.add_argument("id")
    m.add_argument("--type", required=True, choices=["article", "video"])
    m.add_argument("--template", default="", choices=[""] + sorted(ARTICLE_TEMPLATES),
                   help="图文模板（留空=按平台自动选）")
    m.add_argument("--voice", default="Tingting")
    m.add_argument("--rate", type=int, default=180)
    m.set_defaults(func=cmd_make)

    k = sub.add_parser("package", help="打包成可分发成品")
    k.add_argument("id")
    k.add_argument("--clipboard", action="store_true", help="富文本写进剪贴板")
    k.set_defaults(func=cmd_package)

    e = sub.add_parser("metrics", help="回填发布后数据")
    e.add_argument("id")
    e.add_argument("--views", type=int, default=0)
    e.add_argument("--likes", type=int, default=0)
    e.add_argument("--comments", type=int, default=0)
    e.add_argument("--favorites", type=int, default=0)
    e.add_argument("--url", default="")
    e.set_defaults(func=cmd_metrics)

    sub.add_parser("insight", help="从数据里找规律").set_defaults(func=cmd_insight)
    sub.add_parser("templates", help="列出图文模板").set_defaults(func=cmd_templates)
    return p


def cmd_templates(ws: Workspace, args) -> int:
    print("可用图文模板：\n")
    for k, t in ARTICLE_TEMPLATES.items():
        pf = [p for p, v in PLATFORM_TEMPLATE.items() if v == k]
        print(f"  {k:<10} {t['name']:<6} {t['desc']}")
        if pf:
            print(f"             {PLATFORM_NAME_ZH.get(pf[0], pf[0])} 等平台默认使用")
    print("\n用法: content_ops.py make <id> --type article --template <名字>")
    return 0


def main(argv: Sequence[str]) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    fn = getattr(args, "func", None)
    if fn is None:
        p.print_help()
        return 2
    ws = Workspace(args.ws)
    try:
        return fn(ws, args)
    except OpsError as e:
        print(f"\n❌ {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
