#!/bin/bash
# ═══════════════════════════════════════════════════════════
#  运营助手 · 一键安装
#
#  双击本文件即可。它会：
#    1. 检查 Python
#    2. 在项目目录建独立环境（不污染你的系统 Python）
#    3. 装 Python 依赖
#    4. 下载自带 ffmpeg（不需要 Homebrew）
#    5. 检查字体
#    6. 造示例素材（首次运行）
#    7. 启动
# ═══════════════════════════════════════════════════════════

cd "$(dirname "$0")" || exit 1
ROOT="$PWD"
VENV="$ROOT/.venv"
BIN="$ROOT/bin"
PORT="${1:-8765}"

# ── 输出工具 ──
B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'
ok()   { echo "  ${G}✅${N} $1"; }
warn() { echo "  ${Y}⚠️ ${N} $1"; }
bad()  { echo "  ${R}❌${N} $1"; }
step() { echo; echo "${B}$1${N}"; }

clear
echo "${B}运营助手 · 内容工厂${N}"
echo "${D}─────────────────────────────────────────${N}"

# ═══ 1. Python ═══
step "① 检查 Python"

PY=""
for cand in python3 python3.13 python3.12 python3.11 python3.10 python3.9; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
      PY="$(command -v "$cand")"; break
    fi
  fi
done

if [ -z "$PY" ]; then
  bad "找不到 Python 3.9 或更新版本"
  echo
  echo "  macOS 现在不自带 Python。两种装法，任选一种："
  echo
  echo "  ${B}方法一（推荐，命令行里执行）：${N}"
  echo "      xcode-select --install"
  echo
  echo "  ${B}方法二（图形界面下载）：${N}"
  echo "      https://www.python.org/downloads/macos/"
  echo
  echo "  装完后重新双击本文件。"
  echo
  read -r -p "按回车关闭…"
  exit 1
fi
ok "Python $("$PY" -c 'import platform;print(platform.python_version())')  ${D}($PY)${N}"

# ═══ 2. 独立环境 ═══
step "② 建立独立环境"
if [ -x "$VENV/bin/python" ]; then
  ok "已有 .venv（跳过）"
else
  echo "  ${D}在项目目录建 .venv —— 不碰你的系统 Python${N}"
  if "$PY" -m venv "$VENV" 2>/dev/null; then
    ok "已创建 .venv"
  else
    bad "创建 .venv 失败"
    echo "     可能是 Python 安装不完整（缺 venv 模块）。"
    echo "     试：brew install python  或重装 python.org 版本"
    read -r -p "按回车关闭…"; exit 1
  fi
fi
VPY="$VENV/bin/python"

# ═══ 3. Python 依赖 ═══
step "③ 安装 Python 依赖"
need_install=0
"$VPY" -c 'import PIL' 2>/dev/null || need_install=1
"$VPY" -c 'import numpy' 2>/dev/null || need_install=1

if [ "$need_install" = "1" ]; then
  echo "  ${D}安装 Pillow + numpy（约 20MB，看网速要一两分钟）${N}"
  # 有些环境 ~/Library/Caches/pip 不可写，会刷一堆 WARNING。
  # 用项目内的缓存目录，既安静又不污染系统。
  export PIP_CACHE_DIR="$ROOT/.pipcache"
  export PIP_DISABLE_PIP_VERSION_CHECK=1
  "$VPY" -m pip install --quiet --upgrade pip 2>/dev/null
  if "$VPY" -m pip install --quiet Pillow numpy; then
    ok "Pillow + numpy"
  else
    bad "安装失败。可能是网络问题，重试一次；"
    echo "     或换国内源："
    echo "     $VPY -m pip install Pillow numpy -i https://pypi.tuna.tsinghua.edu.cn/simple"
    read -r -p "按回车关闭…"; exit 1
  fi
else
  ok "Pillow + numpy（已装）"
fi

# ═══ 4. ffmpeg ═══
step "④ 准备 ffmpeg"
FF=""

# 项目自带优先
if [ -x "$BIN/ffmpeg" ] && "$BIN/ffmpeg" -version >/dev/null 2>&1; then
  FF="$BIN/ffmpeg"; ok "项目自带 bin/ffmpeg"
fi

# 其次系统里已有的
if [ -z "$FF" ]; then
  for c in ffmpeg /opt/homebrew/bin/ffmpeg /usr/local/bin/ffmpeg; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -version >/dev/null 2>&1; then
      FF="$(command -v "$c")"; ok "用系统已有 ffmpeg  ${D}($FF)${N}"; break
    fi
  done
fi

