#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app_server — 运营助手 · 本地图形界面（服务端）

零新增依赖：只用 Python 标准库 `http.server`，把已有的 10 个模块包成 JSON API，
配一个单页前端，就跑出桌面 App 的形态。

    浏览器打开 http://127.0.0.1:8765
    （将来装了 pywebview，同一套代码可直接包成原生窗口）

设计约束
--------
1. **只绑 127.0.0.1** —— 不对外暴露，本机专用
2. **不出目录** —— 静态文件与素材访问都做路径校验，禁止 ../ 穿越
3. **长任务异步** —— 生产流水线要 15 秒，用后台线程 + 轮询，不阻塞界面
4. **只读优先** —— 危险动作（发布）本就没有，填草稿的 RPA 也默认关闭

用法
----
    python3 app_server.py                 # 启动并自动打开浏览器
    python3 app_server.py --port 8899 --no-open
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import tempfile
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urlparse, unquote

# ---- 已有的业务模块 ----
import content_ops as co

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(HERE, "ui")

DEFAULT_ASSETS = os.path.join(HERE, "assets")
DEFAULT_WS = os.path.join(HERE, "workspace")


# ═══════════════════════════ 后台任务 ═══════════════════════════

class Jobs:
    """极简后台任务表 —— 长任务异步跑，前端轮询进度。"""

    def __init__(self):
        self._d: dict[str, dict] = {}
        self._lock = threading.Lock()

    def new(self, kind: str) -> str:
        jid = uuid.uuid4().hex[:12]
        with self._lock:
            self._d[jid] = {"id": jid, "kind": kind, "state": "running",
                            "log": [], "result": None, "error": None,
                            "started": time.time(), "ended": None}
        return jid

    def log(self, jid: str, msg: str) -> None:
        with self._lock:
            j = self._d.get(jid)
            if j:
                j["log"].append(msg)

    def done(self, jid: str, result: Any = None) -> None:
        with self._lock:
            j = self._d.get(jid)
            if j:
                j.update(state="done", result=result, ended=time.time())

    def fail(self, jid: str, err: str) -> None:
        with self._lock:
            j = self._d.get(jid)
            if j:
                j.update(state="error", error=err, ended=time.time())

    def get(self, jid: str) -> Optional[dict]:
        with self._lock:
            j = self._d.get(jid)
            return dict(j) if j else None


JOBS = Jobs()


class Tee:
    """把输出同时收集进任务日志。

    ⚠️ 必须**同时接管 stdout 和 stderr**：
    video_studio 的渲染进度（最耗时的几十秒）是打到 stderr 的，
    只包 stdout 会让日志在视频阶段长时间"卡住不动"。
    """

    def __init__(self, jid: str, real):
        self.jid, self.real = jid, real

    def write(self, s: str):
        try:
            self.real.write(s)
        except Exception:
            pass
        for line in s.splitlines():
            if line.strip():
                JOBS.log(self.jid, line.rstrip())
        return len(s)

    def flush(self):
        try:
            self.real.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        return False

    def flush(self):
        self.real.flush()


# ═══════════════════════════ 业务封装 ═══════════════════════════

def ws() -> co.Workspace:
    return co.Workspace(DEFAULT_WS)


