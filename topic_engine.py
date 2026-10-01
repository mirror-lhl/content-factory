#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
topic_engine — 选题引擎

不是"随机标题生成器"，而是三者相乘：

    你手里的素材  ×  验证过的选题结构  ×  已发布内容的数据反馈

核心设计
--------
1. **素材驱动** —— 从素材库盘点你实际有什么（哪部书、哪篇、哪段原文），
   再基于它生成选题。产出的是「你真能做的」，不是空想标题。
2. **结构库** —— 6 种已被验证的选题结构（反差/溯源/误读/实测/翻车/方法），
   每种都标明它靠什么立住、需要你补什么。
3. **数据反馈** —— 读已发布内容的表现，给同类结构加权。
4. **明码标价** —— 每条选题都写清**需要你提供什么**。
   引擎不替你编经历，编出来的东西平台会判"非真人创作"。

用法
----
    # 盘点你手里有什么
    python3 topic_engine.py inventory

    # 生成候选选题
    python3 topic_engine.py gen --niche "AI×国学" -n 12

    # 按平台偏好生成
    python3 topic_engine.py gen --niche "AI×国学" --platforms xiaohongshu,zhihu

    # 把第 3 条直接建成 content_ops 项目（闭环）
    python3 topic_engine.py pick --from candidates.json -i 3

    # 看每种结构的历史表现
    python3 topic_engine.py perf
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Optional, Sequence

import content_ops as co
from asset_library import AssetLibrary



# ═══════════════════════════ 书目知识库 ═══════════════════════════
#
# 选题引擎要基于**真实素材**生成选题，就必须知道每本书是什么。
# 这里为 21 部术数/医经/易学古籍建立知识卡：
#   kind      书的性质          dynasty/author  成书信息
#   value     文献价值          angles          可做的角度
#   safe      平台安全的表述     risky           会触发审核的表述
#
# ⚠️ 表述安全很关键：平台审核看关键词，不看动机。
#    "算命/风水/转运" 这类词会直接触发审核，哪怕你讲的是纯文献。

@dataclass
class BookCard:
    title: str
    kind: str = ""            # 类别
    dynasty: str = ""
    author: str = ""
    value: str = ""           # 文献价值
    angles: list[str] = field(default_factory=list)
    safe_terms: list[str] = field(default_factory=list)
    risky_terms: list[str] = field(default_factory=list)
    line: str = ""            # 所属内容线：medical / yijing / shushu / other


