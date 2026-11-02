#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""研华 I/O 卡手机控制台 —— 桥接服务。

手机浏览器(以及安装到桌面的 PWA)不能直接建 TCP/SSH 连接, 因此由本服务
在后台承担 SSH 与远端代理的实际工作, 手机端只负责界面与交互:

    手机浏览器 / PWA  ──HTTP+SSE──▶  bridge.py  ──SSH──▶  目标 Linux (研华 I/O 卡)
                                     └─或 --local 模式直接在本机执行

两种运行方式:
    python bridge.py                     # 远程模式: 桥接跑在 PC/服务器上, SSH 到目标机
    python bridge.py --local             # 本机模式: 直接跑在带 I/O 卡的目标 Linux 上

依赖: 仅标准库 + paramiko(远程模式才用到), 无第三方 Web 框架。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import socket
import sys
import threading
import time
import traceback
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional
from urllib.parse import unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import logging                                                            # noqa: E402

logging.getLogger("paramiko").setLevel(logging.CRITICAL)

from daq_remote import __version__, assets                                # noqa: E402
from daq_remote.deployer import (                                         # noqa: E402
    CARD_MODELS, Deployer, DeployOptions, drivers_for_models)
from daq_remote.io_client import DeviceInfo, IOSession                    # noqa: E402
from daq_remote.ssh_session import SSHError, SSHSession, SSHTarget        # noqa: E402
from local_shell import LocalShell                                        # noqa: E402

APP_TITLE = "研华I/O卡 手机控制台"
MAX_UPLOAD = 512 * 1024 * 1024      # 单个安装包上限 512MB


# =============================================================== 事件广播
class EventHub:
    """极简发布/订阅: 每个 SSE 客户端一个队列, 满了就丢(实时数据无需保序重放)。"""

    def __init__(self) -> None:
        self._subs: List["queue.Queue[str]"] = []
        self._lock = threading.Lock()

    def subscribe(self) -> "queue.Queue[str]":
        q: "queue.Queue[str]" = queue.Queue(maxsize=400)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue[str]") -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: dict) -> None:
        try:
            payload = json.dumps(event, ensure_ascii=False)
        except (TypeError, ValueError):
            return
        with self._lock:
            targets = list(self._subs)
        for q in targets:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# =============================================================== 控制台状态