def overview() -> dict:
    w = ws()
    projects = w.all()
    stage_count: dict[str, int] = {}
    for p in projects:
        stage_count[p.stage] = stage_count.get(p.stage, 0) + 1

    assets = {"total": 0, "present": 0, "books": 0, "tags": 0}
    try:
        from asset_library import AssetLibrary
        lib = AssetLibrary(DEFAULT_ASSETS)
        s = lib.stats()
        assets = {"total": s["total"], "present": s["present"],
                  "books": len([b for b in s["by_book"] if b != "（未分类）"]),
                  "tags": len(s["tags"])}
    except Exception:
        pass

    # 结构表现
    perf = []
    try:
        from topic_engine import TopicEngine, PATTERNS
        eng = TopicEngine(asset_root=DEFAULT_ASSETS, ws_root=DEFAULT_WS)
        for k, d in eng.pattern_performance().items():
            perf.append({"key": k, "name": PATTERNS[k].name if k in PATTERNS else k,
                         **d})
        perf.sort(key=lambda x: -x["rate"])
    except Exception:
        pass

    return {
        "projects": len(projects),
        "stages": stage_count,
        "stage_labels": {s.value: co.STAGE_LABEL[s] for s in co.Stage},
        "assets": assets,
        "performance": perf,
        "platforms": {k: v["name"] for k, v in co.PLATFORMS.items()},
        "templates": {k: {"name": v["name"], "desc": v["desc"]}
                      for k, v in co.ARTICLE_TEMPLATES.items()},
        "styles": _cover_styles(),
        "video_templates": _video_templates(),
        "workspace": DEFAULT_WS,
        "assets_dir": DEFAULT_ASSETS,
    }


def _cover_styles() -> dict:
    try:
        import cover_studio as cs
        return {k: {"name": v.name, "desc": v.desc} for k, v in cs.STYLES.items()}
    except Exception:
        return {}


def sop_definition() -> dict:
    """把 SOP 定义暴露给前端。"""
    try:
        import sop as _sop
        S = _sop.SOP
        return {
            "key": S.key, "name": S.name, "desc": S.desc, "source": S.source,
            "steps": [{"key": st.key, "name": st.name, "goal": st.goal,
                       "who": st.who, "how": st.how, "need": st.need,
                       "out": st.out, "pass_when": st.pass_when}
                      for st in S.steps],
            "red_words": _sop.RED_WORDS,
            "amber_words": _sop.AMBER_WORDS,
        }
    except Exception as e:
        return {"error": str(e)}


def qa_project(pid: str) -> dict:
    """对一个项目跑出厂质检。"""
    import sop as _sop
    w = ws()
    proj = w.load(pid)
    r = _sop.check_project(proj, ws=w)
    return {
        "pid": r.pid, "title": r.title, "passed": r.passed,
        "structure": r.structure, "stats": r.stats,
        "issues": [{"level": i.level, "where": i.where, "word": i.word,
                    "advice": i.advice} for i in r.issues],
        "red_count": len(r.red), "amber_count": len(r.amber),
    }


def wizard(pid: str) -> dict:
    """SOP 流程向导状态 —— 界面靠它决定「现在显示哪一步的输入框」。"""
    import sop as _sop
    proj = ws().load(pid)
    w = _sop.wizard(proj)
    w["pid"] = pid
    w["title"] = proj.title or proj.angle or pid
    # 第①步要给「推荐素材」—— 否则用户不知道去哪选
    if not proj.brief.assets:
        w["suggested"] = suggest_assets_for(proj.angle or proj.title or "", n=6)
    else:
        w["suggested"] = []
    return w


def submit_step(pid: str, body: dict) -> dict:
    """提交某一步的产出：校验通过才推进。"""
    import sop as _sop
    w = ws()
    proj = w.load(pid)
    key = body.get("step") or _sop.next_key(proj)
    if not key:
        return {"ok": True, "done": True, "issues": [],
                "wizard": wizard(pid)}
    data = {k: v for k, v in body.items() if k != "step"}
    ok, issues = _sop.submit_step(proj, key, data)
    # 同步推进内容状态机的阶段
    try:
        if key == "pick" and proj.stage_enum == co.Stage.DRAFT:
            co.advance(proj, co.Stage.TOPIC_SELECTED, "SOP 定选题")
    except Exception:
        pass
    w.save(proj)
    return {"ok": ok, "step": key, "issues": issues, "wizard": wizard(pid)}


# ── 素材搜索与缩略图（第①步选素材要用）──

_THUMB_DIR = os.path.join(tempfile.gettempdir(), "ops_thumbs")