BOOK_KB: dict[str, BookCard] = {
    # ── 术数 · 命理 ──
    "三命通会": BookCard(
        "三命通会", "命理类书", "明", "万民英",
        "《四库全书》子部术数类收录；把唐宋以来的命理文献汇编成体系，是类书式的集大成之作",
        ["类书编纂方法", "四库馆臣对术数书的取舍标准", "明代出版业与畅销书", "五行理论的体系化过程"],
        ["命理文献", "五行理论", "类书", "四库全书", "文献整理"],
        ["算命", "批八字", "改运", "看命"], "shushu"),
    "河洛理数": BookCard(
        "河洛理数", "易数", "宋（传）", "邵雍（传）",
        "以河图洛书数理推演，是宋代象数易学的代表；把卦爻与数字对应起来",
        ["象数易学的数学结构", "河图洛书的数理逻辑", "邵雍的先天学"],
        ["象数", "易学", "数理结构", "文献"],
        ["占卜", "起卦算命"], "yijing"),
    "新鋟希夷陳先生紫微斗数全書": BookCard(
        "新鋟希夷陳先生紫微斗数全書", "星命术", "明刊（托名宋陈抟）",
        "题陈抟撰（实为明代托名）",
        "紫微斗数体系的早期刊本；托名现象是古籍版本学的好案例",
        ["古籍托名现象", "星命术的星官体系", "明代书商如何造伪书"],
        ["星命术文献", "古籍托名", "版本学", "星官体系"],
        ["紫微斗数算命", "排盘", "看命"], "shushu"),

    # ── 术数 · 占卜 ──
    "卜筮全書": BookCard(
        "卜筮全書", "占卜类书", "明",
        "", "明代汇编的卜筮类书，保留了大量早期占法资料",
        ["明代占卜文献的汇编体例", "早期占法的形态"],
        ["占卜文献", "术数文献", "汇编体例"],
        ["占卜教学", "起卦"], "shushu"),
    "増刪卜易": BookCard(
        "増刪卜易", "六爻", "清", "野鹤老人",
        "清代六爻代表作，对前代占法做了删订，体现清代术数的实用化倾向",
        ["清代术数的实用化转向", "对前代文献的删订方法"],
        ["六爻文献", "术数史", "文献删订"],
        ["六爻教学", "断卦", "占卜"], "shushu"),
    "増広玉匣記通書": BookCard(
        "増広玉匣記通書", "通书 / 择日", "明清",
        "", "民间日用类书，把择日、宜忌、杂占汇编成日常参考书",
        ["民间日用类书的社会史价值", "明清日常生活的知识体系"],
        ["日用类书", "通书", "民俗文献", "社会史"],
        ["择日", "黄历", "宜忌"], "shushu"),
    "欽定協紀弁方書": BookCard(
        "欽定協紀弁方書", "历法 / 择日（官修）", "清·乾隆", "允禄等奉敕编",
        "**《四库全书》收录的官修择日书**，乾隆命人系统整理历代择日学说并统一体例，是官方对民间术数的规范化",
        ["官方如何规范民间术数", "四库全书的编纂方法", "清代历法与国家治理"],
        ["官修文献", "四库全书", "历法", "国家治理", "文献规范"],
        ["择日", "看日子", "黄道吉日"], "shushu"),

    # ── 术数 · 相术 ──
    "神相全編": BookCard(
        "神相全編", "相术汇编", "明",
        "", "明代相术的集大成汇编，把历代相法资料系统整理",
        ["相术文献的汇编过程", "古人如何用身体特征做分类"],
        ["相术文献", "身体分类学", "汇编"],
        ["看相", "面相", "识人术"], "shushu"),
    "新刊校正増釈合併麻衣先生人相編": BookCard(
        "新刊校正増釈合併麻衣先生人相編", "相术（麻衣系）", "明刊",
        "题麻衣道者（托名）",
        "麻衣相法系统的明代刊本，含增释与合并，可见书坊的编辑加工",
        ["书坊的编辑加工痕迹", "托名麻衣的文献系统"],
        ["相术文献", "书坊刻书", "版本"],
        ["麻衣神相", "看相"], "shushu"),
    "新刻袁柳庄先生秘伝相法": BookCard(
        "新刻袁柳庄先生秘伝相法", "相术（袁柳庄系）", "明",
        "题袁珙（柳庄）",
        "袁柳庄相法系统的刊本；袁珙是明初真实历史人物，可考其生平与传说化过程",
        ["历史人物如何被传说化", "明代相术的流派分化"],
        ["相术文献", "人物考据", "流派"],
        ["袁柳庄相法教学", "看相"], "shushu"),

    # ── 术数 · 堪舆 ──
    "葬書真本": BookCard(
        "葬書真本", "堪舆", "晋（托名）", "郭璞（托名）",
        "托名郭璞的堪舆经典；'真本'二字本身就说明当时已有多种版本竞争",
        ["托名与版本竞争", "堪舆文献的经典化过程"],
        ["堪舆文献", "托名", "版本学"],
        ["风水", "看阴宅", "点穴"], "shushu"),

    # ── 易学 ──
    "周易兼義": BookCard(
        "周易兼義", "易学（义疏）", "唐", "孔颖达等奉敕撰",
        "**《五经正义》之一**，唐代官方对《周易》的标准化注疏，王弼注+韩康伯注+孔颖达疏合刊",
        ["唐代官方经学标准化", "注疏体例的形成", "王弼易学的影响"],
        ["经学", "注疏", "易学", "唐代文献", "标准化"],
        [], "yijing"),
    "伊川易伝": BookCard(
        "伊川易伝", "易学（义理）", "宋", "程颐",
        "宋代义理易学的代表作；程颐以理释易，与邵雍的象数路线形成对立",
        ["义理派 vs 象数派", "以理释易的方法", "宋代理学与易学"],
        ["易学", "义理", "理学", "宋学"],
        [], "yijing"),
    "周易参同契": BookCard(
        "周易参同契", "丹道 / 易", "东汉", "魏伯阳",
        "**被称为'万古丹经王'**，把易理、黄老、炉火三者结合；被后世视为古代化学的先声",
        ["丹道与易理的结合", "古代化学史", "经典的层累形成"],
        ["丹道文献", "化学史", "易学", "科技史"],
        [], "yijing"),

    # ── 丹道 ──
    "悟真篇三註": BookCard(
        "悟真篇三註", "丹道", "宋", "张伯端",
        "《悟真篇》与三家注合刊；内丹学的核心文本，注本差异本身就是研究题目",
        ["注本的诠释分歧", "内丹学的术语体系"],
        ["丹道文献", "注释学", "术语体系"],
        [], "other"),

    # ── 医经 ──
    "重広補注黄帝内経素問": BookCard(
        "重広補注黄帝内経素問", "医经", "唐·王冰注 / 宋·林亿等校正",
        "王冰注，林亿等重广补注",
        "**中医理论奠基之作**的权威注本；王冰次注与宋人校正构成完整的文本层累",
        ["中医理论体系", "四时养生", "文本层累与校勘"],
        ["中医", "养生", "医经", "校勘", "四时"],
        [], "medical"),
    "新刊黄帝内経霊枢": BookCard(
        "新刊黄帝内経霊枢", "医经", "",
        "", "《灵枢》与《素问》并称《内经》；偏重针灸经络理论",
        ["经络理论的构建", "针灸的早期形态"],
        ["中医", "经络", "针灸", "医经"],
        [], "medical"),
    "王翰林集註黄帝八十一難経": BookCard(
        "王翰林集註黄帝八十一難経", "医经", "唐", "王冰（集注，托名）",
        "《难经》集注本；以问答体讨论脉学、经络、腧穴",
        ["问答体的医学文本", "脉学的形成"],
        ["中医", "脉学", "医经"],
        [], "medical"),
    "註解傷寒論": BookCard(
        "註解傷寒論", "医经", "汉·张仲景", "成无己注",
        "**中医四大经典之一**的注释本；成无己是首个系统注解《伤寒论》的人",
        ["六经辨证体系", "中医经典注释史"],
        ["中医", "伤寒论", "辨证", "经典注释"],
        [], "medical"),
    "飲膳正要": BookCard(
        "飲膳正要", "食疗 / 宫廷", "元", "忽思慧",
        "**元代宫廷饮食养生专著**，含大量食谱与食材考，是研究元代饮食史的珍贵资料",
        ["元代宫廷饮食", "食疗理论", "食材史", "民族饮食交流"],
        ["食疗", "养生", "饮食史", "宫廷", "元史"],
        [], "medical"),

    # ── 其他 ──
    "霊棋経": BookCard(
        "霊棋経", "棋谱", "宋（传）", "题张拟",
        "**现存最早的围棋理论专著之一**，以兵法论棋，十三篇体例模仿《孙子》",
        ["最早的围棋理论", "以兵论棋的思维方式", "十三篇体例"],
        ["围棋", "棋谱", "理论专著", "体育史"],
        [], "other"),
}


