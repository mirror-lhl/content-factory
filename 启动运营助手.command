#!/bin/bash
# 双击即可启动「运营助手 · 内容工厂」
#
# 这个窗口需要保持开着 —— 关掉窗口就等于退出应用。

cd "$(dirname "$0")" || exit 1
PORT="${1:-8765}"
URL="http://127.0.0.1:${PORT}"
PY="${PYTHON:-python3}"

echo "运营助手 · 内容工厂"
echo "─────────────────────────────"

# ① 已经在跑？直接开浏览器，不重复启动
if curl -s -o /dev/null --max-time 2 "$URL/"; then
  echo "✅ 服务已在运行：$URL"
  echo "   正在打开浏览器…"
  open "$URL"
  echo
  echo "（这个窗口可以直接关掉了）"
  exit 0
fi

# ② 端口被别的程序占了？
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "❌ 端口 $PORT 被其他程序占用："
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN | tail -2
  echo
  echo "换个端口再试，例如："
  echo "  ./启动运营助手.command 8899"
  echo
  read -r -p "按回车关闭…"
  exit 1
fi

# ③ 依赖预检 —— 缺什么直接说清楚，别等跑到一半才报错
echo "检查依赖…"
MISSING=0

# Python 版本
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
  echo "  ❌ 需要 Python 3.9 或更新（当前：$("$PY" -V 2>&1)）"
  MISSING=1
else
  echo "  ✅ $("$PY" -V 2>&1)"
fi

# Pillow（封面/字幕/缩略图必需）
if "$PY" -c 'import PIL' 2>/dev/null; then
  echo "  ✅ Pillow"
else
  echo "  ❌ Pillow 未安装 —— 封面、字幕、素材缩略图都用不了"
  echo "     装：$PY -m pip install Pillow"
  MISSING=1
fi

# numpy（素材页识别用）
if "$PY" -c 'import numpy' 2>/dev/null; then
  echo "  ✅ numpy"
else
  echo "  ⚠️  numpy 未安装 —— 素材「校准页识别」会失效（配图可能混进灰阶卡）"
  echo "     装：$PY -m pip install numpy"
fi

# ffmpeg（视频必需）
if command -v ffmpeg >/dev/null 2>&1; then
  echo "  ✅ ffmpeg  ($(command -v ffmpeg))"
else
  echo "  ⚠️  ffmpeg 未安装 —— 视频做不了（图文和封面不受影响）"
  echo "     装：brew install ffmpeg   或  https://ffmpeg.org/download.html"
fi

echo
if [ "$MISSING" = "1" ]; then
  echo "❌ 缺少必需依赖，先把上面的装上再启动。"
  echo
  read -r -p "按回车关闭…"
  exit 1
fi

# ④ 首次运行：还没素材就提示造一份
if [ ! -d "$PWD/assets" ] || [ -z "$(ls -A "$PWD/assets" 2>/dev/null)" ]; then
  echo "素材库还是空的。可以先用示例素材试跑："
  echo "  $PY make_sample.py && $PY asset_library.py import-collection --root assets"
  echo
fi

# ⑤ 启动
echo "正在启动服务… 地址：$URL"
echo "（保持这个窗口开着；要退出按 Ctrl+C 或直接关窗口）"
echo
[ -d "$PWD/rpa_libs" ] && export PYTHONPATH="$PWD/rpa_libs:$PYTHONPATH"
exec "$PY" -u app_server.py --port "$PORT"
