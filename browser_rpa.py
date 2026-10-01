#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
browser_rpa — 通用的「浏览器 + 像素 RPA」骨架

配合 rpa_kit 使用：不装 Playwright、不碰 CDP、不改你的浏览器配置，
纯粹用「截图 → 识别 → 点击」驱动你正在用的 Google Chrome。

三条设计原则
------------
1. **永不动你已有的标签页** —— 所有操作都在新开的标签页里进行，用完即关
2. **永不自动点「发布」** —— 危险操作用 danger 标记，强制人工确认才放行
3. **坐标用视口相对比例存** —— 换分辨率、换窗口大小仍然可用

工作流
------
    # ① 打开目标站点（新标签页，不影响你现有页面）
    python3 browser_rpa.py open https://creator.xiaohongshu.com

    # ② 截图 + 网格，用来读控件坐标（这一步是视觉校准）
    python3 browser_rpa.py look xiaohongshu

    # ③ 人工登录（脚本会暂停等你）
    python3 browser_rpa.py login xiaohongshu

    # ④ 从视觉上读出的坐标填进 profiles/<name>.json，然后跑流程
    python3 browser_rpa.py run xiaohongshu --dry-run   # 先彩排
    python3 browser_rpa.py run xiaohongshu             # 再真跑

依赖 rpa_kit：PYTHONPATH=./rpa_libs python3 browser_rpa.py ...

⚠️ 重要提醒
-----------
国内主流内容平台（微信/小红书/抖音/B站/知乎/头条）的用户协议**普遍明文禁止**
使用自动化程序/脚本/第三方工具操作平台。本工具仅供**个人在自己的账号上做效率
辅助**，且设计上强制把「发布」这一步留给人。请阅读 `自媒体平台自动化发布-可行性
报告.md` 了解完整风险，并自行承担使用后果。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional, Sequence

from rpa_kit import RPA, Capture, RPAError, Window


# ─────────────────────────── 异常 ───────────────────────────

class HumanConfirmationRequired(RPAError):
    """触碰了被标记为危险的操作，需要人工确认。"""


class ProfileError(RPAError):
    """profile 配置有问题（缺坐标、缺字段等）。"""


# ─────────────────────────── 数据结构 ───────────────────────────

@dataclass
class Hotspot:
    """视口内的一个可点位置，坐标是**相对视口的比例 (0~1)**。

    rx=0 是视口最左，rx=1 是最右；ry 同理。
    """

    rx: float
    ry: float
    label: str = ""
    #: True 表示这是「发布/提交」类不可逆操作，必须人工确认
    danger: bool = False

    def abs_point(self, viewport: "Viewport") -> tuple[float, float]:
        return viewport.rel(self.rx, self.ry)


@dataclass
class Viewport:
    """浏览器**内容区**在屏幕上的绝对矩形（点）。

    浏览器窗口 ≠ 内容区 —— 顶部有标签栏和地址栏，底部可能有状态栏。
    内容区由 Chrome 窗口 frame 减去 insets 得出。
    """

    x: float
    y: float
    w: float
    h: float

    @property
    def frame(self) -> tuple[float, float, float, float]:
        return (self.x, self.y, self.w, self.h)

    def rel(self, rx: float, ry: float) -> tuple[float, float]:
        return (self.x + rx * self.w, self.y + ry * self.h)

    def sub_region(self, rx: float, ry: float, rw: float, rh: float
                   ) -> tuple[float, float, float, float]:
        """视口内的相对矩形 → 屏幕绝对矩形。"""
        return (self.x + rx * self.w, self.y + ry * self.h, rw * self.w, rh * self.h)

    def __str__(self) -> str:
        return f"Viewport({self.x:.0f},{self.y:.0f} {self.w:.0f}×{self.h:.0f})"


@dataclass
class Insets:
    """Chrome 窗口 frame 到内容区的内边距（点）。"""

    top: float = 88.0      # 标签栏 + 地址栏（需按实际校准）
    left: float = 0.0
    right: float = 0.0
    bottom: float = 0.0

    def apply(self, win: Window) -> Viewport:
        return Viewport(
            x=win.x + self.left,
            y=win.y + self.top,
            w=win.w - self.left - self.right,
            h=win.h - self.top - self.bottom,
        )