def search_assets(q: str = "", book: str = "", limit: int = 40,
                  *, asset_ids: Optional[list[str]] = None,
                  content_only: bool = True) -> dict:
    """搜索素材。支持关键词、按书过滤，或按 id 回查。"""
    from asset_library import AssetLibrary
    lib = AssetLibrary(DEFAULT_ASSETS)
    from asset_library import is_content_page
    if asset_ids:
        items = []
        for aid in asset_ids:
            try:
                items.append(lib.get(aid))
            except Exception:
                pass
    elif q or book:
        # 多取一些再过滤校准页，否则筛完不够数
        raw = lib.search(q=q, book=book, only_present=True)[:limit * 3]
        items = [a for a in raw
                 if not content_only or is_content_page(lib.abspath(a), cache_key=a.path)]
        items = items[:limit]
    else:
        # 没条件时给一个跨书的样本 —— AI 类选题常需要"随便一页古籍"做视觉
        items, seen = [], set()
        for a in lib.assets.values():
            if not a.present or a.book in seen:
                continue
            if content_only and not is_content_page(lib.abspath(a), cache_key=a.path):
                continue          # 跳过校准页
            seen.add(a.book)
            items.append(a)
            if len(items) >= limit:
                break
    return {"items": [{
        "id": a.id, "book": a.book, "chapter": a.chapter, "page": a.page,
        "name": a.clean_name, "w": a.w, "h": a.h,
        "edition": a.edition, "collection": a.collection,
        # path 是**可直接提交**给 brief 的路径（相对工作区）
        "path": os.path.join(os.path.basename(DEFAULT_ASSETS), a.path),
        "thumb": f"/thumb/{a.id}",
    } for a in items], "total": len(items)}


def thumb_path(asset_id: str, *, width: int = 260) -> Optional[str]:
    """生成/取用缩略图缓存。原图 3000px、近 1MB，列表里直接加载会很慢。"""
    from asset_library import AssetLibrary
    lib = AssetLibrary(DEFAULT_ASSETS)
    try:
        a = lib.get(asset_id)
    except Exception:
        return None
    src = lib.abspath(a)
    if not os.path.exists(src):
        return None
    os.makedirs(_THUMB_DIR, exist_ok=True)
    out = os.path.join(_THUMB_DIR, f"{a.id}_{width}.jpg")
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(src):
        return out
    try:
        from PIL import Image
        im = Image.open(src)
        im.thumbnail((width, width * 3), Image.LANCZOS)
        im.convert("RGB").save(out, quality=82)
        return out
    except Exception:
        return None


def suggest_assets_for(text: str, *, n: int = 6) -> list[dict]:
    """为一段文字推荐素材。

    关键是**多样性**：如果直接采用语义匹配结果，常常 6 张全来自同一册，
    配到视频里画面毫无变化。所以按「每本书最多 1 张」去重，再跨书补足。
    """
    from asset_library import AssetLibrary
    lib = AssetLibrary(DEFAULT_ASSETS)
    if not lib.assets:
        return []

    picks: list = []
    seen_book: set[str] = set()

    from asset_library import is_content_page

    def _take(cands, *, per_book: int = 1) -> None:
        cnt: dict[str, int] = {}
        for a in cands:
            if len(picks) >= n:
                return
            if not a.present:
                continue
            # 跳过扫描校准页 —— 那些灰阶卡配到视频里很出戏
            if not is_content_page(lib.abspath(a), cache_key=a.path):
                continue
            if cnt.get(a.book, 0) >= per_book:
                continue
            cnt[a.book] = cnt.get(a.book, 0) + 1
            seen_book.add(a.book)
            picks.append(a)

    # ① 语义匹配（按选题文字找相关的书）
    if (text or "").strip():
        _take(lib.suggest(text, n=n * 4))
    # ② 还不够就跨书补 —— 每本书取**第一张内容页**（不是第 1 页，那多半是校准靶）
    if len(picks) < n:
        by_book: dict[str, list] = {}
        for a in lib.assets.values():
            if a.present:
                by_book.setdefault(a.book, []).append(a)
        for book in sorted(by_book):
            if len(picks) >= n:
                break
            if book in seen_book:
                continue
            for a in sorted(by_book[book], key=lambda x: (x.volume, x.page)):
                if is_content_page(lib.abspath(a), cache_key=a.path):
                    picks.append(a)
                    seen_book.add(book)
                    break
    # ③ 书都用过了 → 放宽到同书不同册
    if len(picks) < n:
        _take([a for a in lib.assets.values() if a.present], per_book=2)

    return [{"id": a.id, "book": a.book, "chapter": a.chapter, "page": a.page,
             "name": a.clean_name,
             "path": os.path.join(os.path.basename(DEFAULT_ASSETS), a.path),
             "thumb": f"/thumb/{a.id}"} for a in picks[:n]]


