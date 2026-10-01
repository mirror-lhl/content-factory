#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rpa_example.py — rpa_kit 实战示例

运行方式：
    PYTHONPATH=./rpa_libs python3 rpa_example.py check
    PYTHONPATH=./rpa_libs python3 rpa_example.py look  <App名>     # 截图+网格，供视觉定位
    PYTHONPATH=./rpa_libs python3 rpa_example.py demo  <App名>     # 完整闭环演示（安全操作）
    PYTHONPATH=./rpa_libs python3 rpa_example.py capcut            # 剪映流程骨架（彩排）

先跑 check 确认环境：
    PYTHONPATH=./rpa_libs python3 rpa_kit.py doctor
"""

from __future__ import annotations

import os

import sys
import time

from rpa_kit import RPA, Capture, TimeoutExpired, WindowNotFound


# ══════════════════════════════════════════════════════════════════
# 1. 环境确认 —— 任何脚本的第一步
# ══════════════════════════════════════════════════════════════════

def cmd_check() -> int:
    """确认权限和截图能力都正常，否则后面的坐标全无意义。"""
    rpa = RPA(verbose=False)

    if not rpa.trusted():
        print("❌ 缺少「辅助功能」权限，无法点击/键入。")
        print("   系统设置 → 隐私与安全性 → 辅助功能 → 打开 DeepSeek Harness")
        print("   然后**完全退出并重开** DeepSeek Harness（权限仅启动时读取）")
        return 1
    print("✅ 辅助功能权限正常")

    cap = rpa.screenshot(name="check")
    print(f"✅ 截图正常：{cap.px_w}×{cap.px_h}px @{cap.scale:.1f}x")
    if cap.scale == 1.0:
        print("   （非 Retina，像素坐标 == 屏幕点坐标，换算退化为恒等）")

    wins = rpa.windows()
    print(f"✅ 窗口枚举正常：{len(wins)} 个窗口")
    print()
    print(rpa.describe())
    return 0


# ══════════════════════════════════════════════════════════════════
# 2. 「眼睛」—— 截图 + 网格，交给视觉定位
# ══════════════════════════════════════════════════════════════════

def cmd_look(app: str) -> int:
    """截取目标窗口并叠加网格，便于读出控件坐标。

    典型流程：
        1. 跑这个命令得到带网格的截图路径
        2. 看图，直接按格子读出目标控件的像素坐标
        3. 用 click_capture_point(cap, px, py) 精确点击
    """
    rpa = RPA(verbose=False)
    try:
        win = rpa.ensure_front(app)
    except WindowNotFound as e:
        print(f"❌ {e}")
        return 1

    print(f"目标窗口: {win}")
    cap = rpa.screenshot_window(win, name="look")
    grid = rpa.grid_overlay(cap, cols=8, rows=6, name="look_grid")

    print(f"\n原始截图: {cap.path}")
    print(f"网格截图: {grid.path}")
    print(f"\n换算关系: 1 屏幕点 = {cap.scale:.0f} 像素")
    print(f"  窗口左上角(屏幕点) = ({cap.x:.0f}, {cap.y:.0f})")
    print(f"  网格每格           = {cap.px_w/8:.0f} × {cap.px_h/6:.0f} 像素")
    print(f"\n点截图内某像素 (px,py) 的换算：")
    print(f"  屏幕点 = ({cap.x:.0f} + px/{cap.scale:.0f}, {cap.y:.0f} + py/{cap.scale:.0f})")
    print(f"  —— 直接用 rpa.click_capture_point(cap, px, py) 即可，无需手算")
    return 0


# ══════════════════════════════════════════════════════════════════
# 3. 完整闭环演示 —— 看 → 动 → 验
# ══════════════════════════════════════════════════════════════════

def cmd_demo(app: str) -> int:
    """演示推荐的安全交互模式：点击 → 验证 → 不稳定就重试。

    这里只做无副作用的操作（点标题栏、等待画面稳定），
    目的是让你看到整套骨架长什么样。
    """
    rpa = RPA()
    try:
        win = rpa.ensure_front(app)
    except WindowNotFound as e:
        print(f"❌ {e}")
        return 1

    print(f"\n目标: {win}\n")

    # ① 记录操作前的画面
    before = rpa.screenshot_window(win, name="demo_before")

    # ② 相对坐标点击（窗口被移动过也没关系，ensure_front 会重取 frame）
    print("→ 点击窗口标题栏（相对坐标 0.5, 0.02）")
    rpa.click_window(win, rel=(0.5, 0.02))

    # ③ 等界面渲染稳定，而不是盲目 sleep
    try:
        rpa.wait_stable(region=win.frame, timeout=8, interval=0.3, settle=2)
        print("→ 界面已稳定")
    except TimeoutExpired as e:
        print(f"→ 界面一直在动（{e}），继续执行")

    # ④ 对比前后画面，确认操作真的产生了效果
    after = rpa.screenshot_window(rpa.window(app), name="demo_after")
    d = rpa.pixel_diff(before, after)
    print(f"→ 画面差异 {d*100:.2f}%（阈值 0.2%）")
    if rpa.changed(before, after):
        print("→ ✅ 操作产生了可见变化")
    else:
        print("→ ⚠️ 画面几乎没变 —— 可能没点中，或该操作本就无视觉反馈")

    # ⑤ 演练模式：不真正操作，只打印将要执行的动作
    print("\n【dry_run 彩排】把整个流程先跑一遍看逻辑对不对：")
    rehearsal = RPA(dry_run=True)
    rehearsal.click_window(win, rel=(0.5, 0.02))
    rehearsal.type_text("测试文本")
    rehearsal.hotkey("cmd", "s")
    return 0


# ══════════════════════════════════════════════════════════════════
# 4. 剪映流程骨架 —— 真实任务的写法模板
# ══════════════════════════════════════════════════════════════════

def cmd_capcut(*, live: bool = False) -> int:
    """剪映自动化骨架。

    剪映没有 CLI、没有 AppleScript 支持，草稿文件还是加密的，
    所以**只能**靠视觉 RPA。下面标出每个环节的写法。

    默认彩排（dry_run），加 --live 才真正操作。
    """
    rpa = RPA(dry_run=not live)
    print(f"模式: {'🔴 真实操作' if live else '🟡 彩排（只打印动作）'}\n")

    # ── 步骤 1: 启动并聚焦剪映 ────────────────────────────────
    win = rpa.find_window("剪映") or rpa.find_window("VideoFusion")
    if win is None:
        print("剪映未运行，正在启动…")
        import subprocess
        subprocess.run(["open", "-a", "/Applications/VideoFusion-macOS.app"], check=False)
        try:
            win = rpa.wait_window("剪映", timeout=30)
        except TimeoutExpired:
            win = rpa.wait_window("VideoFusion", timeout=10)
    win = rpa.ensure_front(win)
    print(f"① 剪映窗口: {win}")

    # ── 步骤 2: 用视觉定位控件 ────────────────────────────────
    # 剪映的可访问性树是空的，拿不到按钮语义，所以必须看图定位。
    # 做法：先跑 `python3 rpa_example.py look 剪映` 拿到带网格的截图，
    #       从图上读出「新建草稿」按钮的像素坐标，填到这里。
    #
    # 下面用相对坐标举例（相对坐标跨分辨率更稳）：
    cap = rpa.screenshot_window(win, name="capcut_home") if live else None
    print(f"② 首页截图: {cap.path if cap else '(彩排跳过)'}")
    print("   → 若用像素坐标定位：rpa.click_capture_point(cap, px, py)")

    # 示例：点「开始创作」按钮（坐标需用 look 命令实际标定）
    rpa.click_window(win, rel=(0.50, 0.62), wait=1.5)   # ← 真实使用时替换为实测坐标
    print("③ 已点击「开始创作」（相对坐标 0.50, 0.62 —— 需按实际界面校准）")

    # ── 步骤 3: 等编辑器加载 ─────────────────────────────────
    # 关键：不要用固定 sleep，用画面稳定性判断，慢机器也不会出错
    if live:
        rpa.wait_stable(timeout=30, interval=0.5, settle=3, desc="编辑器加载完成")
    print("④ 已确认编辑器加载完成")

    # ── 步骤 4: 导入素材 ─────────────────────────────────────
    # 导入文件对话框是系统原生控件，用快捷键最稳：⌘I
    rpa.hotkey("cmd", "i")
    time.sleep(1.2)
    # 在文件对话框里用 ⌘⇧G 输入路径，比导航目录稳得多
    rpa.hotkey("cmd", "shift", "g")
    time.sleep(0.8)
    rpa.type_text(os.path.expanduser("~/Movies"))     # ← 改成你自己的目录
    rpa.key("return")
    time.sleep(0.6)
    rpa.key("return")          # 确认选择
    print("⑤ 已导入素材（⌘I → ⌘⇧G 输路径 → 回车）")

    # ── 步骤 5: 导出 ────────────────────────────────────────
    # 导出对话框选项多，务必每步截图确认，不要盲点
    rpa.hotkey("cmd", "e")
    time.sleep(2.0)
    if live:
        rpa.wait_stable(timeout=20, interval=0.5, settle=2, desc="导出面板出现")
        rpa.screenshot(name="capcut_export_panel")
        print("   已截图导出面板，请人工确认分辨率/码率设置无误")
    print("⑥ 导出面板已打开 —— 下一步需要视觉确认具体设置项")

    print("\n完成。真实使用时请用 look 命令逐步标定每处坐标。")
    return 0


# ══════════════════════════════════════════════════════════════════

def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd, *rest = argv

    if cmd == "check":
        return cmd_check()
    if cmd == "look":
        return cmd_look(rest[0] if rest else "剪映")
    if cmd == "demo":
        return cmd_demo(rest[0] if rest else "DeepSeek Harness")
    if cmd == "capcut":
        return cmd_capcut(live="--live" in rest)

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
