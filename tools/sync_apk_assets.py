# -*- coding: utf-8 -*-
"""把界面资产与 Python 桥接源码同步进 APK 工程。改完代码后运行一次再提交。"""
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "android" / "web"
DST = ROOT / "android-apk" / "app" / "src" / "main" / "assets" / "web"
PY_SRC = ROOT / "android-apk" / "app" / "src" / "main" / "python"

ASSETS = [
    "index.html", "app.css", "app.js", "activate.js",
    "manifest.webmanifest", "sw.js",
    "icon-192.png", "icon-512.png", "apple-touch-icon.png",
]

# 桥接逻辑: 与 PWA/桌面版完全同一份代码
PY_FILES = [
    (ROOT / "android" / "bridge.py", "bridge.py"),
    (ROOT / "android" / "local_shell.py", "local_shell.py"),
]

DAQ = ROOT / "daq_remote"


def main():
    DST.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in ASSETS:
        src = SRC / name
        if not src.exists():
            print("跳过缺失:", name)
            continue
        shutil.copy2(src, DST / name)
        copied.append(name)
        print("同步:", name)

    # 资产清单: MainActivity 解压时按清单复制,
    # 规避部分新系统 AssetManager.list() 对目录返回空的问题
    (DST / "filelist.txt").write_text("\n".join(copied) + "\n", encoding="utf-8")
    print("同步: filelist.txt (%d 个文件)" % len(copied))

    # 应用图标
    icon_dst = ROOT / "android-apk" / "app" / "src" / "main" / "res" / "drawable-nodpi"
    icon_dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SRC / "icon-512.png", icon_dst / "ic_launcher.png")
    print("同步: ic_launcher.png")

    # Python 桥接源码
    PY_SRC.mkdir(parents=True, exist_ok=True)
    for src, name in PY_FILES:
        shutil.copy2(src, PY_SRC / name)
        print("同步:", name)
    daq_dst = PY_SRC / "daq_remote"
    daq_dst.mkdir(parents=True, exist_ok=True)
    for py in DAQ.glob("*.py"):
        shutil.copy2(py, daq_dst / py.name)
        print("同步: daq_remote/%s" % py.name)

    print("完成。")


if __name__ == "__main__":
    main()