def project_delete(pid: str, hard: bool = False) -> dict:
    """删除一个创作项目（图文、封面、视频等全部产出）。"""
    w = ws()
    return w.delete(pid, hard=hard)


def trash_list() -> dict:
    w = ws()
    items = w.trash()
    return {"items": items,
            "count": len(items),
            "bytes": sum(t["bytes"] for t in items)}


def _video_templates() -> dict:
    try:
        from video_studio import TEMPLATES
        return {k: {"name": v.name, "desc": v.desc,
                    "intro": v.intro, "outro": v.outro,
                    "transition": v.transition, "motion": v.motion}
                for k, v in TEMPLATES.items()}
    except Exception:
        return {}


def project_brief(p: co.ContentProject) -> dict:
    return {"hook": p.brief.hook, "key_points": p.brief.key_points,
            "personal": p.brief.personal, "assets": p.brief.assets,
            "notes": p.brief.notes}


def project_summary(p: co.ContentProject) -> dict:
    try:
        import sop as _sop
        pg = _sop.progress(p)
        sopinfo = {"done": pg["done"], "total": pg["total"],
                   "percent": pg["percent"], "current": pg["current"],
                   "current_name": next((st["name"] for st in pg["steps"]
                                         if st["key"] == pg["current"]), "")}
    except Exception:
        sopinfo = {"done": 0, "total": 6, "percent": 0, "current": "", "current_name": ""}
    return {
        "sop": sopinfo,
        "id": p.id, "niche": p.niche,
        "platform": p.platform,
        "platform_name": p.platform_meta.get("name", p.platform),
        "angle": p.angle, "title": p.title, "pattern": p.pattern,
        "stage": p.stage, "stage_label": co.STAGE_LABEL[p.stage_enum],
        "has_article": bool(p.article), "has_video": bool(p.video),
        "covers": (p.covers or {}).get("count", 0),
        "metrics": (p.metrics.get("records") or [{}])[-1] if p.metrics.get("records") else None,
        "created_at": p.created_at,
    }


def project_detail(pid: str) -> dict:
    p = ws().load(pid)
    out = os.path.join(DEFAULT_WS, p.id, "output")
    files: list[dict] = []
    if os.path.isdir(out):
        for root, _d, fs in os.walk(out):
            for f in sorted(fs):
                fp = os.path.join(root, f)
                files.append({
                    "name": os.path.relpath(fp, out),
                    "size": os.path.getsize(fp),
                    "kind": _file_kind(f),
                })
    d = project_summary(p)
    d.update({"brief": project_brief(p), "outputs": files,
              "video": p.video, "article": p.article, "covers": p.covers,
              "history": p.history})
    return d


def _file_kind(name: str) -> str:
    e = os.path.splitext(name)[1].lower()
    if e in (".png", ".jpg", ".jpeg", ".webp"):
        return "image"
    if e in (".mp4", ".mov"):
        return "video"
    if e in (".html",):
        return "html"
    if e in (".md", ".txt", ".json"):
        return "text"
    return "other"


