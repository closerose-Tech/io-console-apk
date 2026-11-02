/*****************************************************************************
 * bio1762_io.c  --  Advantech PCI-1762 远程 IO 测试代理程序 (Linux 端)
 *
 * 说明:
 *   本程序由 Windows 端"研华 PCI-1762 远程部署与 IO 测试工具"通过 SSH 上传,
 *   并链接 DAQNavi Linux SDK (libbiodaq) 编译运行在目标 Linux 主机上。
 *
 *   PCI-1762 硬件资源: 16 路隔离数字量输入(DI) + 16 路继电器输出(DO)
 *                      => DI 2 个端口(port0/port1), DO 2 个端口(port0/port1)
 *
 * 编译(由工具自动完成):
 *   gcc -O2 -I/opt/advantech/inc -o bio1762_io bio1762_io.c \
 *       -L/opt/advantech/libs -lbiodaq -Wl,-rpath,/opt/advantech/libs
 *
 * 运行方式:
 *   1) 一次性命令(便于人工按 FAQ 手工测试):
 *        ./bio1762_io list                     列出已安装设备
 *        ./bio1762_io info                     显示当前设备 DI/DO 资源
 *        ./bio1762_io readdi [起始端口] [端口数]
 *        ./bio1762_io readdo [起始端口] [端口数]
 *        ./bio1762_io setdo  <端口> <十六进制值>
 *        ./bio1762_io setbit <端口> <位> <0|1>
 *        ./bio1762_io selftest                 DO 回读自检 + DI 读取
 *   2) 行协议服务(供 Windows 端长连接控制):
 *        ./bio1762_io server [设备描述子串]
 *
 *      内部参数(由程序自身 execv 自替换时使用, 无需手工指定):
 *        --select-reply   首行应答 "OK SELECT <desc>" 而非 "READY"
 *        --no-respawn     禁止自替换(调试用, 正常流程不使用)
 *
 *      说明: 实测 bio1761 驱动在同一进程内 Dispose 后无法重新取得设备权限
 *      (ErrorPrivilegeNotAvailable = 0xE0000010), 而全新进程总是可以打开。
 *      因此 SELECT 切换设备若在进程内打开失败, 代理会 execv 自身重启为新进程
 *      来完成打开 —— stdin/stdout 不变, 对上层完全透明。
 *
 * 行协议(每条命令一行, 应答一行, 均以 \n 结束, 均为 ASCII):
 *   >> PING                         << PONG
 *   >> LIST                         << OK LIST 0|PCI-1762,BID#0;
 *   >> SELECT <索引|描述子串>       << OK SELECT <描述>
 *   >> INFO                         << OK INFO desc=... di_ports=2 do_ports=2
 *                                                  di_lines=16 do_lines=16
 *   >> DI <起始端口> <端口数>       << OK DI 1A 00
 *   >> DO <起始端口> <端口数>       << OK DO 00 00        (读继电器回读状态)
 *   >> SETDO <起始端口> <值> [值..] << OK SETDO
 *   >> SETBIT <端口> <位> <0|1>     << OK SETBIT
 *   >> GETBIT <端口> <位>           << OK BIT <0|1>
 *   >> WATCH <间隔ms> <起始端口> <端口数>
 *                                   << OK WATCH          随后持续推送
 *                                                         DI 1A 00
 *   >> UNWATCH                      << OK UNWATCH
 *   >> QUIT                         << BYE
 *   任意错误应答:  ERR <十进制错误码> <可读说明>
 *****************************************************************************/

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <time.h>
#include <locale.h>
#include <wchar.h>
#include <signal.h>

#include "bdaqctrl.h"

#define MAX_PORT_BUF   128
#define LINE_BUF       512

/*------------------------------------------------------------- 全局状态 */
static InstantDiCtrl *g_di = NULL;
static InstantDoCtrl *g_do = NULL;
static ErrorCode      g_diRet = Success;
static ErrorCode      g_doRet = Success;
static wchar_t        g_desc[MAX_DEVICE_DESC_LEN + 1];
static int            g_diPorts = 0;
static int            g_doPorts = 0;
static int            g_opened  = 0;

/* 看门狗式 DI 轮询状态 */
static int            g_watching  = 0;
static int            g_watchMs   = 200;
static int            g_watchPort = 0;
static int            g_watchCnt  = 2;
static struct timespec g_lastTick;

/* 自替换(execv)相关:
 *   实测 bio1761 驱动存在缺陷 —— 同一进程内 Dispose 之后无法再次取得设备权限
 *   (ErrorPrivilegeNotAvailable = 0xE0000010, 且 lsmod 中模块引用计数不会归零),
 *   而全新进程总能正常打开。因此当进程内重新打开失败时, 代理直接 execv 自身,
 *   由新进程完成打开并应答。新进程沿用同一套 stdin/stdout, 行协议不受影响。 */
static char          g_self[512] = "";
static int           g_allow_respawn = 1;
static const char   *g_respawn_to = NULL;

/* DO 回读一致性
 *   实测 PCI-1761 的 InstantDoCtrl_ReadAny 读的是周期性刷新的缓存:
 *   写入后立即回读会拿到"上一次的旧值", 约 100~150ms 后才变成新值
 *   (PCI-1762 无此现象, 而 ReadBit 两者都准确)。
 *   因此这里记录"刚写入的值", 回读时若与记录不符就短间隔重读到一致为止,
 *   对没有该问题的卡(值一开始就相符)不产生任何额外延时。 */
