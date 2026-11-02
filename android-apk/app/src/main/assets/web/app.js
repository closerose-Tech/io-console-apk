/* =========================================================================
   研华 I/O 卡手机控制台 —— 前端逻辑
   与后端通过 REST 交互, 实时日志/进度/DI 采样走 SSE(/api/events)。
   无框架、无构建步骤, 便于直接放进 PWA 缓存离线运行。
   ========================================================================= */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const S = {
  state: null,
  tab: 'conn',
  sub: 'di',
  online: false,
  firstRender: true,
  logCount: 0,
  autoscroll: true,
  selDevice: '',
  diCells: [],
  doCells: [],
  byteInputs: [],
  diCount: -1,
  doCount: -1,
  doPorts: -1,
  sheetKey: '',
  cmdHist: [],
  cmdHistIdx: -1,
  lastJob: '',
  wasBusy: false,
};

/* ------------------------------------------------------------------ 基础 */
async function api(path, body, method) {
  const opt = {
    method: method || (body === undefined ? 'GET' : 'POST'),
    headers: {},
    cache: 'no-store',
  };
  if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  let resp;
  try {
    resp = await fetch(window.Activation ? Activation.url(path) : path, opt);
  } catch (e) {
    setOnline(false);
    throw new Error('无法连接控制台服务');
  }
  setOnline(true);
  const data = await resp.json().catch(() => ({ ok: false, error: '响应解析失败' }));
  if (!resp.ok || data.ok === false) throw new Error(data.error || ('请求失败 ' + resp.status));
  return data;
}

function setOnline(v) {
  if (S.online === v) return;
  S.online = v;
  if (!v) {
    $('#connChip').className = 'chip err';
    $('#connChip').textContent = '服务断开';
  }
}

let snackTimer = null;
function toast(msg, kind = '') {
  const el = $('#snack');
  el.textContent = msg;
  el.className = 'snackbar show ' + kind;
  clearTimeout(snackTimer);
  snackTimer = setTimeout(() => { el.className = 'snackbar ' + kind; }, 2400);
}

function buzz(ms = 12) {
  try { if (navigator.vibrate) navigator.vibrate(ms); } catch (e) { /* 部分浏览器不支持 */ }
}

function fail(e) {
  const msg = (e && e.message) || String(e);
  toast(msg, 'err');
  buzz(28);
}