class Console:
    """把 daq_remote 的能力包装成一组给手机调用的操作。"""

    def __init__(self, mode: str = "ssh", data_dir: str = HERE):
        self.mode = mode
        self.data_dir = data_dir
        os.makedirs(os.path.join(data_dir, "uploads"), exist_ok=True)

        self.hub = EventHub()
        self.lock = threading.RLock()
        self.logs: deque = deque(maxlen=600)

        self.ssh: Optional[object] = None
        self.io: Optional[IOSession] = None
        self.deployer: Optional[Deployer] = None

        self.busy = False
        self.job = ""
        self.progress_pct = 0.0
        self.progress_msg = "就绪"

        self.device: Optional[DeviceInfo] = None
        self.di: List[int] = []
        self.do: List[int] = []
        self.do_state: List[int] = []
        self.watching = False
        self.devices: List[str] = []
        self.report: List[str] = []
        self.report_time = 0.0
        self.sudo_ok = False
        self.shell_info = ""

        self.cfg: Dict = {}
        self.annotations: Dict[str, Dict[str, str]] = {}
        self._load()

        if mode == "local":
            self.ssh = LocalShell(self.log)
            try:
                self.ssh.connect()
                self.connected  # noqa: B018  触发一次状态计算
            except Exception as exc:      # noqa: BLE001
                self.log("本机模式初始化失败: %s" % exc, "err")

    # ------------------------------------------------------------ 日志
    def log(self, text: str, level: str = "info") -> None:
        item = {"ts": time.strftime("%H:%M:%S"), "level": level, "msg": text}
        with self.lock:
            self.logs.append(item)
        self.hub.publish({"t": "log", **item})

    def set_progress(self, pct: float, msg: str = "") -> None:
        with self.lock:
            self.progress_pct = max(0.0, min(1.0, float(pct)))
            if msg:
                self.progress_msg = msg
            data = {"pct": self.progress_pct, "msg": self.progress_msg}
        self.hub.publish({"t": "progress", **data})

    # ------------------------------------------------------------ 配置持久化
    def _cfg_path(self) -> str:
        return os.path.join(self.data_dir, "console_config.json")

    def _anno_path(self) -> str:
        return os.path.join(self.data_dir, "console_annotations.json")

    def _load(self) -> None:
        try:
            with open(self._cfg_path(), "r", encoding="utf-8") as f:
                self.cfg = json.load(f) or {}
        except (OSError, ValueError):
            self.cfg = {}
        try:
            with open(self._anno_path(), "r", encoding="utf-8") as f:
                self.annotations = json.load(f) or {}
        except (OSError, ValueError):
            self.annotations = {}
        if not self.cfg.get("installer"):
            guess = assets.guess_installer_path()
            if guess:
                self.cfg["installer"] = guess

    def save_cfg(self, data: Optional[dict] = None) -> None:
        with self.lock:
            if data:
                for k, v in data.items():
                    if k == "password" and not data.get("remember"):
                        continue
                    if k != "password":
                        self.cfg[k] = v
                    elif v:
                        self.cfg[k] = v
            if not self.cfg.get("remember"):
                self.cfg.pop("password", None)
        try:
            with open(self._cfg_path(), "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            self.log("保存配置失败: %s" % exc, "warn")

    def save_annotations(self) -> None:
        try:
            with open(self._anno_path(), "w", encoding="utf-8") as f:
                json.dump(self.annotations, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # ------------------------------------------------------------ 派生状态
    @property
    def connected(self) -> bool:
        try:
            return bool(self.ssh and getattr(self.ssh, "connected", False))
        except Exception:      # noqa: BLE001
            return False

    def anno_key(self) -> str:
        desc = (self.device.desc if self.device else "") or ""
        if "1761" in desc:
            return "PCI-1761"
        if "1762" in desc:
            return "PCI-1762"
        return desc or "未识别设备"

    def anno_map(self) -> Dict[str, str]:
        return self.annotations.setdefault(self.anno_key(), {})

    def device_summary(self) -> Optional[dict]:
        if not self.device:
            return None
        d = self.device
        model = ("PCI-1762" if "1762" in (d.desc or "")
                 else "PCI-1761" if "1761" in (d.desc or "") else "未知型号")
        return {
            "desc": d.desc, "model": model,
            "di_ports": d.di_ports, "do_ports": d.do_ports,
            "di_lines": d.di_lines or (d.di_ports or 0) * 8,
            "do_lines": d.do_lines or (d.do_ports or 0) * 8,
            "di_ok": bool(d.di_ok), "do_ok": bool(d.do_ok),
        }

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "mode": self.mode,
                "version": __version__,
                "title": APP_TITLE,
                "connected": self.connected,
                "shell_info": self.shell_info,
                "sudo_ok": self.sudo_ok,
                "busy": self.busy,
                "job": self.job,
                "progress": {"pct": self.progress_pct, "msg": self.progress_msg},
                "device": self.device_summary(),
                "devices": self.devices,
                "io_running": bool(self.io and self.io.running),
                "watching": self.watching,
                "di": self.di,
                "do": self.do,
                "installer": self.installer_path(),
                "installers": self.list_installers(),
                "annotations": self.anno_map(),
                "anno_key": self.anno_key(),
                "config": self.public_cfg(),
                "report": {"lines": len(self.report), "ts": self.report_time},
            }

    def public_cfg(self) -> dict:
        c = dict(self.cfg)
        if not c.get("remember"):
            c.pop("password", None)
        return c

    def publish_state(self) -> None:
        try:
            self.hub.publish({"t": "state", "state": self.snapshot()})
        except Exception:      # noqa: BLE001
            pass

    # ------------------------------------------------------------ 安装包
    def upload_dir(self) -> str:
        return os.path.join(self.data_dir, "uploads")

    def list_installers(self) -> List[dict]:
        out: List[dict] = []
        seen = set()
        for base in (self.upload_dir(), ROOT, os.path.dirname(ROOT)):
            try:
                names = os.listdir(base)
            except OSError:
                continue
            for name in sorted(names):
                if not name.endswith(".run") or "/" in name:
                    continue
                path = os.path.join(base, name)
                if path in seen or not os.path.isfile(path):
                    continue
                seen.add(path)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                out.append({"name": name, "path": path, "size": size})
        return out

    def installer_path(self) -> str:
        p = (self.cfg.get("installer") or "").strip()
        if p and os.path.isfile(p):
            return p
        items = self.list_installers()
        if items:
            self.cfg["installer"] = items[0]["path"]
            return items[0]["path"]
        return ""

    def save_upload(self, name: str, stream, total: int) -> dict:
        name = os.path.basename(name or "").replace("\\", "_")
        if not name.lower().endswith(".run"):
            raise ApiError("只接受 .run 驱动安装包")
        if total <= 0:
            raise ApiError("上传内容为空")
        if total > MAX_UPLOAD:
            raise ApiError("文件过大(上限 512MB)")
        dst = os.path.join(self.upload_dir(), name)
        done = 0
        tmp = dst + ".part"
        with open(tmp, "wb") as f:
            while done < total:
                chunk = stream.read(min(262144, total - done))
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
        if done != total:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise ApiError("上传中断(收到 %d/%d 字节)" % (done, total))
        os.replace(tmp, dst)
        os.chmod(dst, 0o755)
        with self.lock:
            self.cfg["installer"] = dst
        self.save_cfg()
        self.log("安装包已上传: %s (%.1f MB)" % (name, done / 1048576.0), "ok")
        return {"name": name, "path": dst, "size": done}

    # ------------------------------------------------------------ 任务调度
    def start_job(self, name: str, fn) -> None:
        with self.lock:
            if self.busy:
                raise ApiError("当前有任务正在执行: %s" % (self.job or "任务"), 409)
            self.busy = True
            self.job = name
        self.publish_state()

        def runner() -> None:
            ok = False
            try:
                ok = fn() is not False
            except SSHError as exc:
                self.log(str(exc), "err")
            except ApiError as exc:
                self.log(str(exc), "err")
            except Exception as exc:      # noqa: BLE001
                self.log("发生异常: %s" % exc, "err")
                self.log(traceback.format_exc(), "err")
            finally:
                with self.lock:
                    self.busy = False
                    self.job = ""
                self.set_progress(1.0 if ok else self.progress_pct,
                                  "完成" if ok else "已结束(请查看日志)")
                self.publish_state()

        threading.Thread(target=runner, name="job-%s" % name, daemon=True).start()

    def require_conn(self) -> None:
        if not self.connected:
            raise ApiError("尚未连接目标主机", 409)

    # ------------------------------------------------------------ 连接
    def connect(self, host: str, port: int, user: str, password: str,
                key: str = "", workdir: str = "") -> None:
        if self.mode == "local":
            if not self.ssh:
                self.ssh = LocalShell(self.log)
                self.ssh.connect()
            self.log("本机模式: 已在目标机本地运行, 无需 SSH", "ok")
            self.sudo_ok = os.geteuid() == 0 if hasattr(os, "geteuid") else False
            if not self.sudo_ok:
                self.log("当前进程不是 root, 安装驱动可能失败(建议用 systemd 以 root 运行)", "warn")
            self.publish_state()
            return

        def job() -> bool:
            self.set_progress(0.15, "正在连接 %s ..." % host)
            if self.ssh:
                try:
                    self.ssh.close()
                except Exception:      # noqa: BLE001
                    pass
            self.ssh = SSHSession(SSHTarget(
                host=host, port=int(port or 22), username=user or "root",
                password=password or "", key_path=key or ""))
            banner = self.ssh.connect()
            self.log("SSH 连接成功: %s" % (banner or "(无 banner)"), "ok")
            res = self.ssh.exec("hostname; uname -r; uname -m; id -un", timeout=25)
            self.shell_info = " | ".join(x.strip() for x in res.stdout.splitlines() if x.strip())
            for line in res.stdout.strip().splitlines():
                self.log("  " + line, "out")
            dep = Deployer(self.ssh, self.log, DeployOptions())
            self.sudo_ok = dep.check_connectivity()
            if self.sudo_ok:
                self.log("连接就绪, 可以开始部署或测试", "ok")
            else:
                self.log("已连接, 但权限不足(安装驱动需要 root 或可用 sudo)", "warn")
            self.publish_state()
            return True

        self.start_job("connect", job)

    def disconnect(self) -> None:
        try:
            if self.io:
                self.io.stop()
        except Exception:      # noqa: BLE001
            pass
        self.io = None
        self.watching = False
        if self.mode != "local" and self.ssh:
            try:
                self.ssh.close()
            except Exception:      # noqa: BLE001
                pass
            self.ssh = None
        self.device = None
        self.devices = []
        self.di = []
        self.do = []
        self.log("已断开连接", "info")
        self.publish_state()

    # ------------------------------------------------------------ 工作目录
    def workdir(self) -> str:
        wd = (self.cfg.get("workdir") or "").strip()
        if wd:
            return wd
        if self.deployer and self.deployer.remote_workdir:
            return self.deployer.remote_workdir
        try:
            home = self.ssh.exec("echo $HOME", timeout=15).stdout.strip() or "/root"
        except Exception:      # noqa: BLE001
            home = os.path.expanduser("~")
        return "%s/advdaq_install" % home

    # ------------------------------------------------------------ 部署
    def _models(self, payload: dict) -> List[str]:
        raw = payload.get("models") or self.cfg.get("models") or ["PCI-1762"]
        if isinstance(raw, str):
            raw = [m.strip() for m in raw.replace("+", " ").split() if m.strip()]
        return [m for m in raw if m]

    def _deploy_opts(self, payload: dict) -> DeployOptions:
        models = self._models(payload)
        drivers = drivers_for_models(models) or ["pci1762"]
        opt = DeployOptions(
            installer_local_path=self.installer_path(),
            remote_workdir=(payload.get("workdir") or self.cfg.get("workdir") or "").strip(),
            driver_name=drivers[0],
            extra_drivers=drivers[1:],
            install_deps=bool(payload.get("install_deps", True)),
            update_apt_source=bool(payload.get("update_source", True)),
            disable_comedi=bool(payload.get("blacklist", True)),
            reboot_after=bool(payload.get("reboot", True)),
        )
        # 本机模式下"重启"会把本服务自身重启掉, 交给界面上的独立按钮更安全
        if self.mode == "local":
            opt.reboot_after = False
        return opt

    def deploy(self, payload: dict) -> None:
        self.require_conn()
        opt = self._deploy_opts(payload)
        if not opt.installer_local_path:
            raise ApiError("未找到驱动安装包(.run), 请先在页面上传")

        def job() -> bool:
            dep = Deployer(self.ssh, self.log, opt)
            self.deployer = dep
            ok = dep.run_all(lambda p, m: self.set_progress(p, m))
            if ok:
                self.log("驱动部署完成, 可以到【测试】页读取点位了", "ok")
                if self.mode == "local":
                    self.log("本机模式下驱动需重启后才会加载, 请点【重启目标机】", "warn")
            else:
                self.log("部署未全部成功, 请根据日志排查", "err")
            return ok

        self.start_job("deploy", job)

    def uninstall(self, payload: dict) -> None:
        self.require_conn()
        opt = self._deploy_opts(payload)

        def job() -> bool:
            dep = Deployer(self.ssh, self.log, opt)
            self.deployer = dep
            self.set_progress(0.3, "正在卸载驱动 ...")
            return dep.uninstall_drivers()

        self.start_job("uninstall", job)

    def verify(self, payload: dict) -> None:
        self.require_conn()
        opt = self._deploy_opts(payload)

        def job() -> bool:
            dep = Deployer(self.ssh, self.log, opt)
            self.set_progress(0.5, "验证中 ...")
            return dep.verify()

        self.start_job("verify", job)

    def probe(self, payload: dict) -> None:
        self.require_conn()
        opt = self._deploy_opts(payload)

        def job() -> bool:
            dep = Deployer(self.ssh, self.log, opt)
            self.deployer = dep
            self.set_progress(0.3, "探测系统环境 ...")
            dep.check_connectivity()
            dep.probe_system()
            return True

        self.start_job("probe", job)

    def reboot(self, payload: dict) -> None:
        self.require_conn()
        opt = self._deploy_opts(payload)

        def job() -> bool:
            dep = Deployer(self.ssh, self.log, opt)
            dep.reboot()
            if self.mode == "local":
                self.log("本机重启中, 手机端会短暂显示离线, 约 40 秒后自动恢复", "warn")
                return True
            dep.wait_for_reboot(lambda p, m: self.set_progress(p, m))
            return True

        self.start_job("reboot", job)

    # ------------------------------------------------------------ 代理
    def agent_deploy(self) -> None:
        self.require_conn()

        def job() -> bool:
            self.set_progress(0.3, "部署测试代理 ...")
            wd = self.workdir()
            session = IOSession(self.ssh, self.log, wd)
            ok = session.build(force=False)
            if ok:
                self.log("测试代理就绪: %s/iotest/bio1762_io" % wd, "ok")
            return ok

        self.start_job("agent", job)

    def agent_remove(self) -> None:
        self.require_conn()

        def job() -> bool:
            wd = self.workdir()
            res = self.ssh.exec("rm -rf %s/iotest && echo removed" % wd, timeout=40)
            if res.stdout.strip() == "removed":
                self.log("测试代理已删除 (%s/iotest)" % wd, "ok")
                return True
            self.log("删除失败: %s" % res.text, "err")
            return False

        self.start_job("agent-remove", job)

    # ------------------------------------------------------------ IO 会话
    def refresh_devices(self) -> None:
        self.require_conn()

        def job() -> bool:
            wd = self.workdir()
            session = self.io if (self.io and self.io.running) else None
            temp = False
            if session is None:
                session = IOSession(self.ssh, self.log, wd)
                if not session.build(force=False):
                    return False
                session.start()
                temp = True
            try:
                devs = session.list_devices()
            finally:
                if temp:
                    session.stop()
            with self.lock:
                self.devices = devs or []
            if devs:
                self.log("发现 %d 个设备: %s" % (len(devs), ", ".join(devs)), "ok")
            else:
                self.log("未检测到任何 DAQ 设备", "warn")
            self.publish_state()
            return bool(devs)

        self.start_job("devices", job)

    def io_start(self, payload: dict) -> None:
        self.require_conn()
        token = (payload.get("device") or "").strip()

        def job() -> bool:
            wd = self.workdir()
            if self.io:
                try:
                    self.io.stop()
                except Exception:      # noqa: BLE001
                    pass
            session = IOSession(self.ssh, self.log, wd)
            session.set_error_callback(self._on_io_error)
            session.set_di_callback(self._on_di_data)
            self.io = session
            self.set_progress(0.2, "编译并启动测试代理 ...")
            if not session.build(force=False):
                return False
            if not session.start(token):
                return False
            self.set_progress(0.7, "读取设备信息 ...")
            info = session.fetch_info()
            if info is None:
                self.log("未取到设备信息, 请确认驱动已安装且 I/O 卡已插好", "err")
                return False
            with self.lock:
                self.device = info
                self.watching = False
            self.log("设备: %s (DI %d 路 / DO %d 路)" % (
                info.desc or "未知", info.di_lines or info.di_ports * 8,
                info.do_lines or info.do_ports * 8), "ok")
            self.read_di()
            self.read_do()
            self.publish_state()
            return True

        self.start_job("io-start", job)

    def io_stop(self) -> None:
        try:
            if self.io:
                if self.watching:
                    self.io.stop_watch()
                self.io.stop()
        except Exception:      # noqa: BLE001
            pass
        self.io = None
        self.watching = False
        self.device = None
        self.log("IO 会话已结束", "info")
        self.publish_state()

    def _on_io_error(self, msg: str) -> None:
        self.log(msg, "err")
        self.hub.publish({"t": "io_error", "msg": msg})

    def _on_di_data(self, values: List[int]) -> None:
        with self.lock:
            self.di = list(values)
        self.hub.publish({"t": "di", "ports": list(values)})

    def _need_io(self) -> IOSession:
        if not (self.io and self.io.running):
            raise ApiError("IO 会话未启动", 409)
        return self.io

    def read_di(self) -> Optional[List[int]]:
        io = self._need_io()
        vals = io.read_di(0, None)
        if vals is None:
            self.log("读取 DI 失败", "err")
            return None
        self._on_di_data(vals)
        return vals

    def read_do(self) -> Optional[List[int]]:
        io = self._need_io()
        vals = io.read_do(0, None)
        if vals is None:
            self.log("读取 DO 回读失败", "err")
            return None
        with self.lock:
            self.do = list(vals)
            bits = []
            for v in vals:
                for b in range(8):
                    bits.append((v >> b) & 1)
            self.do_state = bits
        self.hub.publish({"t": "do", "ports": list(vals)})
        return vals

    def _after_write(self) -> None:
        try:
            self.read_do()
        except Exception:      # noqa: BLE001
            pass
        self.publish_state()

    def do_bit(self, payload: dict) -> dict:
        io = self._need_io()
        idx = int(payload.get("index", -1))
        value = 1 if payload.get("value") else 0
        if idx < 0:
            raise ApiError("点位序号无效")
        port, bit = idx // 8, idx % 8
        if not io.write_bit(port, bit, value):
            raise ApiError("写入 K%d 失败" % idx, 500)
        self.log("继电器 K%d -> %s" % (idx, "吸合" if value else "释放"), "info")
        self._after_write()
        return {"ok": True}

    def do_all(self, value: int) -> dict:
        io = self._need_io()
        n = max(1, self.device.do_ports if self.device else 2)
        if not io.write_do(0, [value & 0xFF] * n):
            raise ApiError("批量写入失败", 500)
        self.log("全部继电器 -> 0x%02X" % (value & 0xFF), "ok")
        self._after_write()
        return {"ok": True}

    def do_invert(self) -> dict:
        io = self._need_io()
        n = max(1, self.device.do_ports if self.device else 2)
        cur = self.read_do() or []
        ports = (list(cur) + [0] * n)[:n]
        new = [(~v) & 0xFF for v in ports]
        if not io.write_do(0, new):
            raise ApiError("反相写入失败", 500)
        self.log("继电器已反相 -> %s" % " ".join("0x%02X" % v for v in new), "ok")
        self._after_write()
        return {"ok": True}

    def do_bytes(self, payload: dict) -> dict:
        io = self._need_io()
        raw = payload.get("values") or []
        try:
            vals = [int(str(v).strip() or "0", 16) & 0xFF for v in raw]
        except (TypeError, ValueError):
            raise ApiError("请输入 00~FF 的十六进制值")
        n = max(1, self.device.do_ports if self.device else 2)
        vals = (vals + [0] * n)[:n]
        if not io.write_do(0, vals):
            raise ApiError("按字节写入失败", 500)
        self.log("按字节写入: %s" % " ".join("P%d=0x%02X" % (i, v) for i, v in enumerate(vals)), "ok")
        self._after_write()
        return {"ok": True}

    def watch_start(self, payload: dict) -> dict:
        io = self._need_io()
        try:
            interval = int(float(payload.get("interval", 200)))
        except (TypeError, ValueError):
            interval = 200
        interval = max(20, min(5000, interval))
        if not io.start_watch(interval, 0, None):
            raise ApiError("启动实时监视失败", 500)
        with self.lock:
            self.watching = True
        self.log("已开始 DI 实时监视(间隔 %d ms)" % interval, "ok")
        self.publish_state()
        return {"ok": True, "interval": interval}

    def watch_stop(self) -> dict:
        io = self._need_io()
        ok = io.stop_watch()
        with self.lock:
            self.watching = False
        if ok:
            self.log("已停止 DI 实时监视", "info")
        self.publish_state()
        return {"ok": bool(ok)}

    def selftest(self, payload: dict) -> None:
        self._need_io()
        per_bit = bool(payload.get("per_bit", True))

        def job() -> bool:
            self.set_progress(0.05, "开始功能自检 ...")
            self.log("\n========== 开始功能自检 ==========", "step")
            ok, lines = self.io.run_selftest(per_bit=per_bit,
                                             progress=lambda t: self.log(t, "out"))
            anno = self.anno_map()
            if anno:
                lines.append("")
                lines.append("---- 点位备注表 ----")
                for key in sorted(anno, key=lambda k: (k[:2], int(k[2:] or 0))):
                    lines.append("  %-6s = %s" % (key, anno[key]))
            with self.lock:
                self.report = lines
                self.report_time = time.time()
            self.log("自检%s" % ("通过" if ok else "结束(存在不一致项)"),
                     "ok" if ok else "warn")
            self._after_write()
            self.publish_state()
            return ok

        self.start_job("selftest", job)

    def report_text(self) -> str:
        head = [
            APP_TITLE,
            "远程 I/O 测试报告",
            "生成时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S",
                                         time.localtime(self.report_time or time.time())),
            "运行模式: %s" % ("本机(目标机内)" if self.mode == "local" else "远程 SSH"),
            "设备信息: %s" % (self.device.desc if self.device else "未知"),
            "工具版本: %s · 设计开发人：陈宠" % __version__,
            "-" * 60,
        ]
        return "\n".join(head + (self.report or ["(尚未运行自检)"])) + "\n"

    # ------------------------------------------------------------ 备注
    def set_annotation(self, key: str, name: str) -> dict:
        key = (key or "").strip()
        if not key:
            raise ApiError("点位名称无效")
        name = (name or "").strip()
        m = self.anno_map()
        if name:
            m[key] = name
        else:
            m.pop(key, None)
        self.save_annotations()
        self.log("点位备注 %s = %s" % (key, name or "(已清除)"), "ok")
        self.publish_state()
        return {"ok": True, "annotations": m}


