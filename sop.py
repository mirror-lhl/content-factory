#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sop — 爆款创作标准流程（Standard Operating Procedure）

把「爆款拆解」变成**可执行、可检查**的固定流程，每次都照走。

SOP 从哪来
----------
来自三个真实爆款的公开拆解 + 官媒分析：

  · AI 老子讲《道德经》   12 作品涨粉 15 万，单条点赞 10 万+
  · 古诗解读 AI 动画      单条点赞 125 万
  · 「老祖宗回答我」      人民日报 / 光明网 / 央广网集体分析

提炼出的四段式结构：
    ① 反差钩子 → ② 情境代入 → ③ 内容输出 → ④ 留白互动

为什么要有「质检关」
--------------------
官媒在分析那个爆款时**自己划了红线**：
    同质化   「每个老祖宗都用同样的语调咆哮……把历史人物扁平化」
    反戏说   「对历史的敬重是创作底线，别让『戏说』变成『胡说』」
    肖像权   「AI 换脸可能侵犯死者肖像权，需标注 AI 合成」
    触发词   微信/抖音/小红书都明文限制「风水、运势」等噱头

这些不是建议，是**能不能发出去的判据**。所以把它们写成自动检查，
每条内容出厂前跑一遍 —— 这一步才是 SOP 的价值所在。

用法
----
    python3 sop.py show                        # 看完整流程
    python3 sop.py check <项目id>               # 对某个项目跑质检
    python3 sop.py check <项目id> --json        # 机器可读
    python3 sop.py audit                        # 全库体检（含同质化）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import content_ops as co


# ═══════════════════════════ SOP 定义 ═══════════════════════════

@dataclass
class Step:
    key: str
    name: str
    goal: str                    # 这一步要达成什么
    who: str                     # tool / human / both
    how: list[str] = field(default_factory=list)      # 做法（来自爆款拆解）
    need: list[str] = field(default_factory=list)     # 需要什么
    out: list[str] = field(default_factory=list)      # 产出什么
    pass_when: list[str] = field(default_factory=list)  # 通过标准


@dataclass
class Sop:
    key: str
    name: str
    desc: str
    source: str
    steps: list[Step]


BORROW_SOP = Sop(
    key="borrow", name="爆款借鉴四段式",
    desc="反差钩子 → 情境代入 → 内容输出 → 留白互动，外加出厂质检",
    source="三个真实爆款拆解 + 官媒分析（见 sop.py 头部说明）",
    steps=[
        Step(
            key="pick", name="① 定选题", who="tool+human",
            goal="从爆款结构库选一个结构，配上你真实有的素材",
            how=[
                "结构从三个爆款里挑：反差型 / 实测型 / 文献型",
                "素材必须是你真有的（7911 张古籍页 + 你的 AI 实战）",
                "选题引擎会给评分和「需要你提供什么」",
            ],
            need=["内容领域", "目标平台"],
            out=["选题角度", "选题结构标记"],
            pass_when=["角度已定", "素材库里能找到对应素材"],
        ),
        Step(
            key="hook", name="② 反差钩子", who="human",
            goal="前 3 秒决定完播率 —— 用反差抓住人",
            how=[
                "爆款机制：反差感 + 从『知识灌输』转向『价值共鸣』",
                "公式：所有人都以为 X，我实测发现 Y",
                "禁止：平铺直叙的介绍式开头",
            ],
            need=["你的一句话钩子（不是工具生成）"],
            out=["钩子文案"],
            pass_when=["钩子含反差或悬念", "不出现触发词"],
        ),
        Step(
            key="body", name="③ 内容输出", who="human",
            goal="这是灵魂 —— 真材实料的解读，区别于搬运号",
            how=[
                "官媒推崇的『苏州博物馆模式』：严谨调研 + AI 技术",
                "你的差异化：拿 7911 页真实古籍实测过",
                "至少一个可验证的细节（出处 / 数据 / 原页）",
            ],
            need=["你的真实经历或实测数据", "对应古籍的书名版本", "原文或原页"],
            out=["正文 / 脚本"],
            pass_when=["有至少一个具体案例或数字", "有文献依据（书名/版本）"],
        ),
        Step(
            key="close", name="④ 留白互动", who="human",
            goal="结尾留一句让人想接话的话，拉互动率",
            how=[
                "爆款机制：参与感 ——『在一声声你回答我中增强受众参与』",
                "做法：抛一个开放问题，或给一个反差结论",
                "禁止：『点赞关注』这类硬广式收尾",
            ],
            need=["你的收尾句"],
            out=["结尾文案"],
            pass_when=["结尾是开放问题或反差结论"],
        ),
        Step(
            key="produce", name="⑤ 出成品", who="tool",
            goal="一键产出图文 + 多平台封面 + 双画幅视频",
            how=["选视频模板（国学内容用「国风典雅」）", "配图优先用 brief 指定的素材"],
            need=["已填好的 brief"],
            out=["article.html", "covers/", "video_*.mp4", "发布清单.md"],
            pass_when=["sop.py check 全部通过"],
        ),
        Step(
            key="qa", name="⑥ 出厂质检", who="tool",
            goal="**能不能发出去** —— 把官媒和平台的边界做成自动检查",
            how=[
                "触发词扫描：算命/风水/转运 等会直接触发审核",
                "同质化检查：最近几条是不是同一个结构、同一个句式",
                "依据检查：有没有文献出处，避免『胡说』",
                "AI 标注：AI 生成的图像/视频需要标注",
            ],
            need=["完整成品"],
            out=["质检报告"],
            pass_when=["无硬红线命中", "同质化未超标", "有文献依据"],
        ),
    ],
)


