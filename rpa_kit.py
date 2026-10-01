#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rpa_kit — macOS 视觉 RPA 工具模块

用纯界面操作驱动任意 macOS 应用，**不需要目标应用提供任何 API**。
适合没有 CLI / 没有 AppleScript 支持的原生应用（例如剪映专业版）。

核心思路
--------
    CGWindowListCopyWindowInfo  →  定位窗口 frame
    screencapture -R <frame>    →  定点截图
    视觉识别（人 or 模型看图）    →  得出控件坐标
    CGEvent 合成 点击/拖拽/键入   →  执行
    再截图比对                    →  验证是否生效

设计原则
--------
1. **坐标永不缓存** —— 每次操作前重新探测窗口 frame；窗口被拖动或分辨率变化后依然正确
2. **相对坐标优先** —— 用窗口宽高比例定位，换分辨率/换机器仍然可用
3. **每步可验证** —— 截图哈希比对，判断 UI 是否变化、是否已稳定
4. **默认安全** —— failsafe 急停、dry_run 彩排、动作前强制把目标窗口置前

依赖
----
    pip install --target=./rpa_libs pyobjc-framework-Quartz \\
                pyobjc-framework-ApplicationServices pyobjc-framework-Cocoa
    运行: PYTHONPATH=./rpa_libs python3 your_script.py

权限（系统设置 → 隐私与安全性）
------------------------------
    辅助功能（Accessibility）  ← 必需，合成鼠标/键盘事件
    屏幕录制（Screen Recording）← 必需，截图
    自动化（Automation）        ← 不需要，本模块走 CGEvent 底层路线

快速上手
--------
    from rpa_kit import RPA

    rpa = RPA()
    win = rpa.window("剪映专业版")          # 按 App 名找窗口
    rpa.ensure_front(win)                   # 确保它在最前
    rpa.click_window(win, rel=(0.5, 0.02))  # 点标题栏（相对坐标）
    rpa.screenshot_window(win)              # 截这个窗口，交给视觉判断
    rpa.type_text("你好")                    # 输入中文
    rpa.hotkey("cmd", "s")                  # 快捷键
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence, Tuple

try:
    import Quartz
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "缺少 pyobjc。请先安装：\n"
        "  pip install --target=./rpa_libs pyobjc-framework-Quartz "
        "pyobjc-framework-ApplicationServices pyobjc-framework-Cocoa\n"
        "然后以 PYTHONPATH=./rpa_libs 运行。"
    ) from e

try:
    from ApplicationServices import AXIsProcessTrusted
except ImportError:  # pragma: no cover
    def AXIsProcessTrusted() -> bool:  # type: ignore
        return False


# ─────────────────────────────── 常量表 ───────────────────────────────

#: 常用按键名 → macOS 虚拟键码（US 布局）
KEYCODES: dict[str, int] = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
    "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29,
    "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51,
    "escape": 53, "esc": 53, "forwarddelete": 117,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "minus": 27, "equal": 24, "leftbracket": 33, "rightbracket": 30,
    "semicolon": 41, "quote": 39, "comma": 43, "period": 47, "slash": 44,
    "backslash": 42, "grave": 50,
}

#: 修饰键名 → 虚拟键码
MOD_KEYCODES: dict[str, int] = {
    "cmd": 55, "command": 55, "shift": 56, "alt": 58, "option": 58,
    "ctrl": 59, "control": 59, "fn": 63,
}

#: 修饰键名 → CGEventFlags
MOD_FLAGS: dict[str, int] = {
    "cmd": Quartz.kCGEventFlagMaskCommand,
    "command": Quartz.kCGEventFlagMaskCommand,
    "shift": Quartz.kCGEventFlagMaskShift,
    "alt": Quartz.kCGEventFlagMaskAlternate,
    "option": Quartz.kCGEventFlagMaskAlternate,
    "ctrl": Quartz.kCGEventFlagMaskControl,
    "control": Quartz.kCGEventFlagMaskControl,
    "fn": Quartz.kCGEventFlagMaskSecondaryFn,
}

_BUTTONS = {
    "left": (Quartz.kCGMouseButtonLeft, Quartz.kCGEventLeftMouseDown,
             Quartz.kCGEventLeftMouseUp, Quartz.kCGEventLeftMouseDragged),
    "right": (Quartz.kCGMouseButtonRight, Quartz.kCGEventRightMouseDown,
              Quartz.kCGEventRightMouseUp, Quartz.kCGEventRightMouseDragged),
    "middle": (Quartz.kCGMouseButtonCenter, Quartz.kCGEventOtherMouseDown,
               Quartz.kCGEventOtherMouseUp, Quartz.kCGEventOtherMouseDragged),
}


# ─────────────────────────────── 异常 ───────────────────────────────

class RPAError(RuntimeError):
    """rpa_kit 所有异常的基类。"""


class PermissionDenied(RPAError):
    """缺少辅助功能或屏幕录制权限。"""


class WindowNotFound(RPAError):
    """按条件没找到窗口。"""


class FailsafeTriggered(RPAError):
    """光标停在急停角落，操作被中止。"""


class TimeoutExpired(RPAError):
    """等待超时。"""


# ─────────────────────────────── 数据结构 ───────────────────────────────