@dataclass
class Profile:
    """一个站点的配置档。JSON 持久化，坐标可反复修订。"""

    name: str
    url: str = ""
    insets: Insets = field(default_factory=Insets)
    hotspots: dict[str, Hotspot] = field(default_factory=dict)
    #: 登录后会发生变化的区域（视口相对矩形 rx,ry,rw,rh）。
    #: 用于自动判断"是否已登录"——不需要参考图，比对变化即可。
    login_marker: Optional[list[float]] = None
    notes: str = ""

    # ---- 持久化 ----
    def save(self, path: str) -> None:
        data = asdict(self)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @staticmethod
    def load(path: str) -> "Profile":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        ins = Insets(**data.get("insets", {}))
        spots = {k: Hotspot(**v) for k, v in data.get("hotspots", {}).items()}
        return Profile(
            name=data["name"], url=data.get("url", ""), insets=ins,
            hotspots=spots, login_marker=data.get("login_marker"),
            notes=data.get("notes", ""),
        )

    def hotspot(self, key: str) -> Hotspot:
        if key not in self.hotspots:
            raise ProfileError(
                f"profile {self.name!r} 里没有坐标 {key!r}。"
                f"已有的: {sorted(self.hotspots) or '（空，需要先用 look 校准）'}")
        return self.hotspots[key]


# ─────────────────────────── 会话 ───────────────────────────

