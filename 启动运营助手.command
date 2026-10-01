#!/bin/bash
# 双击即可启动「运营助手」图形界面
#
# 这个窗口需要保持开着 —— 关掉窗口就等于退出应用。

cd "$(dirname "$0")" || exit 1
PORT="${1:-8765}"
URL="http://127.0.0.1:${PORT}"

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

# ③ 检查依赖目录
if [ ! -d "$PWD/rpa_libs" ]; then
  echo "⚠️  找不到 rpa_libs/（Python 依赖目录）"
  echo "   部分功能（OCR、封面、素材库）可能不可用。"
  echo
fi

# ④ 启动
echo "正在启动服务… 地址：$URL"
echo "（保持这个窗口开着；要退出按 Ctrl+C 或直接关窗口）"
echo
export PYTHONPATH="$PWD/rpa_libs:$PYTHONPATH"
exec python3 -u app_server.py --port "$PORT"