@dataclass(frozen=True)
class Window:
    """一个屏幕上的窗口。所有坐标单位是**点**（point），不是像素。

    Retina 屏上 1 点 = 2 像素，本模块内部统一用点，避免和截图像素混淆。
    """

    app: str
    title: str
    pid: int
    x: float
    y: float
    w: float
    h: float
    layer: int
    number: int

    # ---- 派生几何 ----
    @property
    def frame(self) -> Tuple[float, float, float, float]:
        """(x, y, w, h) 点坐标。"""
        return (self.x, self.y, self.w, self.h)

    @property
    def center(self) -> Tuple[float, float]:
        return (self.x + self.w / 2, self.y + self.h / 2)

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    def rel(self, rx: float, ry: float) -> Tuple[float, float]:
        """把窗口内相对坐标 (0~1) 换算成屏幕绝对点坐标。

        rel=(0.5, 0.5) 是窗口正中；rel=(0, 0) 是左上角。
        """
        return (self.x + rx * self.w, self.y + ry * self.h)

    def rel_rect(self, rx: float, ry: float, rw: float, rh: float
                 ) -> Tuple[float, float, float, float]:
        """窗口内的相对矩形 → 屏幕绝对矩形。"""
        return (self.x + rx * self.w, self.y + ry * self.h, rw * self.w, rh * self.h)

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px <= self.right and self.y <= py <= self.bottom

    def __str__(self) -> str:
        return (f"<Window {self.app!r} title={self.title!r} "
                f"frame=({self.x:.0f},{self.y:.0f},{self.w:.0f}x{self.h:.0f})>")