# ═══════════════════════════ 选题结构库 ═══════════════════════════

@dataclass
class Pattern:
    key: str
    name: str
    #: 生成标题的模板。可用占位符：{subject} {passage} {book} {n} {claim} {reality}
    templates: list[str]
    #: 一句话说明这个结构靠什么立住
    why: str
    #: **必须由人提供**的东西 —— 引擎不会替你编
    needs: list[str]
    #: 最适合的平台
    platforms: list[str]
    #: 基础分（结构本身的强度）
    base: float = 1.0
    #: 需要素材里有哪些字段才生成
    requires: list[str] = field(default_factory=list)
    #: {n} 的取值池。溯源型要"千年"量级，翻车型/方法型要"3、5 个"量级 ——
    #: 用同一个池子会产出「3 年来被讲歪了」这种明显不对的句子。
    n_values: list[str] = field(default_factory=lambda: ["3", "5", "7", "10"])
    #: 主题来源：passage=从古籍原文取 / tech=从 AI 技术实践取 / both=都行
    #: **这一条是关键** —— 结构模板和素材类型错配会产出一读就不通的标题
    source: str = "tech"


PATTERNS: dict[str, Pattern] = {
    "contrast": Pattern(
        key="contrast", name="反差型",
        templates=[
            "所有人都说 {claim}，我实测发现 {reality}",
            "{claim}？我用真实数据打了一遍脸",
            "关于{subject}，我可能要说点不受欢迎的",
        ],
        why="反差是最强钩子，但**必须靠真实数据支撑**，否则就是标题党",
        needs=["你的实测数据或真实结果（样本量、结论、典型例子）"],
        platforms=["xiaohongshu", "douyin", "zhihu", "bilibili"],
        base=1.35,
        source="tech",
    ),
    "origin": Pattern(
        key="origin", name="溯源型",
        templates=[
            "「{passage}」这句话，{n}年来被讲歪了",
            "{book}里的这句话，原意和你想的完全不一样",
            "一句话的{subject}：从{book}到今天",
        ],
        why="古籍原文是最硬的素材，别人抄不走；有图有真相",
        needs=["选定的一句原文", "你对语义变化的判断", "对应的古籍图片"],
        platforms=["bilibili", "zhihu", "gzh", "xiaohongshu"],
        base=1.2,
        requires=["passage"],
        source="passage",
        n_values=["两千", "一千", "几百"],
    ),
    "misread": Pattern(
        key="misread", name="误读型",
        templates=[
            "「{passage}」被误读了一千年",
            "{book}里最容易被理解错的一句话",
            "你以为{passage}是这个意思？其实不是",
        ],
        why="认知反差，天然引发「我居然一直理解错了」的转发冲动",
        needs=["被误读的具体句子", "正确解释的出处（哪家注本）", "古籍原图"],
        platforms=["zhihu", "gzh", "xiaohongshu", "bilibili"],
        base=1.25,
        requires=["passage"],
        source="passage",
    ),
    "test": Pattern(
        key="test", name="实测型",
        templates=[
            "我用 AI {subject}，结果 {reality}",
            "把古籍喂给大模型，第{n}次就翻车了",
            "实测：AI 到底能不能读懂{subject}",
        ],
        why="技术实践有门槛，不容易被抄；真实数据自带可信度",
        needs=["你实际做了什么、怎么做的、结果如何", "至少一个具体错例或数据"],
        platforms=["zhihu", "bilibili", "gzh", "toutiao"],
        base=1.30,
        source="tech",
    ),
    "fail": Pattern(
        key="fail", name="翻车型",
        templates=[
            "我在{subject}上翻车了，原因很蠢",
            "做{subject}踩过的{n}个坑",
            "别学我：{subject}的错误做法",
        ],
        why="公开翻车最容易拉近距离，而且真实（平台要的就是真实创作者）",
        needs=["你真实踩过的坑", "当时的错误判断是什么"],
        platforms=["xiaohongshu", "douyin", "zhihu"],
        base=1.15,
        source="tech",
    ),
    "method": Pattern(
        key="method", name="方法型",
        templates=[
            "做{subject}的正确顺序，我试了{n}遍",
            "我做{subject}的一套方法",
            "关于{subject}，我总结的{n}条经验",
        ],
        why="干货型，收藏率高，适合做账号的「压舱石」内容",
        needs=["你的方法论", "至少一个验证过的例子"],
        platforms=["zhihu", "gzh", "toutiao", "bilibili"],
        base=1.0,
        source="tech",
    ),
}



# ═══════════════════════════ AI 实战素材库 ═══════════════════════════
#
# 主线是 AI 实战，术数是**数据集与应用场景**。
# 这里用**人工策展**的技术×场景配对，而不是笛卡尔积 ——
# 后者会产出「用 RAG 做竖排古籍识别」这种不通的组合。

@dataclass
class AiTopic:
    tech: str          # 技术
    target: str        # 场景 / 数据集
    note: str = ""     # 这条为什么值得做
    line: str = "ai"   # 都属 AI 线
    #: tech = 一项可操作的技术；concern = 一个要处理的问题
    #: 两者套用的模板不同 —— 「我用成本控制做了X」是不通的
    kind: str = "tech"
    #: 场景是否按「页」衡量。知识库/领域模型不是按页算的，
    #: 套「7911 页领域模型的成本」会很怪
    page_based: bool = True