/* ------------------------------------------------------------------ 导航 */
function showTab(name) {
  S.tab = name;
  $$('.page').forEach(p => { p.hidden = p.dataset.page !== name; });
  $$('#bottomNav button').forEach(b => b.classList.toggle('on', b.dataset.tab === name));
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function showSub(name) {
  S.sub = name;
  $$('#ioSeg button').forEach(b => b.classList.toggle('on', b.dataset.sub === name));
  ['di', 'do', 'test'].forEach(k => { $('#sub-' + k).hidden = k !== name; });
}

/* ------------------------------------------------------------------ 渲染 */
function render(st) {
  S.state = st;
  renderHeader(st);
  renderConn(st);
  renderDeploy(st);
  renderIO(st);
  renderCmdState(st);
  if (S.firstRender) {
    fillForm(st.config || {});
    S.firstRender = false;
  }
}

function renderCmdState(st) {
  const tag = $('#cmdState');
  if (!tag) return;
  if (st.connected || st.mode === 'local') {
    tag.className = 'tag ok';
    tag.textContent = '已连接';
  } else {
    tag.className = 'tag';
    tag.textContent = '未连接';
  }
}

function renderHeader(st) {
  const chip = $('#connChip');
  if (!S.online) {
    chip.className = 'chip err';
    chip.textContent = '服务断开';
  } else if (st.busy) {
    chip.className = 'chip busy';
    chip.textContent = jobName(st.job);
  } else if (st.mode === 'local' || st.connected) {
    chip.className = 'chip ok';
    chip.textContent = st.io_running ? '会话中' : '已连接';
  } else {
    chip.className = 'chip';
    chip.textContent = '未连接';
  }

  const parts = [];
  parts.push(st.mode === 'local' ? '本机直连' : (st.config.host || '未设置主机'));
  parts.push('v' + st.version);
  parts.push('开发者：陈宠');
  $('#subtitle').textContent = parts.join(' · ');

  const wrap = $('#progWrap');
  const pct = st.busy ? (st.progress.pct || 0) : 0;
  if (st.busy) {
    wrap.hidden = false;
    $('#progBar').style.width = Math.max(4, pct * 100) + '%';
    $('#progText').textContent = st.progress.msg || '处理中…';
  } else {
    wrap.hidden = true;
  }
}

function jobName(job) {
  return ({
    connect: '连接中', probe: '检测环境', deploy: '安装驱动', uninstall: '卸载驱动',
    verify: '验证中', reboot: '重启中', devices: '枚举设备', 'io-start': '启动会话',
    'agent': '部署代理', 'agent-remove': '删除代理', selftest: '自检中',
  })[job] || '处理中';
}

/* ---------------------------------------------------------- 命令交互 */
const CMD_CHIPS = [
  'uname -a', 'lspci | grep -i advantech', 'lspci -d 13fe:', 'dmesg | tail -20',
  'ip a', 'free -m', 'df -h', 'lsmod',
];
const CMD_HIST_KEY = 'cmd_history';

function pushCmdHist(cmd) {
  S.cmdHist = S.cmdHist.filter(c => c !== cmd);
  S.cmdHist.unshift(cmd);
  if (S.cmdHist.length > 30) S.cmdHist.length = 30;
  try { localStorage.setItem(CMD_HIST_KEY, JSON.stringify(S.cmdHist)); } catch (e) { /* 忽略 */ }
}

function initCmd() {
  try { S.cmdHist = JSON.parse(localStorage.getItem(CMD_HIST_KEY) || '[]'); }
  catch (e) { S.cmdHist = []; }

  const chips = $('#cmdChips');
  CMD_CHIPS.forEach(c => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'chip-btn';
    b.textContent = c;
    b.addEventListener('click', () => { $('#cmdInput').value = c; sendCmd(); });
    chips.appendChild(b);
  });

  $('#cmdSend').addEventListener('click', sendCmd);
  const inp = $('#cmdInput');
  inp.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); sendCmd(); return; }
    if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
      e.preventDefault();
      if (!S.cmdHist.length) return;
      if (e.key === 'ArrowUp') S.cmdHistIdx = Math.min(S.cmdHistIdx + 1, S.cmdHist.length - 1);
      else S.cmdHistIdx = Math.max(S.cmdHistIdx - 1, -1);
      inp.value = S.cmdHistIdx >= 0 ? S.cmdHist[S.cmdHistIdx] : '';
    }
  });
  $('#cmdHist').addEventListener('click', () => {
    if (!S.cmdHist.length) { toast('还没有历史命令'); return; }
    S.cmdHistIdx = 0;
    inp.value = S.cmdHist[0];
    inp.focus();
    toast('再按 ↑ 键可翻更早的命令');
  });

  // 清空命令窗口
  $('#cmdClear').addEventListener('click', () => {
    $('#cmdOut').innerHTML = '<div class="cmd-hint">已清空。继续输入命令即可。</div>';
    buzz(10);
    toast('命令窗口已清空');
  });

  // 复制命令窗口全部内容
  $('#cmdCopy').addEventListener('click', async () => {
    const text = $('#cmdOut').innerText.trim();
    if (!text) { toast('命令窗口还没有内容'); return; }
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        // 兜底: 临时 textarea + execCommand(旧 WebView 可能没有 async clipboard)
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.cssText = 'position:fixed;opacity:0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        ta.remove();
      }
      buzz(10);
      toast('已复制到剪贴板', 'ok');
    } catch (e) {
      toast('复制失败: ' + (e && e.message || e), 'err');
    }
  });
}

function appendCmd(text, cls) {
  const out = $('#cmdOut');
  const div = document.createElement('div');
  if (cls) div.className = cls;
  div.textContent = text;
  out.appendChild(div);
  out.scrollTop = out.scrollHeight;
}

function sendCmd() {
  const inp = $('#cmdInput');
  const cmd = inp.value.trim();
  if (!cmd) return;
  if (!S.state || !S.state.connected) {
    toast('请先在【连接】页连接目标主机', 'err');
    return;
  }
  pushCmdHist(cmd);
  S.cmdHistIdx = -1;
  appendCmd('$ ' + cmd, 'cmd-line');
  inp.value = '';
  const busy = document.createElement('div');
  busy.className = 'cmd-hint';
  busy.id = 'cmdBusy';
  busy.textContent = '执行中…';
  $('#cmdOut').appendChild(busy);
  $('#cmdOut').scrollTop = $('#cmdOut').scrollHeight;

  api('/api/cmd', { cmd: cmd, timeout: 120 })
    .then(r => {
      const b = $('#cmdBusy'); if (b) b.remove();
      if (r.stdout && r.stdout.trim()) appendCmd(r.stdout.replace(/\n$/, ''), '');
      if (r.stderr && r.stderr.trim()) appendCmd(r.stderr.replace(/\n$/, ''), 'cmd-err');
      const e = document.createElement('div');
      e.className = 'cmd-exit' + (r.exit ? ' bad' : '');
      e.textContent = '[退出码 ' + r.exit + '] 用时 ' + (r.duration || 0) + 's';
      $('#cmdOut').appendChild(e);
      $('#cmdOut').scrollTop = $('#cmdOut').scrollHeight;
    })
    .catch(err => {
      const b = $('#cmdBusy'); if (b) b.remove();
      appendCmd('执行失败: ' + err.message, 'cmd-err');
    });
}

