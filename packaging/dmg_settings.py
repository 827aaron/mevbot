# dmgbuild（MIT）配置：标准“拖到应用程序”窗口 —— 左边 App 图标，右边“应用程序”快捷方式，中间箭头背景。
#   dmgbuild -s packaging/dmg_settings.py -D app="dist/詹姆斯智能体.app" "詹姆斯智能体" dist/xxx.dmg
import os

app = defines.get("app")                          # noqa: F821  （dmgbuild 注入 defines）
appname = os.path.basename(app)
here = os.path.dirname(os.path.abspath(globals().get("__file__", "packaging/dmg_settings.py")))
icons = os.path.join(os.path.dirname(app), "..", "packaging", "icons")
if not os.path.isdir(icons):
    icons = os.path.join(os.getcwd(), "packaging", "icons")

format = "UDZO"
compression_level = 9
filesystem = "HFS+"
size = None
files = [app]
symlinks = {"Applications": "/Applications"}
badge_icon = None
icon = os.path.join(icons, "app.icns")
background = os.path.join(icons, "dmg-background.png")
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False
default_view = "icon-view"
window_rect = ((200, 120), (640, 400))
icon_size = 112
text_size = 13
icon_locations = {appname: (160, 190), "Applications": (480, 190)}