static uint8         g_doShadow[MAX_PORT_BUF];
static int           g_doShadowPorts = 0;
static int           g_doDirty = 0;

/*------------------------------------------------------------- 小工具 */
static void out_flush(void)
{
    fflush(stdout);
}

/* 宽字符描述 -> 本机多字节 */
static const char *desc_to_mb(const wchar_t *w)
{
    static char buf[MAX_DEVICE_DESC_LEN * 4 + 8];
    size_t n = wcstombs(buf, w, sizeof(buf) - 1);
    if (n == (size_t)-1) {
        /* 无法转换时退化为逐个字符的 ASCII 过滤 */
        int i;
        for (i = 0; i < MAX_DEVICE_DESC_LEN && w[i]; ++i) {
            wchar_t c = w[i];
            buf[i] = (c > 0 && c < 128) ? (char)c : '?';
        }
        buf[i] = '\0';
    } else {
        buf[n] = '\0';
    }
    return buf;
}

static void err_to_str(ErrorCode code, char *dst, size_t dstsz)
{
    wchar_t wbuf[256] = {0};
    if (BioFailed(code)) {
        AdxEnumToString(L"ErrorCode", (int32)code, 256, wbuf);
        snprintf(dst, dstsz, "%s", desc_to_mb(wbuf));
    } else {
        snprintf(dst, dstsz, "Success");
    }
}

/*------------------------------------------------------------- 设备开关 */
static void close_all(void)
{
    if (g_di) { InstantDiCtrl_Dispose(g_di); g_di = NULL; }
    if (g_do) { InstantDoCtrl_Dispose(g_do); g_do = NULL; }
    g_diPorts = g_doPorts = 0;
    g_opened = 0;
    /* 换设备后旧的写入记录不再有效 */
    g_doShadowPorts = 0;
    g_doDirty = 0;
    memset(g_doShadow, 0, sizeof(g_doShadow));
}

/* 记录刚写入的端口值(用于回读一致性判定) */
static void do_shadow_mark(int port, uint8 val)
{
    if (port < 0 || port >= MAX_PORT_BUF) return;
    g_doShadow[port] = val;
    if (port + 1 > g_doShadowPorts) g_doShadowPorts = port + 1;
    g_doDirty = 1;
}

/* 读取 DO 并做"回读一致性"收敛: 若刚写过且回读值与写入值不符, 说明拿到的是
   滞后缓存, 短间隔重读直至一致(PCI-1761 实测 100~200ms 内收敛; PCI-1762 立即一致)。 */
static ErrorCode do_read_stable(int portStart, int portCount, uint8 *buf)
{
    ErrorCode ret;
    int tries, k;

    memset(buf, 0, (size_t)portCount);
    ret = InstantDoCtrl_ReadAny(g_do, portStart, portCount, buf);
    if (BioFailed(ret) || !g_doDirty) return ret;

    for (tries = 0; tries < 6; ++tries) {
        int diff = 0;
        for (k = 0; k < portCount; ++k) {
            int p = portStart + k;
            if (p >= 0 && p < g_doShadowPorts && buf[k] != g_doShadow[p]) {
                diff = 1;
                break;
            }
        }
        if (!diff) break;
        usleep(80000);
        memset(buf, 0, (size_t)portCount);
        ret = InstantDoCtrl_ReadAny(g_do, portStart, portCount, buf);
        if (BioFailed(ret)) break;
    }
    g_doDirty = 0;
    return ret;
}

static void fill_devinfo(DeviceInformation *info, AccessMode mode)
{
    memset(info, 0, sizeof(*info));
    info->DeviceNumber = -1;
    info->DeviceMode   = mode;
    info->ModuleIndex  = 0;
    wcsncpy(info->Description, g_desc, MAX_DEVICE_DESC_LEN - 1);
    info->Description[MAX_DEVICE_DESC_LEN - 1] = L'\0';
}

/* 以指定访问模式尝试同时打开 DI + DO */
static int try_open_pair(AccessMode diMode, AccessMode doMode)
{
    DeviceInformation info;

    close_all();

    fill_devinfo(&info, diMode);
    g_di = InstantDiCtrl_Create();
    g_diRet = InstantDiCtrl_setSelectedDevice(g_di, &info);
    if (BioFailed(g_diRet)) {
        InstantDiCtrl_Dispose(g_di);
        g_di = NULL;
        return 0;
    }

    fill_devinfo(&info, doMode);
    g_do = InstantDoCtrl_Create();
    g_doRet = InstantDoCtrl_setSelectedDevice(g_do, &info);
    if (BioFailed(g_doRet)) {
        InstantDoCtrl_Dispose(g_do);
        g_do = NULL;
        InstantDiCtrl_Dispose(g_di);
        g_di = NULL;
        return 0;
    }

    g_diPorts = InstantDiCtrl_getPortCount(g_di);
    g_doPorts = InstantDoCtrl_getPortCount(g_do);
    if (g_diPorts <= 0) g_diPorts = 0;
    if (g_doPorts <= 0) g_doPorts = 0;
    g_opened = 1;
    return 1;
}

