"""研华 DAQNavi Linux 驱动远程部署。

严格遵循 FAQ《How to install DAQ DN4 linux driver and test example in linux
system_v2》与 DAQNavi_Linux_Readme.txt 描述的流程:

    1. 安装前准备: 确认内核源码/编译工具链、软件源可用
    2. 禁用冲突驱动 comedi / adv_pci_dio / adv_pci1710
    3. 将驱动包上传到 home 目录
    4. 静默安装: ./DAQ_Linux_xxxx_64bit.run silent install pci1762
    5. 重启系统
    6. 验证: lsmod | grep bio* / /opt/advantech/tools/dndev / /sys/class/daq/*/desc
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .ssh_session import SSHSession, SSHError

LogFn = Callable[[str, str], None]
ProgressFn = Callable[[float, str], None]

# 与 FAQ/readme 一致: 需要屏蔽的冲突驱动
BLACKLIST_DRIVERS = ["comedi", "adv_pci_dio", "adv_pci1710"]

# 卡型定义: 型号 -> (驱动包目录名, 内核模块名, 说明)
#  - PCI-1762: 16 路隔离 DI + 16 路继电器输出
#  - PCI-1761: 8 路隔离 DI + 8 路继电器输出 (驱动包目录为 pci1761_pcm3761i,
#              内核模块名为 bio1761, 见驱动包 Makefile 的 DRV_NAME)
CARD_MODELS = {
    "PCI-1762": ("pci1762", "bio1762",
                 "16 路隔离 DI + 16 路继电器输出"),
    "PCI-1761": ("pci1761_pcm3761i", "bio1761",
                 "8 路隔离 DI + 8 路继电器输出"),
}

# 兼容旧代码
PCI1762_DRIVER = "pci1762"


def drivers_for_models(models) -> List[str]:
    """把卡型列表映射为去重后的驱动包目录名列表。"""
    out = []
    for m in models:
        for name in drivers_for_model_name(m):
            if name not in out:
                out.append(name)
    return out


def drivers_for_model_name(model: str) -> List[str]:
    if model in CARD_MODELS:
        return [CARD_MODELS[model][0]]
    # 允许直接填驱动目录名(高级用法, 空格分隔)
    return [model] if model else []


@dataclass
class DeployOptions:
    installer_local_path: str = ""
    remote_workdir: str = ""          # 空 => 自动使用 $HOME/advdaq_install
    driver_name: str = PCI1762_DRIVER
    install_deps: bool = True
    update_apt_source: bool = True
    disable_comedi: bool = True
    remove_comedi_now: bool = False
    reboot_after: bool = True
    wait_reboot_secs: int = 300
    install_timeout: int = 3600
    extra_drivers: List[str] = field(default_factory=list)


class Deployer:
    def __init__(self, ssh: SSHSession, log: LogFn, options: DeployOptions):
        self.ssh = ssh
        self.log = log
        self.opt = options

        self.installer_base = ""
        self.remote_workdir = ""
        self.remote_installer = ""
        self.env: Dict[str, str] = {}
        self.sudo_ok = False
        self.steps_done: List[str] = []

    # ------------------------------------------------------------------ 工具
    def _run(self, cmd: str, sudo: bool = False, timeout: float = 120.0,
             check: bool = False, quiet: bool = False):
        if not quiet:
            self.log("$ %s" % cmd, "cmd")
        res = self.ssh.exec(cmd, timeout=timeout, sudo=sudo)
        text = res.text.strip()
        if text and not quiet:
            for line in text.splitlines():
                self.log("  " + line, "out" if res.ok else "err")
        if check and not res.ok:
            raise SSHError("命令执行失败(exit=%s): %s" % (res.exit_status, cmd))
        return res

    def _try(self, cmd: str, sudo: bool = False, timeout: float = 120.0) -> str:
        """执行并返回输出, 任何异常(含瞬时通道错误)都不中断流程。"""
        try:
            res = self.ssh.exec(cmd, timeout=timeout, sudo=sudo)
            return res.text.strip()
        except Exception:  # noqa: BLE001
            return ""

    # ============================================================== 步骤 0
    def check_connectivity(self) -> bool:
        """步骤 0: 连通性 & 权限检查。"""
        self.log("正在检查 SSH 连接与权限 ...", "info")
        res = self._run("echo $USER; id -u; uname -r; uname -m; hostname", quiet=True)
        if not res.ok:
            self.log("无法在目标主机执行命令: %s" % res.text, "err")
            return False
        lines = [l.strip() for l in res.stdout.strip().splitlines()]
        self.env["user"] = lines[0] if len(lines) > 0 else ""
        self.env["uid"] = lines[1] if len(lines) > 1 else ""
        self.env["kernel"] = lines[2] if len(lines) > 2 else ""
        self.env["arch"] = lines[3] if len(lines) > 3 else ""
        self.env["hostname"] = lines[4] if len(lines) > 4 else ""

        self.log("目标主机 : %s" % self.env.get("hostname", ""), "ok")
        self.log("登录用户 : %s (uid=%s)" % (self.env.get("user"), self.env.get("uid")), "ok")
        self.log("内核版本 : %s  架构: %s" % (self.env.get("kernel"), self.env.get("arch")), "ok")

        if self.env.get("uid") == "0":
            self.sudo_ok = True
        else:
            # 非 root: 用与后续部署一致的 sudo 包装方式探测是否可用
            res = self.ssh.exec("id -u", sudo=True, timeout=25)
            if res.ok and res.stdout.strip().endswith("0"):
                self.sudo_ok = True
            else:
                self.sudo_ok = False
        if not self.sudo_ok:
            self.log("当前用户不是 root 且 sudo 不可用, 无法安装驱动!", "err")
            return False
        self.log("权限检查 : 通过 (root 或可用 sudo)", "ok")
        return True

    # ============================================================== 步骤 1
    def probe_system(self) -> Dict[str, str]:
        """步骤 1: 探测发行版 / 包管理器 / 编译环境。"""
        self.log("\n[1/7] 探测目标系统环境 ...", "step")

        osrel = self._try("cat /etc/os-release")
        pretty = ""
        m = re.search(r'^PRETTY_NAME="?([^"\n]+)"?', osrel, re.M)
        if m:
            pretty = m.group(1)
        self.env["os"] = pretty

        if not pretty:
            for f, key in (("/etc/redhat-release", "rh"), ("/etc/issue", "issue")):
                v = self._try("cat %s | head -1" % f)
                if v:
                    pretty = v.strip()
                    break
        self.log("操作系统 : %s" % (pretty or "未知"), "ok")

        # 包管理器
        pm = ""
        for cand in ("apt-get", "dnf", "yum", "zypper", "apt"):
            if self._try("command -v %s 2>/dev/null || true" % cand):
                pm = cand
                break
        self.env["pm"] = pm
        self.log("包管理器 : %s" % (pm or "未识别"), "ok" if pm else "warn")

        # 内核头文件
        kdir = self._try('test -d /lib/modules/$(uname -r)/build && echo yes || echo no')
        self.env["kernel_headers"] = "yes" if kdir == "yes" else "no"
        self.log("内核头文件 /lib/modules/%s/build : %s" % (
            self.env.get("kernel"), "已存在" if kdir == "yes" else "缺失"),
            "ok" if kdir == "yes" else "warn")

        # gcc / make
        gcc_v = self._try("gcc --version 2>/dev/null | head -1")
        make_v = self._try("make --version 2>/dev/null | head -1")
        self.env["gcc"] = gcc_v
        self.env["make"] = make_v
        self.log("编译器   : %s" % (gcc_v or "缺失"), "ok" if gcc_v else "warn")
        self.log("make     : %s" % (make_v or "缺失"), "ok" if make_v else "warn")

        # 现存 DAQ 驱动
        loaded = self._try("lsmod | grep -E '^bio' || true")
        self.env["loaded"] = loaded
        if loaded:
            self.log("已加载的 DAQ 驱动:\n" + loaded, "ok")
        else:
            self.log("当前未加载任何 bio* DAQ 驱动", "warn")

        # 是否已安装
        exist = self._try("test -d /opt/advantech && echo yes || echo no")
        self.env["installed"] = "yes" if exist == "yes" else "no"
        self.log("/opt/advantech 安装目录 : %s" % ("存在" if exist == "yes" else "不存在"),
                 "ok" if exist == "yes" else "info")

        # /sys 下已注册设备
        devdesc = self._try("ls /sys/class/daq/ 2>/dev/null || true")
        self.env["sys_devices"] = devdesc
        if devdesc:
            self.log("/sys/class/daq 下已注册: %s" % devdesc.replace("\n", " "), "ok")

        return self.env

    # ============================================================== 步骤 2
    def install_dependencies(self) -> bool:
        """步骤 2: 安装内核头文件与编译工具链。"""
        need_headers = self.env.get("kernel_headers") != "yes"
        need_gcc = not self.env.get("gcc")
        need_make = not self.env.get("make")
        if not (need_headers or need_gcc or need_make):
            self.log("\n[2/7] 编译环境已齐备, 跳过依赖安装", "ok")
            return True
        if not self.opt.install_deps:
            self.log("\n[2/7] 缺少编译环境但已关闭自动安装, 请手动处理后重试", "err")
            return False

        self.log("\n[2/7] 安装内核头文件与编译工具链 ...", "step")
        pm = self.env.get("pm", "")
        kernel = self.env.get("kernel", "$(uname -r)")

        if pm in ("apt-get", "apt"):
            pkgs = "gcc make build-essential autoconf libtool pkg-config pkgconf linux-headers-%s" % kernel
            if self.opt.update_apt_source:
                self.log("更新软件源(apt-get update) ...", "info")
                self._run("apt-get update", sudo=True, timeout=900)
            cmd = "DEBIAN_FRONTEND=noninteractive apt-get install -y %s" % pkgs
            falls = []
        elif pm in ("dnf", "yum"):
            # openEuler / RHEL / CentOS / 麒麟: 带版本号的 kernel-devel 有时不存在,
            # 故准备不带版本号的回退组合
            pkgs = "gcc make autoconf libtool pkgconf kernel-devel-%s kernel-headers-%s" % (kernel, kernel)
            cmd = "%s install -y %s" % (pm, pkgs)
            falls = [
                "%s install -y gcc make autoconf libtool pkgconf kernel-devel kernel-headers" % pm,
                "%s install -y gcc make autoconf libtool pkgconf kernel-devel" % pm,
            ]
        elif pm == "zypper":
            pkgs = "gcc make autoconf libtool pkgconf kernel-default-devel"
            cmd = "zypper --non-interactive install %s" % pkgs
            falls = []
        else:
            self.log("未识别的包管理器, 请手动安装: gcc make 内核头文件(kernel-devel/linux-headers)", "err")
            return False

        res = self._run(cmd, sudo=True, timeout=1800)
        if not res.ok and falls:
            for alt in falls:
                self.log("首次安装未成功, 尝试回退命令: %s" % alt, "warn")
                res = self._run(alt, sudo=True, timeout=1800)
                if res.ok:
                    break
        if not res.ok:
            self.log("依赖安装失败, 请检查网络/软件源", "err")
            return False

        # 复核
        kdir = self._try('test -d /lib/modules/$(uname -r)/build && echo yes || echo no')
        gcc_v = self._try("gcc --version 2>/dev/null | head -1")
        if kdir != "yes":
            self.log("内核头文件仍缺失: /lib/modules/%s/build 不存在" % kernel, "err")
            self.log("提示: 请确认已安装与内核完全匹配的头文件包, 必要时执行 "
                     "'apt-get install linux-headers-$(uname -r)'", "warn")
            return False
        if not gcc_v:
            self.log("gcc 仍不可用", "err")
            return False
        self.log("编译环境就绪: %s" % gcc_v, "ok")
        return True

    # ============================================================== 步骤 3
    def disable_conflict_drivers(self) -> bool:
        """步骤 3: 屏蔽 comedi 等冲突驱动(Driver note)。"""
        if not self.opt.disable_comedi:
            self.log("\n[3/7] 已跳过 comedi 屏蔽(不推荐)", "warn")
            return True

        self.log("\n[3/7] 屏蔽冲突驱动 comedi / adv_pci_dio / adv_pci1710 ...", "step")
        bl = "/etc/modprobe.d/blacklist.conf"
        self._run("touch %s" % bl, sudo=True, quiet=True)

        for drv in BLACKLIST_DRIVERS:
            probe = "grep -qsxE 'blacklist[[:space:]]+%s' %s && echo yes || echo no" % (drv, bl)
            if self._try(probe, sudo=True) == "yes":
                self.log("  %-14s 已在黑名单中" % drv, "info")
            else:
                self._run("sh -c \"echo 'blacklist %s' >> %s\"" % (drv, bl), sudo=True, quiet=True)
                self.log("  %-14s 已加入黑名单" % drv, "ok")

        if self.opt.remove_comedi_now:
            for drv in BLACKLIST_DRIVERS:
                self._try("modprobe -r %s 2>/dev/null || true" % drv, sudo=True)

        self.log("黑名单文件内容:", "info")
        self.log(self._try("cat %s" % bl) or "(空)", "out")
        return True

    # ============================================================== 步骤 4
    def upload_installer(self, progress: Optional[ProgressFn] = None) -> bool:
        """步骤 4: 上传驱动安装包到 home 目录。"""
        self.log("\n[4/7] 上传驱动安装包 ...", "step")
        local = self.opt.installer_local_path
        if not local or not os.path.isfile(local):
            self.log("驱动安装包不存在: %s" % local, "err")
            return False
        size = os.path.getsize(local)
        self.installer_base = os.path.basename(local)

        home = self._try("echo $HOME").strip() or "/root"
        workdir = self.opt.remote_workdir.strip() or ("%s/advdaq_install" % home)
        if not workdir.startswith("/"):
            workdir = "%s/%s" % (home.rstrip("/"), workdir)
        self.remote_workdir = workdir
        self.remote_installer = "%s/%s" % (workdir, self.installer_base)

        self._try("mkdir -p %s" % workdir)

        remote_size = self.ssh.sftp_stat_size(self.remote_installer)
        if remote_size == size:
            self.log("目标机已存在同名同大小文件, 跳过上传 (%d 字节)" % size, "ok")
        else:
            self.log("本地文件 : %s (%d 字节, %.1f MB)" % (local, size, size / 1048576.0), "info")
            self.log("上传至   : %s@%s:%s" % (
                self.ssh.target.username, self.ssh.target.host, self.remote_installer), "info")

            last = [0.0]

            def _p(done: int, total: int):
                if progress is None:
                    return
                now = time.monotonic()
                if now - last[0] < 0.25 and done < total:
                    return
                last[0] = now
                progress(done / float(total or 1), "上传驱动包 %d/%d MB" % (
                    done // 1048576, total // 1048576))

            try:
                self.ssh.sftp_put(local, self.remote_installer, progress=_p)
            except Exception as exc:  # noqa: BLE001
                self.log("SFTP 上传失败: %s" % exc, "err")
                return False

            new_size = self.ssh.sftp_stat_size(self.remote_installer)
            if new_size != size:
                self.log("上传校验失败: 远端 %s 字节, 本地 %s 字节" % (new_size, size), "err")
                return False
            self.log("上传完成并校验通过", "ok")

        self._run("chmod +x %s" % self.remote_installer, quiet=True)
        self.log("已赋予可执行权限 (chmod +x)", "ok")
        return True

    # ============================================================== 步骤 5
    def silent_install(self) -> bool:
        """步骤 5: 静默安装 pci1762 驱动(FAQ 第 2 种方式)。"""
        self.log("\n[5/7] 静默安装驱动 (%s) ..." % self.opt.driver_name, "step")
        drivers = [self.opt.driver_name] + [d for d in self.opt.extra_drivers if d]
        drv_args = " ".join(drivers)

        cmd = ("cd %s && ./%s silent install %s"
               % (self.remote_workdir, self.installer_base, drv_args))
        self.log("执行: %s" % cmd, "info")
        self.log("(驱动需要现场编译内核模块, 可能需要数分钟, 请耐心等待...)", "info")

        res = self.ssh.exec(cmd, timeout=self.opt.install_timeout, sudo=True)
        for line in res.text.splitlines():
            self.log("  " + line, "out" if res.ok else "err")

        if not res.ok:
            low = res.text.lower()
            if "kernel headers" in low:
                self.log("安装失败: 缺少内核头文件(请先完成步骤 2)", "err")
            elif "gcc" in low:
                self.log("安装失败: 缺少 gcc 编译器(请先完成步骤 2)", "err")
            else:
                self.log("安装命令返回非零退出码: %s" % res.exit_status, "err")
            return False

        # 安装脚本自己会把安装清单写入 driver-installed.log
        logtxt = self._try("cat /opt/advantech/driver-installed.log 2>/dev/null || true")
        if logtxt:
            self.log("驱动安装清单 (/opt/advantech/driver-installed.log):", "info")
            for line in logtxt.splitlines():
                self.log("  " + line, "out")

        if not self._try("test -x /opt/advantech/tools/dndev && echo yes"):
            self.log("警告: 未找到 /opt/advantech/tools/dndev, 安装可能不完整", "warn")

        self.log("驱动安装流程完成", "ok")
        return True

    # ============================================================== 驱动卸载
    def uninstall_drivers(self) -> bool:
        """卸载已安装的 DAQ 驱动(对应安装器的 silent uninstall 流程)。

        需要: 本地安装包与安装时为同一版本(卸载脚本从安装包内读取驱动源码
        执行 make uninstall), /opt/advantech 下应有 driver-installed.log。
        """
        self.log("\n[卸载] 静默卸载驱动 (%s) ..." % self.opt.driver_name, "step")
        drivers = [self.opt.driver_name] + [d for d in self.opt.extra_drivers if d]
        drv_args = " ".join(drivers)

        # 解析远端工作目录与安装包路径(卸载可独立于本次会话执行)
        if not self.remote_workdir:
            home = self._try("echo $HOME").strip() or "/root"
            wd = (self.opt.remote_workdir or "").strip() or ("%s/advdaq_install" % home)
            if not wd.startswith("/"):
                wd = "%s/%s" % (home.rstrip("/"), wd)
            self.remote_workdir = wd
        if not self.installer_base and self.opt.installer_local_path:
            self.installer_base = os.path.basename(self.opt.installer_local_path)
        if self.installer_base:
            self.remote_installer = "%s/%s" % (self.remote_workdir, self.installer_base)

        # 前置检查
        if self._try("test -d /opt/advantech && echo yes") != "yes":
            self.log("目标机未安装 DAQNavi 驱动 (/opt/advantech 不存在), 无需卸载", "warn")
            return True
        if not self.remote_installer or self._try(
                "test -f %s && echo yes" % self.remote_installer) != "yes":
            self.log("目标机上未找到驱动安装包, 卸载脚本需要安装包才能执行", "err")
            self.log("提示: 请先在上方选择与安装时相同的 .run 安装包再卸载", "warn")
            return False

        cmd = ("cd %s && ./%s silent uninstall %s"
               % (self.remote_workdir, self.installer_base, drv_args))
        self.log("执行: %s" % cmd, "info")
        res = self.ssh.exec(cmd, timeout=self.opt.install_timeout, sudo=True)
        for line in res.text.splitlines():
            self.log("  " + line, "out" if res.ok else "err")
        if not res.ok:
            self.log("卸载命令返回非零退出码: %s" % res.exit_status, "err")
            return False

        # 复核: 内核模块应已不存在
        loaded = self._try("lsmod | grep -E '^bio' || true")
        if loaded:
            self.log("警告: 卸载后仍有 bio* 模块在内核中, 建议重启后复查:\n%s" % loaded, "warn")
        else:
            self.log("内核中已无 bio* DAQ 模块", "ok")
        gone = self._try("test -d /opt/advantech/libs && echo exists || echo gone")
        if gone == "gone":
            self.log("SDK 文件已清除 (/opt/advantech/libs 不存在)", "ok")
        else:
            self.log("/opt/advantech 仍存在部分 SDK 文件(卸载脚本行为, 重启后可手动清理)", "info")
        self.log("驱动卸载流程完成", "ok")
        return True

    # ============================================================== 步骤 6
    def reboot(self) -> None:
        """步骤 6: 重启目标系统(FAQ 要求)。"""
        self.log("\n[6/7] 重启目标系统 ...", "step")
        res = self.ssh.exec("shutdown -r now", sudo=True, timeout=15)
        if res.ok:
            self.log("已下发重启指令", "ok")
        else:
            # 某些系统 shutdown 返回非零但仍会重启
            self.log("重启指令返回: %s" % (res.text or "无输出"), "warn")
        # 等系统开始关机
        time.sleep(3)

    def wait_for_reboot(self, progress: Optional[ProgressFn] = None) -> bool:
        """等待目标机重启完成并重新建立 SSH 连接。"""
        self.log("等待目标机重新上线(最长 %d 秒) ..." % self.opt.wait_reboot_secs, "info")
        deadline = time.monotonic() + self.opt.wait_reboot_secs
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            if progress:
                spent = 1 - max(0.0, (deadline - time.monotonic()) / self.opt.wait_reboot_secs)
                progress(spent, "等待重启, 第 %d 次探测 ..." % attempt)
            try:
                self.ssh.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.ssh.connect()
                # 再确认能执行命令
                if self.ssh.exec("echo up", timeout=10).stdout.strip() == "up":
                    self.log("目标机已重新上线", "ok")
                    return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(5)
        self.log("等待重启超时, 请手动确认目标机状态后点击\"验证安装\"", "err")
        return False

    # ============================================================== 步骤 7
    def verify(self) -> bool:
        """步骤 7: 验证驱动加载与设备识别(FAQ Step6)。"""
        self.log("\n[7/7] 验证驱动安装结果 ...", "step")
        ok = True

        # (1) lsmod | grep bio*
        self.log("① lsmod | grep bio*", "info")
        lsmod = self._try("lsmod | grep -E '^bio' || true")
        if lsmod:
            for line in lsmod.splitlines():
                self.log("  " + line, "ok")
        else:
            self.log("  未发现 bio* 内核模块! 驱动可能未加载", "err")
            ok = False

        # (2) dndev 工具
        self.log("② /opt/advantech/tools/dndev", "info")
        dndev = self._try("/opt/advantech/tools/dndev 2>&1 || true")
        if dndev:
            for line in dndev.splitlines():
                self.log("  " + line, "ok")
        else:
            self.log("  dndev 无输出", "warn")

        # (3) /sys/class/daq/*/desc  (FAQ Step3: cat /sys/class/daq/daq0/desc)
        self.log("③ cat /sys/class/daq/*/desc", "info")
        descs = self._try("for d in /sys/class/daq/*; do "
                          "[ -r \"$d/desc\" ] && echo \"$d/desc: $(cat $d/desc)\"; done 2>/dev/null || true")
        if descs:
            for line in descs.splitlines():
                self.log("  " + line, "ok")
            self.env["device_desc"] = descs
        else:
            self.log("  未发现 /sys/class/daq/*/desc, 请确认 PCI-1762 已插好", "err")
            ok = False

        # (4) 服务状态
        self.log("④ daqnavi 服务状态", "info")
        svc = self._try("systemctl is-active daqnavi_daemon 2>/dev/null || service daqnavi_daemon status 2>&1 | head -3 || true")
        if svc:
            for line in svc.splitlines()[:4]:
                self.log("  " + line, "out")

        if ok:
            self.log("驱动部署验证通过 ✓", "ok")
        else:
            self.log("驱动部署验证未完全通过, 请查看上面的错误项", "err")
        return ok

    # ============================================================== 编排
    def run_all(self, progress: Optional[ProgressFn] = None) -> bool:
        """完整执行 FAQ 描述的部署流程。"""

        def _p(pct, msg):
            if progress:
                progress(pct, msg)

        _p(0.02, "检查连接")
        if not self.check_connectivity():
            return False
        self.steps_done.append("connectivity")

        _p(0.06, "探测系统环境")
        self.probe_system()
        self.steps_done.append("probe")

        _p(0.12, "准备编译环境")
        if not self.install_dependencies():
            return False
        self.steps_done.append("deps")

        _p(0.20, "屏蔽冲突驱动")
        if not self.disable_conflict_drivers():
            return False
        self.steps_done.append("blacklist")

        if not self.upload_installer(lambda f, m: _p(0.20 + f * 0.45, m)):
            return False
        self.steps_done.append("upload")

        _p(0.68, "静默安装驱动")
        if not self.silent_install():
            return False
        self.steps_done.append("install")

        if self.opt.reboot_after:
            _p(0.82, "重启目标机")
            self.reboot()
            _p(0.84, "等待重启")
            if not self.wait_for_reboot(lambda f, m: _p(0.84 + f * 0.10, m)):
                return False
            self.steps_done.append("reboot")
        else:
            self.log("\n[6/7] 按当前设置跳过自动重启", "warn")
            self.log("注意: 驱动需要重启系统后才会自动加载, 请稍后手动执行 reboot", "warn")

        _p(0.96, "验证安装结果")
        ok = self.verify()
        self.steps_done.append("verify")
        _p(1.0, "完成")
        return ok
