# rpa_kit — macOS 视觉 RPA 工具模块

用**纯界面操作**驱动任意 macOS 应用，不需要目标应用提供任何 API。
专为「没有 CLI、没有 AppleScript 支持、数据文件还加密」的原生应用设计（比如剪映专业版）。

---

## 为什么需要它

有些应用从外部完全无法驱动：

| 应用 | CLI | AppleScript | 草稿/数据文件 | 结论 |
|---|---|---|---|---|
| 剪映专业版 11.5 | ❌ | ❌ | 🔒 加密 | **只能靠视觉 RPA** |

`rpa_kit` 走的是最低层但最通用的路：截屏理解界面 → 合成鼠标键盘事件。

---

## 原理

```
CGWindowListCopyWindowInfo  →  定位窗口 frame（位置/尺寸，实时）
        ↓
screencapture -R <frame>    →  定点截图（只截目标窗口）
        ↓
视觉识别（人或模型看图）      →  得出控件坐标
        ↓
CGEvent 合成 点击/拖拽/键入   →  执行操作
        ↓
再截图比对                    →  验证是否真的生效
```

---

## 安装

```bash
pip install --target=./rpa_libs \
    pyobjc-framework-Quartz \
    pyobjc-framework-ApplicationServices \
    pyobjc-framework-Cocoa

# 之后运行都加上 PYTHONPATH
PYTHONPATH=./rpa_libs python3 your_script.py
```

可选：装 `ffmpeg` 可启用**精确像素比对**（`pixel_diff`）。没有则退化为哈希比对。

---

## 权限（系统设置 → 隐私与安全性）

| 权限 | 是否必需 | 用途 |
|---|---|---|
| **辅助功能**（Accessibility） | ✅ 必需 | 合成鼠标/键盘事件 |
| **屏幕录制**（Screen Recording） | ✅ 必需 | 截图与窗口枚举 |
| 自动化（Automation） | ❌ 不需要 | 本模块走 CGEvent，不用 Apple Events |

> ⚠️ **授权后必须完全退出并重开该 App** —— 辅助功能权限只在进程启动时读取一次。
> 如果开了还不生效：在辅助功能列表里用 `−` 删掉该项，再用 `+` 重新添加。

---

## 快速开始

```bash
# 1. 环境体检
PYTHONPATH=./rpa_libs python3 rpa_kit.py doctor

# 2. 逐项自检（证明真的可用）
PYTHONPATH=./rpa_libs python3 rpa_kit.py selftest
PYTHONPATH=./rpa_libs python3 rpa_kit.py selftest -i   # 含中文输入端到端测试

# 3. 看有哪些窗口
PYTHONPATH=./rpa_libs python3 rpa_kit.py windows

# 4. 截图 + 网格（用于视觉定位控件）
PYTHONPATH=./rpa_libs python3 rpa_example.py look 剪映
```

最小示例：

```python
from rpa_kit import RPA

rpa = RPA()
rpa.require_permission()               # 没权限就立刻报错，附带修复指引

win = rpa.ensure_front("剪映")          # 找窗口并置前，返回最新 frame
cap = rpa.screenshot_window(win)       # 截这个窗口

# 方式 A：相对坐标（跨分辨率最稳，推荐）
rpa.click_window(win, rel=(0.5, 0.62))

# 方式 B：像素坐标（视觉看图定位后直接用，自动处理 Retina 换算）
rpa.click_capture_point(cap, px=1280, py=910)

rpa.type_text("你好，世界")             # 支持中文
rpa.hotkey("cmd", "s")
```

---

## 核心概念

### 点 vs 像素（最容易踩的坑）

macOS 的坐标系用**点**；Retina 屏上 `1 点 = 2 像素`。

- 窗口 frame、`CGEvent` 坐标、`screencapture -R` 参数 → **点**
- 截出来的 PNG 文件尺寸 → **像素**（Retina 上是 2 倍）

`rpa_kit` 内部统一用点。当视觉从图上读出坐标时，用 `Capture.to_screen()`
或直接 `click_capture_point(cap, px, py)` 完成换算，**不要手动乘除**。

```
截图宽 2560px，窗口宽 1280 点  →  scale = 2.0
图上像素 (1280, 820)  →  窗口内偏移 (640, 410)  →  屏幕点 (116+640, 38+410)
```