/*****************************************************************************
 * 打开设备控制器。
 *
 * DAQNavi 采用"独占/共享"访问模式, 同一块卡上的 DI 与 DO 是否能由两个控制器
 * 同时打开与访问模式有关, 因此这里按多组模式依次尝试, 保证在绝大多数驱动
 * 配置下都能工作; 若 DI 与 DO 无法同时打开, 则优先保留 DO(继电器输出),
 * 再退回仅 DI。
 *****************************************************************************/
static int open_device(void)
{
    DeviceInformation info;

    g_diRet = Success;
    g_doRet = Success;

    /* 组合尝试: 写+写 -> 读+写 -> 共享写+共享写 */
    if (try_open_pair(ModeWrite, ModeWrite))            return 1;
    if (try_open_pair(ModeRead,  ModeWrite))            return 1;
    if (try_open_pair(ModeWriteShared, ModeWriteShared))return 1;

    /* 退化 1: 仅 DO */
    close_all();
    fill_devinfo(&info, ModeWrite);
    g_do = InstantDoCtrl_Create();
    g_doRet = InstantDoCtrl_setSelectedDevice(g_do, &info);
    if (!BioFailed(g_doRet)) {
        g_doPorts = InstantDoCtrl_getPortCount(g_do);
        if (g_doPorts < 0) g_doPorts = 0;
        g_opened = 1;
        return 1;
    }
    InstantDoCtrl_Dispose(g_do);
    g_do = NULL;

    /* 退化 2: 仅 DI */
    close_all();
    fill_devinfo(&info, ModeWrite);
    g_di = InstantDiCtrl_Create();
    g_diRet = InstantDiCtrl_setSelectedDevice(g_di, &info);
    if (!BioFailed(g_diRet)) {
        g_diPorts = InstantDiCtrl_getPortCount(g_di);
        if (g_diPorts < 0) g_diPorts = 0;
        g_opened = 1;
        return 1;
    }
    InstantDiCtrl_Dispose(g_di);
    g_di = NULL;

    return 0;
}

/* 按索引或描述子串选择设备 */
static int select_by_token(const char *token)
{
    IArray *arr = DeviceCtrl_getInstalledDevices();
    int n, i, idx = -1;

    if (!arr) return 0;
    n = Array_getLength(arr);
    if (n <= 0) return 0;

    if (token && *token) {
        /* 纯数字视为索引 */
        char *end = NULL;
        long v = strtol(token, &end, 10);
        if (end && *end == '\0' && v >= 0 && v < n) {
            idx = (int)v;
        } else {
            for (i = 0; i < n; ++i) {
                DeviceTreeNode *node = (DeviceTreeNode *)Array_getItem(arr, i);
                if (!node) continue;
                if (strstr(desc_to_mb(node->Description), token) != NULL) {
                    idx = i;
                    break;
                }
            }
        }
    } else {
        idx = 0;
    }

    if (idx < 0) return 0;

    {
        DeviceTreeNode *node = (DeviceTreeNode *)Array_getItem(arr, idx);
        if (!node) return 0;
        wcsncpy(g_desc, node->Description, MAX_DEVICE_DESC_LEN - 1);
        g_desc[MAX_DEVICE_DESC_LEN - 1] = L'\0';
    }
    return 1;
}

static int ensure_open(void)
{
    if (g_opened) return 1;
    if (g_desc[0] == L'\0') {
        if (!select_by_token(NULL)) return 0;
    }
    return open_device();
}

/*------------------------------------------------------------- 命令实现 */
static void cmd_list(char *resp, size_t sz)
{
    IArray *arr = DeviceCtrl_getInstalledDevices();
    int n, i;
    size_t off = 0;

    if (!arr) { snprintf(resp, sz, "ERR 2147483649 no_device_controller"); return; }
    n = Array_getLength(arr);
    off = (size_t)snprintf(resp, sz, "OK LIST ");
    for (i = 0; i < n && off < sz - 8; ++i) {
        DeviceTreeNode *node = (DeviceTreeNode *)Array_getItem(arr, i);
        if (!node) continue;
        off += (size_t)snprintf(resp + off, sz - off, "%d|%s;",
                                i, desc_to_mb(node->Description));
    }
    if (n == 0) snprintf(resp + off, sz - off, "(none);");
}

static void cmd_info(char *resp, size_t sz)
{
    char ebuf[160];
    if (!ensure_open()) {
        err_to_str(g_diRet, ebuf, sizeof(ebuf));
        snprintf(resp, sz, "ERR 2147483649 open_failed di=0x%X(%s) do=0x%X",
                 (unsigned)g_diRet, ebuf, (unsigned)g_doRet);
        return;
    }
    snprintf(resp, sz,
             "OK INFO desc=%s di_ports=%d do_ports=%d di_lines=%d do_lines=%d di_ok=%d do_ok=%d",
             desc_to_mb(g_desc), g_diPorts, g_doPorts,
             g_diPorts * 8, g_doPorts * 8,
             g_di ? 1 : 0, g_do ? 1 : 0);
}