def produce_async(pid: str, opts: dict) -> str:
    """在后台线程跑生产流水线。"""
    jid = JOBS.new("produce")

    def run():
        real_out, real_err = sys.stdout, sys.stderr
        sys.stdout = Tee(jid, real_out)
        sys.stderr = Tee(jid, real_err)
        try:
            import pipeline as pl
            proj = ws().load(pid)
            styles = opts.get("styles") or []
            platforms = opts.get("platforms") or []
            only = opts.get("only") or []
            o = pl.Options(
                article=("article" in only) if only else True,
                covers=("covers" in only) if only else True,
                video=("video" in only) if only else True,
                manifest=("manifest" in only) if only else True,
                styles=styles if isinstance(styles, list) else [],
                platforms=platforms if isinstance(platforms, list) else [],
                template=opts.get("template", ""),
                voice=opts.get("voice", "Tingting"),
                rate=int(opts.get("rate", 180)),
                subtitle_band=bool(opts.get("subtitle_band", True)),
                video_template=opts.get("video_template", "") or "",
            )
            res = pl.produce(proj, ws(), o)
            # 生产成功 → 自动推进 SOP 的「出成品」这一步，直接落到第⑥步质检。
            # 让用户手动点"我已生产完成"是多余的：机器明明知道成没成。
            try:
                import sop as _sop
                if res.ok and _sop.next_key(proj) == "produce":
                    ok, issues = _sop.submit_step(proj, "produce", {})
                    ws().save(proj)
                    JOBS.log(jid, "  ✅ SOP 第⑤步自动通过 → 进入第⑥步出厂质检"
                             if ok else f"  第⑤步未通过：{issues}")
            except Exception as _e:
                JOBS.log(jid, f"  (SOP 自动推进跳过: {_e})")
            JOBS.done(jid, {"ok": res.ok,
                            "produced": {k: len(v) for k, v in res.produced.items()},
                            "skipped": res.skipped,
                            "errors": res.errors})
        except Exception as e:
            JOBS.log(jid, f"❌ {e}")
            JOBS.fail(jid, str(e))
        finally:
            sys.stdout, sys.stderr = real_out, real_err

    threading.Thread(target=run, daemon=True).start()
    return jid


def topics_generate(body: dict) -> dict:
    from topic_engine import TopicEngine
    eng = TopicEngine(asset_root=DEFAULT_ASSETS, ws_root=DEFAULT_WS)
    cands = eng.generate(niche=body.get("niche") or "AI×国学",
                         platforms=body.get("platforms") or None,
                         n=int(body.get("n") or 10))
    from dataclasses import asdict
    return {"candidates": [asdict(c) for c in cands]}


def topics_pick(body: dict) -> dict:
    from topic_engine import TopicEngine, Candidate
    eng = TopicEngine(asset_root=DEFAULT_ASSETS, ws_root=DEFAULT_WS)
    c = Candidate(**body["candidate"])
    platform = body.get("platform") or (c.platforms[0] if c.platforms else "gzh")
    pid = eng.pick(c, platform=platform)
    return {"id": pid, "detail": project_detail(pid)}


def assets_list() -> dict:
    from asset_library import AssetLibrary
    lib = AssetLibrary(DEFAULT_ASSETS)
    s = lib.stats()
    items = [{"id": a.id, "path": a.path, "book": a.book, "chapter": a.chapter,
              "edition": a.edition, "passage": a.passage, "tags": a.tags,
              "kind": a.kind, "w": a.w, "h": a.h, "bytes": a.bytes,
              "present": a.present, "name": a.clean_name}
             for a in lib.assets.values()]
    items.sort(key=lambda x: (x["book"], x["chapter"], x["name"]))
    return {"stats": s, "items": items, "root": DEFAULT_ASSETS}


# ═══════════════════════════ HTTP ═══════════════════════════

