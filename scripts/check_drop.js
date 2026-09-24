#!/usr/bin/env node
/**
 * check_drop.js — 校验「把 .geojson 拖进页面即时叠加」链路（v2.5 可选检查）
 * =======================================================================
 * check_render.js 只验证页面能跑；本脚本额外用合成 DragEvent 模拟
 * 用户拖入一个 .geojson 文件，确认 parseGeoJSONText → buildOverlays 链路
 * 真正生效（叠加标签数量发生变化、无 pageerror）。
 *
 * 用法:
 *   NODE_PATH=<node workspace>/node_modules node check_drop.js <html> <geojson> [截图.png]
 *
 * 通过标准: 无 pageerror 且 拖入后 .ovl 标签数 > 0（若拖入前已有叠加，则数量发生变化）
 */
const fs = require('fs');
const path = require('path');
const http = require('http');
const { chromium } = require('playwright-core');

const CHROME_CANDIDATES = [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
];

const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.png': 'image/png', '.json': 'application/json' };

function serve(dir) {
  return new Promise(resolve => {
    const srv = http.createServer((req, res) => {
      const rel = decodeURIComponent(req.url.split('?')[0]).replace(/^\/+/, '');
      if (rel === 'favicon.ico') { res.writeHead(204); res.end(); return; }
      const fp = path.join(dir, rel);
      if (!fp.startsWith(dir) || !fs.existsSync(fp) || fs.statSync(fp).isDirectory()) {
        res.writeHead(404); res.end('not found'); return;
      }
      res.writeHead(200, { 'Content-Type': MIME[path.extname(fp).toLowerCase()] || 'application/octet-stream' });
      fs.createReadStream(fp).pipe(res);
    });
    srv.listen(0, '127.0.0.1', () => resolve(srv));
  });
}

(async () => {
  const [html, gj, shot] = process.argv.slice(2);
  if (!html || !gj) {
    console.error('用法: node check_drop.js <html路径> <geojson路径> [截图.png]');
    process.exit(2);
  }
  const dir = path.resolve(path.dirname(html));
  const srv = await serve(dir);
  const url = `http://127.0.0.1:${srv.address().port}/${encodeURIComponent(path.basename(html))}`;
  const gjName = path.basename(gj);

  const browser = await chromium.launch({
    executablePath: CHROME_CANDIDATES.find(p => fs.existsSync(p)),
    args: ['--enable-unsafe-swiftshader', '--use-gl=angle', '--use-angle=swiftshader', '--ignore-gpu-blocklist'],
  });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const errs = [];
  page.on('console', m => { if (m.type() === 'error' && !/favicon/i.test(m.text())) errs.push('[console.error] ' + m.text()); });
  page.on('pageerror', e => { if (!/favicon/i.test(e.message)) errs.push('[pageerror] ' + e.message); });

  await page.goto(url, { waitUntil: 'load', timeout: 60000 });
  await page.waitForTimeout(9000);
  const before = await page.evaluate(() => document.querySelectorAll('.ovl').length);

  // 合成 drop：DataTransfer 里塞入 geojson 文件
  await page.evaluate(async name => {
    const txt = await (await fetch(name)).text();
    const dt = new DataTransfer();
    dt.items.add(new File([txt], name, { type: 'application/json' }));
    window.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt }));
  }, gjName);
  await page.waitForTimeout(1500);

  const after = await page.evaluate(() => document.querySelectorAll('.ovl').length);
  if (shot) await page.screenshot({ path: shot });
  await browser.close();
  srv.close();

  const ok = errs.length === 0 && after > 0 && after !== before;
  console.log(JSON.stringify({ ok, before, after, dropped: gjName, errors: errs }, null, 2));
  process.exit(ok ? 0 : 1);
})();