class BrowserSession:
    """在真实 Chrome 上执行像素级 RPA。"""

    CHROME_APP = "Google Chrome"

    def __init__(self, *, rpa: Optional[RPA] = None,
                 profile_dir: str = "browser_profiles",
                 shot_dir: Optional[str] = None,
                 allow_danger: bool = False,
                 verbose: bool = True):
        self.rpa = rpa or RPA(verbose=verbose, shot_dir=shot_dir)
        self.profile_dir = profile_dir
        self.allow_danger = allow_danger
        self.verbose = verbose
        os.makedirs(profile_dir, exist_ok=True)

        self._tab_opened = False
        self.journal: list[str] = []

    # ───────── 日志 ─────────

    def _log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        self.journal.append(line)
        if self.verbose:
            print(line, file=sys.stderr, flush=True)

    # ───────── 窗口与视口 ─────────

    def chrome(self) -> Window:
        """拿到 Chrome 窗口（不存在就抛错并给提示）。"""
        w = self.rpa.find_window(self.CHROME_APP)
        if w is None:
            raise RPAError(
                "找不到 Google Chrome 窗口。请先打开 Chrome（哪怕只有一个窗口）。\n"
                "提示：如果 Chrome 在后台，切到前台再试，或者先手动打开。")
        return w

    def viewport(self, profile: Profile) -> Viewport:
        """按 profile 的 insets 计算内容区（每次都重取窗口 frame）。"""
        return profile.insets.apply(self.chrome())

    # ───────── 导航 ─────────

    def open_tab(self) -> None:
        """新开一个标签页（**不动你现有的标签页**）。

        注意 macOS 的一个坑：**关掉所有窗口 != 退出 Chrome**。
        进程还在，但没有任何窗口，此时 cmd+T 无处可发。
        所以先检查有没有窗口，没有就唤起一个。
        """
        if self.rpa.find_window(self.CHROME_APP) is None:
            self._log("Chrome 无窗口（进程在但窗口都关了），先唤起一个")
            subprocess.run(["open", "-a", self.CHROME_APP], check=False)
            for _ in range(20):                 # 最多等 6 秒
                time.sleep(0.3)
                if self.rpa.find_window(self.CHROME_APP) is not None:
                    break
            else:
                raise RPAError(
                    "唤起了 Google Chrome 但仍找不到窗口。请手动打开 Chrome 后重试。")
        self.rpa.ensure_front(self.CHROME_APP)
        time.sleep(0.35)
        self.rpa.hotkey("cmd", "t")
        time.sleep(0.6)
        self._tab_opened = True
        self._log("已新开标签页")

    def close_tab(self) -> None:
        """关掉本会话新开的标签页。"""
        if not self._tab_opened:
            return
        self.rpa.ensure_front(self.CHROME_APP)
        time.sleep(0.25)
        self.rpa.hotkey("cmd", "w")
        time.sleep(0.5)
        self._tab_opened = False
        self._log("已关闭本会话的标签页")

    def goto(self, url: str, *, new_tab: bool = True) -> None:
        """导航到 URL。默认新开标签页，绝不覆盖你当前的页面。"""
        if new_tab and not self._tab_opened:
            self.open_tab()
        else:
            self.rpa.ensure_front(self.CHROME_APP)
            time.sleep(0.3)
        self._log(f"导航 → {url}")
        self.rpa.hotkey("cmd", "l")        # 聚焦地址栏
        time.sleep(0.4)
        self.rpa.type_text(url)
        time.sleep(0.3)
        self.rpa.key("return")
        time.sleep(1.5)

    def wait_page_ready(self, profile: Profile, *, timeout: float = 30.0,
                        settle: int = 2) -> None:
        """等页面渲染稳定（比死等 sleep 可靠）。"""
        self.rpa.wait_stable(profile.insets.apply(self.chrome()).frame,
                             timeout=timeout, interval=0.5, settle=settle,
                             desc="页面渲染稳定")

    # ───────── 截图与视觉校准 ─────────

    def shot(self, profile: Profile, *, name: str = "page",
             inset: float = 0.0) -> Capture:
        """截取浏览器内容区。"""
        vp = self.viewport(profile)
        x, y, w, h = vp.frame
        if inset:
            x, y, w, h = x + inset, y + inset, w - 2 * inset, h - 2 * inset
        return self.rpa.screenshot((x, y, w, h), name=name)

    def look(self, profile: Profile, *, cols: int = 10, rows: int = 8) -> tuple[Capture, Capture]:
        """截图 + 叠网格，用于**视觉校准**坐标。

        返回 (原始截图, 网格截图)。从网格图上按格子读出目标控件的像素位置，
        再换算成视口相对比例填进 profile。
        """
        cap = self.shot(profile, name=f"look_{profile.name}")
        grid = self.rpa.grid_overlay(cap, cols=cols, rows=rows,
                                     name=f"look_{profile.name}_grid")
        self._log(f"校准截图: {cap.path}")
        self._log(f"网格截图: {grid.path}（{cols}×{rows} 格，每格 "
                  f"{cap.px_w/cols:.0f}×{cap.px_h/rows:.0f} 像素）")
        return cap, grid

    def describe_viewport(self, profile: Profile) -> str:
        vp = self.viewport(profile)
        win = self.chrome()
        ins = profile.insets
        return (
            f"Chrome 窗口 : ({win.x:.0f},{win.y:.0f}) {win.w:.0f}×{win.h:.0f}\n"
            f"内容区(视口): ({vp.x:.0f},{vp.y:.0f}) {vp.w:.0f}×{vp.h:.0f}\n"
            f"内边距      : top={ins.top:.0f} left={ins.left:.0f} "
            f"right={ins.right:.0f} bottom={ins.bottom:.0f}\n"
            f"换算        : 屏幕点 = 视口内比例 × ({vp.w:.0f}, {vp.h:.0f}) "
            f"+ ({vp.x:.0f}, {vp.y:.0f})"
        )

    # ───────── 登录 ─────────

    def wait_login(self, profile: Profile, *, timeout: float = 300.0,
                   poll: float = 2.0) -> None:
        """等用户完成登录。

        两种模式：
        - profile 配了 login_marker → 轮询该区域，等它**发生变化**（登录成功
          通常会让头像/菜单区改变），变化即认为登录完成
        - 没配 → 提示用户，按回车确认

        无论哪种模式，**本工具都不接触你的账号密码**。
        """
        print("\n" + "─" * 60)
        print("  🔐 请在打开的 Chrome 窗口里手动完成登录")
        print("     （扫码 / 输密码都由你来，我不接触任何凭据）")
        print("─" * 60)

        if profile.login_marker:
            rx, ry, rw, rh = profile.login_marker
            vp = self.viewport(profile)
            region = vp.sub_region(rx, ry, rw, rh)
            before = self.rpa.screenshot(region, name="login_before")
            print(f"  正在监视登录区域（{timeout:.0f}s 内检测到变化即自动继续）…")
            deadline = time.time() + timeout
            n = 0
            while time.time() < deadline:
                time.sleep(poll)
                n += 1
                cur = self.rpa.screenshot(region, name=f"login_poll_{n}")
                d = self.rpa.pixel_diff(before, cur)
                if d > 0.02:
                    print(f"  ✅ 检测到登录区域变化（{d*100:.1f}%），继续执行\n")
                    self._log(f"登录检测命中（差异 {d*100:.2f}%）")
                    return
            raise RPAError(f"{timeout:.0f}s 内没检测到登录完成，已中止")

        # 交互模式
        try:
            input("  登录完成后，回到这里按【回车】继续… ")
        except EOFError:
            raise RPAError(
                "当前环境无法读取键盘输入，请在 profile 里配置 login_marker 后重试")
        print("  ✅ 继续\n")
        self._log("用户确认已登录")

    # ───────── 点击（带危险操作守卫）─────────

    def click(self, profile: Profile, key: str, *, wait: float = 0.15,
              confirm: bool = False) -> tuple[float, float]:
        """按 profile 里的坐标点击。

        若该坐标被标记 `danger: true`（发布/提交类），**默认拒绝执行**，
        除非显式传入 `confirm=True` 且 allow_danger 已开启。
        """
        spot = profile.hotspot(key)
        vp = self.viewport(profile)
        x, y = spot.abs_point(vp)

        if spot.danger:
            if not self.allow_danger:
                raise HumanConfirmationRequired(
                    f"坐标 {key!r}（{spot.label or '危险操作'}）被标记为危险操作。\n"
                    f"为避免误触发布，默认拒绝执行。\n"
                    f"如确需如此，请加 --allow-danger，且每次都会再问你一次。")
            if not confirm:
                raise HumanConfirmationRequired(
                    f"即将点击 **{spot.label or key}** —— 这是不可逆的操作。\n"
                    f"  坐标: {key} → 屏幕点 ({x:.0f}, {y:.0f})")
            self._confirm_interactive(f"{spot.label or key} @ ({x:.0f},{y:.0f})")

        self._log(f"click {key!r} → ({x:.0f},{y:.0f})")
        self.rpa.click(x, y, wait=wait)
        return (x, y)

    def _confirm_interactive(self, what: str) -> None:
        print(f"\n⚠️  危险操作: {what}")
        try:
            ans = input("   确认执行？输入 yes 继续（其他任何输入都会中止）: ")
        except EOFError:
            raise HumanConfirmationRequired("无法读取确认输入，已中止")
        if ans.strip().lower() != "yes":
            raise HumanConfirmationRequired("用户未确认，已中止")
        self._log(f"用户已确认: {what}")

    # ───────── 在指定区域输入 ─────────

    def click_and_type(self, profile: Profile, key: str, text: str, *,
                       clear: bool = True) -> None:
        """点某个坐标，然后输入文本（支持中文）。"""
        self.click(profile, key)
        time.sleep(0.4)
        if clear:
            self.rpa.hotkey("cmd", "a")
            time.sleep(0.12)
            self.rpa.key("delete")
            time.sleep(0.12)
        self.rpa.type_text(text)
        self._log(f"已向 {key!r} 输入 {len(text)} 个字符")

    # ───────── 粘贴（富文本编辑器用）─────────

    def paste(self, text: str) -> None:
        """把文本写进剪贴板再粘贴。

        对 contenteditable / ProseMirror / Draft.js 这类编辑器，
        直接键入或设值往往无效，**模拟粘贴是唯一可靠方式**。
        """
        import subprocess
        p = subprocess.run(["pbcopy"], input=text, text=True)
        if p.returncode != 0:
            raise RPAError("写入剪贴板失败（pbcopy）")
        time.sleep(0.15)
        self.rpa.hotkey("cmd", "v")
        self._log(f"已粘贴 {len(text)} 个字符")


