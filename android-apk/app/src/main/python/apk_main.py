# -*- coding: utf-8 -*-
"""APK 内嵌桥接入口: 桥接服务直接跑在手机本地 (127.0.0.1:8080)。

由 MainActivity 通过 Chaquopy 调用 start(data_dir, web_dir)。
启动后 WebView 加载 http://127.0.0.1:8080/ 即为同一套界面,
用户只需填写目标主机 SSH 信息即可连接检测 —— 与桌面版体验一致。
"""
import os

import bridge


def start(data_dir: str, web_dir: str) -> None:
    bridge.serve(data_dir=data_dir, web_dir=web_dir,
                 host="127.0.0.1", port=8080)