AI_TOPICS: list[AiTopic] = [
    AiTopic("OCR", "竖排古籍识别", "竖排中文 OCR 是真实的硬骨头，公开资料很少"),
    AiTopic("视觉大模型", "古籍版面分析", "版框、界行、双行小注的自动切分"),
    AiTopic("大模型", "古籍自动标点", "古文断句是 AI 最容易翻车的地方，反差感强"),
    AiTopic("RAG", "术数术语知识库", "把 7900 页古籍做成可检索的知识库", page_based=False),
    AiTopic("向量检索", "古籍引文溯源", "一句话出自哪本书哪一卷，自动定位", page_based=False),
    AiTopic("知识图谱", "术数概念关系网", "把五行/天干/地支/神煞的关系抽成图", page_based=False),
    AiTopic("Agent", "古籍文献整理助手", "让 Agent 走完 OCR→校对→标点→入库全流程", page_based=False),
    AiTopic("微调 / LoRA", "古籍领域模型", "通用模型在古文上的表现到底差多少", page_based=False),
    AiTopic("多模态", "图文对照检索", "用图找文：给一页扫描图，找出对应的文本", page_based=False),
    AiTopic("数据清洗", "扫描件质量分级", "7900 张图里哪些能直接用、哪些要重扫", kind="concern"),
    AiTopic("提示工程", "古文翻译白话", "同一个模型，提示词差一句效果差多少", kind="concern", page_based=False),
    AiTopic("文本对齐", "多版本异文比对", "同一部书不同版本的差异自动找出来", kind="concern", page_based=False),
    AiTopic("成本控制", "大规模文档处理", "7900 页跑一遍要花多少钱，怎么省", kind="concern"),
    AiTopic("本地部署", "古籍私有知识库", "数据不出本地的前提下做检索增强", kind="concern", page_based=False),
]

#: 选题结构的借鉴来源 —— 用于在选题卡上标明「这条借的是什么机制」
BORROWED_FROM = {
    "ai_contrast": "老祖宗回答我（官媒点名的反差感机制）",
    "ai_doc": "苏州博物馆模式（官媒推崇的严谨调研+AI）",
    "ai_fail": "老祖宗回答我（反常识）",
    "ai_compare": "古诗解读 AI 动画（实测对比）",
}

#: AI 实战的选题结构（与通用结构分开，因为语气和重点不同）
AI_TEMPLATES: dict[str, tuple[list[str], list[str]]] = {
    # key: (模板列表, 需要用户提供什么)
    "ai_build": (
        ["我用「{tech}」做了{target}，效果比预期差",
         "给{target}上「{tech}」：完整过程和一个意外发现"],
        ["你实际怎么做的、用了什么工具", "真实结果（越好越差都要）",
         "至少一个具体样例或数字"]),
    "ai_fail": (
        ["用「{tech}」处理{target}，我踩了 {n} 个坑",
         "「{tech}」做{target}：我一开始的判断全错了"],
        ["具体的坑", "当时的错误判断是什么", "怎么发现和修正的"]),
    "ai_compare": (
        ["「{tech}」做{target}，真实效果到底怎么样",
         "试了 {n} 种方案做{target}，只有一种能用"],
        ["对比的几组方案", "实测数据", "结论和适用边界"]),
    "ai_cost": (
        ["{target}：{pages} 页数据跑一遍，花了多少钱",
         "{pages} 页{target}的真实成本，和教程里说的不一样"],
        ["真实的时间/金钱/算力消耗", "哪些环节最贵"]),
    "ai_tool": (
        ["我做了个{target}的工具，开源了",
         "{target}：一个能直接用的「{tech}」方案"],
        ["工具功能", "实现要点", "地址或代码"]),
    # ── 以下两个来自爆款拆解 ──
    #: 借鉴「老祖宗回答我」的**反差感**机制（官媒点名认可）
    "ai_contrast": (
        ["AI 以为的{target}，和真实古籍差多远",
         "让「{tech}」解读{target}，它给出的答案让我意外"],
        ["AI 的原始输出（截图或逐字）", "对照的真实古籍原页",
         "差在哪，为什么差"]),
    #: 借鉴官媒推崇的「苏州博物馆模式」：严谨文献调研 + AI 技术
    "ai_doc": (
        ["{target}：我用「{tech}」核了一遍文献",
         "关于{target}，文献里其实写得很清楚"],
        ["具体书名与版本", "原文出处", "你核对发现的问题"]),
}

# ═══════════════════════════ 数据模型 ═══════════════════════════

@dataclass
class Candidate:
    title: str
    pattern: str
    pattern_name: str
    niche: str = ""
    subject: str = ""
    passage: str = ""
    book: str = ""
    hook: str = ""
    needs: list[str] = field(default_factory=list)
    assets: list[str] = field(default_factory=list)     # 素材 id
    asset_paths: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    score: float = 0.0
    reason: str = ""
    why: str = ""

    @property
    def ready(self) -> bool:
        """素材齐了吗？不齐的标注出来，别让人做到一半卡住。"""
        return len(self.assets) > 0


# ═══════════════════════════ 引擎 ═══════════════════════════

