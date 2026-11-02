"""资源定位: 同时兼容源码运行与 PyInstaller 打包后的单文件 exe。"""

from __future__ import annotations

import os
import sys


def resource_root() -> str:
    """返回打包进程序的资源根目录。"""
    if getattr(sys, "frozen", False):
        # PyInstaller 解包目录
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    # daq_remote/assets.py -> daq_remote/ -> 工具根目录
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def remote_src_dir() -> str:
    return os.path.join(resource_root(), "remote_src")


def remote_c_source() -> str:
    return os.path.join(remote_src_dir(), "bio1762_io.c")


def remote_makefile() -> str:
    return os.path.join(remote_src_dir(), "Makefile")


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def guess_installer_path() -> str:
    """在若干常见位置寻找 DAQNavi Linux 安装包。"""
    candidates = []
    root = resource_root()
    candidates.append(os.path.join(root, "DAQ_Linux_20260306_64bit.run"))
    candidates.append(os.path.join(os.path.dirname(root), "DAQ_Linux_20260306_64bit.run"))
    candidates.append(os.path.join(os.path.dirname(root), "DAQ_Linux_20260306_64bit.run"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    # 退而求其次: 扫描同目录及上级目录下所有 *_64bit.run / *_32bit.run
    for base in (root, os.path.dirname(root)):
        try:
            for name in os.listdir(base):
                if name.endswith("_64bit.run") or name.endswith("_32bit.run"):
                    return os.path.join(base, name)
        except OSError:
            continue
    return ""


def icon_path() -> str:
    p = os.path.join(resource_root(), "app.ico")
    return p if os.path.isfile(p) else ""


def logo_path() -> str:
    """公司 logo (顶栏显示)。"""
    p = os.path.join(resource_root(), "assets", "company_logo.png")
    return p if os.path.isfile(p) else ""


class Logger:
    """极简日志接口: 由 GUI 注入具体的输出函数。"""

    def __init__(self, sink=None):
        self._sink = sink

    def set_sink(self, sink):
        self._sink = sink

    def __call__(self, text: str, level: str = "info"):
        if self._sink:
            self._sink(text, level)
        else:
            print("[%s] %s" % (level, text))