class Handler(BaseHTTPRequestHandler):
    server_version = "ContentOpsUI/1.0"

    def log_message(self, fmt, *args):          # 静音默认日志
        pass

    # ---- 工具 ----
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _err(self, msg: str, code: int = 400) -> None:
        self._json({"error": msg}, code)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---- 路由 ----
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/"):
                return self._api_get(path)
            if path.startswith("/file/"):
                return self._serve_output(unquote(path[len("/file/"):]))
            if path.startswith("/thumb/"):
                tp = thumb_path(unquote(path[len("/thumb/"):]))
                if not tp:
                    return self._err("not found", 404)
                with open(tp, "rb") as f:
                    return self._send(200, f.read(), "image/jpeg")
            return self._serve_static(path)
        except Exception as e:
            traceback.print_exc()
            self._err(f"{type(e).__name__}: {e}", 500)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            return self._api_post(path)
        except Exception as e:
            traceback.print_exc()
            self._err(f"{type(e).__name__}: {e}", 500)

    def do_DELETE(self):
        # 没有这个处理器时 BaseHTTPRequestHandler 会直接回 501 —— 删除接口曾经
        # 因此静默失效。删除走和 POST 同一套路由（靠 self.command 区分）。
        path = urlparse(self.path).path
        try:
            return self._api_post(path)
        except Exception as e:
            traceback.print_exc()
            self._err(f"{type(e).__name__}: {e}", 500)

    # ---- 静态 ----
    def _serve_static(self, path: str):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        fp = os.path.normpath(os.path.join(UI_DIR, rel))
        if not fp.startswith(UI_DIR) or not os.path.isfile(fp):   # 防目录穿越
            return self._err("not found", 404)
        ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        with open(fp, "rb") as f:
            self._send(200, f.read(), ctype)

    def _serve_output(self, rel: str):
        """提供 workspace 内产物的预览（严格限制在 workspace 下）。"""
        base = os.path.abspath(DEFAULT_WS)
        # 容忍调用方多带一层工作区目录名 —— 之前拼成 workspace/workspace/…
        # 导致所有产物预览 404（图片打不开、html 打不开）
        wsname = os.path.basename(base)
        head, _, tail = rel.replace("\\", "/").partition("/")
        if head == wsname and tail:
            rel = tail
        fp = os.path.normpath(os.path.join(base, rel))
        if not fp.startswith(base) or not os.path.isfile(fp):
            return self._err("not found", 404)
        ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        # 文本类必须显式声明 charset —— 否则浏览器按系统默认编码猜，
        # 中文会整篇乱码（实测被解成 Big5）。
        if ctype.startswith("text/") or ctype in ("application/javascript",
                                                  "application/json"):
            ctype += "; charset=utf-8"
        with open(fp, "rb") as f:
            self._send(200, f.read(), ctype)

    # ---- API: GET ----
    def _api_get(self, path: str):
        if path == "/api/overview":
            return self._json(overview())
        if path == "/api/projects":
            return self._json({"projects": [project_summary(p) for p in ws().all()]})
        m = re.fullmatch(r"/api/projects/([\w\-]+)", path)
        if m:
            return self._json(project_detail(m.group(1)))
        if path == "/api/assets":
            return self._json(assets_list())
        if path == "/api/assets/search":
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            ids = qs.get("ids", [None])[0]
            return self._json(search_assets(
                q=(qs.get("q", [""])[0] or ""),
                book=(qs.get("book", [""])[0] or ""),
                limit=int(qs.get("limit", ["40"])[0]),
                asset_ids=ids.split(",") if ids else None,
                content_only=(qs.get("raw", ["0"])[0] not in ("1", "true"))))
        if path == "/api/sop":
            return self._json(sop_definition())
        if path == "/api/trash":
            return self._json(trash_list())
        if path == "/api/audit":
            import sop as _sop
            return self._json(_sop.audit(ws()))
        m = re.fullmatch(r"/api/projects/([\w\-]+)/size", path)
        if m:
            return self._json(ws().folder_stats(m.group(1)))
        m = re.fullmatch(r"/api/projects/([\w\-]+)/qa", path)
        if m:
            return self._json(qa_project(m.group(1)))
        m = re.fullmatch(r"/api/projects/([\w\-]+)/wizard", path)
        if m:
            return self._json(wizard(m.group(1)))
        m = re.fullmatch(r"/api/jobs/(\w+)", path)
        if m:
            j = JOBS.get(m.group(1))
            return self._json(j) if j else self._err("no such job", 404)
        return self._err("no such endpoint", 404)

    # ---- API: POST ----
    def _api_post(self, path: str):
        body = self._body()

        if path == "/api/projects":
            p = ws().new(body.get("niche") or "未分类",
                         body.get("platform") or "gzh")
            return self._json({"id": p.id, "detail": project_detail(p.id)})

        m = re.fullmatch(r"/api/projects/([\w\-]+)/brief", path)
        if m:
            w = ws()
            p = w.load(m.group(1))
            b = body
            p.brief.hook = b.get("hook", p.brief.hook)
            p.brief.personal = b.get("personal", p.brief.personal)
            p.brief.notes = b.get("notes", p.brief.notes)
            if "key_points" in b:
                p.brief.key_points = [k.strip() for k in b["key_points"] if k.strip()]
            if "assets" in b:
                p.brief.assets = b["assets"]
            if p.stage_enum == co.Stage.DRAFT and p.angle:
                co.advance(p, co.Stage.TOPIC_SELECTED, "补充 brief")
            w.save(p)
            return self._json({"ok": True, "detail": project_detail(p.id)})

        m = re.fullmatch(r"/api/projects/([\w\-]+)/angle", path)
        if m:
            w = ws()
            p = w.load(m.group(1))
            p.angle = body.get("angle", "")
            p.title = body.get("title") or p.angle
            if body.get("pattern"):
                p.pattern = body["pattern"]
            if p.stage_enum == co.Stage.DRAFT and p.angle:
                co.advance(p, co.Stage.TOPIC_SELECTED, "设定选题")
            w.save(p)
            return self._json({"ok": True, "detail": project_detail(p.id)})

        m = re.fullmatch(r"/api/trash/([^/]+)/restore", path)
        if m:
            return self._json({"restored": ws().restore(unquote(m.group(1)))})
        m = re.fullmatch(r"/api/trash/empty", path)
        if m:
            return self._json(ws().empty_trash())

        m = re.fullmatch(r"/api/projects/([\w\-]+)", path)
        if m and self.command == "DELETE":
            hard = "hard" in urlparse(self.path).query
            return self._json(project_delete(m.group(1), hard=hard))

        m = re.fullmatch(r"/api/projects/([\w\-]+)/step", path)
        if m:
            return self._json(submit_step(m.group(1), body))

        m = re.fullmatch(r"/api/projects/([\w\-]+)/produce", path)
        if m:
            jid = produce_async(m.group(1), body)
            return self._json({"job": jid})

        if path == "/api/topics":
            return self._json(topics_generate(body))
        if path == "/api/topics/pick":
            return self._json(topics_pick(body))
        if path == "/api/assets/rescan":
            from asset_library import AssetLibrary
            lib = AssetLibrary(DEFAULT_ASSETS)
            r = lib.rescan()
            return self._json({"ok": True, "rescan": r, **assets_list()})
        return self._err("no such endpoint", 404)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="运营助手 · 本地图形界面")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args(argv)

    if not os.path.isdir(UI_DIR):
        print(f"❌ 找不到界面目录 {UI_DIR}")
        return 1
    os.makedirs(DEFAULT_WS, exist_ok=True)
    os.makedirs(DEFAULT_ASSETS, exist_ok=True)

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    url = f"http://{a.host}:{a.port}"
    print(f"运营助手已启动 → {url}")
    print(f"  工作区: {DEFAULT_WS}")
    print(f"  素材库: {DEFAULT_ASSETS}")
    print("  Ctrl+C 停止\n")
    if not a.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