static void cmd_read_di(int portStart, int portCount, char *resp, size_t sz)
{
    uint8 buf[MAX_PORT_BUF];
    size_t off;
    int i;
    ErrorCode ret;

    if (!g_di) { snprintf(resp, sz, "ERR 2147483650 di_not_available"); return; }
    if (portCount <= 0 || portCount > MAX_PORT_BUF) portCount = (g_diPorts > 0 ? g_diPorts : 2);
    if (portStart < 0) portStart = 0;
    if (g_diPorts > 0 && portStart + portCount > g_diPorts) {
        portCount = g_diPorts - portStart;
        if (portCount <= 0) { snprintf(resp, sz, "ERR 2147483651 port_range_invalid"); return; }
    }

    memset(buf, 0, sizeof(buf));
    ret = InstantDiCtrl_ReadAny(g_di, portStart, portCount, buf);
    if (BioFailed(ret)) {
        char ebuf[160];
        err_to_str(ret, ebuf, sizeof(ebuf));
        snprintf(resp, sz, "ERR %u di_read_failed %s", (unsigned)ret, ebuf);
        return;
    }

    off = (size_t)snprintf(resp, sz, "OK DI");
    for (i = 0; i < portCount; ++i)
        off += (size_t)snprintf(resp + off, sz - off, " %02X", (unsigned)buf[i]);
}

static void cmd_read_do(int portStart, int portCount, char *resp, size_t sz)
{
    uint8 buf[MAX_PORT_BUF];
    size_t off;
    int i;
    ErrorCode ret;

    if (!g_do) { snprintf(resp, sz, "ERR 2147483652 do_not_available"); return; }
    if (portCount <= 0 || portCount > MAX_PORT_BUF) portCount = (g_doPorts > 0 ? g_doPorts : 2);
    if (portStart < 0) portStart = 0;
    if (g_doPorts > 0 && portStart + portCount > g_doPorts) {
        portCount = g_doPorts - portStart;
        if (portCount <= 0) { snprintf(resp, sz, "ERR 2147483651 port_range_invalid"); return; }
    }

    ret = do_read_stable(portStart, portCount, buf);
    if (BioFailed(ret)) {
        char ebuf[160];
        err_to_str(ret, ebuf, sizeof(ebuf));
        snprintf(resp, sz, "ERR %u do_read_failed %s", (unsigned)ret, ebuf);
        return;
    }

    off = (size_t)snprintf(resp, sz, "OK DO");
    for (i = 0; i < portCount; ++i)
        off += (size_t)snprintf(resp + off, sz - off, " %02X", (unsigned)buf[i]);
}

static void cmd_set_do(int portStart, int nvals, char **vals, char *resp, size_t sz)
{
    uint8 buf[MAX_PORT_BUF];
    ErrorCode ret;
    int i;

    if (!g_do) { snprintf(resp, sz, "ERR 2147483652 do_not_available"); return; }
    if (nvals <= 0 || nvals > MAX_PORT_BUF) { snprintf(resp, sz, "ERR 2147483653 bad_value_count"); return; }

    for (i = 0; i < nvals; ++i) {
        char *end = NULL;
        long v = strtol(vals[i], &end, 0);
        if (!end || *end != '\0' || v < 0 || v > 255) {
            snprintf(resp, sz, "ERR 2147483654 bad_value '%s'", vals[i]);
            return;
        }
        buf[i] = (uint8)v;
    }

    ret = InstantDoCtrl_WriteAny(g_do, portStart, nvals, buf);
    if (BioFailed(ret)) {
        char ebuf[160];
        err_to_str(ret, ebuf, sizeof(ebuf));
        snprintf(resp, sz, "ERR %u do_write_failed %s", (unsigned)ret, ebuf);
        return;
    }
    for (i = 0; i < nvals; ++i) do_shadow_mark(portStart + i, buf[i]);
    snprintf(resp, sz, "OK SETDO");
}

static void cmd_set_bit(int port, int bit, int val, char *resp, size_t sz)
{
    ErrorCode ret;

    if (!g_do) { snprintf(resp, sz, "ERR 2147483652 do_not_available"); return; }
    if (bit < 0 || bit > 7) { snprintf(resp, sz, "ERR 2147483655 bad_bit"); return; }

    ret = InstantDoCtrl_WriteBit(g_do, port, bit, (uint8)(val ? 1 : 0));
    if (BioFailed(ret)) {
        char ebuf[160];
        err_to_str(ret, ebuf, sizeof(ebuf));
        snprintf(resp, sz, "ERR %u do_writebit_failed %s", (unsigned)ret, ebuf);
        return;
    }
    {
        /* 同步更新该端口的写入记录 */
        uint8 cur = (port >= 0 && port < g_doShadowPorts) ? g_doShadow[port] : 0;
        cur = (uint8)(val ? (cur | (1u << bit)) : (cur & ~(1u << bit)));
        do_shadow_mark(port, cur);
    }
    snprintf(resp, sz, "OK SETBIT");
}

static void cmd_get_bit(int port, int bit, char *resp, size_t sz)
{
    uint8 data = 0;
    ErrorCode ret;

    if (!g_do) { snprintf(resp, sz, "ERR 2147483652 do_not_available"); return; }
    if (bit < 0 || bit > 7) { snprintf(resp, sz, "ERR 2147483655 bad_bit"); return; }

    ret = InstantDoCtrl_ReadBit(g_do, port, bit, &data);
    if (BioFailed(ret)) {
        char ebuf[160];
        err_to_str(ret, ebuf, sizeof(ebuf));
        snprintf(resp, sz, "ERR %u do_readbit_failed %s", (unsigned)ret, ebuf);
        return;
    }
    snprintf(resp, sz, "OK BIT %d", data ? 1 : 0);
}