SOP = BORROW_SOP


# ═══════════════════════════ 质检规则 ═══════════════════════════

#: 🔴 硬红线 —— 命中的是**服务承诺**，平台明文打击，必须改
RED_WORDS = [
    "算命", "批八字", "八字算命", "看相", "面相", "手相", "摸骨",
    "转运", "改运", "改命", "消灾", "化解", "祈福", "开光", "法事",
    "招财", "辟邪", "驱邪", "镇宅", "姻缘", "合婚", "断吉凶",
    "大师", "玄学大师", "国学大师", "风水大师", "命理师", "道长",
    "免费测算", "在线测算", "付费咨询", "一对一咨询",
]

#: 🟡 敏感词 —— 不一定违规，但**容易触发审核**，建议换中性表述
AMBER_WORDS = [
    "风水", "运势", "财运", "桃花运", "吉凶", "宜忌", "黄道吉日",
    "算命先生", "占卜", "起卦", "断卦", "排盘", "看日子",
    "紫微斗数", "六爻", "奇门", "梅花易数", "四柱",
]

#: 🟢 中性学术表述 —— 建议用这些替换敏感词
NEUTRAL_MAP = {
    "算命": "命理文献研究", "批八字": "四柱文献", "看相": "相术文献",
    "面相": "相术文献", "风水": "堪舆文献", "运势": "传统术数理论",
    "转运": "（不出现）", "改命": "（不出现）", "占卜": "卜筮文献",
    "起卦": "卦爻结构", "排盘": "星命术式结构", "黄道吉日": "择日文献",
    "大师": "研究者", "紫微斗数": "星命术文献",
}

#: 硬广式收尾（拉低观感）
HARD_SELL = ["点赞关注", "关注我", "求关注", "点击下方", "私信我", "加我微信", "扫码"]


@dataclass
class Issue:
    level: str          # red / amber / info
    where: str          # 位置：标题 / 钩子 / 正文 / 要点
    word: str
    advice: str


@dataclass
class Report:
    pid: str
    title: str
    issues: list[Issue] = field(default_factory=list)
    structure: dict[str, bool] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def red(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "red"]

    @property
    def amber(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "amber"]

    @property
    def passed(self) -> bool:
        return not self.red and all(self.structure.values())


def _scan(text: str, where: str) -> list[Issue]:
    out: list[Issue] = []
    if not text:
        return out
    for w in RED_WORDS:
        if w in text:
            out.append(Issue("red", where, w,
                             f"硬红线。建议改为「{NEUTRAL_MAP.get(w, '中性学术表述')}」"
                             f"或直接删掉"))
    for w in AMBER_WORDS:
        if w in text:
            out.append(Issue("amber", where, w,
                             f"易触发审核。建议改为「{NEUTRAL_MAP.get(w, '中性表述')}」"))
    return out