/* ---------------------------------------------------------- 连接页 */
function fillForm(cfg) {
  const set = (id, v) => {
    const el = $(id);
    if (el && document.activeElement !== el) el.value = v == null ? '' : v;
  };
  set('#fHost', cfg.host);
  set('#fPort', cfg.port || '22');
  set('#fUser', cfg.user || 'root');
  set('#fPass', cfg.password || '');
  set('#fKey', cfg.key);
  set('#fWorkdir', cfg.workdir);
  $('#fRemember').checked = !!cfg.remember;
}

function renderConn(st) {
  const local = st.mode === 'local';
  $('#localNotice').hidden = !local;
  $('#connForm').hidden = local;

  $('#kvMode').textContent = local ? '本机（目标机内运行）' : '远程 SSH';
  $('#kvState').textContent = st.connected ? '已连接' : '未连接';
  $('#kvState').style.color = st.connected ? 'var(--ok)' : 'var(--err)';
  $('#kvSys').textContent = st.shell_info || '—';
  $('#kvSudo').textContent = st.connected
    ? (st.sudo_ok ? 'root / sudo 可用' : '权限不足')
    : '—';
  $('#kvSudo').style.color = st.connected && !st.sudo_ok ? 'var(--warn)' : '';

  const cfg = st.config || {};
  if (!document.activeElement || !document.activeElement.matches('input')) {
    fillForm(cfg);
  }
}

/* ---------------------------------------------------------- 驱动页 */
function currentModels() {
  const on = $('#modelSeg button.on');
  const v = on ? on.dataset.model : 'PCI-1762';
  if (v === 'both') return ['PCI-1762', 'PCI-1761'];
  return [v];
}

const MODEL_HINTS = {
  'PCI-1762': '驱动 pci1762 / 内核模块 bio1762',
  'PCI-1761': '驱动 pci1761_pcm3761i / 内核模块 bio1761',
  'both': '两个驱动一起安装，适合机箱内同时插了两块卡',
};

function renderDeploy(st) {
  const cfg = st.config || {};
  const models = cfg.models || ['PCI-1762'];
  const key = models.length > 1 ? 'both' : models[0];
  $$('#modelSeg button').forEach(b => b.classList.toggle('on', b.dataset.model === key));
  $('#modelHint').textContent = MODEL_HINTS[key] || '';

  // 开关只在用户没动过的时候跟随配置
  const sw = (id, v) => { const el = $(id); if (el) el.checked = v !== false; };
  if (S.firstRender || document.activeElement !== $('#optDeps')) sw('#optDeps', cfg.install_deps);
  if (S.firstRender || document.activeElement !== $('#optSrc')) sw('#optSrc', cfg.update_source);
  if (S.firstRender || document.activeElement !== $('#optBlack')) sw('#optBlack', cfg.blacklist);
  if (S.firstRender || document.activeElement !== $('#optReboot')) sw('#optReboot', cfg.reboot);

  const list = $('#instList');
  const cur = st.installer || '';
  const items = st.installers || [];
  const sig = items.map(i => i.path + i.size).join('|') + '#' + cur;
  if (list.dataset.sig !== sig) {
    list.dataset.sig = sig;
    list.innerHTML = '';
    items.forEach(it => {
      const d = document.createElement('div');
      d.className = 'installer' + (it.path === cur ? ' on' : '');
      d.dataset.path = it.path;
      d.innerHTML = '<span class="dot"></span>' +
        '<span class="name"></span><span class="size"></span>';
      d.querySelector('.name').textContent = it.name;
      d.querySelector('.size').textContent = (it.size / 1048576).toFixed(1) + ' MB';
      d.addEventListener('click', async () => {
        try {
          await api('/api/config', { installer: it.path });
          toast('已选择 ' + it.name, 'ok');
          refresh();
        } catch (e) { fail(e); }
      });
      list.appendChild(d);
    });
  }
  $('#instTag').textContent = cur ? '已选择' : '未选择';
  $('#instTag').className = 'tag' + (cur ? ' ok' : '');

  const locked = st.busy || !st.connected;
  ['#btnDeploy', '#btnProbe', '#btnVerify', '#btnReboot', '#btnUninstall',
   '#btnAgent', '#btnAgentDel', '#btnUpload'].forEach(sel => {
    const el = $(sel);
    if (el) el.disabled = locked;
  });
}

