# 研华 I/O 卡 手机控制台（安卓版）

把桌面版《研华I/O卡安装与测试工具》完整搬到手机上：**驱动安装/卸载、设备枚举、
DI 实时监视、继电器输出、逐位自检、测试报告导出**，全部功能与桌面版一一对应，
界面则按安卓习惯重新设计（底部导航、大触控目标、底部弹出层、振动反馈、深色模式）。

手机端是一个**可安装的 Web 应用（PWA）**：在安卓 Chrome 里打开后选择
【添加到主屏幕】，桌面即出现独立图标，全屏运行、可离线打开界面——无需应用市场、
无需签名、无需数据线调试，更新时服务端一换全端生效。

```
手机(安卓/PWA) ──HTTP+SSE──▶ bridge.py 桥接服务 ──▶ 目标 Linux(研华 PCI-1762/1761)
                                  ├─ 远程模式: 通过 SSH 操作目标机(桥接跑在 PC/服务器)
                                  └─ 本机模式: 直接跑在目标机上, 免 SSH(推荐)
```

> 手机浏览器不能直接建立 SSH/TCP 连接，因此由桥接服务承担实际的网络工作；
> 手机端只负责界面。桥接逻辑 100% 复用桌面版经过实机验证的
> `daq_remote`（SSH 噪声过滤、PCI-1761 权限缺陷规避、回读收敛等全部保留）。

---

## 一、两种部署方式

### 方式 A（推荐）：控制台直接装在目标机上（本机模式，现场零电脑）

带 I/O 卡的目标机常年开机，把桥接服务装在它上面最省事——现场只需要一部手机。

在目标机上执行一次：

```bash
# 1. 把整个项目目录拷到目标机
scp -r PCI1762_RemoteTool/ root@<目标机IP>:/root/daq_tool

# 2. 目标机上以 root 安装(自动装依赖、写 systemd、放行端口、开机自启)
cd /root/daq_tool && sudo bash android/install_phone_console.sh
```

完成后手机浏览器打开 `http://<目标机IP>:8080` 即可。服务异常时：

```bash
systemctl restart daq-console     # 重启服务
journalctl -u daq-console -f      # 查看日志
```

> 本机模式下无需 SSH；"重启目标机"后手机端会短暂离线约 40 秒，服务会随系统自启。

### 方式 B：桥接跑在 Windows 电脑上（远程 SSH 模式）

目标机不方便装东西时，在同网段的 Windows 电脑上双击：

```
android\启动手机控制台.bat
```

启动后窗口里会列出可访问地址（如 `http://192.168.1.50:8080`），
手机与该电脑同一网络即可打开。此模式下需在手机端【连接】页填写目标机
SSH 信息（与桌面版一致），驱动安装包也可直接从手机上传（约 23MB）。

---

## 二、安装到手机桌面（安卓）

1. 手机 Chrome 打开控制台地址；
2. 菜单（⋮）→ **添加到主屏幕/安装应用**；
3. 桌面出现「IO检测」图标，点开即全屏运行，与原生 App 无异；
4. 界面缓存已离线保存，现场网络不佳时也能打开（实时数据仍需网络）。

---

## 三、界面与桌面版的功能对照

| 桌面版 | 手机版 | 说明 |
| --- | --- | --- |
| 连接配置页 | 底部导航【连接】 | 主机/端口/账号/密码/私钥/工作目录，记住密码；连接状态卡显示内核与权限 |
| 驱动安装/卸载页 | 【驱动】 | 卡型号分段选择（1762/1761/两块都装）、安装包管理与**手机直传**、四个部署选项开关、一键安装/检测/验证/重启/卸载、测试代理部署 |
| IO 测试页 | 【测试】 | DI 实时监视（滑杆调间隔）、DO 继电器点按 + 长按备注、全部吸合/释放/反相/回读、按字节写入、功能自检与报告导出 |
| 运行日志 | 【日志】 | 彩色控制台、自动滚动开关、清空；部署进度条常驻顶栏 |

按安卓习惯做的适配：

* 底部导航 + 单手可达的操作布局，主按钮均 ≥48dp；
* 点位备注改为**底部弹出层**（桌面版是双击弹窗）；
* 继电器切换单点即动作，**长按**设备注，带振动反馈；
* 顶栏常驻细进度条 + 当前步骤文字，任务进行中按钮自动锁定；
* 全局 Snackbar 轻提示；危险操作（全部吸合/卸载/重启）二次确认；
* 深色模式跟随系统；刘海屏/手势条安全区适配；横屏时点位栅格自动加宽。

