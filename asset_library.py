#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
asset_library — 本地素材库（桌面 App · 本地处理）

面向「桌面 App + 本地目录」形态设计。核心取舍：

    素材文件**保持原样躺在目录里**（你可以直接用 Finder 管理），
    工具只维护一个**可重建的 JSON 索引**记录元数据。

为什么不用数据库
----------------
数据库会让"素材目录"变成工具的私有资产，你就不能直接用 Finder 整理了。
JSON 索引 + 目录扫描 = 索引随时能从文件系统重建，**永远不会和文件失同步**。

双向同步（关键设计）
--------------------
    工具导入  →  文件进目录 + 写索引
    你手动拖  →  `rescan` 扫出来，自动补进索引
    文件被删  →  `rescan` 标记 missing（保留元数据，不丢标签）

目录结构
--------
    assets/
    ├── _index.json                  # 索引（可删，rescan 会重建）
    ├── 论语/学而/3f2a1b-原文页.jpg   # 按书/篇自动归档
    └── _inbox/9c8d7e-未分类.jpg      # 没指定书名的进这里

国学专用元数据
--------------
    书名 / 篇名 / 版本（宋刻本·影印·点校本）/ 朝代 / 原文片段 / 注家 / 标签

用法
----
    python3 asset_library.py init --root assets
    python3 asset_library.py import 扫描件/ --book 论语 --chapter 学而 --edition 宋刻本
    python3 asset_library.py list --book 论语
    python3 asset_library.py search 学而
    python3 asset_library.py tag <id> --add 竖排 --add 善本
    python3 asset_library.py rescan
    python3 asset_library.py stats
    python3 asset_library.py suggest "学而时习之" -n 3

与创作引擎集成
--------------
    from asset_library import AssetLibrary
    lib = AssetLibrary("assets")
    imgs = lib.suggest("学而时习之", n=3)
    Segment(text="...", image=lib.resolve(imgs[0].id))   # 直接喂给 video_studio
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional, Sequence

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore


# ═══════════════════════════ 常量 ═══════════════════════════

INDEX_NAME = "_index.json"
INBOX = "_inbox"

KIND_BY_EXT = {
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".webp": "image",
    ".tif": "image", ".tiff": "image", ".heic": "image", ".bmp": "image", ".gif": "image",
    ".mp4": "video", ".mov": "video", ".mkv": "video", ".webm": "video", ".avi": "video",
    ".mp3": "audio", ".m4a": "audio", ".wav": "audio", ".aac": "audio", ".flac": "audio",
    ".pdf": "doc", ".txt": "doc", ".md": "doc", ".docx": "doc", ".epub": "doc",
}

#: 扫描时忽略的文件（索引自己、系统文件）
IGNORE_NAMES = {INDEX_NAME, ".DS_Store", "Thumbs.db"}
IGNORE_PREFIX = ("._", "~$")

#: 导入时按书/篇归档，这些字符不能进路径
_UNSAFE = str.maketrans({c: "_" for c in r'\/:*?"<>|'})


class AssetError(RuntimeError):
    pass


# ═══════════════════════════ 数据模型 ═══════════════════════════

@dataclass
class Asset:
    """一个素材。path 是**相对素材根目录**的路径。"""

    id: str
    path: str
    sha256: str
    kind: str = "image"
    bytes: int = 0
    w: int = 0
    h: int = 0
    added_at: str = ""
    mtime: float = 0.0
    present: bool = True

    # ---- 国学专用元数据 ----
    book: str = ""          # 书名，如 论语
    chapter: str = ""       # 篇名，如 学而
    edition: str = ""       # 版本，如 宋刻本 / 影印本 / 点校本
    dynasty: str = ""       # 朝代，如 宋
    passage: str = ""       # 图片对应的原文片段
    annotator: str = ""     # 注家，如 朱熹
    # ---- 典藏溯源（古籍素材必备：版权与可信度）----
    collection: str = ""    # 馆藏机构，如 国立公文書館 内閣文庫
    call_number: str = ""   # 请求番号 / 索书号
    grade: str = ""         # 底本等级
    source_url: str = ""    # 来源页面
    license: str = ""       # 使用许可
    volume: str = ""        # 册号，如 0001
    page: str = ""          # 页码，如 0071
    tags: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def clean_name(self) -> str:
        """去掉入库时加的哈希前缀，还原人可读的文件名。"""
        import re as _re
        return _re.sub(r"^[0-9a-f]{8}-", "", os.path.basename(self.path))

    @property
    def display(self) -> str:
        bits = [b for b in (self.book, self.chapter) if b]
        head = "·".join(bits) if bits else "未分类"
        return f"{head} / {self.clean_name}"

    @property
    def size_human(self) -> str:
        n = float(self.bytes)
        for u in ("B", "KB", "MB", "GB"):
            if n < 1024 or u == "GB":
                return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
            n /= 1024
        return f"{n:.1f}GB"

    #: 所有可被检索的文本
    def haystack(self) -> str:
        return " ".join([self.book, self.chapter, self.edition, self.dynasty,
                         self.passage, self.annotator, self.note,
                         self.collection, self.call_number,
                         self.volume, self.page,
                         " ".join(self.tags), os.path.basename(self.path)]).lower()