/* ---------------------------------------------------------- 测试页 */
function renderIO(st) {
  const dev = st.device;
  const running = !!st.io_running;

  $('#devTitle').textContent = dev ? dev.desc : (running ? '已启动' : '未启动会话');
  $('#devSub').textContent = dev
    ? `${dev.model} · DI ${dev.di_lines} 路 / 继电器 ${dev.do_lines} 路`
    : (st.connected ? '点【刷新设备】枚举板卡' : '请先在【连接】页建立连接');
  const tag = $('#ioTag');
  tag.textContent = running ? (st.watching ? '监视中' : '会话中') : '空闲';
  tag.className = 'tag' + (running ? ' ok' : '');

  // 设备 chips
  const chips = $('#devChips');
  const devices = st.devices || [];
  const sig = devices.join('|');
  if (chips.dataset.sig !== sig) {
    chips.dataset.sig = sig;
    chips.innerHTML = '';
    devices.forEach(d => {
      const b = document.createElement('button');
      b.textContent = d;
      b.dataset.dev = d;
      if (d === S.selDevice) b.classList.add('on');
      b.addEventListener('click', () => {
        S.selDevice = d;
        $$('#devChips button').forEach(x => x.classList.toggle('on', x.dataset.dev === d));
        if (st.io_running) toast('已选中，结束当前会话后重新启动生效');
      });
      chips.appendChild(b);
    });
  }

  $('#btnIoStart').disabled = st.busy || !st.connected;
  $('#btnIoStop').disabled = !running;
  $('#btnRefresh').disabled = st.busy || !st.connected;

  buildGrids(dev);
  paintDi(st.di || []);
  paintDo(st.do || []);
  paintHex(st.di || [], st.do || [], dev);

  const wb = $('#btnWatch');
  wb.textContent = st.watching ? '停止实时监视' : '开始实时监视';
  wb.disabled = !running;
  $('#btnWatch').classList.toggle('filled', !st.watching);
  $('#btnWatch').classList.toggle('danger', st.watching);
  ['#btnReadDi', '#btnReadDo', '#btnAllOn', '#btnAllOff', '#btnInvert',
   '#btnWriteBytes', '#btnSelftest'].forEach(sel => { $(sel).disabled = !running; });

  // 自检结果
  const res = $('#selfResult');
  if (st.report && st.report.lines > 0) {
    res.hidden = false;
  }
}

function buildGrids(dev) {
  const diCount = dev ? dev.di_lines : 0;
  const doCount = dev ? dev.do_lines : 0;
  const doPorts = dev ? dev.do_ports : 0;

  if (diCount !== S.diCount) {
    S.diCount = diCount;
    const g = $('#diGrid');
    g.innerHTML = '';
    S.diCells = [];
    for (let i = 0; i < diCount; i++) {
      const d = document.createElement('div');
      d.className = 'di';
      d.innerHTML = '<span class="led"></span><b>DI' + i + '</b><small></small>';
      d.addEventListener('click', () => openSheet('DI' + i, 'DI' + i));
      g.appendChild(d);
      S.diCells.push(d);
    }
    if (!diCount) g.innerHTML = '<p class="muted" style="grid-column:1/-1">未检测到 DI 通道</p>';
  }

  if (doCount !== S.doCount) {
    S.doCount = doCount;
    const g = $('#doGrid');
    g.innerHTML = '';
    S.doCells = [];
    for (let i = 0; i < doCount; i++) {
      const b = document.createElement('button');
      b.className = 'relay';
      b.innerHTML = '<span class="k">K' + i + '</span>' +
        '<span class="st">OFF</span><span class="note"></span>';
      tapOrLong(b,
        () => toggleRelay(i),
        () => openSheet('K' + i, 'K' + i));
      g.appendChild(b);
      S.doCells.push(b);
    }
    if (!doCount) g.innerHTML = '<p class="muted" style="grid-column:1/-1">未检测到继电器输出</p>';
  }

  if (doPorts !== S.doPorts) {
    S.doPorts = doPorts;
    const row = $('#byteRow');
    row.innerHTML = '';
    S.byteInputs = [];
    for (let p = 0; p < doPorts; p++) {
      const lab = document.createElement('label');
      lab.innerHTML = '<span>端口 ' + p + '</span>' +
        '<input type="text" inputmode="text" maxlength="2" value="00">';
      row.appendChild(lab);
      S.byteInputs.push(lab.querySelector('input'));
    }
  }

  // 备注名
  const anno = S.state ? (S.state.annotations || {}) : {};
  S.diCells.forEach((el, i) => {
    el.querySelector('small').textContent = anno['DI' + i] || '';
  });
  S.doCells.forEach((el, i) => {
    el.querySelector('.note').textContent = anno['K' + i] || '';
  });
}