---

## 四、目录结构

```
android/
├─ bridge.py                     桥接服务 (HTTP+SSE, 纯标准库, 复用 daq_remote)
├─ local_shell.py                本机 Shell 适配层(让部署/IO逻辑在本机模式原样复用)
├─ web/                          手机端 PWA
│  ├─ index.html / app.css / app.js
│  ├─ manifest.webmanifest       安装清单(独立图标/全屏/主题色)
│  ├─ sw.js                      离线缓存(界面可离线打开)
│  └─ icon-*.png                 图标(android/tools/make_icons.py 生成)
├─ tools/make_icons.py           图标生成脚本(纯标准库, 无需 Pillow)
├─ install_phone_console.sh      目标机一键安装(systemd 开机自启)
└─ 启动手机控制台.bat             Windows 远程模式启动器
```

## 五、接口一览（调试用）

```
GET  /api/state            全量状态快照        GET  /api/log/history   日志回放
GET  /api/events           SSE 实时流(日志/进度/DI/DO/状态)
GET  /api/report           下载测试报告        GET  /api/devices       枚举设备(触发任务)
POST /api/connect|disconnect                   POST /api/upload        上传 .run(原始字节)
POST /api/probe|deploy|uninstall|verify|reboot POST /api/agent/deploy|remove
POST /api/io/start|stop    POST /api/di/read   POST /api/do/read
POST /api/do/bit|all|invert|bytes               POST /api/watch/start|stop
POST /api/selftest         POST /api/annotations {key, name}
```

## 六、自测

```bash
# 1. 起模拟目标机与桥接服务
python dev/mock_linux_ssh.py --port 2299
python android/bridge.py --port 8081
# 2. API 全链路(上传->连接->部署->IO->自检->报告, 12 组断言)
python dev/mobile_e2e.py
# 3. 手机视口(412x915)截图与交互, 浅色+深色共 14 张
node dev/ui_mobile_shot.mjs
```

## 七、常见问题

| 现象 | 处理 |
| --- | --- |
| 手机打不开控制台 | 确认手机与桥接服务在同一网络；Windows 端注意防火墙放行 8080 |
| 提示"服务断开" | 桥接服务未运行或端口变了；刷新页面，或检查 systemctl status daq-console |
| 【添加到主屏幕】灰色 | 需通过 http(s) 正常打开页面一次；个别国产浏览器入口叫"添加到桌面" |
| 上传安装包很慢 | 23MB 走手机 Wi-Fi 一般几秒~几十秒；同型号同大小包在目标机已存在时会自动跳过上传 |
| 本机模式点"重启目标机" | 服务随系统自启，手机端约 40 秒后重新可访问 |
| 安卓低版本浏览器界面异常 | 建议使用系统 Chrome 或 Edge；界面不需要任何插件 |

---

## 安装/运行验证码（V2.2.0 新增）

- 软件（PWA 和安卓 APP）首次安装启动时必须输入**校验码**激活；之后每次运行自动校验激活状态，未激活/换机会重新弹出激活页。
- 机器码：APP 内取安卓设备号；浏览器内取本浏览器持久化 ID。激活页可一键复制发给管理员。
- **校验码生成工具**：`tools/校验码生成工具.py`（独立通用版，读码器性能评测工具也可用）
  - 图形界面：`python tools/校验码生成工具.py`
  - 命令行：`python tools/校验码生成工具.py <机器码>`
  - 算法：SHA-256(密钥|机器码) → Base32 前 16 位 → XXXX-XXXX-XXXX-XXXX，离线校验。
- 版本显示区（顶栏副标题/报告/激活页）末尾显示 **开发者：陈宠**。

## 安卓 APP（APK）

- 工程：`android-apk/`（WebView 壳 + WebViewAssetLoader，界面与 PWA 同一套资产，`tools/sync_apk_assets.py` 同步）。
- 云端构建：GitHub 仓库 `closerose-Tech/io-console-apk`，Actions 自动构建（推送 main 或 v* 标签触发）。
- 成品：`输出目录/研华IO卡检测控制台_v2.2.0_debug.apk`（debug 签名，可直接安装）。
- APP 首次启动需填写**控制台服务地址**（桥接服务所在机器 IP:端口），连接页也可修改。

---

## V2.3.0 变更

