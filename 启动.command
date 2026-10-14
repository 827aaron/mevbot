#!/bin/bash
# macOS 双击启动（第一次会自动创建虚拟环境并安装依赖）
cd "$(dirname "$0")" || exit 1
echo ""
echo "  正在启动… / Starting…"
echo ""
PY=""
for c in python3.14 python3.13 python3.12 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ ! -x ".venv/bin/python" ]; then
  if [ -z "$PY" ]; then
    echo "  ❌ 没有找到 Python 3.12 或更高版本。请先安装：https://www.python.org/downloads/"
    echo "     Python 3.12+ not found. Please install it from https://www.python.org/downloads/"
    open "https://www.python.org/downloads/" 2>/dev/null
    read -r -p "  按回车键退出 / Press Enter to exit" _
    exit 1
  fi
  echo "  正在创建运行环境… / Creating the runtime environment…"
  "$PY" -m venv .venv || { echo "  ❌ 创建虚拟环境失败 / Could not create venv"; read -r -p "  按回车键退出 / Press Enter" _; exit 1; }
fi
.venv/bin/python run.py "$@"
code=$?
if [ $code -ne 0 ]; then
  read -r -p "  程序已退出（代码 $code），按回车键关闭 / Exited ($code). Press Enter to close" _
fi