function paintDi(ports) {
  const bits = [];
  ports.forEach(v => { for (let b = 0; b < 8; b++) bits.push((v >> b) & 1); });
  S.diCells.forEach((el, i) => {
    el.classList.toggle('hi', bits[i] === 1);
  });
}

function paintDo(ports) {
  const bits = [];
  ports.forEach(v => { for (let b = 0; b < 8; b++) bits.push((v >> b) & 1); });
  S.doCells.forEach((el, i) => {
    const on = bits[i] === 1;
    el.classList.toggle('on', on);
    el.querySelector('.st').textContent = on ? 'ON' : 'OFF';
  });
}

function paintHex(di, do_) {
  const f = a => a.map(v => '0x' + v.toString(16).toUpperCase().padStart(2, '0')).join(' ');
  $('#diHex').textContent = di.length ? f(di) : '--';
  $('#doHex').textContent = do_.length ? f(do_) : '--';
}

/* 点按 / 长按 */
function tapOrLong(el, onTap, onLong) {
  let timer = null;
  let long = false;
  el.addEventListener('touchstart', () => {
    long = false;
    timer = setTimeout(() => { timer = null; long = true; onLong(); buzz(18); }, 520);
  }, { passive: true });
  const cancel = () => { if (timer) { clearTimeout(timer); timer = null; } };
  el.addEventListener('touchmove', cancel, { passive: true });
  el.addEventListener('touchend', (ev) => {
    cancel();
    if (long) ev.preventDefault();
  });
  el.addEventListener('click', () => {
    if (long) { long = false; return; }
    cancel();
    onTap();
  });
  el.addEventListener('contextmenu', e => e.preventDefault());
}

/* ---------------------------------------------------------- 操作 */
async function toggleRelay(i) {
  const el = S.doCells[i];
  const next = el && el.classList.contains('on') ? 0 : 1;
  buzz(next ? 16 : 8);
  try {
    await api('/api/do/bit', { index: i, value: next });
  } catch (e) { fail(e); refresh(); }
}

function openSheet(key, prefix) {
  S.sheetKey = key;
  const cur = (S.state && S.state.annotations && S.state.annotations[key]) || '';
  $('#sheetTitle').textContent = '设置 ' + key + ' 备注';
  $('#sheetSub').textContent = '给这个点位起个现场名称，会随测试报告一起导出。';
  $('#sheetInput').value = cur;
  $('#sheet').hidden = false;
  $('#scrim').hidden = false;
  setTimeout(() => $('#sheetInput').focus(), 120);
}

function closeSheet() {
  $('#sheet').hidden = true;
  $('#scrim').hidden = true;
}

async function saveSheet(name) {
  try {
    await api('/api/annotations', { key: S.sheetKey, name: name });
    closeSheet();
    toast(name ? '备注已保存' : '备注已清除', 'ok');
    refresh();
  } catch (e) { fail(e); }
}

/* ---------------------------------------------------------- 日志 */
function appendLog(item) {
  const list = $('#logList');
  const ln = document.createElement('div');
  ln.className = 'ln l-' + (item.level || 'info');
  ln.innerHTML = '<span class="ts"></span><span class="msg"></span>';
  ln.querySelector('.ts').textContent = item.ts || '';
  ln.querySelector('.msg').textContent = item.msg || '';
  list.appendChild(ln);
  S.logCount++;
  while (list.childElementCount > 500) list.removeChild(list.firstChild);
  $('#logCount').textContent = S.logCount + ' 行';
  if (S.autoscroll) list.scrollTop = list.scrollHeight;
}

/* ---------------------------------------------------------- 数据同步 */
async function refresh() {
  try {
    const d = await api('/api/state');
    render(d.state);
    return d.state;
  } catch (e) {
    return null;
  }
}