# =============================================================== HTTP 服务
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/activate.js": ("activate.js", "application/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json; charset=utf-8"),
    "/sw.js": ("sw.js", "application/javascript; charset=utf-8"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
}

WEB_DIR = os.path.join(HERE, "web")


def set_web_dir(path: str) -> None:
    """嵌入式运行(APK)时重设界面资产目录。"""
    global WEB_DIR
    WEB_DIR = path


class Handler(BaseHTTPRequestHandler):
    console: Console = None         # type: ignore[assignment]
    server_version = "DaqConsole/%s" % __version__
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------- 基础
    def log_message(self, fmt: str, *args) -> None:      # noqa: A003
        pass        # 静音: 每个 IO 操作都打一行会把控制台刷满

    # APK 壳从本地资产页(https://appassets.androidplatform.net)跨源访问服务,
    # 因此所有响应统一携带 CORS 头
    def end_headers(self) -> None:                       # noqa: N802
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def do_OPTIONS(self) -> None:                        # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-File-Name")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"ok": False, "error": message}, status)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError("请求体不是合法 JSON")

    # ---------------------------------------------------------- GET
    def do_GET(self) -> None:      # noqa: N802
        path = urlparse(self.path).path
        try:
            if path in STATIC_FILES:
                return self._static(*STATIC_FILES[path])
            if path == "/api/state":
                return self._json({"ok": True, "state": self.console.snapshot()})
            if path == "/api/log/history":
                return self._json({"ok": True, "logs": list(self.console.logs)})
            if path == "/api/events":
                return self._sse()
            if path == "/api/report":
                return self._report()
            if path == "/api/installers":
                return self._json({"ok": True, "installers": self.console.list_installers()})
            if path == "/api/devices":
                self.console.refresh_devices()
                return self._json({"ok": True, "started": True})
            if path == "/api/ping":
                return self._json({"ok": True, "ts": time.time(), "version": __version__})
            return self._error("未知接口: %s" % path, 404)
        except ApiError as exc:
            return self._error(str(exc), exc.status)
        except Exception as exc:      # noqa: BLE001
            return self._error("服务端异常: %s" % exc, 500)

    # ---------------------------------------------------------- POST
    def do_POST(self) -> None:     # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/upload":
                name = unquote(self.headers.get("X-File-Name") or "")
                total = int(self.headers.get("Content-Length") or 0)
                info = self.console.save_upload(name, self.rfile, total)
                self.console.publish_state()
                return self._json({"ok": True, **info})

            data = self._body()
            c = self.console

            if path == "/api/cmd":
                # 命令交互: 在已连接的目标主机上执行命令并回传输出
                if not c.connected:
                    return self._error("尚未连接目标主机", 409)
                cmd = (data.get("cmd") or "").strip()
                if not cmd:
                    return self._error("命令不能为空")
                if len(cmd) > 2000:
                    return self._error("命令过长(>2000字符)")
                try:
                    timeout = min(float(data.get("timeout") or 60.0), 300.0)
                except (TypeError, ValueError):
                    timeout = 60.0
                c.log("$ %s" % cmd, "step")
                try:
                    res = c.ssh.exec(cmd, timeout=timeout)
                except Exception as exc:      # noqa: BLE001
                    c.log("命令执行失败: %s" % exc, "err")
                    return self._json({"ok": False, "error": "命令执行失败: %s" % exc})
                return self._json({
                    "ok": True, "cmd": cmd,
                    "stdout": res.stdout, "stderr": res.stderr,
                    "exit": res.exit_status, "duration": round(res.duration, 3),
                })

            if path == "/api/connect":
                c.save_cfg({
                    "host": data.get("host", "").strip(),
                    "port": str(data.get("port", "22")).strip(),
                    "user": data.get("user", "root").strip(),
                    "key": data.get("key", "").strip(),
                    "workdir": data.get("workdir", "").strip(),
                    "password": data.get("password", ""),
                    "remember": bool(data.get("remember")),
                })
                c.connect(data.get("host", "").strip(), data.get("port", 22),
                          data.get("user", "root").strip(), data.get("password", ""),
                          data.get("key", "").strip())
                return self._json({"ok": True, "started": True})

            if path == "/api/disconnect":
                c.disconnect()
                return self._json({"ok": True})

            if path == "/api/config":
                c.save_cfg(data)
                c.publish_state()
                return self._json({"ok": True})

            if path in ("/api/probe", "/api/deploy", "/api/uninstall",
                        "/api/verify", "/api/reboot"):
                fn = {"/api/probe": c.probe, "/api/deploy": c.deploy,
                      "/api/uninstall": c.uninstall, "/api/verify": c.verify,
                      "/api/reboot": c.reboot}[path]
                c.save_cfg({k: data[k] for k in
                            ("models", "install_deps", "update_source", "blacklist",
                             "reboot", "workdir", "installer") if k in data})
                fn(data)
                return self._json({"ok": True, "started": True})

            if path == "/api/agent/deploy":
                c.agent_deploy()
                return self._json({"ok": True, "started": True})
            if path == "/api/agent/remove":
                c.agent_remove()
                return self._json({"ok": True, "started": True})

            if path == "/api/io/start":
                c.io_start(data)
                return self._json({"ok": True, "started": True})
            if path == "/api/io/stop":
                c.io_stop()
                return self._json({"ok": True})

            if path == "/api/di/read":
                return self._json({"ok": True, "ports": c.read_di()})
            if path == "/api/do/read":
                return self._json({"ok": True, "ports": c.read_do()})

            if path == "/api/do/bit":
                return self._json(c.do_bit(data))
            if path == "/api/do/all":
                return self._json(c.do_all(0xFF if data.get("value") else 0x00))
            if path == "/api/do/invert":
                return self._json(c.do_invert())
            if path == "/api/do/bytes":
                return self._json(c.do_bytes(data))

            if path == "/api/watch/start":
                return self._json(c.watch_start(data))
            if path == "/api/watch/stop":
                return self._json(c.watch_stop())

            if path == "/api/selftest":
                c.selftest(data)
                return self._json({"ok": True, "started": True})

            if path == "/api/annotations":
                return self._json(c.set_annotation(data.get("key", ""),
                                                   data.get("name", "")))

            return self._error("未知接口: %s" % path, 404)
        except ApiError as exc:
            return self._error(str(exc), exc.status)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:      # noqa: BLE001
            return self._error("服务端异常: %s" % exc, 500)

    # ---------------------------------------------------------- 静态资源
    def _static(self, name: str, ctype: str) -> None:
        path = os.path.join(WEB_DIR, name)
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self._error("资源缺失: %s" % name, 404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _report(self) -> None:
        body = self.console.report_text().encode("utf-8")
        fname = "IO测报告_%s.txt" % time.strftime("%Y%m%d_%H%M%S")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition",
                         "attachment; filename*=UTF-8''%s" % _url_quote(fname))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # ---------------------------------------------------------- SSE
    def _sse(self) -> None:
        q = self.console.hub.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.write(_sse_pack({"t": "state", "state": self.console.snapshot()}))
            self.wfile.flush()
            last_ping = time.monotonic()
            while True:
                try:
                    payload = q.get(timeout=2.0)
                    self.wfile.write(b"data: " + payload.encode("utf-8") + b"\n\n")
                    self.wfile.flush()
                except queue.Empty:
                    if time.monotonic() - last_ping > 20:
                        last_ping = time.monotonic()
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.console.hub.unsubscribe(q)