@dataclass(frozen=True)
class Capture:
    """一次截图的结果。"""

    path: str
    x: float
    y: float
    w: float
    h: float
    px_w: int
    px_h: int

    @property
    def scale(self) -> float:
        """像素 / 点 的缩放系数（Retina 通常为 2.0）。"""
        return (self.px_w / self.w) if self.w else 1.0

    @property
    def region(self) -> Tuple[float, float, float, float]:
        return (self.x, self.y, self.w, self.h)

    def to_screen(self, px: float, py: float) -> Tuple[float, float]:
        """截图内的**像素**坐标 → 屏幕**点**坐标。

        用视觉模型给出的坐标（基于图片像素）定位控件时用它换算。
        """
        s = self.scale or 1.0
        return (self.x + px / s, self.y + py / s)

    def to_pixels(self, sx: float, sy: float) -> Tuple[float, float]:
        """屏幕点坐标 → 本截图内的像素坐标（画标注框用）。"""
        s = self.scale or 1.0
        return ((sx - self.x) * s, (sy - self.y) * s)

    def md5(self) -> str:
        with open(self.path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()

    def __str__(self) -> str:
        return (f"<Capture {self.px_w}x{self.px_h}px @{self.scale:.1f}x "
                f"region=({self.x:.0f},{self.y:.0f},{self.w:.0f}x{self.h:.0f}) "
                f"{os.path.basename(self.path)}>")


# ─────────────────────────────── 主类 ───────────────────────────────

class RPA:
    """macOS 视觉 RPA 控制器。

    Parameters
    ----------
    dry_run:
        True 时只记录将要执行的动作，不真正点击/键入。用于彩排流程。
    failsafe:
        True 时，若光标位于屏幕左上角 5 点内则中止所有操作（紧急停止）。
    shot_dir:
        截图存放目录，默认在系统临时目录下建 rpa_shots。
    verbose:
        是否把每一步动作打到 stderr。
    """

    #: 急停角落的判定半径（点）
    FAILSAFE_RADIUS = 5.0

    #: 用于像素比对的 ffmpeg（缺失时 pixel_diff 自动退化为哈希比较）
    _ffmpeg = shutil.which("ffmpeg")

    def __init__(self, *, dry_run: bool = False, failsafe: bool = True,
                 shot_dir: Optional[str] = None, verbose: bool = True):
        self.dry_run = dry_run
        self.failsafe = failsafe
        self.verbose = verbose
        self.shot_dir = shot_dir or os.path.join(tempfile.gettempdir(), "rpa_shots")
        os.makedirs(self.shot_dir, exist_ok=True)
        self._shot_seq = 0

    # ───────────── 日志 & 前置检查 ─────────────

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[rpa {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)

    def _check_failsafe(self) -> None:
        if not self.failsafe:
            return
        p = self.cursor_pos()
        if p[0] <= self.FAILSAFE_RADIUS and p[1] <= self.FAILSAFE_RADIUS:
            raise FailsafeTriggered(
                f"光标位于急停角落 {p}，操作已中止（把光标移开即可恢复）")

    @staticmethod
    def trusted() -> bool:
        """当前进程是否已获得「辅助功能」权限。"""
        return bool(AXIsProcessTrusted())

    def require_permission(self) -> None:
        """没权限就抛异常，并给出修复指引。"""
        if not self.trusted():
            raise PermissionDenied(
                "缺少「辅助功能」权限，无法合成鼠标/键盘事件。\n"
                "修复：系统设置 → 隐私与安全性 → 辅助功能 → 打开 DeepSeek Harness，"
                "然后**完全退出并重开该 App**（权限仅在进程启动时读取）。")

    def doctor(self) -> dict:
        """体检：权限、工具、显示器、截图能力。返回诊断字典。"""
        report: dict = {"accessibility": self.trusted()}
        report["screencapture"] = bool(shutil.which("screencapture"))
        try:
            b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
            report["main_display_pts"] = (int(b.size.width), int(b.size.height))
        except Exception:
            report["main_display_pts"] = None
        try:
            cap = self.screenshot(name="doctor")
            report["capture_ok"] = True
            report["capture_scale"] = round(cap.scale, 2)
            report["window_count"] = len(self.windows())
        except Exception as e:
            report["capture_ok"] = False
            report["capture_error"] = str(e)
        return report

    # ───────────── 窗口 ─────────────

    def windows(self, *, on_screen_only: bool = True,
                include_small: bool = False, min_size: int = 120) -> list[Window]:
        """列出当前所有窗口，按**前后顺序**（最前在前）返回。"""
        opts = Quartz.kCGWindowListOptionOnScreenOnly if on_screen_only else 0
        opts |= Quartz.kCGWindowListExcludeDesktopElements
        raw = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID)
        out: list[Window] = []
        for w in raw or []:
            b = w.get("kCGWindowBounds") or {}
            width, height = float(b.get("Width", 0)), float(b.get("Height", 0))
            if not include_small and (width < min_size or height < min_size):
                continue
            out.append(Window(
                app=w.get("kCGWindowOwnerName") or "?",
                title=w.get("kCGWindowName") or "",
                pid=int(w.get("kCGWindowOwnerPID") or 0),
                x=float(b.get("X", 0)), y=float(b.get("Y", 0)),
                w=width, h=height,
                layer=int(w.get("kCGWindowLayer") or 0),
                number=int(w.get("kCGWindowNumber") or 0),
            ))
        return out

    def window(self, app: str, *, title_contains: Optional[str] = None,
               index: int = 0, layer: int = 0,
               min_size: int = 120) -> Window:
        """按 App 名（可再按标题过滤）找窗口，返回第 index 个。

        匹配规则：App 名不区分大小写做**子串**匹配，所以 "剪映" 能命中
        "剪映专业版"，"chrome" 能命中 "Google Chrome"。
        """
        win = self.find_window(app, title_contains=title_contains,
                               index=index, layer=layer, min_size=min_size)
        if win is None:
            raise WindowNotFound(
                f"找不到窗口 app≈{app!r}"
                + (f" title≈{title_contains!r}" if title_contains else "")
                + f"\n当前可见窗口: {sorted({w.app for w in self.windows()})}")
        return win

    def find_window(self, app: str, *, title_contains: Optional[str] = None,
                    index: int = 0, layer: int = 0,
                    min_size: int = 120) -> Optional[Window]:
        """同 window()，但找不到时返回 None 而不抛异常。"""
        needle = app.lower()
        hits = [
            w for w in self.windows(min_size=min_size)
            if w.layer == layer
            and needle in w.app.lower()
            and (title_contains is None or title_contains.lower() in w.title.lower())
        ]
        return hits[index] if len(hits) > index else None

    def window_by_number(self, number: int) -> Optional[Window]:
        """按窗口号（kCGWindowNumber）取窗口，**跨所有层级**。

        窗口号在一次会话内是稳定的，比「App 名 + 标题」匹配可靠得多，
        适合在动作之间重新取同一个窗口的最新 frame。
        """
        for w in self.windows(include_small=True):
            if w.number == number:
                return w
        return None

    def wait_window(self, app: str, *, timeout: float = 15.0,
                    title_contains: Optional[str] = None,
                    interval: float = 0.4) -> Window:
        """等窗口出现（用于启动 App 后）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            w = self.find_window(app, title_contains=title_contains)
            if w is not None:
                return w
            time.sleep(interval)
        raise TimeoutExpired(f"{timeout}s 内没等到 {app!r} 的窗口")

    def frontmost_app(self) -> Optional[str]:
        """当前最前的 App 名。"""
        try:
            from AppKit import NSWorkspace
            a = NSWorkspace.sharedWorkspace().frontmostApplication()
            if a is not None:
                return str(a.localizedName())
        except Exception:
            pass
        wins = self.windows()
        return wins[0].app if wins else None

    def activate(self, app: str) -> bool:
        """把 App 置前。优先用 NSRunningApplication（不需要自动化权限）。"""
        try:
            from AppKit import NSRunningApplication, NSApplicationActivateIgnoringOtherApps
            for a in NSRunningApplication.runningApplicationsWithBundleIdentifier_(""):
                pass
        except Exception:
            pass
        try:
            from AppKit import NSWorkspace, NSApplicationActivateIgnoringOtherApps
            needle = app.lower()
            for a in NSWorkspace.sharedWorkspace().runningApplications():
                name = str(a.localizedName() or "")
                if needle in name.lower():
                    ok = a.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
                    self._log(f"activate({name}) → {bool(ok)}")
                    return bool(ok)
        except Exception as e:
            self._log(f"NSRunningApplication 激活失败({e})，回退 open -a")
        r = subprocess.run(["open", "-a", app], capture_output=True, text=True)
        self._log(f"open -a {app!r} → rc={r.returncode}")
        return r.returncode == 0

    def ensure_front(self, win: Window | str, *, tries: int = 3,
                     settle: float = 0.35) -> Window:
        """确保目标窗口在最前，返回**重新探测过**的窗口对象。

        CGEvent 是盲打的：如果目标窗口不在最前，点击会打到别的窗口上。
        所以任何针对具体窗口的操作前都应先调用它。
        """
        app = win.app if isinstance(win, Window) else win
        needle = app.lower()
        current = self.find_window(app)
        for _ in range(tries):
            if current is not None and (self.frontmost_app() or "").lower().find(needle) >= 0:
                return current
            self.activate(app)
            time.sleep(settle)
            current = self.find_window(app)
        # 兜底：直接点它标题栏把它拽到最前
        if current is not None:
            self._log(f"activate 未生效，改用点击标题栏置前 {current.app}")
            self.click(*current.rel(0.5, 0.02), wait=0.0)
            time.sleep(settle)
            current = self.find_window(app)
        if current is None:
            raise WindowNotFound(f"置前后仍找不到 {app!r} 的窗口")
        return current

    @contextmanager
    def focused(self, win: Window | str):
        """上下文管理器：进入时确保窗口置前并交回最新 frame。"""
        yield self.ensure_front(win)

    # ───────────── 截图 ─────────────

    @staticmethod
    def _png_size(path: str) -> Optional[Tuple[int, int]]:
        """直接读 PNG 头拿像素尺寸，避免多起一个进程。"""
        try:
            with open(path, "rb") as f:
                head = f.read(33)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                return struct.unpack(">II", head[16:24])
        except Exception:
            pass
        return None

    def screenshot(self, region: Optional[Sequence[float]] = None, *,
                   name: Optional[str] = None,
                   settle: float = 0.0) -> Capture:
        """截图。region=(x,y,w,h) 为屏幕点坐标；省略则截主显示器整屏。"""
        if settle:
            time.sleep(settle)
        if region is None:
            b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
            region = (b.origin.x, b.origin.y, b.size.width, b.size.height)
        x, y, w, h = (float(v) for v in region)
        if w <= 1 or h <= 1:
            raise RPAError(f"截图区域无效: {region}")

        self._shot_seq += 1
        tag = name or f"{self._shot_seq:03d}"
        path = os.path.join(self.shot_dir, f"{tag}.png")
        # 负坐标（显示器在左侧）时 -R 参数必须整体作为一个 argv 传入
        arg = f"-R{int(round(x))},{int(round(y))},{int(round(w))},{int(round(h))}"
        r = subprocess.run(["screencapture", "-x", arg, path],
                           capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(path):
            raise RPAError(
                f"screencapture 失败 (rc={r.returncode}): {r.stderr.strip()}\n"
                "若提示权限问题：系统设置 → 隐私与安全性 → 屏幕录制 → 勾选本 App")
        px = self._png_size(path) or (0, 0)
        cap = Capture(path=path, x=x, y=y, w=w, h=h, px_w=px[0], px_h=px[1])
        self._log(f"screenshot → {cap}")
        return cap

    def screenshot_window(self, win: Window, *, name: Optional[str] = None,
                          inset: float = 0.0) -> Capture:
        """只截某个窗口（先用最新 frame，不缓存坐标）。

        优先按窗口号重新取，窗口被移动/改变尺寸后也不会截错位置。
        """
        fresh = self.window_by_number(win.number)
        if fresh is None:                     # 窗口号失效，退回按 App 名重取
            fresh = self.find_window(win.app, layer=win.layer) or win
        x, y, w, h = fresh.frame
        if inset:
            x, y, w, h = x + inset, y + inset, w - 2 * inset, h - 2 * inset
        return self.screenshot((x, y, w, h), name=name or fresh.app.replace(" ", "_"))

    # ───────────── 光标 ─────────────

    def cursor_pos(self) -> Tuple[float, float]:
        loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        return (float(loc.x), float(loc.y))

    def move(self, x: float, y: float, *, duration: float = 0.0) -> None:
        """移动光标。duration>0 时按步进平滑移动（某些 UI 需要）。"""
        self._check_failsafe()
        if self.dry_run:
            return self._log(f"[dry] move({x:.0f},{y:.0f})")
        if duration <= 0:
            self._post_mouse(Quartz.kCGEventMouseMoved, x, y)
        else:
            sx, sy = self.cursor_pos()
            steps = max(2, int(duration / 0.016))
            for i in range(1, steps + 1):
                t = i / steps
                self._post_mouse(Quartz.kCGEventMouseMoved,
                                 sx + (x - sx) * t, sy + (y - sy) * t)
                time.sleep(duration / steps)

    def _post_mouse(self, etype: int, x: float, y: float,
                    button: str = "left", flags: int = 0) -> None:
        btn = _BUTTONS[button][0]
        ev = Quartz.CGEventCreateMouseEvent(None, etype, (float(x), float(y)), btn)
        if flags:
            Quartz.CGEventSetFlags(ev, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

    # ───────────── 鼠标动作 ─────────────

    def click(self, x: float, y: float, *, button: str = "left",
              clicks: int = 1, interval: float = 0.06,
              wait: float = 0.12) -> None:
        """在屏幕点坐标处点击。clicks=2 为双击。"""
        self._check_failsafe()
        if self.dry_run:
            return self._log(f"[dry] click({x:.0f},{y:.0f}) button={button} x{clicks}")
        _, down, up, _ = _BUTTONS[button]
        self._post_mouse(Quartz.kCGEventMouseMoved, x, y, button)
        for i in range(clicks):
            self._post_mouse(down, x, y, button)
            time.sleep(0.02)
            self._post_mouse(up, x, y, button)
            if i < clicks - 1:
                time.sleep(interval)
        self._log(f"click({x:.0f},{y:.0f}) {button} x{clicks}")
        if wait:
            time.sleep(wait)

    def click_window(self, win: Window, rel: Tuple[float, float], **kw) -> Window:
        """按**相对坐标**点击窗口内某处，操作前自动置前并重新取 frame。"""
        fresh = self.ensure_front(win)
        x, y = fresh.rel(*rel)
        self.click(x, y, **kw)
        return fresh

    def double_click(self, x: float, y: float, **kw) -> None:
        self.click(x, y, clicks=2, **kw)

    def right_click(self, x: float, y: float, **kw) -> None:
        self.click(x, y, button="right", **kw)

    def drag(self, start: Tuple[float, float], end: Tuple[float, float], *,
             button: str = "left", duration: float = 0.5,
             steps: int = 25, hold: float = 0.15) -> None:
        """从 start 拖到 end（屏幕点坐标），中间分步移动以便目标识别拖拽。"""
        self._check_failsafe()
        if self.dry_run:
            return self._log(f"[dry] drag {start} → {end}")
        _, down, up, dragged = _BUTTONS[button]
        sx, sy = start
        ex, ey = end
        self._post_mouse(Quartz.kCGEventMouseMoved, sx, sy, button)
        time.sleep(0.05)
        self._post_mouse(down, sx, sy, button)
        time.sleep(hold)
        for i in range(1, steps + 1):
            t = i / steps
            self._post_mouse(dragged, sx + (ex - sx) * t, sy + (ey - sy) * t, button)
            time.sleep(duration / steps)
        time.sleep(0.05)
        self._post_mouse(up, ex, ey, button)
        self._log(f"drag ({sx:.0f},{sy:.0f}) → ({ex:.0f},{ey:.0f})")
        time.sleep(0.2)

    def drag_window(self, win: Window, rel_from: Tuple[float, float],
                    rel_to: Tuple[float, float], **kw) -> None:
        """在窗口内按相对坐标拖拽。"""
        fresh = self.ensure_front(win)
        self.drag(fresh.rel(*rel_from), fresh.rel(*rel_to), **kw)

    def scroll(self, amount: int, *, x: Optional[float] = None,
               y: Optional[float] = None, unit: str = "line") -> None:
        """滚轮。amount 为正向上、负向下。"""
        self._check_failsafe()
        if self.dry_run:
            return self._log(f"[dry] scroll({amount})")
        if x is not None and y is not None:
            self.move(x, y)
        u = Quartz.kCGScrollEventUnitLine if unit == "line" else Quartz.kCGScrollEventUnitPixel
        ev = Quartz.CGEventCreateScrollWheelEvent(None, u, 1, int(amount))
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.08)

    # ───────────── 键盘动作 ─────────────

    def _key_event(self, keycode: int, down: bool, flags: int = 0):
        ev = Quartz.CGEventCreateKeyboardEvent(None, int(keycode), bool(down))
        if flags:
            Quartz.CGEventSetFlags(ev, flags)
        return ev

    def key(self, key: str | int, *, flags: int = 0, wait: float = 0.05) -> None:
        """按一下某个键。key 可以是名字（'return'/'f5'）或虚拟键码。"""
        self._check_failsafe()
        code = KEYCODES.get(key.lower(), None) if isinstance(key, str) else int(key)
        if code is None:
            raise RPAError(f"未知按键名: {key!r}（可用: 字母/数字/return/tab/escape/方向键/f1-f12）")
        if self.dry_run:
            return self._log(f"[dry] key({key})")
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, self._key_event(code, True, flags))
        time.sleep(0.01)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, self._key_event(code, False, flags))
        self._log(f"key({key})")
        if wait:
            time.sleep(wait)

    def hotkey(self, *keys: str, wait: float = 0.08) -> None:
        """组合键，如 hotkey('cmd','s') / hotkey('cmd','shift','4')。"""
        self._check_failsafe()
        if not keys:
            return
        *mods, main = keys
        if isinstance(main, str) and main.lower() not in KEYCODES:
            raise RPAError(f"未知按键名: {main!r}")
        flags = 0
        mod_codes = []
        for m in mods:
            ml = m.lower()
            if ml not in MOD_KEYCODES:
                raise RPAError(f"未知修饰键: {m!r}（可用 cmd/shift/alt/ctrl/fn）")
            flags |= MOD_FLAGS[ml]
            mod_codes.append(MOD_KEYCODES[ml])
        code = KEYCODES[main.lower()] if isinstance(main, str) else int(main)
        if self.dry_run:
            return self._log(f"[dry] hotkey({' + '.join(keys)})")
        for mc in mod_codes:
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, self._key_event(mc, True, flags))
            time.sleep(0.012)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, self._key_event(code, True, flags))
        time.sleep(0.02)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, self._key_event(code, False, flags))
        for mc in reversed(mod_codes):
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, self._key_event(mc, False, 0))
            time.sleep(0.012)
        self._log(f"hotkey({' + '.join(keys)})")
        if wait:
            time.sleep(wait)

    def type_text(self, text: str, *, interval: float = 0.012) -> None:
        """输入任意文本，**支持中文/emoji**（走 unicode 字符串通道，不依赖键盘布局）。"""
        self._check_failsafe()
        if self.dry_run:
            return self._log(f"[dry] type_text({text!r})")
        for ch in text:
            for down in (True, False):
                ev = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
                try:
                    Quartz.CGEventKeyboardSetUnicodeString(ev, len(ch), ch)
                except TypeError:
                    Quartz.CGEventKeyboardSetUnicodeString(ev, ch)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(interval)
        self._log(f"type_text({text[:24]!r}{'…' if len(text) > 24 else ''})")

    def clear_and_type(self, text: str, *, select_all: str = "cmd") -> None:
        """全选清空后重新输入。"""
        self.hotkey(select_all, "a")
        time.sleep(0.06)
        self.key("delete")
        time.sleep(0.06)
        self.type_text(text)

    # ───────────── 等待 / 验证 ─────────────

    def region_hash(self, region: Optional[Sequence[float]] = None) -> str:
        """截一小块并返回像素哈希。

        ⚠️ 精确哈希对真实界面很脆弱 —— 任何动画、光标闪烁、抗锯齿抖动都会
        改变哈希。判断「界面是否变化」请优先用 pixel_diff()。
        """
        return self.screenshot(region, name=f"probe_{self._shot_seq + 1}").md5()

    def pixel_diff(self, a: Capture | str, b: Capture | str) -> float:
        """两张截图的**归一化平均像素差**：0.0 = 完全相同，1.0 = 完全不同。

        这是判断界面是否变化的推荐方式：可以设阈值，忽略动画和渲染噪声。
        用 ffmpeg 的 blend(difference) + signalstats 计算；
        没有 ffmpeg 时退化为「完全相同 → 0.0，否则 → 1.0」。
        """
        pa = a.path if isinstance(a, Capture) else a
        pb = b.path if isinstance(b, Capture) else b
        if self._ffmpeg:
            cmd = [
                self._ffmpeg, "-hide_banner", "-loglevel", "error",
                "-i", pa, "-i", pb,
                "-lavfi",
                "[1:v][0:v]scale2ref[b][a];"
                "[a][b]blend=all_mode=difference,format=gray,"
                "signalstats,metadata=print:file=-",
                "-f", "null", "-",
            ]
            r = subprocess.run(cmd, capture_output=True, text=True)
            m = re.search(r"signalstats\.YAVG=([\d.]+)", r.stdout or "")
            if m:
                return float(m.group(1)) / 255.0
        # 退化路径：只能判断「是否逐字节相同」
        same = self._file_md5(pa) == self._file_md5(pb)
        return 0.0 if same else 1.0

    @staticmethod
    def _file_md5(path: str) -> str:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()

    def changed(self, a: Capture | str, b: Capture | str, *,
                threshold: float = 0.002) -> bool:
        """界面是否发生了**有意义**的变化（默认阈值 0.2% 像素差异）。"""
        return self.pixel_diff(a, b) > threshold

    def wait_for(self, predicate: Callable[[], bool], *, timeout: float = 15.0,
                 interval: float = 0.4, desc: str = "条件成立") -> None:
        """轮询等待条件成立，超时抛 TimeoutExpired。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if predicate():
                    self._log(f"✓ {desc}")
                    return
            except Exception:
                pass
            time.sleep(interval)
        raise TimeoutExpired(f"{timeout}s 内未满足: {desc}")

    def wait_for_window(self, app: str, **kw) -> Window:
        return self.wait_window(app, **kw)

    def wait_change(self, region: Optional[Sequence[float]] = None, *,
                    timeout: float = 15.0, interval: float = 0.35,
                    threshold: float = 0.002,
                    desc: str = "界面发生变化") -> Capture:
        """等某区域画面**变化**（比如点完按钮后界面刷新）。返回变化后的截图。"""
        ref = self.screenshot(region, name="wait_change_ref")
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(interval)
            cur = self.screenshot(region, name="wait_change_cur")
            d = self.pixel_diff(ref, cur)
            if d > threshold:
                self._log(f"✓ {desc}（像素差 {d*100:.2f}%）")
                return cur
        raise TimeoutExpired(
            f"{timeout}s 内画面未变化: {desc}（阈值 {threshold*100:.2f}%）")

    def wait_stable(self, region: Optional[Sequence[float]] = None, *,
                    timeout: float = 15.0, interval: float = 0.35,
                    threshold: float = 0.002, settle: int = 2,
                    desc: str = "界面稳定") -> Capture:
        """等某区域画面**连续 settle 次几乎不变**（等动画/渲染结束）。"""
        prev = self.screenshot(region, name="wait_stable_0")
        same = 0
        deadline = time.time() + timeout
        n = 0
        while time.time() < deadline:
            time.sleep(interval)
            n += 1
            cur = self.screenshot(region, name=f"wait_stable_{n}")
            d = self.pixel_diff(prev, cur)
            same = same + 1 if d <= threshold else 0
            prev = cur
            if same >= settle:
                self._log(f"✓ {desc}（连续 {settle} 次像素差 ≤ {threshold*100:.2f}%）")
                return cur
        raise TimeoutExpired(f"{timeout}s 内画面未稳定: {desc}")

    def diff(self, a: Capture | str, b: Capture | str,
             *, threshold: float = 0.002) -> bool:
        """两张截图是否有差异（True = 有变化）。"""
        return self.changed(a, b, threshold=threshold)

    def click_capture_point(self, cap: Capture, px: float, py: float, **kw
                            ) -> Tuple[float, float]:
        """在截图内按**像素坐标**点击。返回实际点击的屏幕点坐标。

        这是「视觉 → 动作」的关键桥梁：视觉看到的坐标是基于图片像素的
        （Retina 上是屏幕点的 2 倍），本方法自动完成换算。
        """
        x, y = cap.to_screen(px, py)
        self._log(f"截图像素({px:.0f},{py:.0f}) → 屏幕点({x:.0f},{y:.0f})")
        self.click(x, y, **kw)
        return (x, y)

    def grid_overlay(self, cap: Capture, *, cols: int = 8, rows: int = 8,
                     name: str = "grid") -> Capture:
        """在截图上叠加等距网格，方便按格子读出目标控件的像素坐标。

        返回新截图；网格线为半透明红色。需要 ffmpeg（无则原样返回）。
        """
        if not self._ffmpeg:
            self._log("无 ffmpeg，跳过网格叠加")
            return cap
        out = os.path.join(self.shot_dir, f"{name}.png")
        cw, ch = cap.px_w / cols, cap.px_h / rows
        cmd = [self._ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-i", cap.path, "-vf",
               f"drawgrid=w={cw:.2f}:h={ch:.2f}:t=1:c=red@0.45", out]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(out):
            self._log(f"网格叠加失败: {r.stderr.strip()[:120]}")
            return cap
        self._log(f"网格 {cols}×{rows} → {out}（每格 {cw:.0f}×{ch:.0f}px）")
        return Capture(path=out, x=cap.x, y=cap.y, w=cap.w, h=cap.h,
                       px_w=cap.px_w, px_h=cap.px_h)

    # ───────────── 便捷组合 ─────────────

    def click_and_verify(self, win: Window, rel: Tuple[float, float], *,
                         region: Optional[Sequence[float]] = None,
                         timeout: float = 8.0, desc: str = "点击生效") -> Capture:
        """点一下窗口内某处，并确认界面确实变了。

        这是最推荐的交互方式：不会「以为点到了」而实际什么都没发生。
        """
        fresh = self.ensure_front(win)
        before = self.screenshot(region or fresh.frame, name="before")
        self.click(*fresh.rel(*rel))
        try:
            self.wait_change(region or fresh.frame, timeout=timeout, desc=desc)
        except TimeoutExpired:
            self._log(f"⚠ {desc}：点击后画面未变化，可能没点中或该操作无视觉反馈")
            raise
        return self.screenshot(region or fresh.frame, name="after")

    @staticmethod
    def grid(win: Window, cols: int = 10, rows: int = 10) -> list[list[Tuple[float, float]]]:
        """生成窗口内的相对坐标网格，便于用视觉定位后确认位置。"""
        return [[((c + 0.5) / cols, (r + 0.5) / rows) for c in range(cols)]
                for r in range(rows)]

    def describe(self) -> str:
        """打印当前屏幕上的窗口清单，便于确定要操作哪个。"""
        lines = [f"{'App':<26} {'标题':<24} {'位置':<18} {'尺寸':<14} 层级"]
        lines.append("-" * 92)
        for w in self.windows():
            lines.append(f"{w.app[:25]:<26} {w.title[:23]:<24} "
                         f"{f'({w.x:.0f},{w.y:.0f})':<18} {f'{w.w:.0f}×{w.h:.0f}':<14} {w.layer}")
        return "\n".join(lines)


# ─────────────────────────────── 自检 ───────────────────────────────

def _selftest(interactive: bool = False) -> int:
    """逐项验证本模块在这台机器上真的可用。

    判据说明：合成事件是否真的投递到系统，用 CGEventSourceCounterForEventType
    的计数器增量来判定 —— 这个方法已用「鼠标移动」对照实验验证过是可靠的。
    （注意：修饰键走 FlagsChanged 通道，不能用 KeyDown 计数器判断。）
    """
    HID = Quartz.kCGEventSourceStateHIDSystemState
    ctr = lambda e: Quartz.CGEventSourceCounterForEventType(HID, e)

    results: list[tuple[str, bool, str]] = []

    def check(name: str, fn: Callable[[], str]) -> bool:
        try:
            detail = fn()
            results.append((name, True, detail))
            print(f"  ✅ {name:<26} {detail}")
            return True
        except Exception as e:
            results.append((name, False, f"{type(e).__name__}: {e}"))
            print(f"  ❌ {name:<26} {type(e).__name__}: {e}")
            return False

    print("\n════ rpa_kit 自检 ════\n")
    rpa = RPA(verbose=False)

    def _need_perm() -> str:
        if not rpa.trusted():
            raise PermissionDenied(
                "未授权。系统设置 → 隐私与安全性 → 辅助功能 → 打开 DeepSeek Harness，"
                "然后完全退出并重开该 App。")
        return "AXIsProcessTrusted() = True"

    check("辅助功能权限", _need_perm)

    check("screencapture 可用", lambda: shutil.which("screencapture") or
          (_ for _ in ()).throw(RPAError("找不到 screencapture")))

    wins = rpa.windows()
    check("窗口枚举", lambda: f"发现 {len(wins)} 个窗口，最前: {wins[0].app if wins else '—'}")

    check("主显示器", lambda: (lambda b: f"{b.size.width:.0f}×{b.size.height:.0f} 点"
                               )(Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())))

    # ---- 截图 + Retina 换算 ----
    cap_holder: dict = {}

    def do_shot() -> str:
        cap = rpa.screenshot(name="selftest")
        cap_holder["cap"] = cap
        return f"{cap.px_w}×{cap.px_h}px @{cap.scale:.1f}x"
    check("整屏截图", do_shot)

    def do_roundtrip() -> str:
        cap = cap_holder["cap"]
        sx, sy = cap.to_screen(cap.px_w / 2, cap.px_h / 2)   # 像素 → 点
        px, py = cap.to_pixels(sx, sy)                        # 点 → 像素
        assert abs(px - cap.px_w / 2) < 0.01 and abs(py - cap.px_h / 2) < 0.01
        return f"像素↔点 换算自洽 (中点 {sx:.0f},{sy:.0f})"
    check("坐标换算", do_roundtrip)

    def do_pixdiff() -> str:
        # 自己和自己比必须是 0；这是 pixel_diff 的正确性基准
        cap = cap_holder["cap"]
        assert rpa.pixel_diff(cap, cap) == 0.0, "同图自比不为 0，pixel_diff 异常"
        # 两次截取同一小块：动态界面上应仍是「几乎没变」
        r = (cap.x + 20, cap.y + 20, 200, 120)
        a1 = rpa.screenshot(r, name="pd1")
        time.sleep(0.3)
        a2 = rpa.screenshot(r, name="pd2")
        d = rpa.pixel_diff(a1, a2)
        assert d < 0.5, f"相邻两次截图差异过大({d:.3f})，截图或比对有问题"
        return f"同图自比=0.000，相邻两帧差={d*100:.2f}%（{self_ffmpeg_note()}）"

    def self_ffmpeg_note() -> str:
        return "ffmpeg 精确比对" if RPA._ffmpeg else "退化哈希比对"
    check("像素比对", do_pixdiff)

    # ---- 窗口定点截图 ----
    normal = [w for w in wins if w.layer == 0]      # 排除程序坞/菜单栏等特殊层

    def do_win_shot() -> str:
        assert normal, "没有普通窗口可截"
        w = normal[0]
        c = rpa.screenshot_window(w)
        # 窗口号回查必须拿到同一个窗口
        again = rpa.window_by_number(w.number)
        assert again is not None and again.number == w.number, "按窗口号回查失败"
        return f"{w.app}: {c.px_w}×{c.px_h}px（窗口号回查一致 #{w.number}）"
    check("窗口定点截图", do_win_shot)

    # ---- 鼠标事件投递 ----
    def do_move() -> str:
        p0 = rpa.cursor_pos()
        tgt = (p0[0] + 60, p0[1] + 40)
        rpa.move(*tgt)
        time.sleep(0.2)
        p1 = rpa.cursor_pos()
        rpa.move(*p0)
        assert abs(p1[0] - tgt[0]) < 3 and abs(p1[1] - tgt[1]) < 3, f"读回 {p1} ≠ 目标 {tgt}"
        return f"移动到 {tgt[0]:.0f},{tgt[1]:.0f} 读回一致"
    check("鼠标移动", do_move)

    def do_click() -> str:
        b = ctr(Quartz.kCGEventLeftMouseDown)
        # 点普通窗口的标题栏 —— 只会重新聚焦，无副作用
        w = normal[0] if normal else wins[0]
        rpa.click(*w.rel(0.5, 0.02))
        a = ctr(Quartz.kCGEventLeftMouseDown)
        assert a > b, f"计数器未增长 ({b}→{a})"
        return f"左键计数器 {b} → {a}（点 {w.app} 标题栏）"
    check("鼠标点击", do_click)

    def do_key() -> str:
        b = ctr(Quartz.kCGEventKeyDown)
        rpa.key("escape")   # 无副作用
        a = ctr(Quartz.kCGEventKeyDown)
        assert a > b, f"KeyDown 计数器未增长 ({b}→{a})"
        return f"KeyDown 计数器 {b} → {a}"
    check("键盘按键", do_key)

    def do_modifier() -> str:
        b = ctr(Quartz.kCGEventFlagsChanged)
        rpa.hotkey("cmd", "escape")   # cmd+esc 无副作用
        a = ctr(Quartz.kCGEventFlagsChanged)
        assert a > b, f"FlagsChanged 计数器未增长 ({b}→{a})"
        return f"FlagsChanged 计数器 {b} → {a}（修饰键走此通道）"
    check("组合键/修饰键", do_modifier)

    # ---- 视觉稳定性等待 ----
    def do_stable() -> str:
        rpa.wait_stable(timeout=10, interval=0.35, settle=2, desc="界面稳定")
        return "画面连续 2 次几乎不变"
    check("等待画面稳定", do_stable)

    # ---- 可选：中文输入端到端（会打开 TextEdit）----
    if interactive:
        print("\n  — 端到端中文输入测试（会临时打开 TextEdit）—")
        tmp = os.path.join(tempfile.gettempdir(), "rpa_kit_typing_test.txt")
        with open(tmp, "w"):
            pass
        payload = "中文输入测试 rpa_kit 123"
        # TextEdit 在中文本地化系统里叫「文本编辑」，两种名字都认
        TE_NAMES = ("TextEdit", "文本编辑")
        cur_pid = os.getpid()

        def pick_editor() -> Optional[Window]:
            """取 TextEdit 的普通窗口（排除本进程和其他 App 的干扰）。"""
            for w in rpa.windows():
                if w.layer == 0 and any(n.lower() in w.app.lower() for n in TE_NAMES):
                    return w
            return None

        def do_type() -> str:
            subprocess.run(["open", "-a", "TextEdit", tmp], check=False)
            rpa.wait_for(lambda: pick_editor() is not None,
                         timeout=20, interval=0.5, desc="TextEdit 窗口出现")
            time.sleep(1.5)
            w = pick_editor()
            assert w is not None, "拿不到 TextEdit 窗口"
            rpa.ensure_front(w)
            time.sleep(0.4)
            # 点文本区中央并全选，确保输入落到正文
            rpa.click(*rpa.window(w.app).rel(0.5, 0.6))
            time.sleep(0.4)
            rpa.hotkey("cmd", "a")
            rpa.type_text(payload)
            time.sleep(0.6)
            # 用剪贴板回读做客观验证（不依赖视觉）
            rpa.hotkey("cmd", "a")
            rpa.hotkey("cmd", "c")
            time.sleep(0.5)
            got = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
            assert payload in got, f"回读内容不匹配，实际得到: {got[:100]!r}"
            return f"输入并回读成功: {got[:28]!r}"
        check("中文端到端输入", do_type)

        def do_cleanup() -> str:
            rpa.hotkey("cmd", "w")      # 关文档
            time.sleep(1.0)
            rpa.key("delete")           # 保存对话框默认「不保存」位置的兜底
            time.sleep(0.3)
            rpa.hotkey("cmd", "d")      # 不保存
            time.sleep(0.8)
            subprocess.run(["rm", "-f", tmp], check=False)
            return "已关闭测试文档（未保存）"
        check("清理测试窗口", do_cleanup)

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n──── 结果: {passed}/{total} 通过 ────")
    if passed < total:
        print("失败项：")
        for n, ok, d in results:
            if not ok:
                print(f"  · {n}: {d}")
    else:
        print("✅ 全部通过，RPA 可用")
    print()
    return 0 if passed == total else 1