function connectEvents() {
  let es;
  try {
    es = new EventSource(window.Activation ? Activation.url('/api/events') : '/api/events');
  } catch (e) {
    return;
  }
  es.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    switch (msg.t) {
      case 'log':
        appendLog(msg);
        break;
      case 'progress':
        if (S.state) {
          S.state.progress = { pct: msg.pct, msg: msg.msg };
          if (S.state.busy) {
            $('#progWrap').hidden = false;
            $('#progBar').style.width = Math.max(4, msg.pct * 100) + '%';
            $('#progText').textContent = msg.msg || '';
          }
        }
        break;
      case 'di':
        if (S.state) S.state.di = msg.ports || [];
        paintDi(msg.ports || []);
        paintHex(msg.ports || [], (S.state && S.state.do) || []);
        break;
      case 'do':
        if (S.state) S.state.do = msg.ports || [];
        paintDo(msg.ports || []);
        paintHex((S.state && S.state.di) || [], msg.ports || []);
        break;
      case 'state':
        render(msg.state);
        onStateChange(msg.state);
        break;
      case 'io_error':
        toast(msg.msg || 'IO 异常', 'err');
        break;
    }
  };
  es.onerror = () => {
    setOnline(false);
  };
  es.onopen = () => setOnline(true);
}

function onStateChange(st) {
  const wasBusy = S.wasBusy;
  S.wasBusy = st.busy;
  if (wasBusy && !st.busy) {
    // 任务刚结束
    if (S.lastJob === 'selftest') loadReport();
    if (S.lastJob === 'deploy') toast('部署流程已结束，请看日志', '');
    if (S.lastJob === 'connect') toast(st.connected ? '连接成功' : '连接失败', st.connected ? 'ok' : 'err');
    if (S.lastJob === 'devices') toast(st.devices.length ? '发现 ' + st.devices.length + ' 个设备' : '未发现设备', st.devices.length ? 'ok' : '');
    if (S.lastJob === 'io-start') toast(st.io_running ? '会话已启动' : '启动未成功', st.io_running ? 'ok' : 'err');
    if (S.lastJob === 'reboot') toast('重启指令已下发');
  }
  if (st.busy && st.job) S.lastJob = st.job;
}

async function loadReport() {
  try {
    const r = await fetch(Activation.url('/api/report'), { cache: 'no-store' });
    const text = await r.text();
    const lines = text.split('\n').slice(7);   // 跳过表头
    const list = $('#selfList');
    const head = $('#selfHead');
    list.innerHTML = '';
    let pass = 0;
    let bad = 0;
    lines.forEach(t => {
      const t2 = t.trim();
      if (!t2) return;
      const li = document.createElement('li');
      li.textContent = t2;
      if (t2.startsWith('[通过]')) { li.className = 'ok'; pass++; }
      else if (t2.startsWith('[差异]') || t2.startsWith('[失败]')) { li.className = 'err'; bad++; }
      list.appendChild(li);
    });
    head.className = 'result-head ' + (bad ? 'err' : 'ok');
    head.textContent = bad
      ? `自检结束：${pass} 项通过，${bad} 项存在差异`
      : `自检通过：${pass} 项全部一致`;
    $('#selfResult').hidden = false;
  } catch (e) { /* 报告未生成时忽略 */ }
}

/* ---------------------------------------------------------- 上传 */
function uploadInstaller(file) {
  if (!file.name.toLowerCase().endsWith('.run')) {
    toast('请选择 .run 驱动安装包', 'err');
    return;
  }
  const wrap = $('#upWrap');
  const bar = $('#upBar');
  wrap.hidden = false;
  bar.style.width = '0%';

  const xhr = new XMLHttpRequest();
  xhr.open('POST', Activation.url('/api/upload'));
  xhr.setRequestHeader('X-File-Name', encodeURIComponent(file.name));
  xhr.setRequestHeader('Content-Type', 'application/octet-stream');
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) bar.style.width = (e.loaded / e.total * 100).toFixed(1) + '%';
  };
  xhr.onload = () => {
    wrap.hidden = true;
    let d = {};
    try { d = JSON.parse(xhr.responseText); } catch (e) { /* ignore */ }
    if (xhr.status === 200 && d.ok) {
      toast('上传完成：' + d.name, 'ok');
      refresh();
    } else {
      toast(d.error || '上传失败', 'err');
    }
  };
  xhr.onerror = () => { wrap.hidden = true; toast('上传失败：网络中断', 'err'); };
  xhr.send(file);
}

