"""所有路径集中在这里。数据目录可用环境变量 SALESTEAM_DATA 覆盖（测试 / 第 5 步安装包用）。"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
DATA_DIR = Path(os.environ.get("SALESTEAM_DATA") or (ROOT / "data")).resolve()
DB_PATH = DATA_DIR / "salesteam.db"
KEY_PATH = DATA_DIR / ".secret.key"
UPLOAD_DIR = DATA_DIR / "uploads"
EXPORT_DIR = DATA_DIR / "exports"


def ensure_dirs():
    for d in (DATA_DIR, UPLOAD_DIR, EXPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