def _sse_pack(obj: dict) -> bytes:
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n"


def _url_quote(text: str) -> str:
    from urllib.parse import quote
    return quote(text, safe="")


# =============================================================== 启动
def local_ips() -> List[str]:
    out: List[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in out and not ip.startswith("127."):
                out.append(ip)
    except Exception:      # noqa: BLE001
        pass
    # 兜底: 主动探测默认路由使用的地址
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.4)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        if ip not in out and not ip.startswith("127."):
            out.insert(0, ip)
        s.close()
    except Exception:      # noqa: BLE001
        pass
    return out


def serve(data_dir: str, web_dir: str, host: str = "127.0.0.1", port: int = 8080):
    """嵌入式入口(APK 内运行): 启动服务并返回 httpd, 由宿主管理线程。

    桥接服务跑在手机本地, WebView 直接加载 http://127.0.0.1:port/,
    用户只需填写目标主机 SSH 信息即可连接检测 —— 与桌面版体验一致。
    """
    set_web_dir(web_dir)
    console = Console(mode="ssh", data_dir=data_dir)
    Handler.console = console
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    console.log("控制台服务已启动 (本地端口 %d)" % port, "info")
    return httpd


def main() -> int:
    ap = argparse.ArgumentParser(description="研华 I/O 卡手机控制台 桥接服务")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址(默认 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8080, help="监听端口(默认 8080)")
    ap.add_argument("--local", action="store_true",
                    help="本机模式: 直接运行在带 I/O 卡的目标 Linux 上(不走 SSH)")
    ap.add_argument("--data-dir", default=HERE, help="配置与上传文件存放目录")
    ap.add_argument("--version", action="store_true", help="打印版本后退出")
    args = ap.parse_args()

    if args.version:
        print("%s v%s · 设计开发人：陈宠" % (APP_TITLE, __version__))
        return 0

    console = Console(mode="local" if args.local else "ssh", data_dir=args.data_dir)
    Handler.console = console

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True

    mode_txt = "本机模式 (直接操作本机 I/O 卡)" if args.local else "远程模式 (SSH 连接目标机)"
    print("=" * 62)
    print("  %s  v%s · 设计开发人：陈宠" % (APP_TITLE, __version__))
    print("  运行方式 : %s" % mode_txt)
    print("  监听     : %s:%d" % (args.host, args.port))
    ips = local_ips()
    if ips:
        print("  手机访问 :")
        for ip in ips:
            print("             http://%s:%d" % (ip, args.port))
    else:
        print("  手机访问 : http://<本机IP>:%d" % args.port)
    print("  说明     : 手机与电脑需在同一局域网; 浏览器菜单选【添加到主屏幕】即可当 App 用")
    print("  停止服务 : Ctrl+C")
    print("=" * 62, flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止", flush=True)
    finally:
        try:
            if console.io:
                console.io.stop()
            if console.ssh:
                console.ssh.close()
        except Exception:      # noqa: BLE001
            pass
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