1. **去掉服务地址配置**：桥接服务通过 Chaquopy 内嵌进 APP（paramiko 直连目标主机），
   打开软件即与桌面版一致：填写目标主机 → 连接 → 检测，不再需要任何中间服务器。
2. **顶部单位横幅**：最上侧显示"中烟信息业保中心"（深黄色，浅色/深色主题自适应）。
3. **命令交互页**：底部导航新增【命令】页，可向目标主机手工输入命令、发送并查看
   stdout/stderr/退出码/耗时；内置常用命令快捷 chips、↑↓ 历史命令（＃按钮）。
   接口：POST /api/cmd（未连接时拒绝）。
4. APK 架构：Chaquopy(Python 3.8) 在手机本地跑 bridge.py 于 127.0.0.1:8080，
   WebView 直接加载本地服务；构建需 ndk.abiFilters（arm64-v8a + armeabi-v7a）。
   改动代码后：`python tools/sync_apk_assets.py` → 推送仓库自动构建。

## V2.3.1 闪退修复

- **根因**：v2.3.0 用的 Chaquopy 15 + Python 3.8，其原生库为 4KB 内存页对齐；
  2025 年后的新手机（Android 15+，16KB 内存页内核）加载时 `dlopen failed`，一打开就闪退。
- **修复**：升级 Chaquopy 17.2.0 + Python 3.13（仅保留 arm64-v8a，32 位不支持 16KB 对齐），
  全部 21 个 .so 已验证 16KB 对齐。
- **加固**：启动流程移入后台线程（避免 ANR）；启动异常不再闪退，
  会在页面显示完整错误堆栈，方便截图反馈。
- 若老设备安装后提示不再支持，属预期（仅支持 64 位手机，2017 年后机型均满足）。

## V2.3.2 启动卡死排查加固

- 现象: 安装后停留在灰屏(启动占位页, 中文未渲染是 loadData 的编码缺陷)。
- 改进:
  1. 启动过程改为**分阶段实时进度页**(每步显示耗时): 界面资产 → Python 引擎
     → 桥接模块(paramiko/加密库首次导入较慢) → 本地服务 → 等待就绪;
  2. 占位页改用 loadDataWithBaseURL, 中文正常显示;
  3. 60 秒看门狗: 超时或异常时显示「设备信息 + 各阶段耗时 + 错误堆栈」,
     用户截图即可精确定位卡在哪一步, 不会无限灰屏。

## V2.4.1 修复「未发现设备」

- 现象: 连接成功后点【刷新设备】提示未发现设备, 日志报 FileNotFoundError:
  `.../chaquopy/AssetFinder/app/remote_src/bio1762_io.c`。
- 根因: sync_apk_assets.py 只复制了 `daq_remote/*.py`, 漏了远程测试代理源码
  `remote_src/`(bio1762_io.c + Makefile)。枚举设备/启动会话需先把它上传到
  目标机编译, 包里没有该文件即异常。
- 修复: 同步脚本改为把 `remote_src/` 一并打进 APK(位于 python 源集根目录,
  Chaquopy 会解压到 AssetFinder/app, 与 assets.py 的定位路径一致)。

## V2.4.0 刷新设备修复 · 命令页增强 · 内置驱动

- 修复【测试】页点【刷新设备】必报「无法连接控制台服务」的问题:
  fetch 规范不允许 GET 请求携带 body, `{}` body 会让请求在发出前就抛 TypeError,
  被兜底逻辑误判为服务断开(服务本身一直是通的)。改为无 body 的 GET 即可。
- 【命令】页新增「复制内容」「清空窗口」按钮(复制带 execCommand 兜底)。
- 驱动安装包直接内置于 APP(assets/driver/DAQ_Linux_20260306_64bit.run, 约 22.3MB):
  启动时按大小清单解压到 filesDir/uploads, 驱动页自动识别, 无需再从手机上传。
- 版本署名统一改为「开发者：陈宠」(原「设计开发人」在手机顶栏显示不全)。

## V2.3.3 修复「界面资产解压失败」

- 现象: Android 17 (API 37) 新机型上报「界面资产解压失败 / EISDIR」。
- 根因: AssetManager.list() 在该系统上对 assets 子目录返回空,
  解压逻辑把目录当文件打开导致 EISDIR。
- 修复: 改为清单文件方案 —— sync_apk_assets.py 生成 assets/web/filelist.txt,
  MainActivity 按清单逐个复制, 不再依赖 AssetManager.list()。
  (注意: 清单不能用点开头文件名, aapt 默认忽略规则会剔除隐藏文件。)