# 都没有 → 下载静态版（不需要 Homebrew）
if [ -z "$FF" ]; then
  ARCH="$(uname -m)"
  if [ "$ARCH" = "arm64" ]; then
    URL="https://www.osxexperts.net/ffmpeg71arm.zip"
    WHAT="Apple 芯片"
  else
    URL="https://www.osxexperts.net/ffmpeg71intel.zip"
    WHAT="Intel 芯片"
  fi
  echo "  检测到 $WHAT，下载自带 ffmpeg（约 20MB，${D}不需要 Homebrew${N}）"
  mkdir -p "$BIN"
  TMPZ="$(mktemp -d)/ff.zip"
  if curl -L --progress-bar --max-time 300 -o "$TMPZ" "$URL" 2>/dev/null; then
    if unzip -o -q "$TMPZ" -d "$BIN" 2>/dev/null; then
      chmod +x "$BIN"/ffmpeg "$BIN"/ffprobe 2>/dev/null
      rm -rf "$BIN/__MACOSX" 2>/dev/null
      if [ -x "$BIN/ffmpeg" ] && "$BIN/ffmpeg" -version >/dev/null 2>&1; then
        FF="$BIN/ffmpeg"; ok "已下载 bin/ffmpeg  ${D}(自带，不需要 Homebrew)${N}"
        # 去掉隔离标记，避免 Gatekeeper 拦
        xattr -dr com.apple.quarantine "$BIN" 2>/dev/null
      fi
    fi
  fi
  if [ -z "$FF" ]; then
    warn "自动下载失败 —— 视频功能将不可用（图文和封面不受影响）"
    echo "     ${D}手动装：brew install ffmpeg${N}"
    echo "     ${D}或下载：https://ffmpeg.org/download.html${N}"
  fi
fi

# ffprobe 检查（有些来源只给 ffmpeg）
if [ -n "$FF" ]; then
  # ffprobe 不再是必需 —— probe_duration 会自动回落到 ffmpeg（时长就在 -i 输出里）
  if ! command -v ffprobe >/dev/null 2>&1 && [ ! -x "$BIN/ffprobe" ]; then
    echo "  ${D}（无 ffprobe，时长探测走 ffmpeg 兜底，不影响）${N}"
  fi
  # 关键滤镜自检
  MISSING=""
  for f in zoompan xfade overlay acrossfade amix; do
    "$FF" -hide_banner -filters 2>/dev/null | grep -qw "$f" || MISSING="$MISSING $f"
  done
  [ -n "$MISSING" ] && warn "ffmpeg 缺少滤镜:$MISSING（视频可能异常）"
fi

# ═══ 5. 字体 ═══
step "⑤ 检查中文字体"
FONT_OK=0
for f in "/System/Library/Fonts/Supplemental/Songti.ttc" \
         "/System/Library/Fonts/STHeiti Medium.ttc" \
         "/System/Library/Fonts/PingFang.ttc"; do
  [ -f "$f" ] && { FONT_OK=1; break; }
done
if [ "$FONT_OK" = "1" ]; then
  ok "系统自带中文字体可用"
else
  warn "找不到中文字体，封面文字可能显示成方块"
  echo "     ${D}可选：把 霞鹜文楷 放进 fonts/ 目录（见 README）${N}"
fi

# ═══ 6. 首次素材 ═══
step "⑥ 示例素材"
if [ -d "$ROOT/assets" ] && [ -n "$(ls -A "$ROOT/assets" 2>/dev/null | grep -v _index.json)" ]; then
  ok "已有素材（$(find "$ROOT/assets" -name '*.jpg' 2>/dev/null | wc -l | tr -d ' ') 张），跳过"
else
  echo "  ${D}造一份示例素材，让流程能立刻跑起来${N}"
  if "$VPY" make_sample.py >/dev/null 2>&1; then
    "$VPY" asset_library.py import-collection --root assets >/dev/null 2>&1
    ok "已生成示例素材（$(find "$ROOT/assets" -name '*.jpg' 2>/dev/null | wc -l | tr -d ' ') 张）"
    echo "     ${D}换成你自己的：把图片放进 assets/ 再跑 import-collection${N}"
  else
    warn "示例素材生成失败（不影响启动）"
  fi
fi

# ═══ 7. 启动 ═══
step "⑦ 启动"
URL="http://127.0.0.1:${PORT}"
if curl -s -o /dev/null --max-time 2 "$URL/"; then
  ok "服务已在运行"
  open "$URL"; echo; echo "（本窗口可以关掉了）"; exit 0
fi

echo "  ${G}${B}安装完成${N}"
echo "  地址：${B}$URL${N}"
echo "  ${D}保持本窗口开着；退出请按 Ctrl+C 或直接关窗口${N}"
echo
export PATH="$BIN:$PATH"
exec "$VPY" -u app_server.py --port "$PORT"