### 坐标永不缓存

每次操作前都会重新探测窗口 frame。窗口被拖动、改变尺寸、切换分辨率后依然正确。
`ensure_front()` / `click_window()` 都返回**新的** `Window` 对象。

### 相对坐标优先

`rel=(0.5, 0.5)` 是窗口正中。用相对坐标写流程，换机器、换分辨率都能跑。

### 窗口号比标题可靠

`Window.number`（`kCGWindowNumber`）在一次会话内稳定。需要在对同一个窗口
连续操作时，用 `window_by_number()` 回查，而不是拿标题字符串去匹配。

---

## 推荐工作流：看 → 动 → 验

**不要写完一串点击就祈祷它成功。** 每步都验证：

```python
# 一次点击 + 自动验证界面确实变了
rpa.click_and_verify(win, rel=(0.5, 0.62), timeout=8)
# 画面没变化会抛 TimeoutExpired —— 说明没点中，而不是默默失败

# 等界面渲染完，而不是盲 sleep
rpa.wait_stable(timeout=30, interval=0.5, settle=3)

# 等某个变化发生
rpa.wait_change(region=win.frame, timeout=10)

# 比对两张截图
if rpa.changed(before, after):     # 差异 > 0.2%
    print("操作生效了")

# 先彩排，确认逻辑无误再真跑
rehearsal = RPA(dry_run=True)
rehearsal.click_window(win, rel=(0.5, 0.62))   # 只打印，不执行
```

### 为什么用 `pixel_diff` 而不是哈希

真实界面永远在动（光标闪烁、加载动画、抗锯齿抖动），**精确哈希必然误判**。
`pixel_diff()` 返回归一化平均像素差，可以设阈值忽略噪声：

```python
rpa.pixel_diff(a, b)              # 0.0 = 完全相同，1.0 = 完全不同
rpa.changed(a, b, threshold=0.002)  # 超过 0.2% 才算变化
```

---

## API 速查

### 窗口

| 方法 | 说明 |
|---|---|
| `windows()` | 列出所有窗口，按前后顺序 |
| `window(app, title_contains=…)` | 找窗口，找不到抛 `WindowNotFound` |
| `find_window(app)` | 同上，找不到返回 `None` |
| `window_by_number(n)` | 按窗口号回查（跨层级，最可靠） |
| `wait_window(app, timeout=…)` | 等窗口出现 |
| `ensure_front(win)` | 确保置前并返回最新 frame |
| `focused(win)` | 上下文管理器版 |
| `frontmost_app()` | 当前最前的 App 名 |
| `describe()` | 打印窗口清单表格 |

### 截图

| 方法 | 说明 |
|---|---|
| `screenshot(region=None)` | 截屏，返回 `Capture` |
| `screenshot_window(win)` | 只截某个窗口 |
| `grid_overlay(cap, cols, rows)` | 叠加网格，便于读坐标 |
| `capture.to_screen(px, py)` | 截图像素 → 屏幕点 |
| `capture.to_pixels(sx, sy)` | 屏幕点 → 截图像素 |

### 鼠标

| 方法 | 说明 |
|---|---|
| `move(x, y)` | 移动光标 |
| `click(x, y, button=…, clicks=…)` | 点击（`clicks=2` 双击） |
| `click_window(win, rel=(rx, ry))` | 按相对坐标点击，自动置前 |
| `click_capture_point(cap, px, py)` | 按截图像素坐标点击 |
| `double_click` / `right_click` | 双击 / 右键 |
| `drag(start, end, duration=…)` | 拖拽（分步移动，目标能识别） |
| `drag_window(win, rel_from, rel_to)` | 窗口内相对拖拽 |
| `scroll(amount, unit="line")` | 滚轮 |

### 键盘

| 方法 | 说明 |
|---|---|
| `type_text(text)` | 输入任意文本，**支持中文/emoji** |
| `clear_and_type(text)` | 全选清空后输入 |
| `key("return")` | 按单键（可用名字或虚拟键码） |
| `hotkey("cmd", "shift", "4")` | 组合键 |

### 等待与验证