# ─────────────────────────── Profile 管理 ───────────────────────────

def profile_path(profile_dir: str, name: str) -> str:
    return os.path.join(profile_dir, f"{name}.json")


def load_or_init(profile_dir: str, name: str, url: str = "") -> Profile:
    os.makedirs(profile_dir, exist_ok=True)
    p = profile_path(profile_dir, name)
    if os.path.exists(p):
        return Profile.load(p)
    prof = Profile(name=name, url=url, notes="坐标需用 look 命令校准后填入")
    prof.save(p)
    return prof


# ─────────────────────────── CLI ───────────────────────────

def cmd_open(args) -> int:
    prof = load_or_init(args.profile_dir, args.name, args.url or "")
    if args.url:
        prof.url = args.url
        prof.save(profile_path(args.profile_dir, prof.name))
    s = BrowserSession(profile_dir=args.profile_dir)
    s.goto(args.url or prof.url)
    print(f"\n已在新标签页打开: {args.url or prof.url}")
    print(f"profile: {profile_path(args.profile_dir, prof.name)}")
    print("\n下一步：")
    print(f"  1) python3 browser_rpa.py look {prof.name}   # 截图+网格，读坐标")
    print(f"  2) 人工登录")
    print(f"  3) python3 browser_rpa.py run {prof.name} --dry-run")
    return 0