def check_project(proj: co.ContentProject, *,
                  ws: Optional[co.Workspace] = None,
                  history_n: int = 5) -> Report:
    """对一个内容项目跑完整质检。"""
    r = Report(pid=proj.id, title=proj.title or proj.angle or "(无标题)")
    b = proj.brief

    # ---- ① 触发词扫描（标题 / 钩子 / 要点 / 正文 / 备注）----
    r.issues += _scan(proj.title, "标题")
    r.issues += _scan(proj.angle, "选题角度")
    r.issues += _scan(b.hook, "钩子")
    for i, kp in enumerate(b.key_points, 1):
        r.issues += _scan(kp, f"要点{i}")
    r.issues += _scan(b.personal, "正文")
    r.issues += _scan(b.notes, "备注")
    # 平台标签也要查
    for t in b.assets:
        r.issues += _scan(os.path.basename(t), "素材文件名")

    # ---- ② 硬广收尾 ----
    tail = (b.personal or "")[-80:] + (b.notes or "")[-80:]
    for w in HARD_SELL:
        if w in tail:
            r.issues.append(Issue("amber", "结尾", w,
                                  "硬广式收尾会拉低观感，建议改成开放问题或反差结论"))

    # ---- ③ 结构完整度 ----
    r.structure = {
        "选题已定": bool(proj.angle),
        "钩子已写": len(b.hook.strip()) >= 8,
        "内容有料": bool(b.key_points) and len(b.personal.strip()) >= 50,
        "文献依据": bool(b.assets),
        "有收尾": len((b.personal or "").strip()) >= 50,
    }

    # ---- ④ 同质化检查（官媒警告的「扁平化」）----
    if ws is not None:
        try:
            others = [p for p in ws.all() if p.id != proj.id]
            others.sort(key=lambda p: p.created_at, reverse=True)
            recent = others[:history_n]
            same_pattern = [p for p in recent
                            if p.pattern and p.pattern == proj.pattern]
            if proj.pattern and len(same_pattern) >= 2:
                r.issues.append(Issue(
                    "amber", "同质化", proj.pattern,
                    f"最近 {len(recent)} 条里有 {len(same_pattern)} 条用了同一个结构"
                    f"「{proj.pattern}」。官媒警告过『每个都用同样语调』会审美疲劳，"
                    f"建议换个结构"))
            # 开头句式重复
            my_head = (b.hook or "")[:6]
            if my_head:
                dup = [p for p in recent if (p.brief.hook or "")[:6] == my_head]
                if dup:
                    r.issues.append(Issue(
                        "info", "同质化", my_head,
                        f"最近有 {len(dup)} 条开头前 6 字相同，建议换个切入"))
            r.stats["recent_patterns"] = [p.pattern or "未标" for p in recent]
        except Exception:
            pass

    # ---- ⑤ 统计 ----
    r.stats.update({
        "标题字数": len(proj.title or ""),
        "钩子字数": len(b.hook or ""),
        "要点条数": len(b.key_points),
        "正文字数": len(b.personal or ""),
        "素材数": len(b.assets),
        "结构": proj.pattern or "未标",
    })
    return r


def audit(ws: co.Workspace, *, history_n: int = 8) -> dict:
    """全库体检 —— 看整体有没有同质化倾向。"""
    pros = sorted(ws.all(), key=lambda p: p.created_at, reverse=True)
    pats: dict[str, int] = {}
    heads: dict[str, int] = {}
    for p in pros:
        pats[p.pattern or "未标"] = pats.get(p.pattern or "未标", 0) + 1
        h = (p.brief.hook or "")[:6]
        if h:
            heads[h] = heads.get(h, 0) + 1
    dup_pat = {k: v for k, v in pats.items() if v >= 3 and k != "未标"}
    dup_head = {k: v for k, v in heads.items() if v >= 2}
    return {"total": len(pros), "patterns": pats,
            "dup_patterns": dup_pat, "dup_heads": dup_head}



# ═══════════════════════════ 流程驱动引擎 ═══════════════════════════
#
# 关键区别：
#   check_project() —— 事后检查器（产完了看合不合格）
#   submit_step()   —— 流程驱动器（上一步不过，不能进下一步）
#
# 产品要的是后者：选题时就建立流程骨架，创作时逐步引导，
# 每步当场校验，不过就卡住并告诉你差什么。

#: 反差/悬念标记 —— 钩子必须有这些，否则撑不住前 3 秒
_CONTRAST_MARKERS = [
    "但", "其实", "居然", "竟然", "原来", "以为", "发现", "错了", "不是",
    "反而", "真相", "没想到", "结果", "却", "全错", "翻车", "失败",
]
_QUESTION_MARKERS = ["？", "?", "吗", "呢"]
#: 具体的细节标记（数字 / 书名号 / 版本词）
_DETAIL_RE = re.compile(r"\d|《|》|卷|册|页|版|刻本|抄本")


