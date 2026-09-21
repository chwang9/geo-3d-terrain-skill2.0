#!/usr/bin/env node
/**
 * check_render.js — 无头浏览器渲染校验（geo-3d-terrain 交付前必跑）
 * ================================================================
 * 只查 JSON 字段是不够的：JS 运行时报错会让页面永远停在「正在构建真实地形…」，
 * 而 terrain.json 里一切正常。本脚本真正把 HTML 跑起来，检查：
 *   canvas 是否创建 / loading 是否隐藏 / 地标标签是否显示 / 有无 pageerror
 *
 * 用法:
 *   node check_render.js <html路径或URL> [截图输出.png]
 *
 * 依赖: playwright-core（已随 WorkBuddy node workspace 提供）
 *   NODE_PATH=<node workspace>/node_modules node check_render.js ...
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

function findBrowser() {
  for (const p of CHROME_CANDIDATES) {
    if (fs.existsSync(p)) return p;
  }
  return undefined; // 交给 playwright-core 自己找
}

const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.png': 'image/png', '.json': 'application/json' };

/** file:// 下 blob: 动态 import 可能被拦，统一起一个临时 http 服务（也更接近真实预览环境） */
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
  const target = process.argv[2];
  const shot = process.argv[3];
  if (!target) {
    console.error('用法: node check_render.js <html路径或URL> [截图.png]');
    process.exit(2);
  }

  let url = target;
  let srv = null;
  if (!/^https?:/.test(target)) {
    const dir = path.resolve(path.dirname(target));
    const name = path.basename(target);
    srv = await serve(dir);
    url = `http://127.0.0.1:${srv.address().port}/${encodeURIComponent(name)}`;
  }

  const browser = await chromium.launch({
    executablePath: findBrowser(),
    args: ['--enable-unsafe-swiftshader', '--use-gl=angle', '--use-angle=swiftshader', '--ignore-gpu-blocklist'],
  });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const logs = [];
  const ignorable = t => /favicon/i.test(t);
  page.on('console', m => {
    if (m.type() === 'error' && !ignorable(m.text())) logs.push('[console.error] ' + m.text());
  });
  page.on('pageerror', e => { if (!ignorable(e.message)) logs.push('[pageerror] ' + e.message); });

  await page.goto(url, { waitUntil: 'load', timeout: 60000 });
  await page.waitForTimeout(9000);

  const state = await page.evaluate(() => {
    const c = document.querySelector('canvas');
    const l = document.getElementById('loading');
    return {
      hasCanvas: !!c,
      canvasSize: c ? c.width + 'x' + c.height : null,
      loadingHidden: l ? l.classList.contains('hide') : null,
      statusText: (document.getElementById('lmsg') || {}).textContent || '',
      visibleLabels: Array.from(document.querySelectorAll('.lbl')).filter(e => e.style.display === 'block').length,
    };
  });
  if (shot) await page.screenshot({ path: shot });
  await browser.close();
  if (srv) srv.close();

  const ok = state.hasCanvas && state.loadingHidden === true && logs.length === 0;
  console.log(JSON.stringify({ ok, ...state, errors: logs }, null, 2));
  process.exit(ok ? 0 : 1);
})();