/* DI 连续采样(由 server 主循环调用) */
static void watch_tick(void)
{
    struct timespec now;
    long long d;
    uint8 buf[MAX_PORT_BUF];
    ErrorCode ret;
    int i;

    if (!g_watching || !g_di) return;

    clock_gettime(CLOCK_MONOTONIC, &now);
    d = (long long)(now.tv_sec - g_lastTick.tv_sec) * 1000LL
        + (long long)(now.tv_nsec - g_lastTick.tv_nsec) / 1000000LL;
    if (d < g_watchMs) return;
    g_lastTick = now;

    memset(buf, 0, sizeof(buf));
    ret = InstantDiCtrl_ReadAny(g_di, g_watchPort, g_watchCnt, buf);
    if (BioFailed(ret)) {
        printf("ERR %u di_read_failed\n", (unsigned)ret);
        out_flush();
        g_watching = 0;
        return;
    }
    printf("DI");
    for (i = 0; i < g_watchCnt; ++i) printf(" %02X", (unsigned)buf[i]);
    printf("\n");
    out_flush();
}

/*------------------------------------------------------------- 参数解析 */
static int parse_int(const char *s, int *out)
{
    char *end = NULL;
    long v;
    if (!s) return 0;
    v = strtol(s, &end, 0);
    if (!end || *end != '\0') return 0;
    *out = (int)v;
    return 1;
}

/* 处理一条 server 命令, resp 中为应答应答 */
static void handle_command(char *line, char *resp, size_t sz, int *quit)
{
    char *tok[24];
    int ntok = 0;
    char *p;

    resp[0] = '\0';
    *quit = 0;

    /* 去尾部空白 */
    for (p = line + strlen(line); p > line && (p[-1] == '\r' || p[-1] == '\n' ||
                                               p[-1] == ' ' || p[-1] == '\t'); --p)
        p[-1] = '\0';

    for (p = strtok(line, " \t"); p && ntok < 24; p = strtok(NULL, " \t"))
        tok[ntok++] = p;

    if (ntok == 0) return;

    if (strcasecmp(tok[0], "PING") == 0) {
        snprintf(resp, sz, "PONG");
    } else if (strcasecmp(tok[0], "QUIT") == 0 || strcasecmp(tok[0], "EXIT") == 0) {
        snprintf(resp, sz, "BYE");
        *quit = 1;
    } else if (strcasecmp(tok[0], "LIST") == 0) {
        cmd_list(resp, sz);
    } else if (strcasecmp(tok[0], "SELECT") == 0) {
        if (ntok < 2) { snprintf(resp, sz, "ERR 2147483657 missing_arg"); return; }
        /* 目标与当前已打开的设备一致时直接复用, 避免无意义的关闭/重开
           (部分驱动关闭后难以重开, 见下方自替换说明)。 */
        if (g_opened && strcmp(desc_to_mb(g_desc), tok[1]) == 0) {
            snprintf(resp, sz, "OK SELECT %s", desc_to_mb(g_desc));
            return;
        }
        close_all();
        g_desc[0] = L'\0';
        if (!select_by_token(tok[1])) {
            snprintf(resp, sz, "ERR 2147483658 device_not_found '%s'", tok[1]);
            return;
        }
        if (open_device()) {
            snprintf(resp, sz, "OK SELECT %s", desc_to_mb(g_desc));
            return;
        }
        /* 进程内重新打开失败(bio1761 的权限释放缺陷): 交给自替换处理,
           令 resp 留空, 由主循环执行 execv, 新进程负责打开并应答。
           自替换不会形成死循环: 新进程以 --select-reply 启动, 打开成功/失败
           都会直接应答, 自身不会再次自替换。 */
        if (g_allow_respawn) {
            g_respawn_to = tok[1];
            return;
        }
        {
            char ebuf[160];
            err_to_str(g_diRet, ebuf, sizeof(ebuf));
            snprintf(resp, sz, "ERR 2147483649 open_failed di=0x%X(%s) do=0x%X",
                     (unsigned)g_diRet, ebuf, (unsigned)g_doRet);
        }
    } else if (strcasecmp(tok[0], "INFO") == 0) {
        cmd_info(resp, sz);
    } else if (strcasecmp(tok[0], "DI") == 0) {
        int ps = 0, pc = 0;
        if (ntok >= 2 && !parse_int(tok[1], &ps)) { snprintf(resp, sz, "ERR 2147483659 bad_arg"); return; }
        if (ntok >= 3 && !parse_int(tok[2], &pc)) { snprintf(resp, sz, "ERR 2147483659 bad_arg"); return; }
        if (!ensure_open()) { snprintf(resp, sz, "ERR 2147483649 open_failed"); return; }
        cmd_read_di(ps, pc, resp, sz);
    } else if (strcasecmp(tok[0], "DO") == 0) {
        int ps = 0, pc = 0;
        if (ntok >= 2 && !parse_int(tok[1], &ps)) { snprintf(resp, sz, "ERR 2147483659 bad_arg"); return; }
        if (ntok >= 3 && !parse_int(tok[2], &pc)) { snprintf(resp, sz, "ERR 2147483659 bad_arg"); return; }
        if (!ensure_open()) { snprintf(resp, sz, "ERR 2147483649 open_failed"); return; }
        cmd_read_do(ps, pc, resp, sz);
    } else if (strcasecmp(tok[0], "SETDO") == 0) {
        int ps = 0;
        if (ntok < 3) { snprintf(resp, sz, "ERR 2147483657 missing_arg"); return; }
        if (!parse_int(tok[1], &ps)) { snprintf(resp, sz, "ERR 2147483659 bad_arg"); return; }
        if (!ensure_open()) { snprintf(resp, sz, "ERR 2147483649 open_failed"); return; }
        cmd_set_do(ps, ntok - 2, &tok[2], resp, sz);
    } else if (strcasecmp(tok[0], "SETBIT") == 0) {
        int port = 0, bit = 0, val = 0;
        if (ntok < 4) { snprintf(resp, sz, "ERR 2147483657 missing_arg"); return; }
        if (!parse_int(tok[1], &port) || !parse_int(tok[2], &bit) || !parse_int(tok[3], &val)) {
            snprintf(resp, sz, "ERR 2147483659 bad_arg"); return;
        }
        if (!ensure_open()) { snprintf(resp, sz, "ERR 2147483649 open_failed"); return; }
        cmd_set_bit(port, bit, val, resp, sz);
    } else if (strcasecmp(tok[0], "GETBIT") == 0) {
        int port = 0, bit = 0;
        if (ntok < 3) { snprintf(resp, sz, "ERR 2147483657 missing_arg"); return; }
        if (!parse_int(tok[1], &port) || !parse_int(tok[2], &bit)) {
            snprintf(resp, sz, "ERR 2147483659 bad_arg"); return;
        }
        if (!ensure_open()) { snprintf(resp, sz, "ERR 2147483649 open_failed"); return; }
        cmd_get_bit(port, bit, resp, sz);
    } else if (strcasecmp(tok[0], "WATCH") == 0) {
        int ms = 200, ps = 0, pc = 0;
        if (ntok >= 2 && !parse_int(tok[1], &ms)) { snprintf(resp, sz, "ERR 2147483659 bad_arg"); return; }
        if (ntok >= 3 && !parse_int(tok[2], &ps)) { snprintf(resp, sz, "ERR 2147483659 bad_arg"); return; }
        if (ntok >= 4 && !parse_int(tok[3], &pc)) { snprintf(resp, sz, "ERR 2147483659 bad_arg"); return; }
        if (!ensure_open()) { snprintf(resp, sz, "ERR 2147483649 open_failed"); return; }
        if (!g_di) { snprintf(resp, sz, "ERR 2147483650 di_not_available"); return; }
        if (ms < 20) ms = 20;
        if (ms > 60000) ms = 60000;
        if (pc <= 0 || pc > MAX_PORT_BUF) pc = (g_diPorts > 0 ? g_diPorts : 2);
        if (g_diPorts > 0 && ps + pc > g_diPorts) pc = g_diPorts - ps;
        if (pc <= 0) { snprintf(resp, sz, "ERR 2147483651 port_range_invalid"); return; }
        g_watchMs = ms;
        g_watchPort = (ps < 0) ? 0 : ps;
        g_watchCnt = pc;
        g_watching = 1;
        clock_gettime(CLOCK_MONOTONIC, &g_lastTick);
        snprintf(resp, sz, "OK WATCH");
    } else if (strcasecmp(tok[0], "UNWATCH") == 0) {
        g_watching = 0;
        snprintf(resp, sz, "OK UNWATCH");
    } else {
        snprintf(resp, sz, "ERR 2147483660 unknown_command '%s'", tok[0]);
    }
}