class TopicEngine:
    def __init__(self, *, lib: Optional[AssetLibrary] = None,
                 ws: Optional[co.Workspace] = None,
                 asset_root: str = "assets", ws_root: str = "workspace"):
        self.lib = lib
        try:
            self.lib = lib or AssetLibrary(asset_root)
        except Exception:
            self.lib = None
        self.ws = ws or co.Workspace(ws_root)
        self._history = self._load_history()

    # ───────── 历史数据（去重 + 反馈）─────────

    def _load_history(self) -> list[co.ContentProject]:
        try:
            return self.ws.all()
        except Exception:
            return []

    def used_titles(self) -> set[str]:
        out = set()
        for p in self._history:
            for t in (p.angle, p.title):
                if t:
                    out.add(self._norm(t))
        return out

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"[\s，。！？、：；「」『』“”\"'()（）\[\]【】]", "", s or "").lower()

    def pattern_performance(self) -> dict[str, dict]:
        """按选题结构统计历史表现 —— 这是反馈闭环的关键。"""
        perf: dict[str, dict] = {}
        for p in self._history:
            pat = getattr(p, "pattern", "") or "unknown"
            recs = (p.metrics or {}).get("records") or []
            if not recs:
                continue
            last = recs[-1]
            v = last.get("views", 0) or 0
            eng = (last.get("likes", 0) or 0) + (last.get("favorites", 0) or 0)
            d = perf.setdefault(pat, {"n": 0, "views": 0, "eng": 0, "rate": 0.0})
            d["n"] += 1
            d["views"] += v
            d["eng"] += eng
        for k, d in perf.items():
            d["rate"] = (d["eng"] / d["views"] * 100) if d["views"] else 0.0
            d["avg_views"] = d["views"] / d["n"] if d["n"] else 0
        return perf

    # ───────── 素材盘点 ─────────

    def inventory(self) -> dict:
        """盘点素材库里实际有什么 —— 选题的地基。"""
        if self.lib is None or not self.lib.assets:
            return {"books": {}, "passages": [], "tags": {}, "empty": True}
        books: dict[str, dict] = {}
        passages: list[dict] = []
        for a in self.lib.assets.values():
            if not a.present:
                continue
            b = books.setdefault(a.book or "（未分类）",
                                 {"chapters": {}, "count": 0, "editions": set()})
            b["count"] += 1
            if a.chapter:
                b["chapters"][a.chapter] = b["chapters"].get(a.chapter, 0) + 1
            if a.edition:
                b["editions"].add(a.edition)
            if a.passage:
                passages.append({"book": a.book, "chapter": a.chapter,
                                 "passage": a.passage, "id": a.id})
        for b in books.values():
            b["editions"] = sorted(b["editions"])
        return {"books": books, "passages": passages,
                "tags": self.lib.all_tags(), "empty": False}

    # ───────── 生成 ─────────

    def generate(self, *, niche: str = "AI×国学",
                 platforms: Optional[Sequence[str]] = None,
                 n: int = 10, seed: Optional[int] = None) -> list[Candidate]:
        """生成候选选题。"""
        if seed is not None:
            random.seed(seed)
        inv = self.inventory()
        perf = self.pattern_performance()
        used = self.used_titles()
        targets = list(platforms or [])

        # 素材池：优先用有 passage 的，其次用 book/chapter
        subjects: list[dict] = []
        for p in inv.get("passages", []):
            subjects.append({"subject": p["passage"][:14], "passage": p["passage"],
                             "book": p["book"], "chapter": p["chapter"], "asset_id": p["id"]})
        for book, info in inv.get("books", {}).items():
            if book == "（未分类）":
                continue
            for chap in (info.get("chapters") or {}) or {"": 0}:
                label = f"{book}·{chap}" if chap else book
                subjects.append({"subject": label, "passage": "", "book": book,
                                 "chapter": chap, "asset_id": ""})

        cands: list[Candidate] = []

        # ① AI 实战（主线）+ ② 古籍文献角度（基于真实在库书目）
        present = set()
        if self.lib is not None:
            present = {a.book for a in self.lib.assets.values() if a.present and a.book}
        cands += self._ai_candidates(list(present))
        cands += self._book_candidates(present)

        # 素材池按类型分两路 —— 结构模板和素材类型必须匹配，否则标题一读就不通
        passage_pool = [s_ for s_ in subjects if s_.get("passage")]
        def _as_subj(t: str) -> dict:
            return {"subject": t, "passage": "", "book": "", "chapter": "", "asset_id": ""}

        action_pool = [_as_subj(t) for t in self._tech_actions()]
        claim_pool = [_as_subj(t) for t in self._tech_claims()]

        for pat in PATTERNS.values():
            if pat.source == "passage":
                pool = passage_pool
            elif pat.key == "contrast":
                pool = claim_pool          # 反差型需要「主张」，不是动作
            elif pat.source == "tech":
                pool = action_pool
            else:
                pool = passage_pool + action_pool
            if not pool:
                continue
            for subj in pool[:6]:
                cands.append(self._make(pat, subj, niche, pat.templates[0], targets))

        # 去重 + 打分 + 排序
        seen: set[str] = set()
        out: list[Candidate] = []
        for c in cands:
            k = self._norm(c.title)
            if k in seen or k in used:
                continue
            seen.add(k)
            if not self._sane(c):
                continue                       # 丢掉落地的标题
            self._score(c, pat=perf.get(c.pattern, {}))
            out.append(c)
        out.sort(key=lambda x: -x.score)
        return out[:n]

    def _sane(self, c: Candidate) -> bool:
        """标题合理性自检 —— 挡掉「我实测了一句古文」这类逻辑不通的组合。"""
        t = c.title
        if "……" in t and t.count("……") > 1:
            return False
        # AI 实战 / 文献解读两类不走旧的自检（它们的表述是策展过的）
        if c.pattern in ("ai_build", "ai_fail", "ai_compare", "ai_cost",
                         "ai_tool", "document"):
            return True
        # 技术动作词 + 纯古文书名/篇名 → 不通
        tech_verbs = ("实测", "翻车", "喂给", "做了", "搭")
        for v in tech_verbs:
            if v in t:
                # 主题必须像个「可做的事」，而不是一句古文或一个篇名
                subj = c.subject
                if "，" in subj or "。" in subj or "？" in subj or len(subj) > 16:
                    return False
                if re.fullmatch(r"[\u4e00-\u9fa5]{1,4}·[\u4e00-\u9fa5]{1,4}", subj):
                    return False
        return True


    # ───────── AI 实战选题（主线）─────────

    #: 每类「考量」适合的模板
    CONCERN_TEMPLATES = [
        "做{target}，「{tech}」这一关最难过",
        "{target}：「{tech}」上我交的学费",
    ]

    def _ai_candidates(self, targets: list[str]) -> list[Candidate]:
        """AI 技术 × 真实数据集场景 —— 这是内容主线。"""
        out: list[Candidate] = []
        # 用**真实数据规模**，不用随机数
        try:
            n_pages = len(self.lib.assets) if self.lib else 0
        except Exception:
            n_pages = 0
        scale = f"{n_pages}" if n_pages > 100 else "{n}"
        for tp_i, tp in enumerate(AI_TOPICS):
            if tp.kind == "concern":
                ct = self.CONCERN_TEMPLATES
                combos = [(f"concern{i}", t) for i, t in
                          enumerate([ct[tp_i % len(ct)]])]
            else:
                combos = []
                for k, (tpls, _needs) in AI_TEMPLATES.items():
                    # "X 页数据跑一遍花了多少钱" 只对按页衡量的场景成立
                    if k == "ai_cost" and not tp.page_based:
                        continue
                    # **按技术序号轮换模板** —— 固定取第 0 个会让同一个句式
                    # 在候选列表里出现十几次，看着像复读
                    combos.append((k, tpls[tp_i % len(tpls)]))
            for ti, (key, tpl) in enumerate(combos):
                title = (tpl.replace("{tech}", tp.tech)
                            .replace("{target}", tp.target)
                            .replace("{pages}", scale)
                            .replace("{n}", random.choice(["3", "5", "7"])))
                needs = (list(AI_TEMPLATES[key][1]) if key in AI_TEMPLATES
                         else ["你在这上面踩过的具体问题", "真实的时间或金钱代价"])
                pat = (f"ai_{key.split('_')[1]}" if key in AI_TEMPLATES
                       else "ai_concern")
                c = Candidate(
                    title=title, pattern=pat,
                    pattern_name="AI 实战",
                    niche="AI 实战",
                    subject=f"{tp.tech}·{tp.target}",
                    needs=list(needs) + [f"你的 {tp.tech} 实测记录"],
                    why=f"{tp.note}。AI 实战有门槛，不容易被抄。",
                    platforms=["zhihu", "bilibili", "gzh", "xiaohongshu"],
                )
                c.score = round(1.6 + random.uniform(0, 0.15), 3)
                borrow = BORROWED_FROM.get(f"ai_{key.split('_')[-1]}", "")
                c.reason = (f"主线内容 · 结构 {key} · 场景有真实数据集支撑"
                            + (f" · 借鉴：{borrow}" if borrow else ""))
                out.append(c)
        return out

    # ───────── 古籍文献角度选题 ─────────

    def _book_candidates(self, present_books: set[str]) -> list[Candidate]:
        """基于**真实在库书目**的文献角度选题。

        角度取自 BOOK_KB 的 angles（文献/历史/版本），
        刻意避开"教人算命"方向 —— 平台打击的是后者。
        """
        tpls = ["{angle}：《{book}》是怎么处理的",
                "从《{book}》看{angle}",
                "{angle}——一部{kind}里的答案"]
        out: list[Candidate] = []
        for book, card in BOOK_KB.items():
            if present_books and book not in present_books:
                continue
            for i, ang in enumerate(card.angles):
                tpl = tpls[i % len(tpls)]
                title = (tpl.replace("{angle}", ang).replace("{book}", book)
                            .replace("{kind}", card.kind))
                c = Candidate(
                    title=title, pattern="document",
                    pattern_name="文献解读",
                    niche="古籍文献",
                    subject=f"{book}·{ang}",
                    book=book,
                    needs=[f"《{book}》的关键页（素材库已有）",
                           f"你对「{ang}」的判断或考据"],
                    why=f"{card.value[:50]}",
                    platforms=["zhihu", "bilibili", "gzh"],
                )
                # 有素材 = 能直接开工
                s_ = 1.25
                if self.lib is not None:
                    hits = self.lib.search(book=book)
                    if hits:
                        s_ += 0.2
                        c.assets = [a.id for a in hits[:2]]
                        c.asset_paths = [os.path.join(self.lib.root, a.path)
                                         for a in hits[:2]]
                c.score = round(s_, 3)
                c.reason = (f"文献角度（不涉测算）· 素材库有 {len(c.assets)} 页可用"
                            if c.assets else "文献角度（不涉测算）")
                out.append(c)
        return out

    @staticmethod
    def _short(text: str, limit: int = 12) -> str:
        """原文太长时取前段做标题，避免出现「…有朋自」这种被硬切断的怪句子。"""
        t = (text or "").strip()
        if len(t) <= limit:
            return t
        # 优先在标点处收尾
        for ch in "，。！？；：、,.!?;:":
            k = t.find(ch)
            if 4 <= k <= limit:
                # 不保留标点 —— 「学而时习之，」这种悬空逗号很难看
                return t[:k]
        return t[:limit] + "……"

    @staticmethod
    def _tech_actions() -> list[str]:
        """⚠️ 已废弃：改用 AI_TOPICS（与真实素材绑定的策展配对）。
        保留空实现是为了兼容旧调用路径。"""
        return []

    @staticmethod
    def _tech_claims() -> list[str]:
        """⚠️ 已废弃：主张类选题改由 AI_TOPICS 提供。"""
        return []

    def _make(self, pat: Pattern, subj: dict, niche: str, tpl: str,
              targets: list[str]) -> Candidate:
        subject = subj.get("subject") or niche
        passage = subj.get("passage") or ""
        book = subj.get("book") or ""
        # 原文作为标题素材时要收短，否则会被硬切成不自然的断句
        shown = self._short(passage) if passage else subject
        title = (tpl.replace("{subject}", subject)
                    .replace("{passage}", shown)
                    .replace("{book}", book or "古籍")
                    .replace("{n}", random.choice(pat.n_values))
                    .replace("{claim}", subject)
                    .replace("{reality}", "……"))
        c = Candidate(
            title=title, niche=niche, pattern=pat.key, pattern_name=pat.name,
            subject=subject, passage=shown if passage else "", book=book,
            needs=list(pat.needs), why=pat.why,
            platforms=[p for p in pat.platforms if not targets or p in targets]
                      or pat.platforms,
        )
        # 匹配素材
        if self.lib is not None and self.lib.assets:
            q = passage or f"{book} {subj.get('chapter','')}".strip() or subject
            for a in self.lib.suggest(q, n=2):
                c.assets.append(a.id)
                c.asset_paths.append(os.path.join(self.lib.root, a.path))
        return c

    def _score(self, c: Candidate, *, pat: dict) -> None:
        # 策展类选题（AI/文献）在生成时已定分，这里只补历史加权
        if c.pattern not in PATTERNS:
            if pat.get("n") and pat.get("rate"):
                bump = min(pat["rate"] / 5.0, 0.5)
                c.score = round(c.score + bump, 3)
                c.reason += f"；同类历史互动率 {pat['rate']:.1f}%（+{bump:.2f}）"
            return
        # AI 实战 / 文献解读是独立结构，不在 PATTERNS 里 —— 给个合理基准分
        base = PATTERNS[c.pattern].base if c.pattern in PATTERNS else 1.3
        s = base
        reasons = [f"结构强度 {base:.2f}（{c.pattern_name}）"]
        if c.assets:
            s += 0.25
            reasons.append(f"已有 {len(c.assets)} 个匹配素材（可直接开工）")
        else:
            reasons.append("暂无匹配素材，需自行准备")
        if c.passage:
            s += 0.15
            reasons.append("有古籍原文，可信度高")
        if pat.get("n"):
            rate = pat.get("rate", 0) or 0
            if rate > 0:
                bump = min(rate / 5.0, 0.5)      # 互动率越高加分越多，封顶 0.5
                s += bump
                reasons.append(f"你同类结构历史互动率 {rate:.1f}%（+{bump:.2f}）")
        c.score = round(s, 3)
        c.reason = "；".join(reasons)

    # ───────── 输出 ─────────

    def to_markdown(self, cands: list[Candidate], *, niche: str) -> str:
        L = [f"# 选题候选 · {niche}", ""]
        L.append(f"> 由 topic_engine 生成于 {time.strftime('%F %T')}")
        L.append("> 每条都标明了**需要你提供什么** —— 引擎不替你编经历。")
        L.append("")
        L.append("---")
        L.append("")
        for i, c in enumerate(cands, 1):
            L.append(f"## {i}. {c.title}")
            L.append("")
            L.append(f"- **结构**：{c.pattern_name}（`{c.pattern}`） —— {c.why}")
            L.append(f"- **评分**：{c.score:.2f}　{c.reason}")
            L.append(f"- **适合平台**：{'、'.join(co.PLATFORM_NAME_ZH.get(p, p) for p in c.platforms)}")
            if c.assets:
                L.append(f"- **可用素材**：{len(c.assets)} 个")
                for p in c.asset_paths[:2]:
                    L.append(f"  - `{os.path.relpath(p)}`")
            else:
                L.append("- **可用素材**：⚠️ 无（需自行准备）")
            L.append(f"- **需要你提供**：")
            for nd in c.needs:
                L.append(f"  - {nd}")
            L.append("")
        L.append("---")
        L.append("")
        L.append("## 下一步")
        L.append("")
        L.append("选定一条后，直接建成项目：")
        L.append("")
        L.append("```bash")
        L.append(f"python3 topic_engine.py pick --from candidates.json -i 1")
        L.append("```")
        return "\n".join(L)

    # ───────── 建项目（闭环）─────────

    def pick(self, c: Candidate, *, platform: str) -> str:
        """把选题建成 content_ops 项目，并写入结构标记（供数据反馈）。"""
        proj = self.ws.new(c.niche or "未分类", platform)
        proj.angle = c.title
        proj.title = c.title
        proj.pattern = c.pattern
        if c.assets and self.lib is not None:
            for aid in c.assets:
                try:
                    a = self.lib.get(aid)
                    proj.brief.assets.append(os.path.join(self.lib.root, a.path))
                except Exception:
                    pass
        co.advance(proj, co.Stage.TOPIC_SELECTED, f"选题引擎 · {c.pattern_name}")
        # **建立 SOP 流程骨架** —— 选题一落地，创作流程就跟着启动
        try:
            import sop as _sop
            _sop.init_sop(proj)
            # 「定选题」这一步在此刻就算完成（角度和素材都已定）
            ok, _issues = _sop.submit_step(proj, "pick", {
                "angle": c.title,
                "assets": proj.brief.assets,
            })
            if ok:
                proj.history.append({
                    "ts": time.strftime("%F %T"), "from": "sop", "to": "pick",
                    "note": "SOP ①定选题 通过"})
        except Exception:
            pass
        self.ws.save(proj)
        # 告诉用户下一步
        try:
            import sop as _sop
            nxt = _sop.next_key(proj)
            if nxt:
                nm = next((x.name for x in _sop.SOP.steps if x.key == nxt), nxt)
                print(f"   SOP 下一步：{nm}")
                print(f"   看引导：python3 sop.py next {proj.id}")
        except Exception:
            pass
        return proj.id


