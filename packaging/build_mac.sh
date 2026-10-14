#!/bin/bash
# 詹姆斯智能体 —— 打 Mac 测试安装包（.dmg）
#   bash packaging/build_mac.sh [python]      python：用来建打包环境的 Python 3.12（默认 python3.12）
# 本机（Apple 芯片）打出 arm64 版；Intel 版由 GitHub Actions 的 macos-15-intel 打（同一个脚本）。
# 不做 Apple 公证（第 5a 测试版），只做 ad-hoc 签名（Apple 芯片必须有签名才能运行）。
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${1:-python3.12}"
ARCH="$(uname -m)"
VER="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' salesteam/__init__.py)"
APP_ZH="詹姆斯智能体"

if [ ! -x .venv-build/bin/python ]; then
  "$PY" -m venv .venv-build
fi
.venv-build/bin/pip install -q --upgrade pip
.venv-build/bin/pip install -q -r requirements.txt -r packaging/requirements-build.txt
.venv-build/bin/pip check
.venv-build/bin/python -m unittest discover -s tests -v

rm -rf build dist/JamesAgent dist/JamesAgent.app "dist/${APP_ZH}.app"
.venv-build/bin/pyinstaller --noconfirm --clean --distpath dist --workpath build packaging/JamesAgent.spec

# 显示名：詹姆斯智能体.app（内部可执行文件仍叫 JamesAgent，路径里没有中文也没关系）
mv dist/JamesAgent.app "dist/${APP_ZH}.app"
rm -rf dist/JamesAgent
xattr -cr "dist/${APP_ZH}.app"
codesign --force --deep --sign - --timestamp=none "dist/${APP_ZH}.app"
codesign --verify --deep --strict "dist/${APP_ZH}.app"

DMG="dist/JamesAgent-${VER}-mac-${ARCH}.dmg"
rm -f "$DMG"
.venv-build/bin/dmgbuild -s packaging/dmg_settings.py -D app="dist/${APP_ZH}.app" "${APP_ZH}" "$DMG"
echo "built: $DMG ($(du -h "$DMG" | cut -f1))"