| 方法 | 说明 |
|---|---|
| `wait_for(predicate, timeout=…)` | 轮询等条件成立 |
| `wait_change(region, timeout=…)` | 等画面变化 |
| `wait_stable(region, settle=2)` | 等画面连续 N 次几乎不变 |
| `pixel_diff(a, b)` | 归一化像素差 0.0~1.0 |
| `changed(a, b)` | 是否超过阈值 |
| `click_and_verify(win, rel)` | 点击并验证生效 |

### 安全与诊断

| 方法 / 参数 | 说明 |
|---|---|
| `RPA(dry_run=True)` | 彩排：只打印动作，不执行 |
| `RPA(failsafe=True)` | 光标移到屏幕左上角 5 点内即急停 |
| `trusted()` | 是否已获辅助功能权限 |
| `require_permission()` | 没权限直接抛异常 |
| `doctor()` | 环境体检报告 |

---

## 局限（务必了解）

1. **`CGEvent` 是盲打的** —— 它把事件塞进系统输入流，不关心目标是谁。
   目标窗口必须置前，否则点击会落到别的窗口上。用 `ensure_front()` 保证。

2. **剪映的可访问性树几乎为空** —— 拿不到按钮的名称和语义，只能纯视觉定位 + 坐标点击。
   界面改版后坐标需要重新标定。

3. **窗口位置/分辨率变化** —— 已用「每次重取 frame + 相对坐标」缓解，但跨显示器
   拖动窗口后仍建议重新标定。

4. **屏幕锁定、休眠、被其他 App 抢焦点** 都会让流程失败。关键步骤务必加验证和重试。

5. **速度** —— 每步截图 + 分析约 0.3~1 秒，适合流程化任务，不适合高频操作。

### 适合 / 不适合交给 RPA 的活

| 适合 | 不适合 |
|---|---|
| 批量导入素材、套用固定模板 | 需要审美判断的创意剪辑 |
| 重复性参数设置、批量导出 | 画面节奏、转场选择 |
| 跨应用的数据搬运 | 一次性的临时操作 |

---

## 文件说明

| 文件 | 用途 |
|---|---|
| `rpa_kit.py` | 模块本体 + CLI（`doctor` / `windows` / `shot` / `move` / `type` / `selftest`） |
| `rpa_example.py` | 实战示例（`check` / `look` / `demo` / `capcut`） |
| `rpa_shots/` | 截图输出目录 |

---

## 自检说明

`selftest` 用 `CGEventSourceCounterForEventType` 的计数器增量来**客观判定**
合成事件是否真的投递到系统，而不是「看起来没报错」。

> 踩过的坑：修饰键（Shift/Cmd）走的是 `FlagsChanged` 通道，**不会**增加 `KeyDown`
> 计数器。最初用 Shift 测键盘得到「失败」是假阴性，换成普通键（Escape）才正确。
> 这个对照实验保留在自检逻辑里。

---

# browser_rpa —— 浏览器像素 RPA 骨架

`rpa_kit` 是底层能力，`browser_rpa` 是**面向"打开浏览器 → 输入网址 → 等登录 → 操作"
这条流程**的上层骨架。不装 Playwright、不碰 CDP、不改浏览器配置。

## 三条设计原则

| 原则 | 实现 |
|---|---|
| **永不动你已有的标签页** | 所有操作都在 `⌘T` 新开的标签页里，用完 `⌘W` 关掉 |
| **永不自动点「发布」** | 危险坐标必须标 `danger: true`，默认**拒绝执行**，需 `--allow-danger` 且逐次人工确认 |
| **坐标用视口相对比例存** | `rx, ry ∈ [0,1]`，换分辨率、改窗口大小仍然可用 |

## 视口 vs 窗口（关键概念）

浏览器**窗口** ≠ 网页**内容区**。窗口顶部有标签栏和地址栏：

```
Chrome 窗口 frame : (0,38) 1512×890      ← CGWindow 拿到的
        ↓ 减去 insets.top = 88
内容区(视口)      : (0,126) 1512×802     ← 截图和点击都用这个
```

本机实测：`insets.top = 88` 精度正确（截出来的图就是纯网页内容，无地址栏）。

## 使用方法