def _step_state(proj: co.ContentProject, key: str) -> dict:
    return (proj.sop_steps or {}).get(key) or {"done": False, "data": {},
                                               "checked_at": "", "issues": []}


def init_sop(proj: co.ContentProject, *, backfill: bool = True) -> None:
    """给项目初始化 SOP 骨架 —— 选题建项目时调用。

    backfill=True 时，对**已有内容**用同样的校验规则回溯推断进度。
    否则 SOP 系统上线前建的老项目会一律显示 0/6，看着像没做。
    回溯是顺序的：遇到第一个不过的就停 —— 因为 SOP 本身就是串行的。
    """
    if not proj.sop_steps:
        proj.sop_steps = {st.key: {"done": False, "data": {},
                                   "checked_at": "", "issues": []}
                          for st in SOP.steps}
        if backfill:
            for st in SOP.steps:
                fn = VALIDATORS.get(st.key)
                if fn is None:
                    break
                try:
                    issues = fn(proj, {})
                except Exception:
                    break
                if issues:
                    break
                proj.sop_steps[st.key]["done"] = True
                proj.sop_steps[st.key]["checked_at"] = "回溯推断"
                proj.sop_steps[st.key]["data"] = {"note": "SOP 上线前已存在的步骤"}


def next_key(proj: co.ContentProject) -> Optional[str]:
    """当前该做哪一步（第一个未完成的）。全部完成返回 None。"""
    init_sop(proj)
    for st in SOP.steps:
        if not _step_state(proj, st.key)["done"]:
            return st.key
    return None


# ─────────── 各步的校验规则 ───────────

def validate_pick(proj: co.ContentProject, data: dict) -> list[str]:
    issues: list[str] = []
    if not (proj.angle or data.get("angle")):
        issues.append("还没定选题角度")
    # 素材 = 文献依据。没有素材的选题要明确豁免理由
    if not proj.brief.assets and not data.get("no_asset_reason"):
        issues.append(
            "没有匹配素材 —— 这一步要求「有文献依据」。"
            "从素材库选一页古籍原图，或写明为什么这条不需要（如纯技术测评）")
    for w in RED_WORDS:
        if w in (proj.angle or ""):
            issues.append(f"选题里命中硬红线「{w}」，建议改为"
                          f"「{NEUTRAL_MAP.get(w, '中性学术表述')}」")
    return issues


def validate_hook(proj: co.ContentProject, data: dict) -> list[str]:
    issues: list[str] = []
    hook = (data.get("hook") or proj.brief.hook or "").strip()
    if len(hook) < 8:
        issues.append("钩子太短（少于 8 字）—— 前 3 秒撑不住")
    if not any(m in hook for m in _CONTRAST_MARKERS):
        issues.append("钩子缺少反差或悬念。套这个公式："
                      "「所有人都以为 X，我实测发现 Y」")
    for w in RED_WORDS:
        if w in hook:
            issues.append(f"命中硬红线「{w}」→ 建议「{NEUTRAL_MAP.get(w, '中性表述')}」")
    for w in AMBER_WORDS:
        if w in hook:
            issues.append(f"易触发审核「{w}」→ 建议「{NEUTRAL_MAP.get(w, '中性表述')}」")
    return issues


def validate_body(proj: co.ContentProject, data: dict) -> list[str]:
    issues: list[str] = []
    kps = data.get("key_points") or proj.brief.key_points or []
    body = (data.get("personal") or proj.brief.personal or "").strip()
    if len([k for k in kps if k.strip()]) < 2:
        issues.append("要点少于 2 条 —— 内容撑不起来")
    if len(body) < 80:
        issues.append(f"正文只有 {len(body)} 字（建议 ≥80）—— 这是内容的灵魂，不能空")
    if body and not _DETAIL_RE.search(body):
        issues.append("正文里没有具体细节（数字／书名／版本）—— "
                      "官媒推崇的做法是「严谨调研 + AI 技术」，要有可验证的东西")
    if not (proj.brief.assets or data.get("assets")):
        issues.append("没有文献依据（古籍原页）—— 这条是你区别于搬运号的地方")
    for w in RED_WORDS:
        if w in body or any(w in k for k in kps):
            issues.append(f"命中硬红线「{w}」")
    return issues


