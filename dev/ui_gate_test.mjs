// 激活门控验证: 激活页显示/错误码拒绝/正确码解锁/算法与Python一致/版本署名。
// 运行: node dev/ui_gate_test.mjs
import { createRequire } from 'node:module';
const require = createRequire('C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/');
const { chromium } = require('playwright-core');
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.join(HERE, '..', 'screenshots', 'mobile-gate');
const BR = process.env.BRIDGE || 'http://127.0.0.1:8081';

const ok = (cond, name) => console.log((cond ? 'PASS' : 'FAIL') + ' - ' + name);

const browser = await chromium.launch({ channel: 'msedge', headless: true });
const ctx = await browser.newContext({
  viewport: { width: 412, height: 915 },
  deviceScaleFactor: 2,
  isMobile: true,
  hasTouch: true,
  locale: 'zh-CN',
});
const page = await ctx.newPage();
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));

await page.goto(BR, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(600);

// 1. 激活页出现
const gateVisible = await page.isVisible('#gate');
ok(gateVisible, '未激活时显示激活页');
const mc = await page.textContent('#gateMc');
ok(/^WEB-[0-9A-F]{16}$/.test(mc.trim()), '机器码格式正确: ' + mc.trim());
await page.screenshot({ path: path.join(OUT, '01-激活页.png') });

// 2. JS 算法与 Python 生成工具一致
const jsCode = await page.evaluate(() => Activation.makeCode('AND-9f3c1a2e77b4d008'));
ok(jsCode === '3BII-PT6D-T3MS-RXOT', 'JS 算法与 Python 端一致: ' + jsCode);

// 3. 错误校验码被拒绝
await page.fill('#gateCode', 'AAAA-BBBB-CCCC-DDDD');
await page.click('#gateGo');
await page.waitForTimeout(300);
const errShown = await page.isVisible('#gateErr');
ok(errShown, '错误校验码被拒绝并提示');
await page.screenshot({ path: path.join(OUT, '02-激活失败.png') });

// 4. 正确校验码激活成功 (用校验码生成工具针对本页机器码现场生成)
const { execFileSync } = await import('node:child_process');
const out = execFileSync('python', ['tools/校验码生成工具.py', mc.trim()],
                         { cwd: path.join(HERE, '..'), encoding: 'utf8' });
const expected = (out.match(/校验码:\s*(\S+)/) || [])[1];
if (!expected) throw new Error('生成工具输出异常: ' + out);
console.log('生成工具 -> 机器码 ' + mc.trim() + ' 校验码 ' + expected);
await page.fill('#gateCode', expected.toLowerCase());
await page.click('#gateGo');
await page.waitForTimeout(500);
ok(!(await page.isVisible('#gate')), '正确校验码激活成功, 激活页关闭');

// 5. 主界面版本区署名
const sub = await page.textContent('#subtitle');
ok(/v2\.\d+\.\d+/.test(sub) && sub.includes('开发者：陈宠'), '版本区显示: ' + sub.trim());
await page.screenshot({ path: path.join(OUT, '03-激活后主页.png') });

// 6. 刷新后自动通过(运行时校验, 不再弹激活页)
await page.reload({ waitUntil: 'domcontentloaded' });
await page.waitForTimeout(600);
ok(!(await page.isVisible('#gate')), '再次运行无需重新输入校验码');

// 7. 桥接服务 CORS 头(APK 壳跨源依赖)
const r = await fetch(BR + '/api/ping');
ok(r.headers.get('access-control-allow-origin') === '*', '桥接服务返回 CORS 头');
const opt = await fetch(BR + '/api/connect', { method: 'OPTIONS' });
ok(opt.status === 204, 'OPTIONS 预检返回 204');

ok(errors.length === 0, '无 JS 报错' + (errors.length ? ': ' + errors.join(' | ') : ''));
await browser.close();
console.log('截图目录:', OUT);