# ─────────────────────────────── CLI ───────────────────────────────

def _main(argv: Sequence[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(description="rpa_kit 命令行工具")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("doctor", help="环境体检（权限/截图/窗口）")
    sub.add_parser("windows", help="列出当前窗口")

    ss = sub.add_parser("selftest", help="逐项自检（证明确实可用）")
    ss.add_argument("-i", "--interactive", action="store_true",
                    help="额外做中文输入端到端测试（会临时打开 TextEdit）")

    sp = sub.add_parser("shot", help="截图")
    sp.add_argument("--app", help="只截某个 App 的窗口；省略则截主屏")
    sp.add_argument("--out", help="输出路径")
    sp.add_argument("--region", help="x,y,w,h")

    sm = sub.add_parser("move", help="移动光标（验证权限用）")
    sm.add_argument("x", type=float)
    sm.add_argument("y", type=float)

    st = sub.add_parser("type", help="输入文本")
    st.add_argument("text")

    args = p.parse_args(argv)
    rpa = RPA()

    if args.cmd == "selftest":
        return _selftest(args.interactive)

    if args.cmd == "doctor":
        rep = rpa.doctor()
        ok = rep.get("accessibility") and rep.get("capture_ok")
        for k, v in rep.items():
            print(f"  {k:<18} {v}")
        print(f"\n结论: {'✅ 可以开始 RPA' if ok else '❌ 还缺权限，见上面输出'}")
        return 0 if ok else 1

    if args.cmd == "windows":
        print(rpa.describe())
        return 0

    if args.cmd == "shot":
        region = tuple(float(v) for v in args.region.split(",")) if args.region else None
        cap = rpa.screenshot_window(rpa.window(args.app)) if args.app else rpa.screenshot(region)
        if args.out:
            shutil.copyfile(cap.path, args.out)
            print(args.out)
        else:
            print(cap.path)
        return 0

    if args.cmd == "move":
        rpa.require_permission()
        before = rpa.cursor_pos()
        rpa.move(args.x, args.y)
        time.sleep(0.2)
        after = rpa.cursor_pos()
        rpa.move(*before)
        ok = abs(after[0] - args.x) < 3 and abs(after[1] - args.y) < 3
        print(f"目标 {args.x},{args.y} → 实际 {after[0]:.0f},{after[1]:.0f}  "
              f"{'✅ 权限正常' if ok else '❌ 未生效'}")
        return 0 if ok else 1

    if args.cmd == "type":
        rpa.require_permission()
        rpa.type_text(args.text)
        print("已输入")
        return 0

    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