# ═══════════════════════════ CLI ═══════════════════════════

def cmd_inventory(eng: TopicEngine, args) -> int:
    inv = eng.inventory()
    if inv.get("empty"):
        print("素材库是空的。先导入素材：")
        print("  python3 asset_library.py import <目录> --auto-parse")
        print("\n（没有素材也能用 gen，但只能给出需要你补素材的技术型选题）")
        return 0
    print(f"素材库盘点\n")
    total = 0
    for book, info in sorted(inv["books"].items(), key=lambda x: -x[1]["count"]):
        chaps = "、".join(f"{k}({v})" for k, v in (info["chapters"] or {}).items())
        eds = "/".join(info["editions"]) if info["editions"] else "—"
        print(f"  {book:<12} {info['count']:>3} 张   版本:{eds:<8} 篇目: {chaps or '—'}")
        total += info["count"]
    print(f"\n  合计 {total} 张 · 标签 {inv['tags']}")
    print(f"  带原文片段的素材: {len(inv['passages'])} 个"
          f"{'  ← 这些能生成溯源/误读型选题' if inv['passages'] else ''}")
    if not inv["passages"]:
        print("  💡 给素材补上 --passage（原文片段），能解锁「溯源型」「误读型」选题")
    return 0


def cmd_gen(eng: TopicEngine, args) -> int:
    platforms = [p.strip() for p in args.platforms.split(",")] if args.platforms else None
    cands = eng.generate(niche=args.niche, platforms=platforms, n=args.n, seed=args.seed)
    if not cands:
        print("没生成任何选题。检查素材库，或换个领域。")
        return 1
    md = eng.to_markdown(cands, niche=args.niche)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md)
        with open(os.path.splitext(args.out)[0] + ".json", "w", encoding="utf-8") as f:
            json.dump([asdict(c) for c in cands], f, ensure_ascii=False, indent=2)
        print(f"✅ {len(cands)} 条选题 → {args.out}")
        print(f"   机器可读版 → {os.path.splitext(args.out)[0]}.json")
    else:
        print(md)
    return 0


