#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中烟信息业保中心 研华I/O卡驱动安装与测试工具 (Windows 10)

功能:
  1. 通过 SSH 远程登录 Linux 目标机;
  2. 按 FAQ 文档自动完成 DAQNavi Linux 驱动的部署与卸载
     (支持研华 PCI-1762 / PCI-1761 数字量 I/O 卡);
  3. 支持远程测试代理 (advdaq_install/iotest) 的部署与卸载;
  4. 通过远端编译的行协议代理, 对 I/O 卡的隔离 DI 与继电器 DO
     进行实时监视、输出测试, 并支持为每个 I/O 点位添加备注名称。

运行:  python app.py      (或直接运行打包好的 exe)
"""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import sys
import threading
import time
import traceback
from tkinter import ttk, filedialog, messagebox, scrolledtext, simpledialog
import tkinter as tk

# 屏蔽 paramiko 在断开/重连时打到 stderr 的噪音日志
logging.getLogger("paramiko").setLevel(logging.CRITICAL)

# 允许以脚本目录为根导入 daq_remote
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from daq_remote import __version__, assets                      # noqa: E402
from daq_remote.deployer import (                               # noqa: E402
    CARD_MODELS, Deployer, DeployOptions, drivers_for_models)
from daq_remote.io_client import IOSession, DeviceInfo          # noqa: E402
from daq_remote.ssh_session import SSHError, SSHSession, SSHTarget  # noqa: E402

APP_TITLE = "中烟信息业保中心 研华I/O卡驱动安装与测试工具"
CONFIG_FILE = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"),
    "AdvantechIOTool.json")
ANNO_FILE = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"),
    "AdvantechIOTool_Annotations.json")

MAX_POINTS = 32      # 单方向最大点数(留足余量)

# =============================================================== 工业风配色
C_HEADER_BG = "#16283c"     # 顶栏深藏青(工业主色)
C_HEADER_FG = "#ffffff"     # 顶栏标题白字
C_STRIPE = "#ff8f00"        # 工业安全橙条
C_BODY_BG = "#dfe3e8"       # 机身浅钢灰
C_CARD_BG = "#f4f6f9"       # 卡片浅底
C_CARD_EDGE = "#aeb9c4"     # 卡片描边
C_ACCENT = "#1f4e79"        # 钢蓝强调色
C_ACCENT_HV = "#2a649e"     # 钢蓝悬停
C_STATUS_BG = "#1b2430"     # 状态栏深钢色
C_STATUS_FG = "#cfd8e3"
C_LOG_BG = "#141a22"        # 日志控制台深色
C_LOG_FG = "#c9d4e0"

LOG_COLORS = {
    "info": "#c9d4e0",
    "ok": "#5ad469",
    "warn": "#ffb84d",
    "err": "#ff6b6b",
    "step": "#6ab7ff",
    "cmd": "#c792ea",
    "out": "#8fa1b3",
}

ON_COLOR = "#ff7043"       # 继电器吸合(工业橙)
OFF_COLOR = "#b9c2cc"      # 继电器释放(钢灰)
DI_HI_COLOR = "#43a047"    # 数字输入为高
DI_LO_COLOR = "#b9c2cc"    # 数字输入为低
BTN_ON_FG = "#ffffff"
BTN_OFF_FG = "#26313d"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE + "  v" + __version__ + "  ·  开发者：陈宠")
        self.root.geometry("1220x860")
        self.root.minsize(1080, 720)
        self.root.configure(bg=C_BODY_BG)

        self.ssh = None                      # type: ignore[assignment]
        self.io = None                       # type: ignore[assignment]
        self.deployer: Deployer | None = None
        self.worker: threading.Thread | None = None
        self.busy = False

        self._log_q: "queue.Queue[tuple]" = queue.Queue()
        self._ui_q: "queue.Queue" = queue.Queue()

        # 点位状态(长度按实际卡型在 _apply_device_info 中动态调整)
        self.di_count = 16
        self.do_count = 16
        self.do_ports = 2
        self.di_ports = 2
        self.do_state = [0] * MAX_POINTS     # 期望的继电器状态
        self.do_readback = [0] * MAX_POINTS  # 回读状态
        self.di_state = [0] * MAX_POINTS
        self.watching = False
        self.report_lines = []
        self.device_model = ""               # PCI-1762 / PCI-1761 / ...

        # I/O 点位备注: {卡型: {"DI0": "备注", "K3": "备注", ...}}
        self.annotations: dict = {}
        self._load_annotations()

        self._build_ui()
        self._load_config()
        self.root.after(80, self._pump)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.log("欢迎使用 %s v%s · 开发者：陈宠" % (APP_TITLE, __version__), "step")
        self.log("支持卡型: PCI-1762 (16DI+16继电器) / PCI-1761 (8DI+8继电器)。", "info")
        self.log("使用前请先在【连接配置】页填写目标 Linux 主机信息并测试连接。", "info")
        self.log("提示: 在【IO 输入输出测试】页双击点位名称即可添加备注。", "info")

    # =============================================================== UI 搭建
    def _build_ui(self) -> None:
        self._build_style()
        self._build_header()

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(10, 6))

        self.tab_conn = ttk.Frame(self.nb)
        self.tab_deploy = ttk.Frame(self.nb)
        self.tab_io = ttk.Frame(self.nb)
        self.nb.add(self.tab_conn, text="  1. 连接配置  ")
        self.nb.add(self.tab_deploy, text="  2. 驱动安装 / 卸载  ")
        self.nb.add(self.tab_io, text="  3. IO 输入输出测试  ")

        self._build_tab_conn()
        self._build_tab_deploy()
        self._build_tab_io()

        # ---- 底部: 进度 + 日志 ----
        bottom = ttk.Frame(self.root)
        bottom.pack(fill="both", expand=False, padx=10, pady=(0, 4))

        pf = ttk.Frame(bottom)
        pf.pack(fill="x")
        self.progress = ttk.Progressbar(pf, mode="determinate", maximum=1000)
        self.progress.pack(side="left", fill="x", expand=True)
        self.progress_lbl = ttk.Label(pf, text="就绪", width=46, anchor="w")
        self.progress_lbl.pack(side="left", padx=8)

        lf = ttk.LabelFrame(bottom, text=" 运行日志 ", style="Console.TLabelframe")
        lf.pack(fill="both", expand=True, pady=(6, 0))
        self.log_text = scrolledtext.ScrolledText(
            lf, height=11, wrap="none", font=("Consolas", 9),
            background=C_LOG_BG, foreground=C_LOG_FG,
            insertbackground=C_LOG_FG, relief="flat", bd=6)
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)
        for tag, color in LOG_COLORS.items():
            self.log_text.tag_config(tag, foreground=color)
        self.log_text.tag_config("step", foreground=LOG_COLORS["step"],
                                 font=("Consolas", 9, "bold"))
        self.log_text.configure(state="disabled")

        # ---- 状态栏 ----
        self.status = tk.StringVar(value="未连接")
        bar = tk.Frame(self.root, bg=C_STATUS_BG, height=26)
        bar.pack(fill="x", side="bottom")
        tk.Label(bar, textvariable=self.status, bg=C_STATUS_BG, fg=C_STATUS_FG,
                 anchor="w", font=("Microsoft YaHei UI", 9)).pack(side="left", padx=10)
        self.dev_var = tk.StringVar(value="")
        tk.Label(bar, textvariable=self.dev_var, bg=C_STATUS_BG, fg="#8fe3a1",
                 anchor="e", font=("Consolas", 9, "bold")).pack(side="right", padx=10)

    def _build_header(self) -> None:
        # 最上侧: 单位名横幅(深黄色)
        tk.Label(self.root, text="中烟信息业保中心", bg=C_HEADER_BG,
                 fg="#c9a227", font=("Microsoft YaHei UI", 11, "bold"),
                 pady=4).pack(fill="x")

        header = tk.Frame(self.root, bg=C_HEADER_BG, height=58)
        header.pack(fill="x")
        header.pack_propagate(False)

        # 左侧: 橙色工业标记 + 标题(按要求仅保留标题文字)
        tk.Label(header, text="◤", bg=C_HEADER_BG, fg=C_STRIPE,
                 font=("Microsoft YaHei UI", 16, "bold")).pack(side="left", padx=(16, 6))
        tk.Label(header, text=APP_TITLE, bg=C_HEADER_BG, fg=C_HEADER_FG,
                 font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")

        # 工业安全橙装饰条
        tk.Frame(self.root, bg=C_STRIPE, height=4).pack(fill="x")

    def _build_style(self) -> None:
        style = ttk.Style()
        # 注意: 必须使用 clam 主题。vista 主题不响应页签自定义配色,
        # 会导致选中页签显示为原生白色方块(白框问题)。
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", font=("Microsoft YaHei UI", 9), background=C_BODY_BG)

        # ---- 页签(扁平工业风) ----
        style.configure("TNotebook", background=C_BODY_BG, borderwidth=0,
                        tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", padding=(20, 9),
                        font=("Microsoft YaHei UI", 10, "bold"),
                        background="#c9d2dc", foreground="#2c3947",
                        borderwidth=0, focusthickness=0)
        style.map("TNotebook.Tab",
                  background=[("selected", C_ACCENT),
                              ("active", "#b4c0cd")],
                  foreground=[("selected", "#ffffff"),
                              ("active", "#1f2c3a")],
                  expand=[("selected", (1, 1, 1, 0))])

        # ---- 卡片式分组框 ----
        style.configure("TLabelframe", background=C_BODY_BG, borderwidth=0)
        style.configure("TLabelframe.Label", font=("Microsoft YaHei UI", 9, "bold"),
                        foreground=C_ACCENT, background=C_BODY_BG)
        style.configure("Card.TLabelframe", background=C_CARD_BG,
                        bordercolor=C_CARD_EDGE, borderwidth=1, relief="solid")
        style.configure("Card.TLabelframe.Label",
                        font=("Microsoft YaHei UI", 9, "bold"),
                        foreground=C_ACCENT, background=C_CARD_BG)
        style.configure("Card.TFrame", background=C_CARD_BG)
        style.configure("Card.TLabel", background=C_CARD_BG)
        style.configure("Card.TCheckbutton", background=C_CARD_BG)
        style.map("Card.TCheckbutton", background=[("active", C_CARD_BG)])

        style.configure("TFrame", background=C_BODY_BG)
        style.configure("TLabel", background=C_BODY_BG)
        style.configure("TCheckbutton", background=C_BODY_BG)
        style.map("TCheckbutton", background=[("active", C_BODY_BG)])
        style.configure("TRadiobutton", background=C_BODY_BG)

        # ---- 输入框 ----
        style.configure("TEntry", fieldbackground="#ffffff", foreground="#1f2c3a",
                        bordercolor=C_CARD_EDGE, lightcolor=C_CARD_EDGE,
                        darkcolor=C_CARD_EDGE, insertcolor="#1f2c3a")
        style.map("TEntry", bordercolor=[("focus", C_ACCENT)],
                  lightcolor=[("focus", C_ACCENT)], darkcolor=[("focus", C_ACCENT)])
        style.configure("TCombobox", fieldbackground="#ffffff", background="#e8edf3",
                        foreground="#1f2c3a", bordercolor=C_CARD_EDGE,
                        lightcolor=C_CARD_EDGE, darkcolor=C_CARD_EDGE,
                        arrowcolor="#1f2c3a")
        style.map("TCombobox", fieldbackground=[("readonly", "#ffffff")],
                  bordercolor=[("focus", C_ACCENT)])
        style.configure("TSpinbox", fieldbackground="#ffffff", bordercolor=C_CARD_EDGE,
                        lightcolor=C_CARD_EDGE, darkcolor=C_CARD_EDGE,
                        arrowcolor="#1f2c3a")

        # ---- 按钮(扁平) ----
        style.configure("TButton", background="#e8edf3", foreground="#1f2c3a",
                        borderwidth=0, focusthickness=0, padding=(12, 6),
                        font=("Microsoft YaHei UI", 9))
        style.map("TButton",
                  background=[("disabled", "#d3d8de"), ("pressed", "#c2cddb"),
                              ("active", "#d3dce6")],
                  foreground=[("disabled", "#8a97a5")])
        style.configure("Go.TButton", background=C_ACCENT, foreground="#ffffff",
                        font=("Microsoft YaHei UI", 10, "bold"), padding=(14, 7))
        style.map("Go.TButton",
                  background=[("disabled", "#7e9ab5"), ("pressed", "#17395a"),
                              ("active", C_ACCENT_HV)],
                  foreground=[("disabled", "#e6ecf2")])
        style.configure("Danger.TButton", background="#c62828", foreground="#ffffff",
                        font=("Microsoft YaHei UI", 10, "bold"), padding=(12, 7))
        style.map("Danger.TButton",
                  background=[("disabled", "#d99a9a"), ("pressed", "#a01f1f"),
                              ("active", "#d63a3a")],
                  foreground=[("disabled", "#f2dede")])

        # ---- 进度条 ----
        style.configure("Horizontal.TProgressbar", background=C_ACCENT,
                        troughcolor="#c6cdd6", borderwidth=0, lightcolor=C_ACCENT,
                        darkcolor=C_ACCENT)

        style.configure("Console.TLabelframe.Label",
                        foreground="#9fb6d4", background=C_BODY_BG)
        # 点位备注标签专用样式(浅底)
        style.configure("Note.TLabel", background="#eef1f4", foreground="#5a6a7a")

    # ---------------------------------------------------------------- Tab1
    def _build_tab_conn(self) -> None:
        f = self.tab_conn

        box = ttk.LabelFrame(f, text="Linux 目标主机", style="Card.TLabelframe")
        box.pack(fill="x", padx=10, pady=10)

        grid = ttk.Frame(box, style="Card.TFrame")
        grid.pack(fill="x", padx=10, pady=10)

        self.v_host = tk.StringVar(value="192.168.1.100")
        self.v_port = tk.StringVar(value="22")
        self.v_user = tk.StringVar(value="root")
        self.v_pass = tk.StringVar()
        self.v_key = tk.StringVar()
        self.v_workdir = tk.StringVar()
        self.v_savepw = tk.BooleanVar(value=False)

        def row(r, label, var, width=32, show=None):
            ttk.Label(grid, text=label, style="Card.TLabel").grid(
                row=r, column=0, sticky="e", padx=(0, 8), pady=5)
            e = ttk.Entry(grid, textvariable=var, width=width, show=show)
            e.grid(row=r, column=1, sticky="w", pady=5)
            return e

        row(0, "主机地址 / IP:", self.v_host)
        row(1, "SSH 端口:", self.v_port, width=10)
        row(2, "登录用户名:", self.v_user, width=20)
        self.e_pass = row(3, "登录密码:", self.v_pass, width=32, show="*")
        ttk.Checkbutton(grid, text="记住密码(明文保存于本机配置文件)",
                        variable=self.v_savepw,
                        style="Card.TCheckbutton").grid(row=3, column=2, sticky="w", padx=8)

        ttk.Label(grid, text="SSH 私钥(可选):", style="Card.TLabel").grid(
            row=4, column=0, sticky="e", padx=(0, 8), pady=5)
        ttk.Entry(grid, textvariable=self.v_key, width=42).grid(row=4, column=1, sticky="w", pady=5)
        ttk.Button(grid, text="浏览...", command=self._pick_key).grid(row=4, column=2, sticky="w")

        ttk.Label(grid, text="远端工作目录:", style="Card.TLabel").grid(
            row=5, column=0, sticky="e", padx=(0, 8), pady=5)
        ttk.Entry(grid, textvariable=self.v_workdir, width=42).grid(row=5, column=1, sticky="w", pady=5)
        ttk.Label(grid, text="(留空 = $HOME/advdaq_install)",
                  style="Card.TLabel").grid(row=5, column=2, sticky="w", padx=8)

        btns = ttk.Frame(box, style="Card.TFrame")
        btns.pack(fill="x", padx=10, pady=(0, 12))
        ttk.Button(btns, text="测试连接", style="Go.TButton",
                   command=self.on_test_connection).pack(side="left")
        ttk.Button(btns, text="断开", command=self.on_disconnect).pack(side="left", padx=8)
        ttk.Button(btns, text="保存配置", command=self._save_config).pack(side="left", padx=8)

        self.conn_state = ttk.Label(box, text="● 未连接", foreground="#c62828",
                                    style="Card.TLabel",
                                    font=("Microsoft YaHei UI", 10, "bold"))
        self.conn_state.pack(anchor="w", padx=14, pady=(0, 12))

        tip = ttk.LabelFrame(f, text="前置条件提示")
        tip.pack(fill="x", padx=10, pady=(0, 10))
        tk.Label(tip, justify="left", anchor="w", font=("Microsoft YaHei UI", 9),
                 foreground="#555555", bg=C_BODY_BG,
                 text=("1. 目标 Linux 已开启 SSH 服务(sshd), 且当前账号可用密码或密钥登录;\n"
                       "2. 安装驱动需要 root 权限: 以 root 登录, 或该账号具备 sudo 权限;\n"
                       "3. 研华 I/O 卡 (PCI-1762 / PCI-1761) 已插在目标机的 PCI 插槽中;\n"
                       "4. 目标机需能访问软件源(apt/yum/dnf), 以便自动安装内核头文件与 gcc。")
                 ).pack(fill="x", padx=12, pady=10)

    # ---------------------------------------------------------------- Tab2
    def _build_tab_deploy(self) -> None:
        f = self.tab_deploy

        box = ttk.LabelFrame(f, text="驱动安装包 (DAQNavi Linux .run)", style="Card.TLabelframe")
        box.pack(fill="x", padx=10, pady=10)
        self.v_installer = tk.StringVar(value=assets.guess_installer_path())
        ttk.Entry(box, textvariable=self.v_installer).pack(
            side="left", fill="x", expand=True, padx=10, pady=10)
        ttk.Button(box, text="浏览...", command=self._pick_installer).pack(side="left", padx=(0, 10))

        opt = ttk.LabelFrame(f, text="部署选项(依据 FAQ 流程)", style="Card.TLabelframe")
        opt.pack(fill="x", padx=10, pady=(0, 10))

        self.v_deps = tk.BooleanVar(value=True)
        self.v_updapt = tk.BooleanVar(value=True)
        self.v_blacklist = tk.BooleanVar(value=True)
        self.v_reboot = tk.BooleanVar(value=True)
        self.v_cardmodel = tk.StringVar(value="PCI-1762")
        self.v_drvname = tk.StringVar(value=drivers_for_models(["PCI-1762"])[0])

        r0 = ttk.Frame(opt, style="Card.TFrame"); r0.pack(fill="x", padx=10, pady=(8, 2))
        ttk.Label(r0, text="I/O 卡型号:", style="Card.TLabel").pack(side="left")
        self.cmb_card = ttk.Combobox(
            r0, textvariable=self.v_cardmodel, width=26, state="readonly",
            values=["PCI-1762", "PCI-1761", "PCI-1762 + PCI-1761"])
        self.cmb_card.pack(side="left", padx=6)
        self.cmb_card.bind("<<ComboboxSelected>>", lambda _e: self._sync_card_model())
        self.card_hint = ttk.Label(r0, text="", foreground="#777777",
                                   style="Card.TLabel")
        self.card_hint.pack(side="left", padx=10)
        self._sync_card_model()

        r1 = ttk.Frame(opt, style="Card.TFrame"); r1.pack(fill="x", padx=10, pady=(4, 2))
        ttk.Checkbutton(r1, text="自动安装编译依赖(gcc/make/内核头文件)",
                        variable=self.v_deps, style="Card.TCheckbutton").pack(side="left")
        ttk.Checkbutton(r1, text="先执行 apt/yum 源更新",
                        variable=self.v_updapt, style="Card.TCheckbutton").pack(side="left", padx=20)

        r2 = ttk.Frame(opt, style="Card.TFrame"); r2.pack(fill="x", padx=10, pady=2)
        ttk.Checkbutton(r2, text="屏蔽冲突驱动(comedi / adv_pci_dio / adv_pci1710)",
                        variable=self.v_blacklist, style="Card.TCheckbutton").pack(side="left")
        ttk.Checkbutton(r2, text="安装完成后自动重启目标机",
                        variable=self.v_reboot, style="Card.TCheckbutton").pack(side="left", padx=20)

        r3 = ttk.Frame(opt, style="Card.TFrame"); r3.pack(fill="x", padx=10, pady=(2, 10))
        ttk.Label(r3, text="驱动目录名(自动填充, 可手改):",
                  style="Card.TLabel").pack(side="left")
        ttk.Entry(r3, textvariable=self.v_drvname, width=34).pack(side="left", padx=6)

        act = ttk.Frame(f)
        act.pack(fill="x", padx=10)
        self.btn_deploy = ttk.Button(act, text="▶ 一键安装驱动(推荐)", style="Go.TButton",
                                     command=self.on_deploy)
        self.btn_deploy.pack(side="left")
        self.btn_probe = ttk.Button(act, text="仅检测环境", command=self.on_probe)
        self.btn_probe.pack(side="left", padx=8)
        self.btn_verify = ttk.Button(act, text="验证安装结果", command=self.on_verify)
        self.btn_verify.pack(side="left", padx=8)
        self.btn_reboot = ttk.Button(act, text="重启目标机", command=self.on_reboot)
        self.btn_reboot.pack(side="left", padx=8)
        self.btn_uninstall = ttk.Button(act, text="✕ 卸载驱动", style="Danger.TButton",
                                        command=self.on_uninstall)
        self.btn_uninstall.pack(side="left", padx=8)

        # ---- 远程测试代理 ----
        agent = ttk.LabelFrame(f, text="远程测试代理 (advdaq_install/iotest)", style="Card.TLabelframe")
        agent.pack(fill="x", padx=10, pady=(8, 0))
        arow = ttk.Frame(agent, style="Card.TFrame")
        arow.pack(fill="x", padx=10, pady=8)
        ttk.Label(arow, text="代理程序 bio1762_io 部署于目标机 <工作目录>/iotest, "
                             "用于 IO 测试页的 DI/DO 读写。",
                  style="Card.TLabel").pack(side="left")
        brow = ttk.Frame(agent, style="Card.TFrame")
        brow.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(brow, text="部署代理(上传并编译)", command=self.on_agent_deploy).pack(side="left")
        ttk.Button(brow, text="查看代理状态", command=self.on_agent_status).pack(side="left", padx=8)
        ttk.Button(brow, text="卸载代理(删除远端文件)", style="Danger.TButton",
                   command=self.on_agent_remove).pack(side="left", padx=8)

        steps = ttk.LabelFrame(f, text="FAQ 部署 / 卸载流程")
        steps.pack(fill="both", expand=True, padx=10, pady=10)
        tk.Label(steps, justify="left", anchor="w", font=("Consolas", 9),
                 fg="#333333", bg=C_BODY_BG,
                 text=(
                     "安装:                                          卸载:\n"
                     "[1] sudo su                        逆向执行卸载脚本:\n"
                     "      获取 root 权限(本工具自动处理)     [1] ./DAQ_Linux_xxx.run silent uninstall <驱动>\n"
                     "[2] chmod +x DAQ_Linux_xxx.run           -> make uninstall 清除内核模块\n"
                     "      赋予可执行权限                 [2] 清理 /opt/advantech SDK 与守护服务\n"
                     "[3] ./DAQ_Linux_xxx.run            [3] lsmod | grep bio* 复核无残留模块\n"
                     "      silent install <驱动>\n"
                     "      驱动: pci1762 / pci1761_pcm3761i\n"
                     "[4] reboot  重启后驱动自动加载\n"
                     "[5] lsmod | grep bio*    确认模块加载\n"
                     "[6] /opt/advantech/tools/dndev   确认识别\n"
                     "[7] cat /sys/class/daq/daq0/desc 设备描述\n"
                 )).pack(fill="x", padx=12, pady=10)

    # ---------------------------------------------------------------- Tab3
    def _build_tab_io(self) -> None:
        f = self.tab_io

        top = ttk.Frame(f)
        top.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(top, text="设备:").pack(side="left")
        self.v_device = tk.StringVar()
        self.cmb_device = ttk.Combobox(top, textvariable=self.v_device, width=34, state="readonly")
        self.cmb_device.pack(side="left", padx=6)
        ttk.Button(top, text="刷新设备列表", command=self.on_refresh_devices).pack(side="left")
        ttk.Button(top, text="启动 IO 会话", style="Go.TButton",
                   command=self.on_io_start).pack(side="left", padx=8)
        ttk.Button(top, text="结束 IO 会话", command=self.on_io_stop).pack(side="left")
        self.io_state = ttk.Label(top, text="● 未启动", foreground="#c62828",
                                  font=("Microsoft YaHei UI", 9, "bold"))
        self.io_state.pack(side="left", padx=12)

        self.v_devinfo = tk.StringVar(value="设备信息: -")
        ttk.Label(f, textvariable=self.v_devinfo, foreground=C_ACCENT,
                  font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w", padx=12, pady=(0, 6))
        self.v_anno_hint = tk.StringVar(
            value="提示: 双击点位名称即可设置/修改备注名 (如 DI1=原料入口检测, K1=引风机), 备注自动保存。")
        ttk.Label(f, textvariable=self.v_anno_hint, foreground="#8a97a5",
                  font=("Microsoft YaHei UI", 8)).pack(anchor="w", padx=12, pady=(0, 4))

        body = ttk.Frame(f)
        body.pack(fill="both", expand=True, padx=10, pady=4)

        # ---------------- DI ----------------
        di = ttk.LabelFrame(body, text="数字量输入 DI", style="Card.TLabelframe")
        di.pack(side="left", fill="both", expand=True, padx=(0, 6))

        self.di_frame = di
        self.di_cells = []       # LED 标签
        self.di_names = []       # 名称标签
        self.di_notes = []       # 备注标签
        self._digrid = None
        self.v_dihex = tk.StringVar(value="DI 原始值: --")
        self._build_di_grid(16)

        ttk.Label(di, textvariable=self.v_dihex, style="Card.TLabel",
                  font=("Consolas", 10, "bold")).pack(anchor="w", padx=12)

        wrow = ttk.Frame(di, style="Card.TFrame")
        wrow.pack(fill="x", padx=10, pady=10)
        ttk.Label(wrow, text="轮询间隔(ms):").pack(side="left")
        self.v_interval = tk.StringVar(value="200")
        ttk.Spinbox(wrow, from_=20, to=5000, increment=20, width=7,
                    textvariable=self.v_interval).pack(side="left", padx=6)
        self.btn_watch = ttk.Button(wrow, text="开始实时监视", command=self.on_watch_toggle)
        self.btn_watch.pack(side="left", padx=4)
        ttk.Button(wrow, text="读取一次", command=self.on_read_di).pack(side="left", padx=4)

        # ---------------- DO ----------------
        do = ttk.LabelFrame(body, text="继电器输出 DO", style="Card.TLabelframe")
        do.pack(side="left", fill="both", expand=True, padx=(6, 0))

        self.do_frame = do
        self.do_cells = []
        self.do_notes = []
        self._dogrid = None
        self.v_dohex = tk.StringVar(value="DO 回读: --")
        self._build_do_grid(16)

        ttk.Label(do, textvariable=self.v_dohex, style="Card.TLabel",
                  font=("Consolas", 10, "bold")).pack(anchor="w", padx=12)

        r1 = ttk.Frame(do, style="Card.TFrame"); r1.pack(fill="x", padx=10, pady=(10, 2))
        ttk.Button(r1, text="全部吸合", command=lambda: self.on_set_all(0xFF)).pack(side="left")
        ttk.Button(r1, text="全部释放", command=lambda: self.on_set_all(0x00)).pack(side="left", padx=6)
        ttk.Button(r1, text="状态反相", command=self.on_invert_do).pack(side="left", padx=6)
        ttk.Button(r1, text="读取回读", command=self.on_read_do).pack(side="left", padx=6)

        self._byte_row_frame = do
        self._byte_row = None
        self._build_byte_row(2)

        # ---------------- 自检 ----------------
        test = ttk.LabelFrame(f, text="功能自检", style="Card.TLabelframe")
        test.pack(fill="x", padx=10, pady=(8, 10))
        trow = ttk.Frame(test, style="Card.TFrame")
        trow.pack(fill="x", padx=10, pady=8)
        self.v_perbit = tk.BooleanVar(value=True)
        ttk.Checkbutton(trow, text="包含逐位(单继电器)测试", variable=self.v_perbit,
                        style="Card.TCheckbutton").pack(side="left")
        ttk.Button(trow, text="▶ 运行 DO 回读 + DI 读取自检", style="Go.TButton",
                   command=self.on_selftest).pack(side="left", padx=12)
        ttk.Button(trow, text="导出测试报告", command=self.on_export_report).pack(side="left")

    # ------------------------------------------------------------ 动态网格
    def _build_di_grid(self, count: int) -> None:
        if self._digrid is not None:
            self._digrid.destroy()
        self.di_cells = []
        self.di_names = []
        self.di_notes = []
        grid = tk.Frame(self.di_frame, bg=C_CARD_BG)
        grid.pack(padx=10, pady=(10, 4))
        self._digrid = grid

        cols = 8
        rows = math.ceil(count / cols)
        for i in range(count):
            r, c = divmod(i, cols)
            cell = tk.Frame(grid, bd=1, relief="solid", bg="#eef1f4")
            cell.grid(row=r * 3, column=c, padx=3, pady=(3, 0), ipadx=8, ipady=3)
            led = tk.Label(cell, text="0", width=3, height=1, bg=DI_LO_COLOR,
                           font=("Consolas", 11, "bold"), fg="#333333")
            led.pack()
            name = tk.Label(grid, text="DI%d" % i, font=("Microsoft YaHei UI", 8, "bold"),
                            fg="#3c4a58", bg=C_CARD_BG, cursor="hand2")
            name.grid(row=r * 3 + 1, column=c, pady=(1, 0))
            note = tk.Label(grid, text="", font=("Microsoft YaHei UI", 7),
                            fg="#8a97a5", bg=C_CARD_BG, width=9, cursor="hand2")
            note.grid(row=r * 3 + 2, column=c, pady=(0, 4))
            for w in (name, note):
                w.bind("<Double-Button-1>", lambda _e, idx=i: self._edit_annotation("DI", idx))
            self.di_cells.append(led)
            self.di_names.append(name)
            self.di_notes.append(note)
            self._refresh_di_annotation(i)
        if rows > 1 and count % cols:
            # 末行补齐提示
            pass
        self._refresh_di_hex()

    def _build_do_grid(self, count: int) -> None:
        if self._dogrid is not None:
            self._dogrid.destroy()
        self.do_cells = []
        self.do_notes = []
        grid = tk.Frame(self.do_frame, bg=C_CARD_BG)
        grid.pack(padx=10, pady=(10, 4))
        self._dogrid = grid

        cols = 8
        for i in range(count):
            r, c = divmod(i, cols)
            b = tk.Button(grid, text="K%d" % i, width=5, height=1,
                          bg=OFF_COLOR, fg=BTN_OFF_FG,
                          font=("Microsoft YaHei UI", 9, "bold"),
                          relief="raised", bd=2,
                          command=lambda idx=i: self.on_toggle_do(idx))
            b.grid(row=r * 2, column=c, padx=3, pady=(3, 0), ipadx=4, ipady=5)
            note = tk.Label(grid, text="", font=("Microsoft YaHei UI", 7),
                            fg="#5a6a7a", bg=C_CARD_BG, width=9, cursor="hand2")
            note.grid(row=r * 2 + 1, column=c, pady=(0, 4))
            note.bind("<Double-Button-1>", lambda _e, idx=i: self._edit_annotation("K", idx))
            b.bind("<Double-Button-1>", lambda _e, idx=i: self._edit_annotation("K", idx))
            self.do_cells.append(b)
            self.do_notes.append(note)
            self._refresh_do_annotation(i)
        self._refresh_do_hex()

    def _build_byte_row(self, nports: int) -> None:
        if self._byte_row is not None:
            self._byte_row.destroy()
        row = ttk.Frame(self._byte_row_frame, style="Card.TFrame")
        row.pack(fill="x", padx=10, pady=(2, 10))
        self._byte_row = row
        self.v_bytes = []
        for p in range(nports):
            ttk.Label(row, text="端口%d(hex):" % p, style="Card.TLabel").pack(side="left")
            v = tk.StringVar(value="00")
            ttk.Entry(row, textvariable=v, width=6).pack(side="left", padx=4)
            self.v_bytes.append(v)
        ttk.Button(row, text="按字节写入", command=self.on_write_bytes).pack(side="left", padx=8)

    # ------------------------------------------------------------ 点位备注
    def _device_anno_key(self) -> str:
        if "1761" in self.device_model:
            return "PCI-1761"
        if "1762" in self.device_model:
            return "PCI-1762"
        return self.device_model or "(未知设备)"

    def _anno_map(self) -> dict:
        return self.annotations.setdefault(self._device_anno_key(), {})

    def _edit_annotation(self, prefix: str, idx: int) -> None:
        key = "%s%d" % (prefix, idx)
        cur = self._anno_map().get(key, "")
        new = simpledialog.askstring(
            "点位备注",
            "为 %s 设置备注名称 (留空清除):" % key,
            initialvalue=cur, parent=self.root)
        if new is None:
            return
        new = new.strip()
        if new:
            self._anno_map()[key] = new
        else:
            self._anno_map().pop(key, None)
        self._save_annotations()
        if prefix == "DI":
            self._refresh_di_annotation(idx)
        else:
            self._refresh_do_annotation(idx)
        self.log("点位备注 %s = %s" % (key, new if new else "(已清除)"), "ok")

    def _refresh_di_annotation(self, idx: int) -> None:
        note = self._anno_map().get("DI%d" % idx, "")
        if idx < len(self.di_notes):
            self.di_notes[idx].configure(
                text=(note[:10] + "…" if len(note) > 10 else note))

    def _refresh_do_annotation(self, idx: int) -> None:
        note = self._anno_map().get("K%d" % idx, "")
        if idx < len(self.do_notes):
            self.do_notes[idx].configure(
                text=(note[:10] + "…" if len(note) > 10 else note))

    def _refresh_all_annotations(self) -> None:
        for i in range(len(self.di_notes)):
            self._refresh_di_annotation(i)
        for i in range(len(self.do_notes)):
            self._refresh_do_annotation(i)

    def _load_annotations(self) -> None:
        try:
            with open(ANNO_FILE, "r", encoding="utf-8") as f:
                self.annotations = json.load(f) or {}
        except (OSError, ValueError):
            self.annotations = {}

    def _save_annotations(self) -> None:
        try:
            with open(ANNO_FILE, "w", encoding="utf-8") as f:
                json.dump(self.annotations, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _point_name(self, key: str) -> str:
        return self._anno_map().get(key, key)

    # =============================================================== 日志
    def log(self, text: str, level: str = "info") -> None:
        self._log_q.put((text, level))

    def _pump(self) -> None:
        try:
            while True:
                text, level = self._log_q.get_nowait()
                self.log_text.configure(state="normal")
                ts = time.strftime("%H:%M:%S")
                self.log_text.insert("end", "[%s] %s\n" % (ts, text), level)
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        try:
            while True:
                fn = self._ui_q.get_nowait()
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
        except queue.Empty:
            pass
        self.root.after(80, self._pump)

    def ui(self, fn) -> None:
        """把回调排到主线程执行。"""
        self._ui_q.put(fn)

    def set_progress(self, pct: float, msg: str = "") -> None:
        def _do():
            self.progress["value"] = max(0, min(1000, int(pct * 1000)))
            if msg:
                self.progress_lbl.configure(text=msg)
        self.ui(_do)

    # =============================================================== 配置
    def _cfg_dict(self) -> dict:
        return {
            "host": self.v_host.get().strip(),
            "port": self.v_port.get().strip(),
            "user": self.v_user.get().strip(),
            "password": self.v_pass.get() if self.v_savepw.get() else "",
            "save_password": bool(self.v_savepw.get()),
            "key": self.v_key.get().strip(),
            "workdir": self.v_workdir.get().strip(),
            "installer": self.v_installer.get().strip(),
            "cardmodel": self.v_cardmodel.get(),
            "drvname": self.v_drvname.get().strip(),
        }

    def _save_config(self) -> None:
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self._cfg_dict(), f, ensure_ascii=False, indent=2)
            self.log("配置已保存到 %s" % CONFIG_FILE, "ok")
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))

    def _load_config(self) -> None:
        # 兼容旧版配置文件
        old = CONFIG_FILE.replace("AdvantechIOTool", "PCI1762_RemoteTool")
        src = CONFIG_FILE if os.path.isfile(CONFIG_FILE) else old
        if not os.path.isfile(src):
            return
        try:
            with open(src, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            return
        self.v_host.set(cfg.get("host", self.v_host.get()))
        self.v_port.set(str(cfg.get("port", "22")))
        self.v_user.set(cfg.get("user", "root"))
        self.v_key.set(cfg.get("key", ""))
        self.v_workdir.set(cfg.get("workdir", ""))
        if cfg.get("installer"):
            self.v_installer.set(cfg["installer"])
        if cfg.get("cardmodel"):
            self.v_cardmodel.set(cfg["cardmodel"])
            self._sync_card_model()
        if cfg.get("drvname"):
            self.v_drvname.set(cfg["drvname"])
        if cfg.get("save_password") and cfg.get("password"):
            self.v_pass.set(cfg["password"])
            self.v_savepw.set(True)
        self.log("已载入上次保存的配置", "info")

    # ------------------------------------------------------------ 卡型
    def _sync_card_model(self) -> None:
        model = self.v_cardmodel.get()
        if "+" in model:
            models = [m.strip() for m in model.split("+")]
        else:
            models = [model]
        self.v_drvname.set(" ".join(drivers_for_models(models)))
        hints = []
        for m in models:
            info = CARD_MODELS.get(m)
            if info:
                hints.append("%s: %s" % (m, info[2]))
        self.card_hint.configure(text="   |   ".join(hints))

    def _selected_models(self) -> list:
        model = self.v_cardmodel.get()
        if "+" in model:
            return [m.strip() for m in model.split("+") if m.strip()]
        return [model] if model else []

    # =============================================================== 选择器
    def _pick_key(self) -> None:
        p = filedialog.askopenfilename(title="选择 SSH 私钥",
                                       filetypes=[("所有文件", "*.*")])
        if p:
            self.v_key.set(p)

    def _pick_installer(self) -> None:
        p = filedialog.askopenfilename(
            title="选择 DAQNavi Linux 驱动安装包",
            filetypes=[("研华驱动安装包", "*.run"), ("所有文件", "*.*")])
        if p:
            self.v_installer.set(p)

    # =============================================================== 线程
    def _run_async(self, target, name: str = "worker") -> None:
        if self.busy:
            messagebox.showwarning("请稍候", "当前有任务正在执行, 请等待其完成。")
            return
        self.busy = True
        self._set_buttons(False)

        def wrapper():
            try:
                target()
            except SSHError as exc:
                self.log(str(exc), "err")
            except Exception as exc:  # noqa: BLE001
                self.log("发生异常: %s" % exc, "err")
                self.log(traceback.format_exc(), "err")
            finally:
                self.busy = False
                self.ui(lambda: self._set_buttons(True))

        self.worker = threading.Thread(target=wrapper, name=name, daemon=True)
        self.worker.start()

    def _run_bg(self, target) -> None:
        """轻量后台任务: 不占用 busy 状态, 供 DI/DO 快速操作使用。"""
        def wrapper():
            try:
                target()
            except SSHError as exc:
                self.log(str(exc), "err")
            except Exception as exc:  # noqa: BLE001
                self.log("发生异常: %s" % exc, "err")
                self.log(traceback.format_exc(), "err")

        threading.Thread(target=wrapper, daemon=True).start()

    def _set_buttons(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for b in (self.btn_deploy, self.btn_probe, self.btn_verify,
                  self.btn_reboot, self.btn_uninstall):
            try:
                b.configure(state=state)
            except Exception:  # noqa: BLE001
                pass

    # =============================================================== 连接
    def _make_target(self) -> SSHTarget:
        return SSHTarget(
            host=self.v_host.get().strip(),
            port=int(self.v_port.get().strip() or "22"),
            username=self.v_user.get().strip(),
            password=self.v_pass.get(),
            key_path=self.v_key.get().strip(),
        )

    def on_test_connection(self) -> None:
        # 所有 Tk 变量必须在主线程读取, 再传给工作线程
        target = self._make_target()
        host_label = self.v_host.get()
        user_label = self.v_user.get()

        def job():
            self.set_progress(0.2, "正在连接 %s ..." % host_label)
            if self.ssh:
                self.ssh.close()
            self.ssh = SSHSession(target)
            banner = self.ssh.connect()
            self.log("SSH 连接成功: %s" % (banner or "(无 banner)"), "ok")

            res = self.ssh.exec("hostname; uname -r; uname -m; id -un", timeout=20)
            for line in res.stdout.strip().splitlines():
                self.log("  " + line, "out")

            dep = Deployer(self.ssh, lambda t, l="info": self.log(t, l),
                           DeployOptions())
            if dep.check_connectivity():
                self.ui(lambda: self.conn_state.configure(
                    text="● 已连接  %s@%s" % (user_label, host_label),
                    foreground="#1a7f37"))
                self.ui(lambda: self.status.set(
                    "已连接 %s@%s" % (user_label, host_label)))
            else:
                self.log("连接成功, 但权限检查未通过(安装驱动需要 root 或 sudo)", "warn")
                self.ui(lambda: self.conn_state.configure(
                    text="● 已连接(权限不足)", foreground="#b26a00"))
            self.set_progress(1.0, "连接测试完成")

        self._run_async(job, "connect")

    def on_disconnect(self) -> None:
        try:
            if self.io:
                self.io.stop()
                self.io = None
            if self.ssh:
                self.ssh.close()
                self.ssh = None
            self.ui(lambda: self.conn_state.configure(text="● 未连接",
                                                      foreground="#c62828"))
            self.ui(lambda: self.status.set("未连接"))
            self.ui(lambda: self.io_state.configure(text="● 未启动", foreground="#c62828"))
            self.log("已断开连接", "info")
        except Exception as exc:  # noqa: BLE001
            self.log("断开时出错: %s" % exc, "warn")

    def _ensure_ssh(self) -> bool:
        if not self.ssh or not self.ssh.connected:
            messagebox.showwarning("未连接", "请先在【连接配置】页建立 SSH 连接。")
            return False
        return True

    # =============================================================== 部署
    def _deploy_options(self) -> DeployOptions:
        names = self.v_drvname.get().split()
        primary = names[0] if names else "pci1762"
        return DeployOptions(
            installer_local_path=self.v_installer.get().strip(),
            remote_workdir=self.v_workdir.get().strip(),
            driver_name=primary,
            extra_drivers=names[1:],
            install_deps=self.v_deps.get(),
            update_apt_source=self.v_updapt.get(),
            disable_comedi=self.v_blacklist.get(),
            reboot_after=self.v_reboot.get(),
        )

    def on_probe(self) -> None:
        if not self._ensure_ssh():
            return
        opt = self._deploy_options()

        def job():
            self.set_progress(0.1, "正在探测目标系统环境 ...")
            dep = Deployer(self.ssh, self.log, opt)
            dep.check_connectivity()
            dep.probe_system()
            self.set_progress(1.0, "环境检测完成")

        self._run_async(job, "probe")

    def on_deploy(self) -> None:
        if not self._ensure_ssh():
            return
        opt = self._deploy_options()
        if not opt.installer_local_path or not os.path.isfile(opt.installer_local_path):
            messagebox.showerror("缺少安装包",
                                 "请先选择 DAQNavi Linux 驱动安装包(.run 文件)。\n\n"
                                 "预期文件: DAQ_Linux_20260306_64bit.run")
            return
        models = " + ".join(self._selected_models())
        if opt.reboot_after:
            if not messagebox.askyesno(
                    "确认安装",
                    "即将在目标机 %s 上:\n\n"
                    "  1) 安装编译依赖\n"
                    "  2) 屏蔽冲突驱动\n"
                    "  3) 上传并静默安装 %s\n"
                    "  4) 重启目标机\n\n"
                    "重启会中断目标机上正在运行的服务, 是否继续?" % (
                        self.v_host.get(), models)):
                return

        def job():
            dep = Deployer(self.ssh, self.log, opt)
            self.deployer = dep
            ok = dep.run_all(lambda p, m: self.set_progress(p, m))
            if ok:
                self.log("\n全部完成! 驱动已部署成功, 可切换到【IO 输入输出测试】页进行测试。", "ok")
                self.set_progress(1.0, "部署完成")
                self.ui(lambda: self.nb.select(self.tab_io))
            else:
                self.log("\n部署未全部成功, 请根据上面的日志排查。", "err")
                self.set_progress(1.0, "部署结束(存在错误)")

        self._run_async(job, "deploy")

    def on_uninstall(self) -> None:
        if not self._ensure_ssh():
            return
        opt = self._deploy_options()
        if not opt.installer_local_path or not os.path.isfile(opt.installer_local_path):
            messagebox.showerror(
                "缺少安装包",
                "卸载驱动需要与安装时相同的 .run 安装包\n"
                "(卸载脚本从安装包内读取驱动源码执行 make uninstall)。\n\n"
                "请先选择 DAQNavi Linux 驱动安装包。")
            return
        if not messagebox.askyesno(
                "确认卸载",
                "即将在目标机 %s 上卸载驱动:\n\n  %s\n\n"
                "卸载会执行 silent uninstall 并清理 /opt/advantech SDK 文件,\n"
                "卸载后 I/O 卡将无法使用, 直到重新安装驱动。\n\n是否继续?" % (
                    self.v_host.get(), " + ".join(self._selected_models()))):
            return

        def job():
            dep = Deployer(self.ssh, self.log, opt)
            self.deployer = dep
            self.set_progress(0.1, "正在卸载驱动 ...")
            ok = dep.uninstall_drivers()
            if ok:
                self.set_progress(0.7, "卸载完成, 建议重启目标机使模块彻底移除")
                self.log("\n驱动卸载完成。建议重启目标机, 然后点击【验证安装结果】复核。", "ok")
            else:
                self.set_progress(1.0, "卸载未完成(存在错误)")
                self.log("\n卸载未完成, 请根据日志排查。", "err")

        self._run_async(job, "uninstall")

    def on_verify(self) -> None:
        if not self._ensure_ssh():
            return
        opt = self._deploy_options()

        def job():
            dep = Deployer(self.ssh, self.log, opt)
            self.set_progress(0.5, "验证中 ...")
            dep.verify()
            self.set_progress(1.0, "验证完成")

        self._run_async(job, "verify")

    def on_reboot(self) -> None:
        if not self._ensure_ssh():
            return
        host = self.v_host.get()
        if not messagebox.askyesno("确认重启", "确定要重启目标机 %s 吗?" % host):
            return
        opt = self._deploy_options()

        def job():
            dep = Deployer(self.ssh, self.log, opt)
            dep.reboot()
            dep.wait_for_reboot(lambda p, m: self.set_progress(p, m))
            self.set_progress(1.0, "重启完成")

        self._run_async(job, "reboot")

    # ---------------------------------------------------------- 远程代理
    def on_agent_deploy(self) -> None:
        if not self._ensure_ssh():
            return
        wd_cfg = self.v_workdir.get()

        def job():
            self.set_progress(0.2, "正在部署远程测试代理 ...")
            wd = self._resolve_workdir(wd_cfg)
            session = IOSession(self.ssh, self.log, wd)
            if session.build(force=False):
                self.log("远程测试代理已就绪: %s/iotest/bio1762_io" % wd, "ok")
                self.set_progress(1.0, "代理部署完成")
            else:
                self.set_progress(1.0, "代理部署失败")

        self._run_async(job, "agent-deploy")

    def on_agent_status(self) -> None:
        if not self._ensure_ssh():
            return
        wd_cfg = self.v_workdir.get()

        def job():
            wd = self._resolve_workdir(wd_cfg)
            src = self.ssh.exec(
                "test -f %s/iotest/bio1762_io.c && echo yes || echo no" % wd,
                timeout=20).stdout.strip()
            binary = self.ssh.exec(
                "test -x %s/iotest/bio1762_io && echo yes || echo no" % wd,
                timeout=20).stdout.strip()
            self.log("代理目录   : %s/iotest %s" % (
                wd, "存在" if src == "yes" else "不存在"), "info")
            self.log("代理可执行 : %s" % (
                "已编译" if binary == "yes" else "未编译/不存在"),
                "ok" if binary == "yes" else "warn")
            self.set_progress(1.0, "代理状态检查完成")

        self._run_async(job, "agent-status")

    def on_agent_remove(self) -> None:
        if not self._ensure_ssh():
            return
        wd_cfg = self.v_workdir.get()
        if not messagebox.askyesno(
                "确认卸载代理",
                "将删除目标机上的远程测试代理目录 (iotest),\n"
                "不影响已安装的驱动。是否继续?"):
            return

        def job():
            self.set_progress(0.2, "正在卸载远程测试代理 ...")
            wd = self._resolve_workdir(wd_cfg)
            res = self.ssh.exec("rm -rf %s/iotest && echo removed" % wd, timeout=30)
            if res.stdout.strip() == "removed":
                self.log("远程测试代理已卸载 (%s/iotest 已删除)" % wd, "ok")
                self.set_progress(1.0, "代理卸载完成")
            else:
                self.log("代理卸载失败: %s" % res.text, "err")
                self.set_progress(1.0, "代理卸载失败")

        self._run_async(job, "agent-remove")

    # =============================================================== IO 会话
    def _resolve_workdir(self, wd_cfg: str) -> str:
        """在工作线程中解析远端工作目录(不访问 Tk 变量)。"""
        wd = (wd_cfg or "").strip()
        if not wd and self.deployer and self.deployer.remote_workdir:
            wd = self.deployer.remote_workdir
        if not wd:
            home = self.ssh.exec("echo $HOME", timeout=15).stdout.strip() or "/root"
            wd = "%s/advdaq_install" % home
        return wd

    def on_refresh_devices(self) -> None:
        if not self._ensure_ssh():
            return
        wd_cfg = self.v_workdir.get()

        def job():
            wd = self._resolve_workdir(wd_cfg)
            session = self.io if (self.io and self.io.running) else None
            temp = False
            if session is None:
                session = IOSession(self.ssh, self.log, wd)
                session.build(force=False)
                session.start()
                temp = True
            try:
                devs = session.list_devices()
                if not devs:
                    self.log("未检测到任何 DAQ 设备。", "warn")
                    devs = ["(未检测到设备)"]
                self.ui(lambda: self._set_device_list(devs))
            finally:
                if temp:
                    session.stop()

        self._run_async(job, "devices")

    def _set_device_list(self, devs) -> None:
        self.cmb_device["values"] = devs
        if devs:
            self.cmb_device.current(0)

    def on_io_start(self) -> None:
        if not self._ensure_ssh():
            return
        wd_cfg = self.v_workdir.get()
        dev = self.v_device.get().strip()

        def job():
            wd = self._resolve_workdir(wd_cfg)
            if self.io:
                self.io.stop()
            self.io = IOSession(self.ssh, self.log, wd)
            self.io.set_error_callback(lambda m: (self.log(m, "err"),
                                                  self.ui(lambda: self.io_state.configure(
                                                      text="● 会话异常", foreground="#c62828"))))
            self.io.set_di_callback(self._on_di_data)

            if not self.io.build(force=False):
                self.set_progress(1.0, "编译失败")
                return
            token = "" if (not dev or dev.startswith("(")) else dev
            if not self.io.start(token):
                self.set_progress(1.0, "启动失败")
                return

            info = self.io.fetch_info()
            if info is None:
                self.log("无法获取设备信息, 请确认驱动已安装且 I/O 卡已插好。", "err")
                self.ui(lambda: self.io_state.configure(text="● 启动但无设备",
                                                        foreground="#c62828"))
                self.set_progress(1.0, "未获取到设备")
                return

            self.ui(lambda: self._apply_device_info(info))
            self.log("IO 会话已启动", "ok")
            self.set_progress(1.0, "IO 会话就绪")
            # 初始读一次回读状态
            self.on_read_do()

        self._run_async(job, "io-start")

    def _apply_device_info(self, info: DeviceInfo) -> None:
        """主线程回调: 依据设备信息重建动态点位界面。"""
        self.io_state.configure(text="● 会话运行中", foreground="#1a7f37")
        self.device_model = info.desc or ""
        self.di_ports = max(1, info.di_ports or 1)
        self.do_ports = max(1, info.do_ports or 1)
        self.di_count = info.di_lines or self.di_ports * 8
        self.do_count = info.do_lines or self.do_ports * 8

        self._build_di_grid(self.di_count)
        self._build_do_grid(self.do_count)
        self._build_byte_row(self.do_ports)
        self._refresh_all_annotations()

        model_name = ("PCI-1762" if "1762" in self.device_model
                      else "PCI-1761" if "1761" in self.device_model
                      else "未知型号")
        self.v_devinfo.set(
            "设备信息: %s [%s]    DI %d 端口/%d 路    继电器 DO %d 端口/%d 路"
            % (info.desc or "未知", model_name, info.di_ports, self.di_count,
               info.do_ports, self.do_count))
        self.dev_var.set(info.desc or "")
        self.log("已按设备资源重建点位面板: DI %d 点 / DO %d 点" % (
            self.di_count, self.do_count), "info")

    def on_io_stop(self) -> None:
        if not self.io:
            return
        try:
            if self.watching:
                self.io.stop_watch()
                self.watching = False
            self.io.stop()
        finally:
            self.io = None
            self.ui(lambda: self.io_state.configure(text="● 未启动", foreground="#c62828"))
            self.log("IO 会话已结束", "info")

    # ---------------------------------------------------------- DI
    def _on_di_data(self, values) -> None:
        def _do():
            bits = []
            for v in values:
                for b in range(8):
                    bits.append((v >> b) & 1)
            for i in range(len(self.di_cells)):
                st = bits[i] if i < len(bits) else 0
                self.di_state[i] = st
                self.di_cells[i].configure(
                    bg=DI_HI_COLOR if st else DI_LO_COLOR,
                    fg="#ffffff" if st else "#333333", text=str(st))
            self._refresh_di_hex(values)
        self.ui(_do)

    def _refresh_di_hex(self, values=None) -> None:
        if values is None:
            ports = max(1, self.di_ports)
            values = [0] * ports
        self.v_dihex.set("DI 原始值: %s" % " ".join("0x%02X" % v for v in values))

    def on_watch_toggle(self) -> None:
        if not self.io or not self.io.running:
            messagebox.showwarning("未启动", "请先点击【启动 IO 会话】。")
            return
        if self.watching:
            self.io.stop_watch()
            self.watching = False
            self.btn_watch.configure(text="开始实时监视")
            self.log("已停止 DI 实时监视", "info")
            return

        try:
            interval = int(float(self.v_interval.get()))
        except ValueError:
            interval = 200
        interval = max(20, min(5000, interval))
        if self.io.start_watch(interval, 0, None):
            self.watching = True
            self.btn_watch.configure(text="停止实时监视")
            self.log("已开始 DI 实时监视(间隔 %d ms)" % interval, "ok")
        else:
            self.log("启动 DI 实时监视失败", "err")

    def on_read_di(self) -> None:
        if not (self.io and self.io.running):
            messagebox.showwarning("未启动", "请先点击【启动 IO 会话】。")
            return

        def job():
            vals = self.io.read_di(0, None)
            if vals:
                self._on_di_data(vals)
            else:
                self.log("读取 DI 失败", "err")

        self._run_bg(job)

    # ---------------------------------------------------------- DO
    def _refresh_do_view(self) -> None:
        for i in range(len(self.do_cells)):
            on = self.do_readback[i]
            name = self._point_name("K%d" % i)
            self.do_cells[i].configure(
                text="%s\n%s" % (name, "ON" if on else "OFF"),
                bg=ON_COLOR if on else OFF_COLOR,
                fg=BTN_ON_FG if on else BTN_OFF_FG)
        self._refresh_do_hex()

    def _refresh_do_hex(self, values=None) -> None:
        if values is None:
            nports = max(1, self.do_ports)
            ports = [0] * nports
            for i in range(len(self.do_cells)):
                if self.do_readback[i]:
                    ports[i // 8] |= (1 << (i % 8))
            values = ports
        self.v_dohex.set("DO 回读: %s" % " ".join("0x%02X" % v for v in values))

    def on_toggle_do(self, idx: int) -> None:
        if not (self.io and self.io.running):
            messagebox.showwarning("未启动", "请先点击【启动 IO 会话】。")
            return
        new = 0 if self.do_state[idx] else 1
        port, bit = idx // 8, idx % 8
        name = self._point_name("K%d" % idx)

        def job():
            if self.io.write_bit(port, bit, new):
                self.do_state[idx] = new
                self.log("继电器 %s (%s, 端口%d 位%d) -> %s" % (
                    name, "K%d" % idx, port, bit,
                    "吸合" if new else "释放"), "info")
                self.on_read_do()
            else:
                self.log("写入 %s 失败" % name, "err")

        self._run_bg(job)

    def on_set_all(self, value: int) -> None:
        if not (self.io and self.io.running):
            messagebox.showwarning("未启动", "请先点击【启动 IO 会话】。")
            return
        nports = max(1, self.do_ports)

        def job():
            ok = self.io.write_do(0, [value] * nports)
            if ok:
                for i in range(len(self.do_cells)):
                    self.do_state[i] = 1 if (value >> (i % 8)) & 1 else 0
                self.log("全部继电器 -> 0x%02X" % value, "ok")
                self.on_read_do()
            else:
                self.log("批量写入失败", "err")

        self._run_bg(job)

    def on_invert_do(self) -> None:
        if not (self.io and self.io.running):
            messagebox.showwarning("未启动", "请先点击【启动 IO 会话】。")
            return
        nports = max(1, self.do_ports)

        def job():
            ports = [0] * nports
            for i in range(len(self.do_cells)):
                if self.do_readback[i]:
                    p = i // 8
                    if p < nports:
                        ports[p] |= (1 << (i % 8))
            new = [(~v) & 0xFF for v in ports]
            if self.io.write_do(0, new[:nports]):
                self.log("继电器状态已反相 -> %s" %
                         " ".join("0x%02X" % v for v in new), "ok")
                self.on_read_do()
            else:
                self.log("反相写入失败", "err")

        self._run_bg(job)

    def on_read_do(self) -> None:
        if not (self.io and self.io.running):
            return

        def job():
            vals = self.io.read_do(0, None)
            if vals is None:
                self.log("读取 DO 回读失败", "err")
                return
            bits = []
            for v in vals:
                for b in range(8):
                    bits.append((v >> b) & 1)
            for i in range(len(self.do_cells)):
                self.do_readback[i] = bits[i] if i < len(bits) else 0
            self.ui(self._refresh_do_view)

        self._run_bg(job)

    def on_write_bytes(self) -> None:
        if not (self.io and self.io.running):
            messagebox.showwarning("未启动", "请先点击【启动 IO 会话】。")
            return
        vals = []
        try:
            for v in self.v_bytes:
                vals.append(int(v.get().strip() or "0", 16) & 0xFF)
        except ValueError:
            messagebox.showerror("格式错误", "请输入 00~FF 的十六进制值。")
            return
        nports = max(1, self.do_ports)
        vals = (vals + [0] * nports)[:nports]

        def job():
            if self.io.write_do(0, vals):
                self.log("按字节写入: %s" %
                         " ".join("端口%d=0x%02X" % (i, v) for i, v in enumerate(vals)), "ok")
                self.on_read_do()
            else:
                self.log("按字节写入失败", "err")

        self._run_bg(job)

    # ---------------------------------------------------------- 自检
    def on_selftest(self) -> None:
        if not (self.io and self.io.running):
            messagebox.showwarning("未启动", "请先点击【启动 IO 会话】。")
            return
        per_bit = bool(self.v_perbit.get())

        def job():
            self.set_progress(0.1, "正在执行自检 ...")
            self.log("\n========== 开始功能自检 ==========", "step")
            ok, lines = self.io.run_selftest(
                per_bit=per_bit,
                progress=lambda t: self.log(t, "out"))
            # 附上点位备注表
            anno = self._anno_map()
            if anno:
                lines.append("")
                lines.append("---- 点位备注表 ----")
                for key in sorted(anno, key=lambda k: (k[:2], int(k[2:]))):
                    lines.append("  %-6s = %s" % (key, anno[key]))
                self.log("已附加点位备注表(共 %d 条)到测试报告" % len(anno), "info")
            self.report_lines = lines
            self.set_progress(1.0, "自检完成")
            self.log("========== 自检%s ==========\n" % ("通过" if ok else "结束(有不一致项)"),
                     "ok" if ok else "warn")
            self.on_read_do()
            self.on_read_di()

        self._run_async(job, "selftest")

    def on_export_report(self) -> None:
        lines = getattr(self, "report_lines", None)
        if not lines:
            messagebox.showinfo("无报告", "请先运行一次自检。")
            return
        path = filedialog.asksaveasfilename(
            title="导出测试报告", defaultextension=".txt",
            initialfile="IO测试报告_%s.txt" % time.strftime("%Y%m%d_%H%M%S"),
            filetypes=[("文本文件", "*.txt")])
        if not path:
            return
        header = [
            APP_TITLE,
            "远程 I/O 测试报告",
            "生成时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
            "目标主机: %s@%s" % (self.v_user.get(), self.v_host.get()),
            "设备信息: %s" % (self.v_devinfo.get().replace("设备信息: ", "")),
            "工具版本: %s · 开发者：陈宠" % __version__,
            "-" * 60,
        ]
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(header + lines) + "\n")
            self.log("测试报告已导出: %s" % path, "ok")
            messagebox.showinfo("导出成功", "报告已保存到:\n%s" % path)
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc))

    # =============================================================== 关闭
    def _on_close(self) -> None:
        if self.busy:
            if not messagebox.askyesno("正在执行任务", "有任务正在执行, 确定要退出吗?"):
                return
        try:
            self._save_config()
            self._save_annotations()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.io:
                self.io.stop()
            if self.ssh:
                self.ssh.close()
        except Exception:  # noqa: BLE001
            pass
        self.root.destroy()


def main() -> int:
    # 命令行自检(便于打包后验证 exe 是否正常)
    if "--version" in sys.argv or "-V" in sys.argv:
        print("%s v%s" % (APP_TITLE, __version__))
        print("Python: %s" % sys.version.split()[0])
        try:
            import paramiko
            print("paramiko: %s" % paramiko.__version__)
        except Exception as exc:  # noqa: BLE001
            print("paramiko: 导入失败 (%s)" % exc)
        return 0
    if "--check-assets" in sys.argv:
        print("资源根目录   : %s" % assets.resource_root())
        print("远端 C 源码  : %s %s" % (
            assets.remote_c_source(), "存在" if os.path.isfile(assets.remote_c_source()) else "缺失!"))
        print("远端 Makefile: %s %s" % (
            assets.remote_makefile(), "存在" if os.path.isfile(assets.remote_makefile()) else "缺失!"))
        logo = assets.logo_path()
        print("公司 Logo    : %s %s" % (logo, "存在" if logo and os.path.isfile(logo) else "缺失!"))
        print("自动定位安装包: %s" % (assets.guess_installer_path() or "(未找到, 需手动选择)"))
        return 0

    root = tk.Tk()
    try:
        # 高 DPI 适配
        from ctypes import windll  # type: ignore
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    app = App(root)
    icon = assets.icon_path()
    if icon:
        try:
            root.iconbitmap(icon)
        except Exception:  # noqa: BLE001
            pass
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