# ═══════════════════════════ 工具函数 ═══════════════════════════

def kind_of(path: str) -> str:
    return KIND_BY_EXT.get(os.path.splitext(path)[1].lower(), "other")


def sha256_of(path: str, *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


def is_media(path: str) -> bool:
    b = os.path.basename(path)
    if b in IGNORE_NAMES or b.startswith(IGNORE_PREFIX):
        return False
    return kind_of(path) != "other"


def safe_name(s: str) -> str:
    return (s or "").strip().translate(_UNSAFE)[:60]


def _image_size(path: str) -> tuple[int, int]:
    if Image is None:
        return (0, 0)
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return (0, 0)



#: 从文件名解析「书名-篇名」的常见模式
_NAME_RE = re.compile(r"^\s*([^\-_—·]{1,12})\s*[-_—·]\s*([^\-_—·]{1,20})")
_PAGE_TAIL = re.compile(r"[_\-]?(p|page|页|第)?\s*\d+\s*$", re.I)



#: 分词用的分隔符（空白 + 中英标点）
_SPLIT_RE = re.compile(r"[\s，。、；：！？,.;:!?·\-—_/\\()（）\[\]【】\"\'“”‘’]+")


def tokenize_query(text: str) -> list[str]:
    """把查询串切成可匹配的词。

    中文没有空格，直接用 split() 会把整句当成一个词，永远匹配不上。
    所以这里：先按标点切，再对长串补 2-gram。
    """
    out: list[str] = []
    for tok in _SPLIT_RE.split((text or "").lower()):
        if not tok:
            continue
        out.append(tok)
        if len(tok) > 2:                       # 长串补 2-gram
            out.extend(tok[i:i + 2] for i in range(len(tok) - 1))
    return [t for t in out if t]


def parse_book_chapter(filename: str) -> tuple[str, str]:
    """从文件名猜书名和篇名。

    支持 `论语-学而_p1.jpg` / `孟子_梁惠王 03.jpg` / `道德经·一章.png` 这类命名。
    猜不出就返回空串，不会瞎猜。
    """
    stem = os.path.splitext(os.path.basename(filename))[0]
    stem = _PAGE_TAIL.sub("", stem).strip()
    m = _NAME_RE.match(stem)
    if not m:
        return ("", "")
    book, chap = m.group(1).strip(), m.group(2).strip()
    # 篇名太短或明显是页码的，丢弃
    if len(chap) < 1 or chap.isdigit():
        return (book, "")
    return (book, chap)



# ─────────── 内容页识别（跳过扫描校准页）───────────
#
# 数字化古籍时，每册首尾会插**灰阶/色卡校准靶**，不是古籍内容。
# 实测规律（21 部书抽样一致）：
#     第 0001 页   暗块占比 41-62%   ← 校准页
#     部分末页     暗块占比 35-37%   ← 校准页
#     正文页       暗块占比 2-15%
# 用「暗块占比」当判据，阈值 20%。
# 结果缓存到磁盘，避免每次列表都重算 7911 次。

_CALIB_CACHE_PATH = os.path.join(
    os.environ.get("TMPDIR", "/tmp"), "ops_content_pages.json")
_calib_mem: Optional[dict] = None


def _load_calib() -> dict:
    global _calib_mem
    if _calib_mem is None:
        try:
            _calib_mem = json.load(open(_CALIB_CACHE_PATH, encoding="utf-8"))
        except Exception:
            _calib_mem = {}
    return _calib_mem


def _save_calib() -> None:
    if _calib_mem is None:
        return
    try:
        tmp = _CALIB_CACHE_PATH + ".tmp"
        json.dump(_calib_mem, open(tmp, "w", encoding="utf-8"))
        os.replace(tmp, _CALIB_CACHE_PATH)
    except Exception:
        pass


def dark_ratio(path: str, *, px: int = 300) -> float:
    """深色像素占比 —— 校准页很高（暗背景），古籍正文页很低（米黄纸）。"""
    try:
        from PIL import Image
        import numpy as _np
        im = Image.open(path).convert("L")
        im.thumbnail((px, px))
        a = _np.asarray(im)
        return float((a < 110).mean())
    except Exception:
        return 0.0


def is_content_page(path: str, *, threshold: float = 0.20,
                    cache_key: str = "") -> bool:
    """这页是不是古籍正文（而非校准靶/隔页）。"""
    key = cache_key or path
    c = _load_calib()
    if key in c:
        return bool(c[key])
    ok = dark_ratio(path) < threshold
    c[key] = ok
    _save_calib()
    return ok


# ═══════════════════════════ 古籍合集导入 ═══════════════════════════

#: 期望的合集结构
#:   <书名>/SOURCE.md                （可选）册目题名、馆藏、索书号
#:   <书名>/<册号>/manifest.json      （必需）来源、许可、逐页清单
#:   <书名>/<册号>/images/<页码>.jpg
_TABLE_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*(.*?)\s*\|?\s*$", re.M)


def _md_rows(text: str) -> list[list[str]]:
    """把 markdown 表格行解析成单元格列表（跳过表头分隔行）。"""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(set(c) <= set("-: ") for c in cells):     # 分隔行
            continue
        out.append(cells)
    return out


def parse_source_md(path: str) -> dict:
    """解析 SOURCE.md —— 提取书名、馆藏、索书号、底本等级、册目题名。"""
    text = open(path, encoding="utf-8").read()
    out: dict = {"title": "", "original_title": "", "collection": "",
                 "call_number": "", "grade": "", "volumes": {}}
    m = re.search(r"^#\s+(.+?)\s*$", text, re.M)
    if m:
        out["title"] = m.group(1).strip()
    KEYMAP = {"馆藏原题": "original_title", "请求番号": "call_number",
              "收藏": "collection", "底本等级": "grade"}
    for cells in _md_rows(text):
        if len(cells) >= 2 and cells[0] in KEYMAP:
            out[KEYMAP[cells[0]]] = cells[1]
    # 册目表：| 册号 | 题名 | 页数 |
    if "## 册目" in text:
        sec = text.split("## 册目", 1)[1]
        for cells in _md_rows(sec):
            if len(cells) >= 2 and re.fullmatch(r"\d{3,4}", cells[0]):
                out["volumes"][cells[0]] = cells[1]
    return out


# ═══════════════════════════ 素材库 ═══════════════════════════

class AssetLibrary:
    """本地素材库。所有公开方法都不依赖 GUI，可直接被桌面 App 调用。"""

    def __init__(self, root: str = "assets"):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self._index_path = os.path.join(self.root, INDEX_NAME)
        self.assets: dict[str, Asset] = {}
        self.load()

    # ───────── 索引读写 ─────────

    def load(self) -> None:
        if not os.path.exists(self._index_path):
            self.assets = {}
            return
        try:
            with open(self._index_path, encoding="utf-8") as f:
                data = json.load(f)
            self.assets = {k: Asset(**v) for k, v in data.get("assets", {}).items()}
        except Exception as e:
            print(f"⚠️ 索引损坏（{e}），将重建。原文件保留为 {INDEX_NAME}.bak",
                  file=sys.stderr)
            shutil.copyfile(self._index_path, self._index_path + ".bak")
            self.assets = {}

    def save(self) -> None:
        tmp = self._index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "root": self.root,
                       "updated_at": time.strftime("%F %T"),
                       "assets": {k: asdict(v) for k, v in self.assets.items()}},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, self._index_path)          # 原子写，避免中断损坏索引

    # ───────── 路径 ─────────

    def abspath(self, asset: Asset | str) -> str:
        p = asset.path if isinstance(asset, Asset) else asset
        return os.path.join(self.root, p)

    def resolve(self, asset_id: str) -> str:
        """素材 id → 绝对路径（给 video_studio 直接喂图用）。"""
        a = self.get(asset_id)
        return self.abspath(a)

    def get(self, asset_id: str) -> Asset:
        if asset_id in self.assets:
            return self.assets[asset_id]
        hits = [a for k, a in self.assets.items() if k.startswith(asset_id)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise AssetError(f"id 前缀 {asset_id!r} 匹配到 {len(hits)} 个素材")
        raise AssetError(f"找不到素材 {asset_id!r}")

    # ───────── 导入 ─────────

    def _target_rel(self, src_name: str, digest: str, meta: dict) -> str:
        """决定归档路径：<书>/<篇>/<hash8>-<原名>，没书名进 _inbox。"""
        ext = os.path.splitext(src_name)[1].lower()
        stem = safe_name(os.path.splitext(os.path.basename(src_name))[0]) or "asset"
        fname = f"{digest[:8]}-{stem}{ext}"
        book, chap = safe_name(meta.get("book", "")), safe_name(meta.get("chapter", ""))
        if book and chap:
            return os.path.join(book, chap, fname)
        if book:
            return os.path.join(book, fname)
        return os.path.join(INBOX, fname)

    def import_file(self, src: str, *, mode: str = "copy", **meta: Any) -> Asset:
        """导入一个文件。**按内容 sha256 去重** —— 同图重复导入不会产生副本。"""
        src = os.path.abspath(src)
        if not os.path.isfile(src):
            raise AssetError(f"不是文件: {src}")
        if not is_media(src):
            raise AssetError(f"不支持的素材类型: {os.path.basename(src)}")

        digest = sha256_of(src)
        for a in self.assets.values():                 # 去重
            if a.sha256 == digest:
                self._merge_meta(a, meta)
                if not a.present and os.path.exists(self.abspath(a)):
                    a.present = True
                return a

        rel = self._target_rel(os.path.basename(src), digest, meta)
        dst = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            dst = dst.replace(".", f"-{uuid.uuid4().hex[:4]}.", 1)
            rel = os.path.relpath(dst, self.root)

        if mode == "move" and os.path.dirname(src) != os.path.dirname(dst):
            shutil.move(src, dst)
        else:
            shutil.copy2(src, dst)

        w, h = _image_size(dst) if kind_of(dst) == "image" else (0, 0)
        asset = Asset(
            id=digest[:12], path=rel, sha256=digest, kind=kind_of(dst),
            bytes=os.path.getsize(dst), w=w, h=h,
            added_at=time.strftime("%F %T"), mtime=os.path.getmtime(dst),
            present=True,
            book=meta.get("book", ""), chapter=meta.get("chapter", ""),
            edition=meta.get("edition", ""), dynasty=meta.get("dynasty", ""),
            passage=meta.get("passage", ""), annotator=meta.get("annotator", ""),
            note=meta.get("note", ""),
            tags=list(meta.get("tags", []) or []),
        )
        self.assets[asset.id] = asset
        return asset

    @staticmethod
    def _merge_meta(a: Asset, meta: dict) -> None:
        """重复导入时，只补充原本为空的字段，不覆盖已有信息。"""
        for k in ("book", "chapter", "edition", "dynasty", "passage", "annotator",
                  "note", "collection", "call_number", "grade", "source_url",
                  "license", "volume", "page"):
            if meta.get(k) and not getattr(a, k, ""):
                setattr(a, k, meta[k])
        for t in meta.get("tags", []) or []:
            if t not in a.tags:
                a.tags.append(t)

    def import_path(self, path: str, *, recursive: bool = True,
                    mode: str = "copy", auto_parse: bool = False,
                    **meta: Any) -> list[Asset]:
        """导入文件或整个目录。"""
        path = os.path.abspath(path)
        if os.path.isfile(path):
            return [self.import_file(path, mode=mode, **meta)]
        if not os.path.isdir(path):
            raise AssetError(f"路径不存在: {path}")
        out: list[Asset] = []
        for dirpath, _dirs, files in os.walk(path):
            for fn in sorted(files):
                fp = os.path.join(dirpath, fn)
                if not is_media(fp):
                    continue
                try:
                    sub = dict(meta)
                    # ① 文件名里带「书名-篇名」就直接用（最准，优先于目录名）
                    if auto_parse:
                        pb, pc = parse_book_chapter(fn)
                        # 用真值判断而非 setdefault —— meta 里的键始终存在
                        # （值为空串），setdefault 会被静默跳过。
                        if pb and not sub.get("book"):
                            sub["book"] = pb
                        if pc and not sub.get("chapter"):
                            sub["chapter"] = pc
                    # ② 仍未指定书名时，用上一层目录名兜底
                    if not sub.get("book"):
                        rel = os.path.relpath(dirpath, path)
                        if rel != ".":
                            parts = rel.split(os.sep)
                            sub.setdefault("book", parts[0])
                            if len(parts) > 1:
                                sub.setdefault("chapter", parts[1])
                    out.append(self.import_file(fp, mode=mode, **sub))
                except AssetError as e:
                    print(f"  跳过 {fn}: {e}", file=sys.stderr)
            if not recursive:
                break
        return out

    # ───────── 古籍合集导入（就地索引，不复制文件）─────────

    def import_collection(self, *, verbose: bool = True) -> dict:
        """就地索引已经整理好的古籍合集。

        与 `import_path` 的区别：**不复制、不改名、不移动文件**。
        素材已经在最终位置，这里只负责把元数据读进索引。

        元数据来源优先级：manifest.json（逐页清单+许可）> SOURCE.md（册目题名+馆藏）
        """
        by_path = {a.path: a for a in self.assets.values()}
        stats = {"books": 0, "volumes": 0, "added": 0, "updated": 0, "skipped": 0}
        books = []

        for book_dir in sorted(os.listdir(self.root)):
            bpath = os.path.join(self.root, book_dir)
            if not os.path.isdir(bpath) or book_dir.startswith("."):
                continue
            vols = [d for d in sorted(os.listdir(bpath))
                    if os.path.isdir(os.path.join(bpath, d))
                    and os.path.exists(os.path.join(bpath, d, "manifest.json"))]
            if not vols:
                continue                              # 不是合集结构的目录，跳过
            books.append(book_dir)

            # SOURCE.md（可选）
            src_md = parse_source_md(os.path.join(bpath, "SOURCE.md")) \
                if os.path.exists(os.path.join(bpath, "SOURCE.md")) else {}

            for vol in vols:
                vdir = os.path.join(bpath, vol)
                try:
                    mf = json.load(open(os.path.join(vdir, "manifest.json"),
                                        encoding="utf-8"))
                except Exception as e:
                    if verbose:
                        print(f"    ⚠️ 读不了 {book_dir}/{vol}/manifest.json: {e}")
                    continue
                stats["volumes"] += 1
                vol_title = (src_md.get("volumes") or {}).get(vol, "")
                chapter = vol_title or f"{book_dir} {vol}"
                source = mf.get("source", "")
                license_ = mf.get("license", "")
                src_url = mf.get("source_url", "")

                for pg in mf.get("pages", []):
                    fn = pg.get("file")
                    if not fn:
                        continue
                    rel = os.path.join(book_dir, vol, "images", fn)
                    fp = os.path.join(self.root, rel)
                    if not os.path.exists(fp):
                        stats["skipped"] += 1
                        continue
                    meta = dict(
                        book=book_dir, chapter=chapter, volume=vol,
                        page=str(pg.get("seq") or os.path.splitext(fn)[0]),
                        collection=src_md.get("collection", "") or source,
                        call_number=src_md.get("call_number", ""),
                        grade=src_md.get("grade", ""),
                        edition=src_md.get("original_title", "") or book_dir,
                        source_url=pg.get("url") or src_url,
                        license=license_,
                        note="国立公文書館デジタルアーカイブ",
                        tags=["古籍", "扫描", "公文书馆"],
                    )
                    old_a = by_path.get(rel)
                    if old_a is not None:
                        self._merge_meta(old_a, meta)
                        old_a.present = True
                        stats["updated"] += 1
                        continue
                    try:
                        w, h = _image_size(fp)
                        digest = sha256_of(fp)
                        # ⚠️ 主键**必须按路径**，不能只用内容哈希。
                        # 不同书里出现完全相同的页很常见（空白页、隔页、
                        # 扫描校准靶、同一枚藏书印），用内容当主键会让
                        # 后一张静默覆盖前一张 —— 页数悄悄变少。
                        # 内容哈希仍保留在 sha256 字段，供查重/校验用。
                        aid = hashlib.sha256(
                            ("path:" + rel.replace(os.sep, "/")).encode()
                        ).hexdigest()[:12]
                        if aid in self.assets and self.assets[aid].path != rel:
                            aid = hashlib.sha256(
                                ("path:" + rel + "#" + digest).encode()
                            ).hexdigest()[:12]
                        a = Asset(id=aid, path=rel, sha256=digest,
                                  kind=kind_of(fp), bytes=os.path.getsize(fp),
                                  w=w, h=h, added_at=time.strftime("%F %T"),
                                  mtime=os.path.getmtime(fp), present=True, **meta)
                        self.assets[a.id] = a
                        stats["added"] += 1
                    except Exception as e:
                        stats["skipped"] += 1
                        if verbose:
                            print(f"    ⚠️ {rel}: {e}")

            if verbose:
                print(f"  ✅ {book_dir}  {len(vols)} 册")

        stats["books"] = len(books)
        self.save()
        return stats

    # ───────── 扫描同步 ─────────

    def rescan(self) -> dict:
        """把索引与文件系统对齐。

        · 目录里有多少了但索引没有 → 补进来（你在 Finder 里拖进来的会被发现）
        · 索引里有但文件没了       → 标记 present=False（**保留标签**）
        · 文件回来了               → present 恢复 True
        """
        on_disk: dict[str, str] = {}      # relpath -> abspath
        for dirpath, _dirs, files in os.walk(self.root):
            for fn in files:
                fp = os.path.join(dirpath, fn)
                if not is_media(fp):
                    continue
                on_disk[os.path.relpath(fp, self.root)] = fp

        added = removed = restored = 0
        known_paths = {a.path for a in self.assets.values()}

        for rel, fp in on_disk.items():                 # 新增
            if rel in known_paths:
                continue
            try:
                w, h = _image_size(fp) if kind_of(fp) == "image" else (0, 0)
                digest = sha256_of(fp)
                # 同内容已在索引里（只是换了路径）→ 更新路径而不是新增
                dup = next((a for a in self.assets.values() if a.sha256 == digest), None)
                if dup:
                    dup.path, dup.present, dup.mtime = rel, True, os.path.getmtime(fp)
                    restored += 1
                    continue
                self.assets[digest[:12]] = Asset(
                    id=digest[:12], path=rel, sha256=digest, kind=kind_of(fp),
                    bytes=os.path.getsize(fp), w=w, h=h,
                    added_at=time.strftime("%F %T"), mtime=os.path.getmtime(fp),
                    present=True, note="rescan 自动收录")
                added += 1
            except Exception as e:
                print(f"  扫描失败 {rel}: {e}", file=sys.stderr)

        for a in self.assets.values():                  # 丢失 / 恢复
            exists = os.path.exists(self.abspath(a))
            if not exists and a.present:
                a.present = False
                removed += 1
            elif exists and not a.present:
                a.present = True
                restored += 1

        self.save()
        return {"added": added, "missing": removed, "restored": restored,
                "total": len(self.assets),
                "on_disk": len(on_disk)}

    # ───────── 检索 ─────────

    def search(self, *, q: str = "", book: str = "", chapter: str = "",
               edition: str = "", kind: str = "", tags: Optional[Iterable[str]] = None,
               only_present: bool = True) -> list[Asset]:
        """多条件检索。q 会在书名/篇名/版本/原文/标签/文件名里做子串匹配。"""
        want = [t.lower() for t in (tags or [])]
        out = []
        for a in self.assets.values():
            if only_present and not a.present:
                continue
            if kind and a.kind != kind:
                continue
            if book and book.lower() not in a.book.lower():
                continue
            if chapter and chapter.lower() not in a.chapter.lower():
                continue
            if edition and edition.lower() not in a.edition.lower():
                continue
            if want and not all(t in [x.lower() for x in a.tags] for t in want):
                continue
            if q and q.lower() not in a.haystack():
                continue
            out.append(a)
        return sorted(out, key=lambda x: (x.book, x.chapter, x.path))

    def suggest(self, query: str, *, n: int = 3, kind: str = "image") -> list[Asset]:
        """按一段文字**智能推荐配图** —— 给视频/图文创作直接调用。

        打分规则：标签命中权重最高，其次书名、篇名，最后原文与文件名。
        """
        terms = tokenize_query(query)
        if not terms:
            return []
        scored: list[tuple[float, Asset]] = []
        for a in self.assets.values():
            if not a.present or (kind and a.kind != kind):
                continue
            hay = a.haystack()
            s = 0.0
            for t in terms:
                if t in [x.lower() for x in a.tags]:
                    s += 3.0
                if t in a.book.lower():
                    s += 3.0
                if t in a.chapter.lower():
                    s += 2.0
                if t in a.passage.lower():
                    s += 1.5
                if t in hay:
                    s += 0.5
            if a.edition:
                s += 0.2                     # 有版本信息的优先（质量更高）
            if s > 0:
                scored.append((s, a))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            # 没命中任何标签/书名/原文 —— 用最新导入的兜底，但要让人知道这不是精确匹配
            pool = [a for a in self.assets.values()
                    if a.present and (not kind or a.kind == kind)]
            pool.sort(key=lambda a: a.added_at, reverse=True)
            if pool:
                print(f"  ⚠️ 「{query}」没有精确匹配，回退到最新素材（建议补标签或原文）",
                      file=sys.stderr)
            return pool[:n]
        return [a for _s, a in scored[:n]]

    # ───────── 打标 ─────────

    def update(self, asset_id: str, **fields: Any) -> Asset:
        a = self.get(asset_id)
        tags = fields.pop("tags", None)
        for k, v in fields.items():
            if hasattr(a, k) and v is not None:
                setattr(a, k, v)
        if tags is not None:
            a.tags = list(tags)
        self.save()
        return a

    def tag(self, asset_id: str, *, add: Sequence[str] = (), remove: Sequence[str] = ()) -> Asset:
        a = self.get(asset_id)
        for t in add:
            if t not in a.tags:
                a.tags.append(t)
        a.tags = [t for t in a.tags if t not in set(remove)]
        self.save()
        return a

    def all_tags(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for a in self.assets.values():
            for t in a.tags:
                c[t] = c.get(t, 0) + 1
        return dict(sorted(c.items(), key=lambda x: -x[1]))

    # ───────── 统计 ─────────

    def stats(self) -> dict:
        present = [a for a in self.assets.values() if a.present]
        by_kind: dict[str, int] = {}
        by_book: dict[str, int] = {}
        total_bytes = 0
        for a in present:
            by_kind[a.kind] = by_kind.get(a.kind, 0) + 1
            by_book[a.book or "（未分类）"] = by_book.get(a.book or "（未分类）", 0) + 1
            total_bytes += a.bytes
        return {
            "root": self.root,
            "total": len(self.assets),
            "present": len(present),
            "missing": len(self.assets) - len(present),
            "bytes": total_bytes,
            "by_kind": by_kind,
            "by_book": dict(sorted(by_book.items(), key=lambda x: -x[1])),
            "tags": self.all_tags(),
        }


# ═══════════════════════════ CLI ═══════════════════════════

def _fmt(a: Asset) -> str:
    size = f"{a.w}×{a.h}" if a.w else a.size_human
    tags = ("[" + ",".join(a.tags) + "]") if a.tags else ""
    mark = "" if a.present else " ⚠️文件缺失"
    return (f"{a.id:<13} {a.kind:<6} {size:<11} {a.display[:44]:<46} {tags}{mark}")


def cmd_init(lib: AssetLibrary, args) -> int:
    lib.save()
    print(f"✅ 素材库已初始化: {lib.root}")
    print(f"   索引: {os.path.join(lib.root, INDEX_NAME)}")
    print(f"\n直接把图片拖进这个目录也行，然后跑 rescan 收录。")
    return 0


def cmd_import(lib: AssetLibrary, args) -> int:
    meta = {
        "book": args.book or "", "chapter": args.chapter or "",
        "edition": args.edition or "", "dynasty": args.dynasty or "",
        "passage": args.passage or "", "annotator": args.annotator or "",
        "note": args.note or "", "tags": args.tag or [],
    }
    assets = lib.import_path(args.path, recursive=not args.no_recursive,
                             mode=args.mode, auto_parse=getattr(args, 'auto_parse', False),
                             **meta)
    lib.save()
    print(f"✅ 导入完成: {len(assets)} 个")
    for a in assets:
        print("   " + _fmt(a))
    return 0


def cmd_list(lib: AssetLibrary, args) -> int:
    items = lib.search(q=args.q or "", book=args.book or "", kind=args.kind or "",
                       tags=args.tag, only_present=not args.include_missing)
    if not items:
        print("没有匹配的素材。")
        return 0
    print(f"{'ID':<13} {'类型':<6} {'尺寸':<11} {'归属 / 文件':<46} 标签")
    print("-" * 104)
    for a in items:
        print(_fmt(a))
    print(f"\n共 {len(items)} 个")
    return 0


def cmd_search(lib: AssetLibrary, args) -> int:
    items = lib.search(q=" ".join(args.keywords))
    if not items:
        print("没找到。试试 rescan，或换个关键词。")
        return 0
    for a in items:
        print(_fmt(a))
    print(f"\n共 {len(items)} 个")
    return 0


def cmd_tag(lib: AssetLibrary, args) -> int:
    a = lib.tag(args.id, add=args.add or [], remove=args.remove or [])
    print(f"✅ {a.id} 标签: {a.tags}")
    return 0


def cmd_update(lib: AssetLibrary, args) -> int:
    fields = {k: getattr(args, k) for k in
              ("book", "chapter", "edition", "dynasty", "passage", "annotator", "note")
              if getattr(args, k)}
    a = lib.update(args.id, **fields)
    print(f"✅ {a.id} 已更新")
    for k in fields:
        print(f"   {k}: {getattr(a, k)}")
    return 0


def cmd_import_collection(lib: AssetLibrary, args) -> int:
    print(f"正在索引古籍合集: {lib.root}")
    r = lib.import_collection(verbose=True)
    print(f"\n✅ 完成：{r['books']} 部书 · {r['volumes']} 册 · "
          f"新增 {r['added']} · 更新 {r['updated']} · 跳过 {r['skipped']}")
    print(f"   索引共 {len(lib.assets)} 条")
    return 0


def cmd_rescan(lib: AssetLibrary, args) -> int:
    print("正在同步索引与文件系统…")
    r = lib.rescan()
    print(f"✅ 新增 {r['added']} · 缺失 {r['missing']} · 恢复 {r['restored']} · "
          f"索引共 {r['total']} · 磁盘文件 {r['on_disk']}")
    return 0


def cmd_stats(lib: AssetLibrary, args) -> int:
    s = lib.stats()
    print(f"素材库: {s['root']}")
    print(f"  总数 {s['total']}（在库 {s['present']}，缺失 {s['missing']}）"
          f"  合计 {s['bytes']/1024/1024:.1f} MB")
    print(f"  按类型: {s['by_kind']}")
    print(f"  按书目:")
    for b, c in s["by_book"].items():
        print(f"    {b:<16} {c}")
    if s["tags"]:
        print(f"  高频标签: {dict(list(s['tags'].items())[:12])}")
    return 0


def cmd_suggest(lib: AssetLibrary, args) -> int:
    items = lib.suggest(" ".join(args.query), n=args.n, kind=args.kind or "image")
    if not items:
        print("素材库是空的，或没有匹配项。")
        return 0
    print(f"为「{' '.join(args.query)}」推荐 {len(items)} 个素材：")
    for a in items:
        print("   " + _fmt(a))
        print(f"      绝对路径: {lib.abspath(a)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="asset_library — 本地素材库",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__)
    p.add_argument("--root", default="assets", help="素材根目录")
    # --root 同时允许写在子命令之后（用 SUPPRESS 避免覆盖顶层值）
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("init", help="初始化素材库",
                   parents=[common]).set_defaults(func=cmd_init)

    i = sub.add_parser("import", help="导入文件或目录", parents=[common])
    i.add_argument("path")
    i.add_argument("--book", help="书名，如 论语")
    i.add_argument("--chapter", help="篇名，如 学而")
    i.add_argument("--edition", help="版本，如 宋刻本")
    i.add_argument("--dynasty", help="朝代，如 宋")
    i.add_argument("--passage", help="对应原文片段")
    i.add_argument("--annotator", help="注家，如 朱熹")
    i.add_argument("--note")
    i.add_argument("--tag", action="append", help="标签，可重复")
    i.add_argument("--mode", choices=["copy", "move"], default="copy")
    i.add_argument("--no-recursive", action="store_true")
    i.add_argument("--auto-parse", action="store_true",
                   help="从文件名猜书名/篇名（如 论语-学而_p1.jpg）")
    i.set_defaults(func=cmd_import)

    l = sub.add_parser("list", help="列出素材", parents=[common])
    l.add_argument("--book"); l.add_argument("--kind"); l.add_argument("--q")
    l.add_argument("--tag", action="append")
    l.add_argument("--include-missing", action="store_true")
    l.set_defaults(func=cmd_list)

    s = sub.add_parser("search", help="关键词搜索", parents=[common])
    s.add_argument("keywords", nargs="+")
    s.set_defaults(func=cmd_search)

    t = sub.add_parser("tag", help="加/减标签", parents=[common])
    t.add_argument("id")
    t.add_argument("--add", action="append")
    t.add_argument("--remove", action="append")
    t.set_defaults(func=cmd_tag)

    u = sub.add_parser("update", help="修改元数据", parents=[common])
    u.add_argument("id")
    for f in ("book", "chapter", "edition", "dynasty", "passage", "annotator", "note"):
        u.add_argument(f"--{f}")
    u.set_defaults(func=cmd_update)

    sub.add_parser("rescan", help="扫描目录，同步索引", parents=[common]).set_defaults(func=cmd_rescan)
    sub.add_parser("import-collection", help="导入古籍合集（就地索引，不复制）",
                   parents=[common]).set_defaults(func=cmd_import_collection)
    sub.add_parser("stats", help="统计", parents=[common]).set_defaults(func=cmd_stats)

    g = sub.add_parser("suggest", help="按文字推荐配图", parents=[common])
    g.add_argument("query", nargs="+")
    g.add_argument("-n", type=int, default=3)
    g.add_argument("--kind", default="image")
    g.set_defaults(func=cmd_suggest)
    return p


def main(argv: Sequence[str]) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    fn = getattr(args, "func", None)
    if fn is None:
        p.print_help()
        return 2
    lib = AssetLibrary(args.root)
    try:
        return fn(lib, args)
    except AssetError as e:
        print(f"\n❌ {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
