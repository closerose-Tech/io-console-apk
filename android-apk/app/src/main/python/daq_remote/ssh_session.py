"""SSH / SFTP 会话封装(基于 paramiko)。

对外提供:
  * SSHTarget      —— 连接参数
  * SSHSession     —— 连接、执行命令(支持 sudo)、流式输出、上传下载
  * ExecChannel    —— 长驻命令通道(用于与远端 IO 代理程序做行协议交互)
"""

from __future__ import annotations

import os
import select
import socket
import stat as stat_mod
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

import paramiko


class SSHError(Exception):
    """SSH 相关异常。"""


@dataclass
class SSHTarget:
    host: str = ""
    port: int = 22
    username: str = "root"
    password: str = ""
    key_path: str = ""
    key_passphrase: str = ""
    timeout: int = 15

    @property
    def is_root(self) -> bool:
        return self.username.strip() == "root"


@dataclass
class CommandResult:
    command: str
    exit_status: int
    stdout: str
    stderr: str
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.exit_status == 0

    @property
    def text(self) -> str:
        parts = []
        if self.stdout.strip():
            parts.append(self.stdout.strip())
        if self.stderr.strip():
            parts.append(self.stderr.strip())
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# 远程 shell "噪声前缀" 处理
#
# 现场很多 Linux(如 openEuler / RHEL / CentOS)的 sshd 在执行远程命令前会先
# 加载用户的 ~/.bashrc。若其中含 `xhost +`、`echo ...`、登录横幅之类的内容,
# 每条命令的 stdout 都会被塞入一行无关文本, 从而破坏:
#     * `test -d xxx && echo yes` 这类精确字符串比对
#     * `echo $HOME` 这类单值读取
#     * IO 长连接进程的 READY 握手
#
# 解决办法: 连接建立后先探测是否存在这类前缀噪声; 若存在, 则给命令套上
# 一对唯一标记, 只取标记之间的内容作为真正的输出。
# ---------------------------------------------------------------------------
MARK_BEGIN = "__BIO_OUT_B_7C31__"
MARK_END = "__BIO_OUT_E_7C31__"
_PROBE_TOKEN = "__BIO_PROBE_7C31__"
_MARK_BYTES_BEGIN = MARK_BEGIN.encode()


def _marker_wrap(command: str) -> str:
    """把命令包在标记之间, 并透传退出码。"""
    return ("printf '\\n%s\\n'; ( %s\n) ; __bio_rc=$? ; printf '\\n%s\\n' ; exit $__bio_rc"
            % (MARK_BEGIN, command, MARK_END))


def _extract_marked(text: str) -> Optional[str]:
    """取出标记之间的内容; 未找到标记返回 None(调用方回退为原始输出)。"""
    i = text.find(MARK_BEGIN)
    if i < 0:
        return None
    i += len(MARK_BEGIN)
    j = text.find(MARK_END, i)
    if j < 0:
        return None
    return text[i:j].strip("\n")


class _StreamFilter:
    """流式回调用的标记过滤器: 丢弃标记之前的内容, 只透出有效输出行。"""

    def __init__(self, emit: Callable[[str], None]):
        self._emit = emit
        self._buf = ""
        self._inside = False
        self._ended = False

    def feed(self, text: str) -> None:
        if self._ended or not text:
            return
        self._buf += text
        if not self._inside:
            k = self._buf.find(MARK_BEGIN)
            if k < 0:
                # 保留可能是标记前缀的尾部
                keep = len(MARK_BEGIN) - 1
                if len(self._buf) > keep:
                    self._buf = self._buf[-keep:]
                return
            self._buf = self._buf[k + len(MARK_BEGIN):]
            if self._buf.startswith("\n"):
                self._buf = self._buf[1:]
            self._inside = True
        while True:
            idx = self._buf.find("\n")
            if idx < 0:
                return
            head, self._buf = self._buf[:idx], self._buf[idx + 1:]
            if MARK_END in head:
                self._ended = True
                self._buf = ""
                return
            if not head and self._buf.startswith(MARK_END):
                continue          # 结束标记前的多余空行
            self._emit(head)

    def finish(self) -> None:
        if self._inside and not self._ended and self._buf:
            self._emit(self._buf)
        self._buf = ""