/*------------------------------------------------------------- server 模式 */
static char g_rbuf[8192];
static size_t g_rlen = 0;

/* 1=取到一行  0=暂无数据  -1=EOF */
static int read_line_nb(char *out, size_t outsz)
{
    ssize_t n;
    char *nl;
    size_t len;

    n = read(STDIN_FILENO, g_rbuf + g_rlen, sizeof(g_rbuf) - g_rlen - 1);
    if (n > 0) {
        g_rlen += (size_t)n;
        g_rbuf[g_rlen] = '\0';
    } else if (n == 0) {
        return -1;
    } else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
        return -1;
    }

    nl = memchr(g_rbuf, '\n', g_rlen);
    if (!nl) return 0;

    len = (size_t)(nl - g_rbuf);
    if (len >= outsz) len = outsz - 1;
    memcpy(out, g_rbuf, len);
    out[len] = '\0';

    memmove(g_rbuf, nl + 1, g_rlen - (size_t)(nl - g_rbuf) - 1);
    g_rlen -= (size_t)(nl - g_rbuf) + 1;
    return 1;
}

static int run_server(const char *initial_desc, int allow_respawn, int select_reply)
{
    char line[LINE_BUF];
    char resp[LINE_BUF];
    int flags, quit = 0;
    int opened = 0;

    g_allow_respawn = allow_respawn;

    /* 把 stdin 设为非阻塞, 以便在推送 DI 数据的同时接收新命令 */
    flags = fcntl(STDIN_FILENO, F_GETFL, 0);
    if (flags >= 0) fcntl(STDIN_FILENO, F_SETFL, flags | O_NONBLOCK);

    /* 自动选择第一个可用设备 */
    if (initial_desc && *initial_desc) {
        if (select_by_token(initial_desc)) opened = open_device();
    } else if (select_by_token(NULL)) {
        opened = open_device();
    }

    if (select_reply) {
        /* 本进程由上一次 SELECT 自替换而来, 首行必须是 SELECT 的应答 */
        if (opened) {
            printf("OK SELECT %s\n", desc_to_mb(g_desc));
        } else {
            char ebuf[160];
            err_to_str(g_diRet, ebuf, sizeof(ebuf));
            printf("ERR 2147483649 open_failed di=0x%X(%s) do=0x%X\n",
                   (unsigned)g_diRet, ebuf, (unsigned)g_doRet);
        }
        out_flush();
    } else {
        printf("READY %s\n", desc_to_mb(g_desc));
        out_flush();
    }

    while (!quit) {
        int r = read_line_nb(line, sizeof(line));
        if (r < 0) {
            if (!g_watching) break;      /* 非 watch 状态收到 EOF 就退出 */
            g_rlen = 0;
        } else if (r == 1) {
            handle_command(line, resp, sizeof(resp), &quit);
            if (resp[0]) {
                printf("%s\n", resp);
                out_flush();
            }
            if (g_respawn_to) {
                /* 自替换为新进程以重新取得设备权限(见 g_self 处注释)。
                   不传 --no-respawn: 后续再切换设备时仍需具备自替换能力。 */
                char *argv[5];
                int n = 0;
                struct timespec ts;
                argv[n++] = (char *)"bio1762_io";
                argv[n++] = (char *)"server";
                argv[n++] = (char *)"--select-reply";
                argv[n++] = (char *)g_respawn_to;
                argv[n] = NULL;
                fprintf(stderr, "[agent] respawn -> %s\n", g_respawn_to);
                fflush(stdout);
                fflush(stderr);
                ts.tv_sec = 0;
                ts.tv_nsec = 200 * 1000 * 1000;   /* 200ms, 让内核释放上一个句柄 */
                nanosleep(&ts, NULL);
                execv(g_self, argv);
                /* 只有自替换失败才会走到这里 */
                printf("ERR 2147483649 open_failed respawn_failed\n");
                out_flush();
                g_respawn_to = NULL;
            }
        } else {
            usleep(g_watching ? 1000 : 5000);
        }
        watch_tick();
    }

    close_all();
    printf("BYE\n");
    out_flush();
    return 0;
}