/* ---------------------------------------------------------- 事件绑定 */
function bind() {
  $$('#bottomNav button').forEach(b => {
    b.addEventListener('click', () => { buzz(8); showTab(b.dataset.tab); });
  });
  $$('#ioSeg button').forEach(b => {
    b.addEventListener('click', () => { buzz(8); showSub(b.dataset.sub); });
  });

  // ---- 命令交互页
  initCmd();

  // ---- 连接页
  $('#btnEye').addEventListener('click', () => {
    const inp = $('#fPass');
    const show = inp.type === 'password';
    inp.type = show ? 'text' : 'password';
    $('#btnEye').innerHTML = '<svg class="ic"><use href="#i-' + (show ? 'eye-off' : 'eye') + '"></use></svg>';
  });

  $('#btnConnect').addEventListener('click', async () => {
    const host = $('#fHost').value.trim();
    if (!host) { toast('请填写主机地址', 'err'); return; }
    try {
      await api('/api/connect', {
        host,
        port: $('#fPort').value.trim() || '22',
        user: $('#fUser').value.trim() || 'root',
        password: $('#fPass').value,
        key: $('#fKey').value.trim(),
        workdir: $('#fWorkdir').value.trim(),
        remember: $('#fRemember').checked,
      });
      toast('正在连接…');
      showTab('deploy');
    } catch (e) { fail(e); }
  });

  $('#btnDisconnect').addEventListener('click', async () => {
    try { await api('/api/disconnect', {}); toast('已断开'); refresh(); }
    catch (e) { fail(e); }
  });

  // ---- 驱动页
  $$('#modelSeg button').forEach(b => {
    b.addEventListener('click', async () => {
      buzz(8);
      const v = b.dataset.model;
      const models = v === 'both' ? ['PCI-1762', 'PCI-1761'] : [v];
      try { await api('/api/config', { models }); refresh(); } catch (e) { fail(e); }
    });
  });

  ['#optDeps', '#optSrc', '#optBlack', '#optReboot'].forEach(sel => {
    $(sel).addEventListener('change', () => {
      api('/api/config', {
        install_deps: $('#optDeps').checked,
        update_source: $('#optSrc').checked,
        blacklist: $('#optBlack').checked,
        reboot: $('#optReboot').checked,
      }).catch(() => {});
    });
  });

  $('#btnUpload').addEventListener('click', () => $('#fileInput').click());
  $('#fileInput').addEventListener('change', (e) => {
    const f = e.target.files && e.target.files[0];
    if (f) uploadInstaller(f);
    e.target.value = '';
  });

  const deployPayload = () => ({
    models: currentModels(),
    install_deps: $('#optDeps').checked,
    update_source: $('#optSrc').checked,
    blacklist: $('#optBlack').checked,
    reboot: $('#optReboot').checked,
  });

  $('#btnDeploy').addEventListener('click', async () => {
    const st = S.state || {};
    if (!st.installer) { toast('请先选择或上传驱动安装包', 'err'); return; }
    const msg = '即将在目标机上安装 ' + currentModels().join(' + ') + ' 驱动' +
      ($('#optReboot').checked ? '，完成后会重启系统。' : '。') + '继续？';
    if (!confirm(msg)) return;
    try {
      await api('/api/deploy', deployPayload());
      toast('开始部署，请留意进度');
      showTab('log');
    } catch (e) { fail(e); }
  });

  $('#btnProbe').addEventListener('click', async () => {
    try { await api('/api/probe', deployPayload()); toast('开始检测环境'); showTab('log'); }
    catch (e) { fail(e); }
  });
  $('#btnVerify').addEventListener('click', async () => {
    try { await api('/api/verify', deployPayload()); toast('开始验证'); showTab('log'); }
    catch (e) { fail(e); }
  });
  $('#btnReboot').addEventListener('click', async () => {
    if (!confirm('确定要重启目标机吗？正在运行的服务会被中断。')) return;
    try { await api('/api/reboot', deployPayload()); toast('重启指令已下发'); showTab('log'); }
    catch (e) { fail(e); }
  });
  $('#btnUninstall').addEventListener('click', async () => {
    if (!confirm('卸载后 I/O 卡将无法使用，直到重新安装驱动。确定继续？')) return;
    try { await api('/api/uninstall', deployPayload()); toast('开始卸载'); showTab('log'); }
    catch (e) { fail(e); }
  });
  $('#btnAgent').addEventListener('click', async () => {
    try { await api('/api/agent/deploy', {}); toast('正在部署测试代理'); }
    catch (e) { fail(e); }
  });
  $('#btnAgentDel').addEventListener('click', async () => {
    if (!confirm('删除目标机上的 iotest 目录？（不影响已装驱动）')) return;
    try { await api('/api/agent/remove', {}); toast('正在删除'); }
    catch (e) { fail(e); }
  });

  // ---- 测试页
  $('#btnRefresh').addEventListener('click', async () => {
    try { await api('/api/devices'); toast('正在枚举设备…'); }
    catch (e) { fail(e); }
  });

  $('#btnIoStart').addEventListener('click', async () => {
    try {
      await api('/api/io/start', { device: S.selDevice });
      toast('正在启动会话…');
    } catch (e) { fail(e); }
  });
  $('#btnIoStop').addEventListener('click', async () => {
    try { await api('/api/io/stop', {}); toast('会话已结束'); refresh(); }
    catch (e) { fail(e); }
  });

  $('#btnReadDi').addEventListener('click', async () => {
    try { const d = await api('/api/di/read', {}); paintDi(d.ports || []); }
    catch (e) { fail(e); }
  });
  $('#btnReadDo').addEventListener('click', async () => {
    try { const d = await api('/api/do/read', {}); paintDo(d.ports || []); }
    catch (e) { fail(e); }
  });
  $('#btnAllOn').addEventListener('click', async () => {
    if (!confirm('把全部继电器置为吸合？')) return;
    buzz(20);
    try { await api('/api/do/all', { value: 1 }); toast('已全部吸合', 'ok'); refresh(); }
    catch (e) { fail(e); }
  });
  $('#btnAllOff').addEventListener('click', async () => {
    buzz(14);
    try { await api('/api/do/all', { value: 0 }); toast('已全部释放', 'ok'); refresh(); }
    catch (e) { fail(e); }
  });
  $('#btnInvert').addEventListener('click', async () => {
    try { await api('/api/do/invert', {}); toast('状态已反相', 'ok'); refresh(); }
    catch (e) { fail(e); }
  });
  $('#btnWriteBytes').addEventListener('click', async () => {
    const vals = S.byteInputs.map(i => (i.value || '0').replace(/^0x/i, ''));
    try { await api('/api/do/bytes', { values: vals }); toast('已按字节写入', 'ok'); refresh(); }
    catch (e) { fail(e); }
  });

  $('#btnWatch').addEventListener('click', async () => {
    const st = S.state || {};
    try {
      if (st.watching) {
        await api('/api/watch/stop', {});
        toast('已停止监视');
      } else {
        const iv = parseInt($('#fInterval').value, 10) || 200;
        await api('/api/watch/start', { interval: iv });
        toast('开始监视（' + iv + 'ms）', 'ok');
      }
      refresh();
    } catch (e) { fail(e); }
  });
  $('#fInterval').addEventListener('input', (e) => {
    $('#intervalVal').textContent = e.target.value + 'ms';
  });

  $('#btnSelftest').addEventListener('click', async () => {
    if (!confirm('自检会依次动作每个继电器，请确认现场负载安全。继续？')) return;
    $('#selfResult').hidden = true;
    try {
      await api('/api/selftest', { per_bit: $('#optPerBit').checked });
      toast('自检已开始');
      showTab('log');
    } catch (e) { fail(e); }
  });

  // ---- 日志页
  $('#btnAutoscroll').addEventListener('click', () => {
    S.autoscroll = !S.autoscroll;
    $('#btnAutoscroll').textContent = '自动滚动: ' + (S.autoscroll ? '开' : '关');
  });
  $('#btnClearLog').addEventListener('click', () => {
    $('#logList').innerHTML = '';
    S.logCount = 0;
    $('#logCount').textContent = '0 行';
  });

  // ---- 弹出层
  $('#scrim').addEventListener('click', closeSheet);
  $('#sheetCancel').addEventListener('click', closeSheet);
  $('#sheetSave').addEventListener('click', () => saveSheet($('#sheetInput').value.trim()));
  $('#sheetClear').addEventListener('click', () => saveSheet(''));
  $('#sheetInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') saveSheet($('#sheetInput').value.trim());
  });
}

/* ---------------------------------------------------------- 启动 */
async function boot() {
  // 安装/运行验证码门控: 未激活时先弹激活页, 激活成功后继续初始化
  if (window.Activation) {
    await Activation.ensure();
  }
  bind();
  showTab('conn');
  showSub('di');

  try {
    const h = await api('/api/log/history');
    (h.logs || []).forEach(appendLog);
  } catch (e) { /* 忽略 */ }

  const st = await refresh();
  if (st) {
    onStateChange(st);
    if (st.mode === 'local' && st.connected) showTab('deploy');
  }
  connectEvents();

  // 兜底轮询: 手机切后台回来时 SSE 可能已被系统掐断
  setInterval(() => { if (!document.hidden) refresh(); }, 3000);

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refresh();
  });

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('sw.js').catch(() => {});
  }
}

document.addEventListener('DOMContentLoaded', boot);