# paramiko 默认禁用了一批老旧算法; 部分现场 Linux(如 CentOS 7/老 OpenSSH)需要放开。
_LEGACY_OPTS = {
    "kex": [
        "diffie-hellman-group14-sha1",
        "diffie-hellman-group1-sha1",
        "diffie-hellman-group-exchange-sha1",
    ],
    "key_types": [
        "ssh-rsa",
        "ssh-dss",
    ],
    "ciphers": [
        "aes128-cbc",
        "3des-cbc",
    ],
}


def _apply_legacy_algorithms():
    """在 paramiko 全局安全选项中放开老旧算法(仅在需要时调用)。"""
    try:
        sec = paramiko.Transport._preferred_kex
        if sec is None:
            return
    except Exception:
        pass
    try:
        paramiko.Transport._preferred_kex = tuple(_LEGACY_OPTS["kex"]) + tuple(
            paramiko.Transport._preferred_kex
        )
        paramiko.Transport._preferred_keys = tuple(_LEGACY_OPTS["key_types"]) + tuple(
            paramiko.Transport._preferred_keys
        )
        paramiko.Transport._preferred_ciphers = tuple(_LEGACY_OPTS["ciphers"]) + tuple(
            paramiko.Transport._preferred_ciphers
        )
    except Exception:
        pass


class ExecChannel:
    """一个长驻的远端命令通道, 支持逐行收发。"""

    def __init__(self, chan: paramiko.Channel, command: str, skip_marker: bool = False):
        self._chan = chan
        self.command = command
        self._buf = b""
        self._closed = False
        self._lock = threading.Lock()
        # 目标机 ~/.bashrc 有噪声时, 需丢弃标记之前的前导输出
        self._skip_marker = skip_marker
        self._preamble_done = not skip_marker

    # ---------------------------------------------------------------- 基础
    @property
    def closed(self) -> bool:
        return self._closed or self._chan.closed

    def send_line(self, text: str) -> None:
        if self.closed:
            raise SSHError("通道已关闭")
        with self._lock:
            self._chan.sendall((text + "\n").encode("utf-8", "replace"))

    def read_line(self, timeout: float = 5.0) -> Optional[str]:
        """读取一行(不含换行符); 超时返回 None; 通道结束返回 ''。"""
        deadline = time.monotonic() + timeout
        while True:
            idx = self._buf.find(b"\n")
            if idx >= 0:
                line = self._buf[:idx]
                self._buf = self._buf[idx + 1:]
                text = line.decode("utf-8", "replace").rstrip("\r")
                if not self._preamble_done:
                    if MARK_BEGIN in text:
                        self._preamble_done = True
                    continue
                return text
            if self.closed and not self._buf:
                return ""

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None

            rl, _, _ = select.select([self._chan], [], [], min(remaining, 0.5))
            if not rl:
                continue
            try:
                data = self._chan.recv(4096)
            except Exception as exc:  # noqa: BLE001
                self._closed = True
                if self._buf:
                    line = self._buf
                    self._buf = b""
                    return line.decode("utf-8", "replace").rstrip("\r")
                raise SSHError(f"读取通道数据失败: {exc}") from exc
            if not data:
                self._closed = True
                continue
            self._buf += data

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._chan.close()
            except Exception:  # noqa: BLE001
                pass