/*------------------------------------------------------------- 一次性命令 */
static void print_usage(const char *prog)
{
    printf("用法:\n");
    printf("  %s list\n", prog);
    printf("  %s info [描述子串]\n", prog);
    printf("  %s readdi [起始端口] [端口数]\n", prog);
    printf("  %s readdo [起始端口] [端口数]\n", prog);
    printf("  %s setdo  <端口> <十六进制值>\n", prog);
    printf("  %s setbit <端口> <位0-7> <0|1>\n", prog);
    printf("  %s selftest [描述子串]\n", prog);
    printf("  %s server [描述子串]\n", prog);
}

static void show_list(void)
{
    IArray *arr = DeviceCtrl_getInstalledDevices();
    int n, i;
    if (!arr) { printf("无法获取设备列表 (libbiodaq 初始化失败)\n"); return; }
    n = Array_getLength(arr);
    printf("已安装 DAQ 设备数量: %d\n", n);
    for (i = 0; i < n; ++i) {
        DeviceTreeNode *node = (DeviceTreeNode *)Array_getItem(arr, i);
        if (!node) continue;
        printf("  [%d] %s\n", i, desc_to_mb(node->Description));
    }
    if (n == 0)
        printf("  (未检测到设备, 请确认 PCI-1762 已插好且 bio1762 驱动已加载)\n");
}

static void show_info(void)
{
    char resp[LINE_BUF];
    cmd_info(resp, sizeof(resp));
    if (strncmp(resp, "OK", 2) != 0) {
        printf("打开设备失败: %s\n", resp);
        return;
    }
    printf("设备描述   : %s\n", desc_to_mb(g_desc));
    printf("DI 端口数  : %d (共 %d 路隔离数字量输入)\n", g_diPorts, g_diPorts * 8);
    printf("DO 端口数  : %d (共 %d 路继电器输出)\n", g_doPorts, g_doPorts * 8);
}

