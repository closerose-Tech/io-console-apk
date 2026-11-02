"""PCI-1762 远程 IO 测试客户端。

工作流程:
    1. 把 bio1762_io.c 上传到目标机 <工作目录>/iotest/ 并编译;
    2. 以长驻进程方式运行 ./bio1762_io server;
    3. 通过 stdin/stdout 行协议下发 DI 读取 / DO 写入命令,
       并接收服务端主动推送的 DI 采样数据流。
"""

from __future__ import annotations

import hashlib
import os
import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from . import assets
from .ssh_session import ExecChannel, SSHSession, SSHError

LogFn = Callable[[str, str], None]

SDK_DIR = "/opt/advantech"


@dataclass
class DeviceInfo:
    desc: str = ""
    di_ports: int = 0
    do_ports: int = 0
    di_lines: int = 0
    do_lines: int = 0
    di_ok: bool = False
    do_ok: bool = False


class IOSession:
    """与远端 bio1762_io server 进程的一次会话。"""

    def __init__(self, ssh: SSHSession, log: LogFn, remote_workdir: str):
        self.ssh = ssh
        self.log = log
        self.workdir = remote_workdir.rstrip("/")
        self.remote_dir = "%s/iotest" % self.workdir
        self.channel: Optional[ExecChannel] = None

        self._reader: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._resp_q: "queue.Queue[str]" = queue.Queue()
        self._req_lock = threading.Lock()

        self._di_cb: Optional[Callable[[List[int]], None]] = None
        self._err_cb: Optional[Callable[[str], None]] = None
        self._watching = False
        self._watch_args: tuple = (200, 0, None)
        self.device = DeviceInfo()
        self.last_error = ""

    # ============================================================== 部署二进制
    def binary_exists(self) -> bool:
        return self.ssh.exec(
            "test -x %s/bio1762_io && echo yes || echo no" % self.remote_dir,
            timeout=20).stdout.strip() == "yes"

    def _upload_source(self) -> None:
        c_src = assets.read_text(assets.remote_c_source())
        mf_src = assets.read_text(assets.remote_makefile())
        self.ssh.sftp_write_text(c_src, "%s/bio1762_io.c" % self.remote_dir)
        self.ssh.sftp_write_text(mf_src, "%s/Makefile" % self.remote_dir)
        self.log("已上传 bio1762_io.c / Makefile", "info")

    def _remote_source_md5(self) -> str:
        return self.ssh.exec(
            "md5sum %s/bio1762_io.c 2>/dev/null | awk '{print $1}'" % self.remote_dir,
            timeout=20).stdout.strip()

    def build(self, force: bool = False) -> bool:
        """上传并编译远端代理程序。"""
        self.log("准备远端测试程序 ...", "step")
        self.ssh.exec("mkdir -p %s" % self.remote_dir, timeout=20)

        local_md5 = hashlib.md5(
            assets.read_text(assets.remote_c_source()).encode("utf-8")).hexdigest()
        remote_md5 = self._remote_source_md5()
        need_upload = force or remote_md5 != local_md5
        has_bin = self.binary_exists()

        if need_upload:
            self._upload_source()
        if not need_upload and has_bin:
            self.log("远端程序已是最新, 跳过编译", "ok")
            return True

        cmd = ("cd %s && gcc -O2 -Wall -I%s/inc -o bio1762_io bio1762_io.c "
               "-L%s/libs -Wl,-rpath,%s/libs -lbiodaq"
               % (self.remote_dir, SDK_DIR, SDK_DIR, SDK_DIR))
        self.log("$ %s" % cmd, "cmd")
        res = self.ssh.exec(cmd, timeout=300)
        if not res.ok:
            # 退路: 直接链接 .so 绝对路径
            alt = ("cd %s && gcc -O2 -Wall -I%s/inc -o bio1762_io bio1762_io.c "
                   "%s/libs/libbiodaq.so" % (self.remote_dir, SDK_DIR, SDK_DIR))
            self.log("首次编译失败, 尝试直接链接动态库 ...", "warn")
            self.log("$ %s" % alt, "cmd")
            res = self.ssh.exec(alt, timeout=300)

        for line in res.text.splitlines():
            self.log("  " + line, "out" if res.ok else "err")

        if not res.ok or not self.binary_exists():
            self.log("远端测试程序编译失败, 请确认 DAQNavi SDK 已正确安装 "
                     "(%s/inc/bdaqctrl.h, %s/libs/libbiodaq.so)" % (SDK_DIR, SDK_DIR), "err")
            return False
        self.log("远端测试程序编译成功", "ok")
        return True

    # ============================================================== 会话
    @property
    def running(self) -> bool:
        return bool(self.channel and not self.channel.closed)

    def start(self, device_token: str = "") -> bool:
        """启动远端 server 进程并进入行协议交互。"""
        if self.running:
            return True
        if not self.binary_exists():
            self.log("远端程序不存在, 请先编译", "err")
            return False

        cmd = "cd %s && ./bio1762_io server" % self.remote_dir
        if device_token:
            cmd += " %s" % _sh_quote(device_token)
        self.log("启动远端 IO 代理: %s" % cmd, "cmd")

        self.channel = self.ssh.open_exec_channel(cmd)
        self._stop_evt.clear()
        self._drain()

        first = self.channel.read_line(timeout=10)
        if not first:
            self.log("远端代理未响应, 启动失败", "err")
            self.stop()
            return False
        if first.startswith("READY"):
            desc = first[6:].strip()
            self.log("远端代理就绪, 当前设备: %s" % (desc or "(未识别到设备)"),
                     "ok" if desc else "warn")
        else:
            self.log("远端代理返回: %s" % first, "warn")

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        return True

    def stop(self) -> None:
        self._stop_evt.set()
        if self.channel:
            try:
                self.channel.send_line("QUIT")
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.1)
            self.channel.close()
            self.channel = None
        self._watching = False

    def _drain(self) -> None:
        while True:
            try:
                self._resp_q.get_nowait()
            except queue.Empty:
                break

    # ---------------------------------------------------------- 读取线程
    def _read_loop(self) -> None:
        while not self._stop_evt.is_set():
            ch = self.channel
            if ch is None:
                break
            try:
                line = ch.read_line(timeout=0.5)
            except SSHError as exc:
                if not self._stop_evt.is_set():
                    self._emit_err("读取远端数据失败: %s" % exc)
                break
            if line is None:
                continue
            if line == "":
                break
            if line.startswith("DI "):
                vals = _parse_hex_list(line[3:])
                if vals and self._di_cb:
                    self._di_cb(vals)
                continue
            if line == "BYE":
                break
            if line.startswith("ERR"):
                self._emit_err(line)
            self._resp_q.put(line)

    def _emit_err(self, msg: str) -> None:
        self.last_error = msg
        if self._err_cb:
            self._err_cb(msg)
        else:
            self.log(msg, "err")

    # ---------------------------------------------------------- 配置回调
    def set_di_callback(self, cb: Optional[Callable[[List[int]], None]]) -> None:
        self._di_cb = cb

    def set_error_callback(self, cb: Optional[Callable[[str], None]]) -> None:
        self._err_cb = cb

    # ---------------------------------------------------------- 请求/应答
    def request(self, command: str, timeout: float = 15.0) -> str:
        """发送一条命令并等待应答(串行)。"""
        if not self.running:
            raise SSHError("远端代理未运行")
        with self._req_lock:
            self._drain()
            self.channel.send_line(command)
            try:
                return self._resp_q.get(timeout=timeout)
            except queue.Empty:
                raise SSHError("等待远端应答超时: %s" % command)

    def _request_ok(self, command: str, timeout: float = 15.0) -> Optional[str]:
        resp = self.request(command, timeout=timeout)
        if resp.startswith("OK"):
            return resp
        if resp.startswith("ERR"):
            self._emit_err("命令 %s 失败: %s" % (command.split()[0], _err_text(resp)))
        return None

    # ---------------------------------------------------------- 设备信息
    def list_devices(self) -> List[str]:
        resp = self._request_ok("LIST")
        if not resp:
            return []
        body = resp[4:].strip()
        out = []
        for item in body.split(";"):
            item = item.strip()
            if not item or item == "(none)":
                continue
            if "|" in item:
                out.append(item.split("|", 1)[1])
        return out

    def select(self, token: str) -> bool:
        """切换设备。

        注意: 远端代理在切换设备且进程内重新打开失败时会 execv 自替换为新进程
        (bio1761 驱动的权限释放缺陷), 自替换后 DI 监视状态会重置, 故此处自动重挂。
        """
        was_watching = self._watching
        w_args = self._watch_args
        self._watching = False
        ok = self._request_ok("SELECT %s" % token) is not None
        if ok and was_watching:
            self.start_watch(*w_args)
        return ok

    def fetch_info(self) -> Optional[DeviceInfo]:
        resp = self._request_ok("INFO")
        if not resp:
            return None
        info = DeviceInfo()
        m = re.search(r"desc=(.*?)\sdi_ports=", resp)
        if m:
            info.desc = m.group(1)
        for key, attr in (("di_ports", "di_ports"), ("do_ports", "do_ports"),
                          ("di_lines", "di_lines"), ("do_lines", "do_lines"),
                          ("di_ok", "di_ok"), ("do_ok", "do_ok")):
            m = re.search(r"%s=(\d+)" % key, resp)
            if m:
                val = int(m.group(1))
                setattr(info, attr, bool(val) if attr.endswith("_ok") else val)
        self.device = info
        return info

    # ---------------------------------------------------------- DI / DO
    def read_di(self, port: int = 0, count: Optional[int] = None) -> Optional[List[int]]:
        cmd = "DI %d" % port if count is None else "DI %d %d" % (port, count)
        resp = self._request_ok(cmd)
        if not resp:
            return None
        body = resp[5:].strip() if resp.startswith("OK DI") else ""
        return _parse_hex_list(body)

    def read_do(self, port: int = 0, count: Optional[int] = None) -> Optional[List[int]]:
        cmd = "DO %d" % port if count is None else "DO %d %d" % (port, count)
        resp = self._request_ok(cmd)
        if not resp:
            return None
        body = resp[5:].strip() if resp.startswith("OK DO") else ""
        return _parse_hex_list(body)

    def write_do(self, port: int, values: List[int]) -> bool:
        vals = " ".join("0x%02X" % (v & 0xFF) for v in values)
        return self._request_ok("SETDO %d %s" % (port, vals)) is not None

    def write_bit(self, port: int, bit: int, value: int) -> bool:
        return self._request_ok("SETBIT %d %d %d" % (port, bit, 1 if value else 0)) is not None

    def read_bit(self, port: int, bit: int) -> Optional[int]:
        resp = self._request_ok("GETBIT %d %d" % (port, bit))
        if not resp:
            return None
        m = re.search(r"OK BIT ([01])", resp)
        return int(m.group(1)) if m else None

    # ---------------------------------------------------------- 连续采样
    def start_watch(self, interval_ms: int = 200, port: int = 0,
                    count: Optional[int] = None) -> bool:
        if count is None:
            count = self.device.di_ports or 2
        ok = self._request_ok("WATCH %d %d %d" % (interval_ms, port, count)) is not None
        self._watching = ok
        if ok:
            self._watch_args = (interval_ms, port, count)
        return ok

    def stop_watch(self) -> bool:
        if not self._watching:
            return True
        ok = self._request_ok("UNWATCH") is not None
        self._watching = False
        return ok

    # ============================================================== 自检
    def run_selftest(self, per_bit: bool = True,
                     progress: Optional[Callable[[str], None]] = None) -> Tuple[bool, List[str]]:
        """在远端代理在线的前提下, 执行 DO 回读自检 + DI 读取。

        返回 (是否全部通过, 报告行列表)。
        """
        lines: List[str] = []

        def rec(text: str) -> None:
            lines.append(text)
            if progress:
                progress(text)

        info = self.fetch_info()
        if info is None:
            rec("[失败] 无法获取设备信息")
            return False, lines

        rec("设备描述      : %s" % (info.desc or "(未知)"))
        rec("DI 端口/通道  : %d / %d" % (info.di_ports, info.di_lines))
        rec("DO 端口/通道  : %d / %d" % (info.do_ports, info.do_lines))
        rec("")

        all_ok = True

        # ---- DO 端口级回读 ----
        if info.do_ports <= 0:
            rec("[跳过] 远端 DO 不可用")
        else:
            for p in range(info.do_ports):
                for pattern, name in ((0x00, "全关 0x00"), (0xFF, "全开 0xFF")):
                    if not self.write_do(p, [pattern]):
                        rec("[失败] DO 端口 %d 写入 %s 指令失败" % (p, name))
                        all_ok = False
                        continue
                    rb = self.read_do(p, 1)
                    got = rb[0] if rb else None
                    if got is None:
                        rec("[失败] DO 端口 %d 回读失败" % p)
                        all_ok = False
                        continue
                    same = (got == pattern)
                    if not same:
                        all_ok = False
                    rec("[%s] DO 端口 %d 写入 0x%02X -> 回读 0x%02X"
                        % ("通过" if same else "差异", p, pattern, got))
                # 每个端口测完复位
                self.write_do(p, [0x00])

        # ---- DI 读取 ----
        if info.di_ports <= 0:
            rec("[跳过] 远端 DI 不可用")
        else:
            rb = self.read_di(0, info.di_ports)
            if rb is None:
                rec("[失败] DI 读取失败")
                all_ok = False
            else:
                rec("DI 当前状态   : %s" % " ".join("0x%02X" % v for v in rb))

        # ---- 逐位测试 ----
        if per_bit and info.do_ports > 0:
            rec("")
            rec("---- 逐位(继电器)测试 ----")
            for p in range(info.do_ports):
                for b in range(8):
                    if not self.write_bit(p, b, 1):
                        rec("[失败] DO 端口 %d 位 %d 置 1 失败" % (p, b))
                        all_ok = False
                        continue
                    rb = self.read_do(p, 1)
                    got1 = rb[0] if rb else None
                    expect1 = 1 << b
                    ok1 = (got1 == expect1)
                    if not ok1:
                        all_ok = False
                    if not self.write_bit(p, b, 0):
                        rec("[失败] DO 端口 %d 位 %d 置 0 失败" % (p, b))
                        all_ok = False
                        continue
                    rb = self.read_do(p, 1)
                    got0 = rb[0] if rb else None
                    ok0 = (got0 == 0x00)
                    if not ok0:
                        all_ok = False
                    rec("[%s] DO 端口 %d 位 %d : 置1回读 0x%02X(期望0x%02X) / 置0回读 0x%02X(期望0x00)"
                        % ("通过" if (ok1 and ok0) else "差异", p, b,
                           got1 if got1 is not None else 0xFF, expect1,
                           got0 if got0 is not None else 0xFF))
            rec("")
            rec("提示: 若回读与写入不一致, 说明该继电器可能为机械触点(需带负载)或通道损坏")

        # 收尾: 全部输出复位
        if info.do_ports > 0:
            self.write_do(0, [0x00] * max(1, info.do_ports))

        rec("")
        rec("==== 自检结果: %s ====" % ("全部通过" if all_ok else "存在不一致项"))
        return all_ok, lines


# ------------------------------------------------------------------ 工具
def _parse_hex_list(text: str) -> List[int]:
    vals: List[int] = []
    for tok in text.split():
        tok = tok.strip()
        if not tok:
            continue
        try:
            vals.append(int(tok, 16))
        except ValueError:
            pass
    return vals


def _err_text(resp: str) -> str:
    parts = resp.split(None, 2)
    if len(parts) >= 3:
        return parts[2]
    return resp


def _sh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"