class SSHSession:
    """一次到目标 Linux 主机的 SSH 会话。"""

    def __init__(self, target: SSHTarget):
        self.target = target
        self._client: Optional[paramiko.SSHClient] = None
        self._sftp: Optional[paramiko.SFTPClient] = None
        self._lock = threading.Lock()
        self._legacy_used = False
        # 远程 shell 噪声前缀(如 ~/.bashrc 里的 xhost +)处理开关
        self._use_markers = False
        self.shell_noise = ""

    # ------------------------------------------------------------ 连接管理
    @property
    def connected(self) -> bool:
        if self._client is None:
            return False
        tr = self._client.get_transport()
        return bool(tr and tr.is_active())

    def connect(self) -> str:
        """建立连接, 返回远端 banner 信息。失败抛出 SSHError。"""
        t = self.target
        if not t.host:
            raise SSHError("未填写目标主机地址")
        if not t.username:
            raise SSHError("未填写登录用户名")

        last_err: Optional[Exception] = None
        for legacy in (False, True):
            if legacy and self._legacy_used:
                continue
            if legacy:
                _apply_legacy_algorithms()
            try:
                self._do_connect(legacy)
                self._legacy_used = legacy
                tr = self._client.get_transport()
                tr.set_keepalive(30)
                ver = ""
                try:
                    ver = tr.remote_version or ""
                except Exception:  # noqa: BLE001
                    pass
                self._detect_shell_noise()
                return ver
            except paramiko.AuthenticationException as exc:
                raise SSHError(
                    "认证失败: 用户名/密码或密钥不正确\n%s" % exc
                ) from exc
            except (paramiko.SSHException, socket.error, OSError) as exc:
                last_err = exc
                self.close()
                msg = str(exc)
                if not legacy and _looks_like_algorithm_error(msg):
                    continue
                break

        raise SSHError("连接 %s:%s 失败: %s" % (t.host, t.port, last_err))

    def _do_connect(self, legacy: bool) -> None:
        t = self.target
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        kwargs = dict(
            hostname=t.host,
            port=int(t.port),
            username=t.username,
            timeout=t.timeout,
            banner_timeout=t.timeout,
            auth_timeout=t.timeout,
            allow_agent=False,
        )
        if t.key_path:
            key_path = os.path.expanduser(t.key_path)
            if not os.path.isfile(key_path):
                raise SSHError("私钥文件不存在: %s" % key_path)
            kwargs["key_filename"] = key_path
            kwargs["look_for_keys"] = False
            if t.password:
                kwargs["passphrase"] = t.password
            if t.key_passphrase:
                kwargs["passphrase"] = t.key_passphrase
        else:
            kwargs["password"] = t.password
            kwargs["look_for_keys"] = False
        if legacy:
            kwargs["disabled_algorithms"] = {}
        else:
            # 现代 OpenSSH 建议禁用 ssh-rsa(SHA-1); 若服务器不支持新算法会自动回退
            kwargs["disabled_algorithms"] = {}
        client.connect(**kwargs)
        self._client = client

    def close(self) -> None:
        if self._sftp is not None:
            try:
                self._sftp.close()
            except Exception:  # noqa: BLE001
                pass
            self._sftp = None
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    # ---------------------------------------------------------- 噪声探测
    def _detect_shell_noise(self) -> bool:
        """探测远程 shell 是否会在每条命令前输出额外内容。

        存在则后续命令自动套用标记包裹, 只取标记之间的真实输出。
        """
        self._use_markers = False
        self.shell_noise = ""
        try:
            res = self._exec_once("echo %s" % _PROBE_TOKEN, 20.0, False, None, "")
        except Exception:  # noqa: BLE001
            return False
        if res.stdout.strip() == _PROBE_TOKEN:
            return False
        noise = res.stdout.replace(_PROBE_TOKEN, "").strip()
        self.shell_noise = noise
        self._use_markers = True
        return True

    # ------------------------------------------------------------ 命令执行
    def _wrap_sudo(self, command: str) -> str:
        if self.target.is_root:
            return command
        # -S 从 stdin 读密码, -p '' 关闭提示
        return "sudo -S -p '' bash -c %s" % _sh_quote(command)

    def exec(
        self,
        command: str,
        timeout: float = 120.0,
        sudo: bool = False,
        on_line: Optional[Callable[[str], None]] = None,
        stdin_data: str = "",
    ) -> CommandResult:
        """执行一条命令并等待结束。

        偶发的通道建立失败(SSHException)会自动重试一次; 若连接已断开则抛错。
        """
        last_exc: Optional[Exception] = None
        for attempt in range(2):
            try:
                return self._exec_once(command, timeout, sudo, on_line, stdin_data)
            except paramiko.SSHException as exc:
                last_exc = exc
                if not self.connected:
                    raise SSHError("SSH 连接已断开: %s" % exc) from exc
                if attempt == 0:
                    time.sleep(0.3)
                    continue
            except SSHError:
                raise
        raise SSHError("命令执行失败: %s" % last_exc)

    def _exec_once(
        self,
        command: str,
        timeout: float,
        sudo: bool,
        on_line: Optional[Callable[[str], None]],
        stdin_data: str,
    ) -> CommandResult:
        if not self.connected:
            raise SSHError("SSH 未连接")

        if self._use_markers:
            command = _marker_wrap(command)
        use_sudo = sudo and not self.target.is_root
        real_cmd = self._wrap_sudo(command) if use_sudo else command

        try:
            chan = self._client.get_transport().open_session(timeout=self.target.timeout)
            chan.settimeout(1.0)
            chan.exec_command(real_cmd)
        except paramiko.SSHException as exc:
            raise paramiko.SSHException("open/exec 通道失败: %s" % exc) from exc

        if use_sudo and self.target.password:
            chan.sendall((self.target.password + "\n").encode())
        if stdin_data:
            chan.sendall(stdin_data.encode())
        try:
            chan.shutdown_write()
        except Exception:  # noqa: BLE001
            pass

        start = time.monotonic()
        out_buf = bytearray()
        err_buf = bytearray()
        pending_out = b""
        pending_err = b""
        stream = _StreamFilter(on_line) if (on_line is not None and self._use_markers) else None

        while True:
            got = False
            if chan.recv_ready():
                data = chan.recv(65536)
                if data:
                    got = True
                    out_buf += data
                    if stream is not None:
                        stream.feed(data.decode("utf-8", "replace"))
                    elif on_line is not None:
                        pending_out = _emit_lines(pending_out + data, on_line)
            if chan.recv_stderr_ready():
                data = chan.recv_stderr(65536)
                if data:
                    got = True
                    err_buf += data
                    if on_line is not None:
                        pending_err = _emit_lines(pending_err + data, on_line)
            if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                break
            if not got:
                if time.monotonic() - start > timeout:
                    chan.close()
                    raise SSHError(
                        "命令执行超时(%.0fs): %s" % (timeout, command.splitlines()[0][:120])
                    )
                time.sleep(0.03)

        # 收尾: 清空残留
        while chan.recv_ready():
            out_buf += chan.recv(65536)
        while chan.recv_stderr_ready():
            err_buf += chan.recv_stderr(65536)

        status = chan.recv_exit_status()
        chan.close()

        stdout = out_buf.decode("utf-8", "replace")
        stderr = err_buf.decode("utf-8", "replace")
        if self._use_markers:
            payload = _extract_marked(stdout)
            if payload is not None:
                stdout = payload
        if stream is not None:
            stream.finish()
        if on_line is not None:
            _emit_lines(pending_out, on_line, final=True)
            _emit_lines(pending_err, on_line, final=True)

        return CommandResult(
            command=command,
            exit_status=status,
            stdout=stdout,
            stderr=stderr,
            duration=time.monotonic() - start,
        )

    def open_exec_channel(self, command: str, get_pty: bool = False) -> ExecChannel:
        """打开一个长驻通道(不等待命令结束)。

        若目标机 shell 存在噪声前缀, 则在命令前打印一个标记, 读取端会丢弃
        标记之前的所有前导输出, 保证首行就是进程自身的输出。
        """
        if not self.connected:
            raise SSHError("SSH 未连接")
        chan = self._client.get_transport().open_session(timeout=self.target.timeout)
        chan.settimeout(1.0)
        if get_pty:
            chan.get_pty()
        if self._use_markers:
            command = "printf '\\n%s\\n'; %s" % (MARK_BEGIN, command)
        chan.exec_command(command)
        return ExecChannel(chan, command, skip_marker=self._use_markers)

    # ------------------------------------------------------------ SFTP
    def _get_sftp(self) -> paramiko.SFTPClient:
        if not self.connected:
            raise SSHError("SSH 未连接")
        if self._sftp is None:
            tr = self._client.get_transport()
            self._sftp = paramiko.SFTPClient.from_transport(tr)
            self._sftp.get_channel().settimeout(60)
        return self._sftp

    def sftp_mkdirs(self, remote_dir: str) -> None:
        sftp = self._get_sftp()
        parts = [p for p in remote_dir.replace("\\", "/").split("/") if p]
        cur = "/" if remote_dir.startswith("/") else ""
        for p in parts:
            cur = cur + p if cur.endswith("/") or cur == "" else cur + "/" + p
            if not cur.startswith("/"):
                cur = "/" + cur if remote_dir.startswith("/") else cur
            try:
                sftp.stat(cur)
            except IOError:
                try:
                    sftp.mkdir(cur)
                except IOError:
                    pass

    def sftp_put(
        self,
        local_path: str,
        remote_path: str,
        progress: Optional[Callable[[int, int], None]] = None,
        chunk: int = 262144,
    ) -> None:
        sftp = self._get_sftp()
        total = os.path.getsize(local_path)
        done = 0
        with open(local_path, "rb") as fsrc:
            with sftp.file(remote_path, "wb") as fdst:
                fdst.set_pipelined(True)
                while True:
                    data = fsrc.read(chunk)
                    if not data:
                        break
                    fdst.write(data)
                    done += len(data)
                    if progress is not None:
                        progress(done, total)

    def sftp_write_text(self, text: str, remote_path: str) -> None:
        sftp = self._get_sftp()
        with sftp.file(remote_path, "wb") as f:
            f.write(text.encode("utf-8"))

    def sftp_read_text(self, remote_path: str) -> str:
        sftp = self._get_sftp()
        with sftp.file(remote_path, "rb") as f:
            return f.read().decode("utf-8", "replace")

    def sftp_stat_size(self, remote_path: str) -> int:
        sftp = self._get_sftp()
        try:
            return sftp.stat(remote_path).st_size
        except IOError:
            return -1

    def sftp_exists(self, remote_path: str) -> bool:
        return self.sftp_stat_size(remote_path) >= 0

    def sftp_get(self, remote_path: str, local_path: str) -> None:
        sftp = self._get_sftp()
        os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
        sftp.get(remote_path, local_path)

    def sftp_chmod(self, remote_path: str, mode: int) -> None:
        sftp = self._get_sftp()
        try:
            sftp.chmod(remote_path, mode)
        except IOError:
            pass


# ------------------------------------------------------------------ 工具函数
def _sh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


def _looks_like_algorithm_error(msg: str) -> bool:
    low = msg.lower()
    keys = [
        "no matching key exchange",
        "no matching host key",
        "incompatible",
        "no acceptable kex",
        "unsupported protocol",
    ]
    return any(k in low for k in keys)


def _emit_lines(buf: bytes, on_line: Callable[[str], None], final: bool = False) -> bytes:
    """把 buffer 中完整的行回调出去, 返回剩余的不完整数据。"""
    while True:
        idx = buf.find(b"\n")
        if idx < 0:
            break
        raw = buf[:idx]
        buf = buf[idx + 1:]
        on_line(raw.decode("utf-8", "replace").rstrip("\r"))
    if final and buf:
        on_line(buf.decode("utf-8", "replace").rstrip("\r"))
        return b""
    return buf