static int one_shot(int argc, char **argv)
{
    const char *cmd = argv[1];
    char resp[LINE_BUF];

    if (strcasecmp(cmd, "list") == 0) {
        show_list();
        return 0;
    }

    if (strcasecmp(cmd, "info") == 0) {
        if (argc >= 3) { select_by_token(argv[2]); open_device(); }
        show_info();
        return 0;
    }

    if (strcasecmp(cmd, "readdi") == 0) {
        int ps = (argc >= 3) ? atoi(argv[2]) : 0;
        int pc = (argc >= 4) ? atoi(argv[3]) : 2;
        ensure_open();
        cmd_read_di(ps, pc, resp, sizeof(resp));
        printf("%s\n", resp);
        return strncmp(resp, "OK", 2) == 0 ? 0 : 1;
    }

    if (strcasecmp(cmd, "readdo") == 0) {
        int ps = (argc >= 3) ? atoi(argv[2]) : 0;
        int pc = (argc >= 4) ? atoi(argv[3]) : 2;
        ensure_open();
        cmd_read_do(ps, pc, resp, sizeof(resp));
        printf("%s\n", resp);
        return strncmp(resp, "OK", 2) == 0 ? 0 : 1;
    }

    if (strcasecmp(cmd, "setdo") == 0) {
        if (argc < 4) { print_usage(argv[0]); return 2; }
        ensure_open();
        cmd_set_do(atoi(argv[2]), 1, &argv[3], resp, sizeof(resp));
        printf("%s\n", resp);
        return strncmp(resp, "OK", 2) == 0 ? 0 : 1;
    }

    if (strcasecmp(cmd, "setbit") == 0) {
        if (argc < 5) { print_usage(argv[0]); return 2; }
        ensure_open();
        cmd_set_bit(atoi(argv[2]), atoi(argv[3]), atoi(argv[4]), resp, sizeof(resp));
        printf("%s\n", resp);
        return strncmp(resp, "OK", 2) == 0 ? 0 : 1;
    }

    if (strcasecmp(cmd, "selftest") == 0) {
        int pi, fail = 0;
        uint8 wbuf[8], rbuf[8];
        ErrorCode ret;

        if (argc >= 3) { select_by_token(argv[2]); }
        if (!ensure_open()) { printf("[失败] 打开设备失败 di=0x%X do=0x%X\n",
                                     (unsigned)g_diRet, (unsigned)g_doRet); return 1; }

        printf("=== PCI-1762 自检报告 ===\n");
        printf("设备          : %s\n", desc_to_mb(g_desc));
        printf("DI 端口/点数  : %d / %d\n", g_diPorts, g_diPorts * 8);
        printf("DO 端口/点数  : %d / %d\n", g_doPorts, g_doPorts * 8);

        if (g_do) {
            for (pi = 0; pi < g_doPorts; ++pi) {
                wbuf[0] = 0x00;
                ret = InstantDoCtrl_WriteAny(g_do, pi, 1, wbuf);
                if (BioFailed(ret)) { printf("[失败] DO 端口 %d 写 0x00 错误 0x%X\n", pi, (unsigned)ret); fail = 1; continue; }
                do_shadow_mark(pi, wbuf[0]);
                memset(rbuf, 0xFF, sizeof(rbuf));
                do_read_stable(pi, 1, rbuf);
                printf("[%s] DO 端口 %d 写 0x00 -> 回读 0x%02X\n",
                       rbuf[0] == 0x00 ? "通过" : "差异", pi, rbuf[0]);

                wbuf[0] = 0xFF;
                ret = InstantDoCtrl_WriteAny(g_do, pi, 1, wbuf);
                if (BioFailed(ret)) { printf("[失败] DO 端口 %d 写 0xFF 错误 0x%X\n", pi, (unsigned)ret); fail = 1; continue; }
                do_shadow_mark(pi, wbuf[0]);
                memset(rbuf, 0x00, sizeof(rbuf));
                do_read_stable(pi, 1, rbuf);
                printf("[%s] DO 端口 %d 写 0xFF -> 回读 0x%02X\n",
                       rbuf[0] == 0xFF ? "通过" : "差异", pi, rbuf[0]);
            }
            /* 复位为 0 */
            wbuf[0] = 0;
            InstantDoCtrl_WriteAny(g_do, 0, 1, wbuf);
            do_shadow_mark(0, wbuf[0]);
        } else {
            printf("[跳过] DO 不可用\n");
        }

        if (g_di) {
            cmd_read_di(0, g_diPorts > 0 ? g_diPorts : 2, resp, sizeof(resp));
            printf("DI 当前状态   : %s\n", resp);
        } else {
            printf("[跳过] DI 不可用\n");
        }

        printf("=== 自检结束: %s ===\n", fail ? "存在失败项" : "全部通过");
        close_all();
        return fail;
    }

    if (strcasecmp(cmd, "server") == 0) {
        return run_server(argc >= 3 ? argv[2] : NULL, 1, 0);
    }

    print_usage(argv[0]);
    return 2;
}

/*------------------------------------------------------------- 入口 */
int main(int argc, char **argv)
{
    const char *desc = NULL;
    int allow_respawn = 1, select_reply = 0;
    ssize_t n;
    int i;

    setlocale(LC_ALL, "");
    signal(SIGPIPE, SIG_IGN);

    /* 记录自身路径, 供 SELECT 打不开设备时 execv 自替换使用 */
    n = readlink("/proc/self/exe", g_self, sizeof(g_self) - 1);
    if (n > 0) {
        g_self[n] = '\0';
    } else if (argc > 0 && argv[0]) {
        snprintf(g_self, sizeof(g_self), "%s", argv[0]);
    }

    if (argc >= 2 && (strcasecmp(argv[1], "--server") == 0 ||
                      strcasecmp(argv[1], "-s") == 0 ||
                      strcasecmp(argv[1], "server") == 0)) {
        for (i = 2; i < argc; ++i) {
            if (strcmp(argv[i], "--no-respawn") == 0) {
                allow_respawn = 0;
            } else if (strcmp(argv[i], "--select-reply") == 0) {
                select_reply = 1;
            } else if (!desc) {
                desc = argv[i];
            }
        }
        return run_server(desc, allow_respawn, select_reply);
    }

    if (argc >= 2) return one_shot(argc, argv);

    return run_server(NULL, 1, 0);
}