def validate_close(proj: co.ContentProject, data: dict) -> list[str]:
    issues: list[str] = []
    close = (data.get("close") or "").strip()
    if len(close) < 6:
        issues.append("还没写收尾句 —— 结尾要留一句让人想接话的话")
    for w in HARD_SELL:
        if w in close:
            issues.append(f"「{w}」是硬广式收尾，会拉低观感。"
                          f"改成开放问题或反差结论")
    if close and not (any(m in close for m in _QUESTION_MARKERS)
                      or any(m in close for m in _CONTRAST_MARKERS)):
        issues.append("收尾偏平。建议抛一个开放问题，或给一个反差结论")
    return issues


def validate_produce(proj: co.ContentProject, data: dict) -> list[str]:
    issues: list[str] = []
    if not (proj.article or proj.video):
        issues.append("还没产出任何成品 —— 先跑 pipeline produce")
    return issues


def validate_qa(proj: co.ContentProject, data: dict) -> list[str]:
    r = check_project(proj)
    return [f"[{i.where}] {i.word}：{i.advice}" for i in r.red] or []


VALIDATORS = {
    "pick": validate_pick, "hook": validate_hook, "body": validate_body,
    "close": validate_close, "produce": validate_produce, "qa": validate_qa,
}


# ─────────── 提交与推进 ───────────

def submit_step(proj: co.ContentProject, key: str, data: dict) -> tuple[bool, list[str]]:
    """提交某一步的产出：写入项目 → 校验 → **通过才标记完成**。

    这是流程驱动的核心 —— 不合格就卡在这一步，并告诉你差什么。
    """
    init_sop(proj)
    fn = VALIDATORS.get(key)
    if fn is None:
        return False, [f"未知步骤 {key!r}"]

    # 把提交的数据落到项目上（这样后续步骤能用）
    if data.get("angle"):
        proj.angle = data["angle"]
        if not proj.title:
            proj.title = data["angle"]
    if data.get("hook"):
        proj.brief.hook = data["hook"]
    if data.get("key_points"):
        proj.brief.key_points = [k.strip() for k in data["key_points"] if k.strip()]
    if data.get("personal"):
        proj.brief.personal = data["personal"]
    if data.get("assets"):
        proj.brief.assets = data["assets"]
    if data.get("close"):
        # 收尾句接到正文末尾
        body = proj.brief.personal or ""
        marker = "\n\n" + data["close"]
        if data["close"] not in body:
            proj.brief.personal = (body + marker).strip()

    issues = fn(proj, data)
    st = proj.sop_steps[key]
    st["data"] = {k: v for k, v in data.items() if v}
    st["checked_at"] = time.strftime("%F %T")
    st["issues"] = issues
    st["done"] = not issues
    return (not issues), issues


def progress(proj: co.ContentProject) -> dict:
    init_sop(proj)
    steps = []
    for st in SOP.steps:
        s = _step_state(proj, st.key)
        steps.append({"key": st.key, "name": st.name, "done": s["done"],
                      "issues": s.get("issues") or []})
    done = sum(1 for x in steps if x["done"])
    cur = next_key(proj)
    for x in steps:
        x["status"] = ("done" if x["done"]
                       else ("current" if x["key"] == cur else "todo"))
    return {"steps": steps, "done": done, "total": len(steps),
            "percent": round(done / len(steps) * 100),
            "current": cur, "can_produce": cur in (None, "produce", "qa")}


def wizard(proj: co.ContentProject) -> dict:
    """给界面的向导数据：当前该做哪步、要什么、怎么填、卡在哪。"""
    pg = progress(proj)
    cur = pg["current"]
    step_def = next((s for s in SOP.steps if s.key == cur), None)
    cur_state = _step_state(proj, cur) if cur else {"issues": []}
    return {
        **pg,
        "current_def": (None if step_def is None else {
            "key": step_def.key, "name": step_def.name, "goal": step_def.goal,
            "who": step_def.who, "how": step_def.how, "need": step_def.need,
            "pass_when": step_def.pass_when,
        }),
        "blocked_by": cur_state.get("issues") or [],
        "draft": {
            "angle": proj.angle or "",
            "hook": proj.brief.hook or "",
            "key_points": proj.brief.key_points or [],
            "personal": proj.brief.personal or "",
            "assets": proj.brief.assets or [],
        },
    }