```bash
alias br="PYTHONPATH=./rpa_libs python3 browser_rpa.py"

# ① 新标签页打开目标站点
br open https://creator.xiaohongshu.com --name xiaohongshu

# ② 看视口换算信息
br show xiaohongshu

# ③ 截图 + 叠网格，用于视觉校准
br look xiaohongshu --cols 10 --rows 8

# ④ 人工登录（脚本暂停等你）
br login xiaohongshu

# ⑤ 从网格图上读出坐标，填进 browser_profiles/xiaohongshu.json
# ⑥ 跑流程
br run xiaohongshu --dry-run     # 彩排
```

### 坐标怎么从图里读出来

网格图每格 `宽/cols × 高/rows` 像素。数出目标控件在第几列第几格：

```
像素坐标 (px, py)  →  视口比例 rx = px / 图宽,  ry = py / 图高
```

填进 profile：

```json
{
  "name": "xiaohongshu",
  "url": "https://creator.xiaohongshu.com",
  "insets": { "top": 88.0, "left": 0.0, "right": 0.0, "bottom": 0.0 },
  "hotspots": {
    "upload_entry": { "rx": 0.312, "ry": 0.401, "label": "上传图文", "danger": false },
    "title_box":    { "rx": 0.500, "ry": 0.130, "label": "标题输入框", "danger": false },
    "publish_btn":  { "rx": 0.883, "ry": 0.937, "label": "发布", "danger": true }
  },
  "login_marker": [0.02, 0.03, 0.10, 0.06]
}
```

### 自动判断"登录完成"

`login_marker` 是视口内的相对矩形（通常是头像区）。逻辑是：

> 登录**前**截一张存为基准 → 轮询该区域 → 像素差异 > 2% 即认为登录完成

**不需要参考图**，比对变化即可。不配 `login_marker` 则退化为"按回车确认"。

## 富文本编辑器必须用粘贴

这是国内平台的共性问题，调研已证实：

| 平台 | 编辑器 | 直接赋值是否有效 |
|---|---|---|
| 微信公众号 | UEditor（iframe） | ❌ 已实测报错 |
| 知乎 | Draft.js | ❌ 原文「不能用元素赋值的方法」 |
| 今日头条 | ProseMirror + React | ❌ 注入 innerHTML 不触发输入事件 |
| 小红书 | tiptap / ProseMirror | ❌ 同类问题 |

所以 `BrowserSession.paste()` 走剪贴板：`pbcopy` 写入 → `⌘V` 粘贴。这是唯一可靠方式。

## ⚠️ 像素 RPA 在浏览器上的固有局限

1. **浮层会拦截点击** —— 实测中 Chrome 自己弹出了 Google 翻译提示框。操作前要检查有没有弹窗遮挡。
2. **页面滚动位置影响坐标** —— 相对坐标只在**同一个滚动位置**下有效。滚动后需重新校准。
3. **页面改版即失效** —— 调研显示抖音创作者中心**几乎每月改版**（placeholder 文案一变就断）。
4. **加载慢的内容** —— 用 `wait_stable()` 而不是 `sleep()`。
5. **无头/后台运行不可用** —— 像素方案必须有可见窗口且在最前。

## 与 CDP 方案的分工

| | 像素 RPA（本骨架） | CDP + Playwright |
|---|---|---|
| 安装 | 无需额外依赖 | 需装 Playwright |
| 复用现有登录态 | ✅ 直接用 | ⚠️ Chrome 136+ 受限 |
| 定位方式 | 坐标（脆弱） | DOM 选择器（稳定） |
| 页面改版 | 立即失效 | 通常仍可用 |
| 视频上传 | 痛苦（需操作原生对话框） | `set_input_files()` 一行 |

**结论**：像素方案适合**快速验证**和**不想动浏览器配置**的场景；一旦流程固定、
要长期跑，CDP 方案在稳定性和维护成本上全面占优。

## 合规提醒

国内主流平台（微信/小红书/抖音/B站/知乎/头条）的用户协议**普遍明文禁止**
自动化程序操作平台，且都有大规模执法先例（小红书处置 42 万账号、B 站对
2 万 star 的开源文档库发律师函）。

本骨架的设计取向是**辅助而非替代**：人工登录、内容由人创作、发布由人确认。
完整风险分析见 `自媒体平台自动化发布-可行性报告.md`。

