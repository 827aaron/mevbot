# -*- mode: python ; coding: utf-8 -*-
# 詹姆斯智能体 / James Agent —— PyInstaller 配置（Mac .app 和 Windows 文件夹共用）
#   pyinstaller --noconfirm --clean packaging/JamesAgent.spec
# 测试安装包：无需激活。浏览器组件（Chromium）首次打开时下载。
import sys
from pathlib import Path
from salesteam import edition

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).resolve().parent
EDITION = edition.current()
DISPLAY_NAME = EDITION["name"]["zh"]
VERSION = "0.2.0"
for line in (ROOT / "salesteam" / "__init__.py").read_text(encoding="utf-8").splitlines():
    if line.startswith("VERSION"):
        VERSION = line.split("=")[1].strip().strip('"')

datas = [
    (str(ROOT / "web"), "web"),                                            # 界面（含 i18n）
    (str(ROOT / "salesteam" / "prompts" / "default"), "salesteam/prompts/default"),   # 默认提示词
    (str(ROOT / "salesteam" / "llm" / "presets.json"), "salesteam/llm"),
    (str(ROOT / "salesteam" / "edition.json"), "salesteam"),
]
binaries = []
hiddenimports = collect_submodules("salesteam") + collect_submodules("uvicorn") + ["multipart", "python_multipart"]

# 带数据文件 / 驱动 / 原生库的第三方包：整体收集
for pkg in ("certifi", "truststore", "playwright", "patchright", "scrapling", "browserforge", "apify_fingerprint_datapoints", "onnxruntime",
            "tokenizers", "curl_cffi", "webview", "telethon", "openpyxl", "pypdf", "tld", "markdownify", "lxml"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:  # noqa: BLE001
        print("collect_all skipped:", pkg, e)

if sys.platform == "win32":
    hiddenimports += ["webview.platforms.edgechromium", "webview.platforms.winforms", "clr", "clr_loader"]
elif sys.platform == "darwin":
    hiddenimports += ["webview.platforms.cocoa"]

excludes = ["tkinter", "matplotlib", "IPython", "pytest", "PyQt5", "PyQt6", "PySide2", "PySide6", "gi"]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

icon = str(ROOT / "packaging" / "icons" / ("app.icns" if sys.platform == "darwin" else "app.ico"))
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JamesAgent",
    console=False,               # 窗口程序：没有黑色控制台窗口
    icon=icon,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="arm64" if sys.platform == "darwin" else None,
    codesign_identity=None,
    entitlements_file=None,
    upx=False,
    version=str(ROOT / "packaging" / "windows" / "version_info.txt") if sys.platform == "win32" else None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="JamesAgent")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="JamesAgent.app",           # 打包后改名为 詹姆斯智能体.app（见 build_mac.sh）
        icon=icon,
        bundle_identifier="com.jamesagent.desktop.myigaming" if EDITION["id"] == "my_igaming" else "com.jamesagent.desktop",
        version=VERSION,
        info_plist={
            "CFBundleName": "JamesAgent",
            "CFBundleDisplayName": DISPLAY_NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "14.0",
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.business",
            "NSHumanReadableCopyright": "詹姆斯智能体 / James Agent",
        },
    )