def _parse_md(text: str) -> dict:
    """解析 brief 式 markdown（## 钩子 / ## 要点 / ## 经历 / ## 素材 / ## 收尾）。"""
    data: dict = {}
    parts = re.split(r"^##\s*(钩子|要点|经历|正文|素材|收尾|备注)\s*$", text, flags=re.M)
    if len(parts) <= 1:
        data["personal"] = text.strip()
        return data
    for i in range(1, len(parts), 2):
        k, v = parts[i], parts[i + 1].strip()
        if k == "钩子":
            data["hook"] = v
        elif k == "要点":
            data["key_points"] = [l.strip("-• \t") for l in v.splitlines() if l.strip()]
        elif k in ("经历", "正文"):
            data["personal"] = v
        elif k == "素材":
            data["assets"] = [l.strip() for l in v.splitlines() if l.strip()]
        elif k == "收尾":
            data["close"] = v
        elif k == "备注":
            data["notes"] = v
    return data


def cmd_next(args) -> int:
    ws = co.Workspace(args.ws)
    proj = ws.load(args.id)
    w = wizard(proj)

    print(f"\n{'═' * 60}")
    print(f"  创作流程 · {proj.title or proj.angle or proj.id}")
    print(f"  进度 {w['done']}/{w['total']}（{w['percent']}%）")
    print(f"{'═' * 60}\n")

    for st in w["steps"]:
        icon = {"done": "✅", "current": "▶", "todo": "○"}[st["status"]]
        print(f"  {icon} {st['name']}")
        if st["status"] == "current" and st["issues"]:
            for i in st["issues"]:
                print(f"        ⚠️ {i}")

    d = w["current_def"]
    if d is None:
        print("\n  🎉 全部完成 —— 可以生产并准备发布了\n")
        print(f"  python3 pipeline.py produce {proj.id}\n")
        return 0

    who = {"tool": "🤖 工具", "human": "🧑 你来", "tool+human": "🤖🧑 协作"}[d["who"]]
    print(f"\n{'─' * 60}")
    print(f"  ▶ 现在做这一步：{d['name']}   [{who}]")
    print(f"    目标：{d['goal']}\n")
    for h in d["how"]:
        print(f"      · {h}")
    if w["blocked_by"]:
        print(f"\n    ⛔ 卡在这里：")
        for i in w["blocked_by"]:
            print(f"       · {i}")
    print(f"\n    提交：python3 sop.py submit {proj.id} --step {d['key']} --file brief.md")
    print(f"{'─' * 60}\n")
    return 0


def cmd_submit(args) -> int:
    ws = co.Workspace(args.ws)
    proj = ws.load(args.id)
    step = args.step or next_key(proj)
    if not step:
        print("✅ 所有步骤都已完成")
        return 0

    data: dict = {}
    if args.file:
        data = _parse_md(open(args.file, encoding="utf-8").read())
    if args.text:
        data[{"hook": "hook", "close": "close", "angle": "angle"}.get(step, "personal")] = args.text
    if args.angle:
        data["angle"] = args.angle
    if args.no_asset_reason:
        data["no_asset_reason"] = args.no_asset_reason

    ok, issues = submit_step(proj, step, data)
    ws.save(proj)

    name = next((s.name for s in SOP.steps if s.key == step), step)
    if ok:
        print(f"\n✅ 「{name}」通过")
        nxt = next_key(proj)
        if nxt:
            nname = next((s.name for s in SOP.steps if s.key == nxt), nxt)
            print(f"   下一步：{nname}")
            print(f"   看引导：python3 sop.py next {proj.id}")
        else:
            print("\n🎉 全部步骤完成 —— 可以生产了")
            print(f"   python3 pipeline.py produce {proj.id}")
        return 0
    print(f"\n⛔ 「{name}」未通过 —— 卡在这一步，差这些：")
    for i in issues:
        print(f"   · {i}")
    print()
    return 1


# ═══════════════════════════ CLI ═══════════════════════════

def _icon(level: str) -> str:
    return {"red": "🔴", "amber": "🟡", "info": "🔵"}.get(level, "·")