def cmd_pick(eng: TopicEngine, args) -> int:
    with open(args.from_file, encoding="utf-8") as f:
        data = json.load(f)
    if not (1 <= args.index <= len(data)):
        print(f"序号超出范围（1~{len(data)}）")
        return 2
    c = Candidate(**data[args.index - 1])
    platform = args.platform or (c.platforms[0] if c.platforms else "gzh")
    pid = eng.pick(c, platform=platform)
    print(f"✅ 已建项目 {pid}")
    print(f"   选题: {c.title}")
    print(f"   结构: {c.pattern_name}")
    print(f"   平台: {co.PLATFORM_NAME_ZH.get(platform, platform)}")
    if c.asset_paths:
        print(f"   已带入 {len(c.asset_paths)} 个素材")
    print(f"\n下一步（内容要点必须由你提供）：")
    print(f"  1) 写 brief：python3 content_ops.py brief {pid} --file brief.md")
    print(f"  2) 一键生产：python3 pipeline.py produce {pid}")
    return 0


def cmd_perf(eng: TopicEngine, args) -> int:
    perf = eng.pattern_performance()
    if not perf:
        print("还没有带数据的已发布内容。")
        print("发布后用 content_ops.py metrics <id> 回填，这里就能看到结构效果。")
        return 0
    print(f"{'结构':<12} {'条数':>4} {'总浏览':>9} {'总互动':>8} {'互动率':>7}")
    print("-" * 50)
    for k, d in sorted(perf.items(), key=lambda x: -x[1]["rate"]):
        name = PATTERNS[k].name if k in PATTERNS else k
        print(f"{name:<12} {d['n']:>4} {d['views']:>9} {d['eng']:>8} {d['rate']:>6.1f}%")
    best = max(perf.items(), key=lambda x: x[1]["rate"])
    if best[0] in PATTERNS:
        print(f"\n💡 「{PATTERNS[best[0]].name}」表现最好 → 选题引擎会给这类结构加权")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="topic_engine — 选题引擎",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__)
    p.add_argument("--assets", default="assets")
    p.add_argument("--ws", default="workspace")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("inventory", help="盘点素材库").set_defaults(func=cmd_inventory)

    g = sub.add_parser("gen", help="生成候选选题")
    g.add_argument("--niche", default="AI×国学")
    g.add_argument("-n", type=int, default=10)
    g.add_argument("--platforms", help="限定平台，逗号分隔")
    g.add_argument("--seed", type=int, help="固定随机种子，便于复现")
    g.add_argument("-o", "--out", help="输出 markdown 路径（同时出 json）")
    g.set_defaults(func=cmd_gen)

    k = sub.add_parser("pick", help="把某条选题建成项目")
    k.add_argument("--from", dest="from_file", required=True, help="候选 json")
    k.add_argument("-i", "--index", type=int, required=True, help="序号（从 1 开始）")
    k.add_argument("--platform")
    k.set_defaults(func=cmd_pick)

    sub.add_parser("perf", help="看各结构的历史表现").set_defaults(func=cmd_perf)
    return p


def main(argv: Sequence[str]) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    fn = getattr(args, "func", None)
    if fn is None:
        p.print_help()
        return 2
    eng = TopicEngine(asset_root=args.assets, ws_root=args.ws)
    try:
        return fn(eng, args)
    except (co.OpsError, OSError, ValueError) as e:
        print(f"\n❌ {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
