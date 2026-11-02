"""本机 Shell 适配层。

手机控制台有两种运行方式:

  * 远程模式  —— 桥接服务跑在 PC/服务器上, 通过 SSH 操作目标 Linux(用 SSHSession);
  * 本机模式  —— 桥接服务直接跑在目标 Linux(带 I/O 卡的那台机器)上, 不需要 SSH。

Deployer 与 IOSession 只依赖一组固定的方法(exec / sftp_* / open_exec_channel /
target), 因此这里用 subprocess 实现同样的接口, 就能让同一套部署与测试逻辑
在本机模式下原样复用, 避免出现两份会逐渐走偏的代码。
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from daq_remote.ssh_session import CommandResult, SSHError

_SHELL = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"


@dataclass
class _LocalTarget:
    """占位用: Deployer 只读取这几个字段用于日志展示。"""

    host: str = "localhost"
    port: int = 0
    username: str = "root"
    password: str = ""

    @property
    def is_root(self) -> bool:
        try:
            return os.geteuid() == 0
        except AttributeError:      # Windows 没有 geteuid
            return False


class LocalChannel:
    """长驻命令通道: 对应 SSHSession.open_exec_channel 的本机实现。

    用独立线程把子进程 stdout 逐行推入队列, 从而实现带超时的 read_line,
    这样 IOSession 的行协议逻辑不需要任何改动。
    """

    def __init__(self, command: str):
        self.command = command
        self._buf: List[str] = []
        self._q: "queue.Queue[Optional[str]]" = queue.Queue()
        self._closed = False
        try:
            self._proc = subprocess.Popen(
                [_SHELL, "-c", command],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,   # 远端代理的错误也走同一路输出
                bufsize=0,
                env=dict(os.environ, PYTHONUNBUFFERED="1"),
            )
        except OSError as exc:
            raise SSHError("启动本机进程失败: %s" % exc) from exc
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # ------------------------------------------------------------ 读取线程
    def _read_loop(self) -> None:
        proc = self._proc
        assert proc.stdout is not None
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                self._q.put(line)
        except Exception:      # noqa: BLE001  进程被杀时读到一半会抛异常
            pass
        finally:
            self._closed = True
            self._q.put(None)   # 哨兵: 表示通道结束

    # ------------------------------------------------------------ 接口
    @property
    def closed(self) -> bool:
        return self._closed or self._proc.poll() is not None

    def send_line(self, text: str) -> None:
        if self.closed:
            raise SSHError("通道已关闭")
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.write((text + "\n").encode("utf-8"))
            self._proc.stdin.flush()
        except OSError as exc:
            raise SSHError("写入通道失败: %s" % exc) from exc

    def read_line(self, timeout: float = 5.0) -> Optional[str]:
        """读取一行; 超时返回 None; 通道结束返回 ''。"""
        if self._buf:
            return self._buf.pop(0)
        try:
            item = self._q.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None
        if item is None:
            self._closed = True
            return ""
        return item

    def close(self) -> None:
        self._closed = True
        if self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:      # noqa: BLE001
                try:
                    self._proc.kill()
                except Exception:  # noqa: BLE001
                    pass


class LocalShell:
    """以"本机就是目标机"的方式提供 SSHSession 的全部能力。"""

    def __init__(self, log: Optional[Callable[[str, str], None]] = None):
        self.log = log or (lambda _t, _l="info": None)
        self.target = _LocalTarget()
        self.shell_noise = ""
        self._closed = False

    # ------------------------------------------------------------ 连接
    @property
    def connected(self) -> bool:
        return not self._closed

    def connect(self) -> str:
        self._closed = False
        try:
            host = os.uname().nodename       # type: ignore[attr-defined]
        except AttributeError:
            host = os.environ.get("COMPUTERNAME", "localhost")
        return "local:%s" % host

    def close(self) -> None:
        self._closed = True

    # ------------------------------------------------------------ 命令执行
    def exec(
        self,
        command: str,
        timeout: float = 120.0,
        sudo: bool = False,
        on_line: Optional[Callable[[str], None]] = None,
        stdin_data: str = "",
    ) -> CommandResult:
        real = command
        # 已经是 root 就不必再套 sudo(避免有的精简系统没装 sudo 反而失败)
        if sudo and not self.target.is_root:
            real = "sudo -S -p '' %s -c %s" % (_SHELL, _sh_quote(command))

        start = time.monotonic()
        try:
            proc = subprocess.Popen(
                [_SHELL, "-c", real],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(os.environ),
            )
        except OSError as exc:
            raise SSHError("执行本机命令失败: %s" % exc) from exc

        out_buf = bytearray()
        err_buf = bytearray()

        def reader(stream, buf: bytearray, emit: bool) -> None:
            try:
                for raw in iter(stream.readline, b""):
                    buf.extend(raw)
                    if emit and on_line is not None:
                        on_line(raw.decode("utf-8", "replace").rstrip("\r\n"))
            except Exception:      # noqa: BLE001
                pass

        t_out = threading.Thread(target=reader, args=(proc.stdout, out_buf, True), daemon=True)
        t_err = threading.Thread(target=reader, args=(proc.stderr, err_buf, True), daemon=True)
        t_out.start()
        t_err.start()

        try:
            if stdin_data and proc.stdin is not None:
                proc.stdin.write(stdin_data.encode("utf-8"))
            if proc.stdin is not None:
                proc.stdin.close()
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:      # noqa: BLE001
                pass
            raise SSHError("命令执行超时(%.0fs): %s" % (timeout, command.splitlines()[0][:120]))
        finally:
            t_out.join(timeout=5)
            t_err.join(timeout=5)

        return CommandResult(
            command=command,
            exit_status=proc.returncode,
            stdout=out_buf.decode("utf-8", "replace"),
            stderr=err_buf.decode("utf-8", "replace"),
            duration=time.monotonic() - start,
        )

    def open_exec_channel(self, command: str, get_pty: bool = False) -> LocalChannel:
        return LocalChannel(command)

    # ------------------------------------------------------------ 文件
    def sftp_stat_size(self, remote_path: str) -> int:
        try:
            return os.path.getsize(remote_path)
        except OSError:
            return -1

    def sftp_exists(self, remote_path: str) -> bool:
        return self.sftp_stat_size(remote_path) >= 0

    def sftp_mkdirs(self, remote_dir: str) -> None:
        try:
            os.makedirs(remote_dir, exist_ok=True)
        except OSError:
            pass

    def sftp_put(self, local_path: str, remote_path: str,
                 progress: Optional[Callable[[int, int], None]] = None,
                 chunk: int = 262144) -> None:
        """同盘直接拷贝; 跨盘分块拷贝并回报进度(与 SSH 上传体验一致)。"""
        total = os.path.getsize(local_path)
        parent = os.path.dirname(os.path.abspath(remote_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            if os.path.exists(remote_path):
                os.remove(remote_path)
            done = 0
            with open(local_path, "rb") as src, open(remote_path, "wb") as dst:
                while True:
                    data = src.read(chunk)
                    if not data:
                        break
                    dst.write(data)
                    done += len(data)
                    if progress is not None:
                        progress(done, total)
            shutil.copystat(local_path, remote_path)
        except OSError as exc:
            raise SSHError("拷贝安装包失败: %s" % exc) from exc

    def sftp_write_text(self, text: str, remote_path: str) -> None:
        parent = os.path.dirname(os.path.abspath(remote_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(remote_path, "wb") as f:
            f.write(text.encode("utf-8"))

    def sftp_read_text(self, remote_path: str) -> str:
        with open(remote_path, "rb") as f:
            return f.read().decode("utf-8", "replace")

    def sftp_get(self, remote_path: str, local_path: str) -> None:
        shutil.copy2(remote_path, local_path)

    def sftp_chmod(self, remote_path: str, mode: int) -> None:
        try:
            os.chmod(remote_path, mode)
        except OSError:
            pass


def _sh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"