def cmd_show(args) -> int:
    print(f"\n{'═' * 62}")
    print(f"  SOP · {SOP.name}")
    print(f"  {SOP.desc}")
    print(f"  来源：{SOP.source}")
    print(f"{'═' * 62}\n")
    for i, s in enumerate(SOP.steps, 1):
        who = {"tool": "🤖 工具", "human": "🧑 你来", "tool+human": "🤖🧑 协作"}[s.who]
        print(f"  {s.name}   [{who}]")
        print(f"    目标：{s.goal}")
        for h in s.how:
            print(f"      · {h}")
        if s.need:
            print(f"    需要：{' / '.join(s.need)}")
        if s.pass_when:
            print(f"    通过：{' / '.join(s.pass_when)}")
        print()
    print("─" * 62)
    print("  跑质检：python3 sop.py check <项目id>")
    print()
    return 0


def cmd_check(args) -> int:
    ws = co.Workspace(args.ws)
    try:
        proj = ws.load(args.id)
    except co.OpsError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    r = check_project(proj, ws=ws)

    if args.json:
        print(json.dumps({
            "pid": r.pid, "title": r.title, "passed": r.passed,
            "structure": r.structure, "stats": r.stats,
            "issues": [{"level": i.level, "where": i.where,
                        "word": i.word, "advice": i.advice} for i in r.issues],
        }, ensure_ascii=False, indent=2))
        return 0 if r.passed else 1

    print(f"\n质检报告 · {r.title}")
    print(f"项目 {r.pid} · 结构 {r.stats.get('结构','未标')}")
    print("─" * 62)

    print("\n【结构完整度】")
    for k, v in r.structure.items():
        print(f"  {'✅' if v else '❌'} {k}")

    if r.red:
        print(f"\n🔴 硬红线 {len(r.red)} 处 —— **必须改，否则发不出去**")
        for i in r.red:
            print(f"  · [{i.where}] 命中「{i.word}」")
            print(f"      → {i.advice}")
    if r.amber:
        print(f"\n🟡 易触发审核 {len(r.amber)} 处 —— 建议改")
        for i in r.amber:
            print(f"  · [{i.where}] 「{i.word}」→ {i.advice}")
    if not r.red and not r.amber:
        print("\n✅ 未命中任何触发词")

    print(f"\n【统计】{r.stats}")
    print("\n" + "═" * 62)
    if r.passed:
        print("  ✅ 质检通过 —— 可以生产并准备发布")
    else:
        miss = [k for k, v in r.structure.items() if not v]
        if r.red:
            print(f"  🔴 未通过：{len(r.red)} 处硬红线")
        if miss:
            print(f"  ❌ 未通过：结构缺 {miss}")
    print("═" * 62 + "\n")
    return 0 if r.passed else 1


def cmd_audit(args) -> int:
    ws = co.Workspace(args.ws)
    a = audit(ws)
    print(f"\n全库体检 · 共 {a['total']} 条内容")
    print("─" * 50)
    print("\n【结构分布】")
    for k, v in sorted(a["patterns"].items(), key=lambda x: -x[1]):
        bar = "█" * v
        print(f"  {k:<14} {v:>3}  {bar}")
    if a["dup_patterns"]:
        print("\n🟡 同质化提示（某结构用了 3 次以上）：")
        for k, v in a["dup_patterns"].items():
            print(f"  · 「{k}」用了 {v} 次 —— 官媒警告过『都在用同一套语调』会审美疲劳")
    else:
        print("\n✅ 结构分布均衡，无明显同质化")
    if a["dup_heads"]:
        print("\n🔵 开头句式重复：")
        for k, v in a["dup_heads"].items():
            print(f"  · 「{k}…」出现 {v} 次")
    print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="sop — 爆款创作标准流程",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__)
    p.add_argument("--ws", default="workspace")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("show", help="打印完整 SOP 流程").set_defaults(func=cmd_show)
    c = sub.add_parser("check", help="对项目跑质检")
    c.add_argument("id")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_check)
    sub.add_parser("audit", help="全库体检（同质化）").set_defaults(func=cmd_audit)

    nx = sub.add_parser("next", help="看当前该做哪一步（流程向导）")
    nx.add_argument("id")
    nx.set_defaults(func=cmd_next)

    sb = sub.add_parser("submit", help="提交某一步的产出（校验通过才推进）")
    sb.add_argument("id")
    sb.add_argument("--step", choices=[s.key for s in SOP.steps])
    sb.add_argument("--file", help="brief 式 markdown")
    sb.add_argument("--text", help="单字段步骤直接给文本")
    sb.add_argument("--angle", help="选题角度")
    sb.add_argument("--no-asset-reason", help="无素材的豁免理由")
    sb.set_defaults(func=cmd_submit)
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
