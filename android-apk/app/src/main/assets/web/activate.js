/* =========================================================================
   激活门控（安装/运行验证码）
   - 机器码: APK 内取安卓设备号(经 AndroidBridge); 浏览器/PWA 内用持久化随机 ID
   - 校验码: SHA-256(密钥|机器码) 前 10 字节 → Base32 16 位, 4-4-4-4 分组
     与 tools/校验码生成工具.py 完全同款, 断网本地校验
   - 首次安装启动必须输入校验码激活; 之后每次运行自动校验,
     校验不过(如换机/清数据)重新弹出激活页
   ========================================================================= */
'use strict';

/* ------------------------------------------------ SHA-256 (纯JS, 无依赖) */
function sha256Bytes(bytes) {
  const K = [
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
  let H = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];
  const l = bytes.length;
  const bitLen = l * 8;
  const withOne = l + 1;
  const total = (Math.ceil((withOne + 8) / 64)) * 64;
  const m = new Uint8Array(total);
  m.set(bytes); m[l] = 0x80;
  const dv = new DataView(m.buffer);
  dv.setUint32(total - 4, bitLen >>> 0);
  dv.setUint32(total - 8, Math.floor(bitLen / 0x100000000));
  const w = new Uint32Array(64);
  const rr = (x, n) => (x >>> n) | (x << (32 - n));
  for (let off = 0; off < total; off += 64) {
    for (let i = 0; i < 16; i++) w[i] = dv.getUint32(off + i * 4);
    for (let i = 16; i < 64; i++) {
      const s0 = rr(w[i-15],7) ^ rr(w[i-15],18) ^ (w[i-15] >>> 3);
      const s1 = rr(w[i-2],17) ^ rr(w[i-2],19) ^ (w[i-2] >>> 10);
      w[i] = (w[i-16] + s0 + w[i-7] + s1) >>> 0;
    }
    let [a,b,c,d,e,f,g,h] = H;
    for (let i = 0; i < 64; i++) {
      const S1 = rr(e,6) ^ rr(e,11) ^ rr(e,25);
      const ch = (e & f) ^ (~e & g);
      const t1 = (h + S1 + ch + K[i] + w[i]) >>> 0;
      const S0 = rr(a,2) ^ rr(a,13) ^ rr(a,22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const t2 = (S0 + maj) >>> 0;
      h = g; g = f; f = e; e = (d + t1) >>> 0;
      d = c; c = b; b = a; a = (t1 + t2) >>> 0;
    }
    H = H.map((x, i) => (x + [a,b,c,d,e,f,g,h][i]) >>> 0);
  }
  const out = new Uint8Array(32);
  H.forEach((w, i) => {
    out[i * 4] = (w >>> 24) & 0xff;
    out[i * 4 + 1] = (w >>> 16) & 0xff;
    out[i * 4 + 2] = (w >>> 8) & 0xff;
    out[i * 4 + 3] = w & 0xff;
  });
  return out;
}

/* ------------------------------------------------ 同款校验码算法 */
const Activation = (function () {
  const SECRET = 'AIC-1762/1761@2026#CHEN';   // 与校验码生成工具一致
  const B32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  const LS_KEY = 'ioconsole_activation';
  const LS_MC  = 'ioconsole_machine_id';

  function utf8(str) {
    return Array.from(new TextEncoder().encode(str));
  }
  function normalizeMc(s) {
    return String(s || '').toUpperCase().replace(/[\s\-_—–]/g, '');
  }
  function makeCode(machineCode) {
    const norm = normalizeMc(machineCode);
    if (!norm) return '';
    const payload = utf8(SECRET + '|' + norm);
    const digest = sha256Bytes(payload).slice(0, 10); // 前 10 字节
    let chars = '', buf = 0, bits = 0;
    for (const byte of digest) {
      buf = ((buf << 8) | byte) >>> 0; bits += 8;
      while (bits >= 5) { bits -= 5; chars += B32[(buf >>> bits) & 0x1F]; }
    }
    return chars.replace(/(.{4})(?=.)/g, '$1-');
  }
  function verify(mc, code) {
    const want = makeCode(mc).replace(/-/g, '');
    return normalizeMc(code) === want && want.length === 16;
  }

  /* ------------------------------------------------ 机器码 */
  function androidMachineId() {
    try {
      if (window.AndroidBridge && window.AndroidBridge.machineCode)
        return String(window.AndroidBridge.machineCode() || '');
    } catch (e) { /* ignore */ }
    return '';
  }
  function isApk() { return !!androidMachineId() || !!window.AndroidBridge; }

  function machineCode() {
    const and = androidMachineId();
    if (and) return 'AND-' + and.toUpperCase();
    let id = localStorage.getItem(LS_MC);
    if (!id) {
      const hex = [];
      for (let i = 0; i < 16; i++) hex.push(Math.floor(Math.random() * 16).toString(16));
      id = 'WEB-' + hex.join('').toUpperCase();
      localStorage.setItem(LS_MC, id);
    }
    return id;
  }

  /* ------------------------------------------------ 服务地址
     桥接服务已内置于 APP(或部署在目标机), 页面永远同源访问, 无需配置 */
  function base() { return ''; }
  function url(path) { return path; }

  /* ------------------------------------------------ 状态存取 */
  function saved() {
    try { return JSON.parse(localStorage.getItem(LS_KEY) || 'null'); }
    catch (e) { return null; }
  }
  function activated() {
    const s = saved();
    return !!(s && s.mc && s.code && verify(s.mc, s.code));
  }
  function activate(mc, code) {
    if (!verify(mc, code)) return false;
    localStorage.setItem(LS_KEY, JSON.stringify({ mc: normalizeMc(mc), code: normalizeMc(code) }));
    return true;
  }

  /* ------------------------------------------------ 激活页 UI */
  const APP_VER = 'v2.3.3';
  let resolver = null;

  function buildGate() {
    const gate = document.createElement('div');
    gate.id = 'gate';
    const mc = machineCode();
    gate.innerHTML = `
      <div class="orgbar">中烟信息业保中心</div>
      <div class="gate-card">
        <div class="gate-brand"><span class="gate-mark">◤</span> I/O 卡检测控制台</div>
        <h2>软件激活</h2>
        <p class="gate-tip">${isApk()
          ? '首次安装需输入校验码解锁。请把下方机器码发给管理员，获取校验码后填入。'
          : '首次使用需输入校验码解锁。请把下方机器码发给管理员，获取校验码后填入。'}</p>
        <div class="gate-mc">
          <div class="gate-mc-label">本机机器码</div>
          <div class="gate-mc-row">
            <code id="gateMc">${mc}</code>
            <button type="button" class="gate-copy" id="gateCopy">复制</button>
          </div>
        </div>
        <div class="gate-field">
          <label>校验码（XXXX-XXXX-XXXX-XXXX）</label>
          <input id="gateCode" type="text" autocapitalize="characters" autocomplete="off"
                 spellcheck="false" placeholder="例如 3BII-PT6D-T3MS-RXOT" maxlength="24">
        </div>
        <p class="gate-err" id="gateErr" hidden>校验码不正确，请核对机器码后重试</p>
        <button type="button" class="gate-btn" id="gateGo">激活</button>
        <p class="gate-foot">${APP_VER} · 设计开发人：陈宠</p>
      </div>`;
    document.body.appendChild(gate);

    $('#gateCopy').addEventListener('click', () => {
      const t = $('#gateMc').textContent;
      const done = () => { $('#gateCopy').textContent = '已复制'; };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(t).then(done).catch(() => fallbackCopy(t, done));
      } else fallbackCopy(t, done);
      if (window.AndroidBridge && window.AndroidBridge.toast) {
        try { window.AndroidBridge.toast('机器码已复制'); } catch (e) { /* ignore */ }
      }
    });

    const tryActivate = () => {
      if (activate(mc, $('#gateCode').value)) {
        gate.remove();
        if (resolver) resolver();
      } else {
        const err = $('#gateErr');
        err.hidden = false; err.textContent = '校验码不正确，请核对机器码后重试';
        $('#gateCode').select();
        if (navigator.vibrate) navigator.vibrate(80);
      }
    };
    $('#gateGo').addEventListener('click', tryActivate);
    $('#gateCode').addEventListener('keydown', (e) => { if (e.key === 'Enter') tryActivate(); });
    setTimeout(() => { const el = $('#gateCode'); el && el.focus(); }, 60);
  }

  function fallbackCopy(text, done) {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); done(); } catch (e) { /* ignore */ }
    ta.remove();
  }

  function ensure() {
    if (activated()) return Promise.resolve();
    return new Promise((resolve) => {
      resolver = resolve;
      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', buildGate);
      } else buildGate();
    });
  }

  return { ensure, activated, activate, machineCode, makeCode, verify, base, url, isApk, APP_VER };
})();

window.Activation = Activation;   // 供 app.js 与 APK 桥接使用