def cmd_look(args) -> int:
    prof = load_or_init(args.profile_dir, args.name)
    s = BrowserSession(profile_dir=args.profile_dir)
    print(s.describe_viewport(prof))
    print()
    cap, grid = s.look(prof, cols=args.cols, rows=args.rows)
    print(f"原始截图: {cap.path}")
    print(f"网格截图: {grid.path}")
    print()
    print("怎么用这两张图校准坐标：")
    print(f"  1. 打开网格截图，数出目标控件在**第几列第几格**")
    print(f"  2. 像素坐标 (px, py) → 视口比例 = (px / {cap.px_w}, py / {cap.px_h})")
    print(f"  3. 填进 {profile_path(args.profile_dir, prof.name)} 的 hotspots")
    print()
    print("⚠️ 若内容区明显偏移，说明窗口顶部内边距不对，调整 profile 的 insets.top")
    return 0


def cmd_login(args) -> int:
    prof = load_or_init(args.profile_dir, args.name)
    s = BrowserSession(profile_dir=args.profile_dir)
    s.wait_login(prof, timeout=args.timeout)
    s.shot(prof, name=f"{prof.name}_after_login")
    print("已截图记录登录后状态，可用于后续校准坐标。")
    return 0


def cmd_shot(args) -> int:
    prof = load_or_init(args.profile_dir, args.name)
    s = BrowserSession(profile_dir=args.profile_dir)
    cap = s.shot(prof, name=args.out or f"{prof.name}_shot")
    print(cap.path)
    return 0


def cmd_show(args) -> int:
    prof = load_or_init(args.profile_dir, args.name)
    s = BrowserSession(profile_dir=args.profile_dir)
    print(s.describe_viewport(prof))
    print()
    if prof.hotspots:
        print(f"{'坐标名':<20} {'视口比例':<18} {'危险':<6} 说明")
        print("-" * 66)
        for k, h in prof.hotspots.items():
            print(f"{k:<20} {f'({h.rx:.3f}, {h.ry:.3f})':<18} "
                  f"{'⚠️ 是' if h.danger else '否':<6} {h.label}")
    else:
        print("（还没有任何坐标，先用 look 命令校准）")
    print()
    print("按视口比例换算成屏幕点的公式：")
    print("  屏幕点 = 视口原点 + 比例 × 视口尺寸")
    return 0


def cmd_close(args) -> int:
    """关掉本会话开的新标签页（如果还开着）。"""
    prof = load_or_init(args.profile_dir, args.name)
    s = BrowserSession(profile_dir=args.profile_dir)
    s.rpa.ensure_front(BrowserSession.CHROME_APP)
    s.rpa.hotkey("cmd", "w")
    print("已关闭当前标签页")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="browser_rpa — 通用浏览器像素 RPA 骨架",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    p.add_argument("--profile-dir", default="browser_profiles",
                   help="profile 存放目录（默认 browser_profiles/）")
    sub = p.add_subparsers(dest="cmd")

    o = sub.add_parser("open", help="在新标签页打开站点")
    o.add_argument("url")
    o.add_argument("--name", required=True, help="profile 名，如 xiaohongshu")
    o.set_defaults(func=cmd_open)

    l = sub.add_parser("look", help="截图+网格，用于视觉校准坐标")
    l.add_argument("name")
    l.add_argument("--cols", type=int, default=10)
    l.add_argument("--rows", type=int, default=8)
    l.set_defaults(func=cmd_look)

    g = sub.add_parser("login", help="等人工完成登录")
    g.add_argument("name")
    g.add_argument("--timeout", type=float, default=300.0)
    g.set_defaults(func=cmd_login)

    sh = sub.add_parser("shot", help="截取浏览器内容区")
    sh.add_argument("name")
    sh.add_argument("--out", default=None)
    sh.set_defaults(func=cmd_shot)

    sw = sub.add_parser("show", help="显示视口信息与已校准的坐标")
    sw.add_argument("name")
    sw.set_defaults(func=cmd_show)

    c = sub.add_parser("close", help="关闭当前标签页")
    c.add_argument("name")
    c.set_defaults(func=cmd_close)

    return p


def main(argv: Sequence[str]) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 2
    try:
        return args.func(args)
    except (RPAError, HumanConfirmationRequired) as e:
        print(f"\n❌ {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
