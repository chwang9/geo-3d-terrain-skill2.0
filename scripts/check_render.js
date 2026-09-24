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
  // 等页面自己宣告"构建完成"（window.__geo3dReady），比死等 9 秒更准也更稳
  try {
    await page.waitForFunction(() => window.__geo3dReady === true, null, { timeout: 30000 });
  } catch (e) { /* 超时就走下面的断言，让状态如实反映出来 */ }
  await page.waitForTimeout(1500);

  const state = await page.evaluate(() => {
    const c = document.querySelector('canvas');
    const l = document.getElementById('loading');
    const shown = sel => Array.from(document.querySelectorAll(sel)).filter(e => e.style.display === 'block').length;
    return {
      hasCanvas: !!c,
      canvasSize: c ? c.width + 'x' + c.height : null,
      loadingHidden: l ? l.classList.contains('hide') : null,
      statusText: (document.getElementById('lmsg') || {}).textContent || '',
      visibleLabels: shown('.lbl'),
      // v2.5: GeoJSON 叠加图层的标签（有叠加图形时不应为 0）
      ovLabelsTotal: document.querySelectorAll('.ovl').length,
      ovLabelsVisible: shown('.ovl'),
      // v2.9: 构建完成标记 + 建地形那一刻纹理是否已就绪
      // （按需渲染下若纹理晚于首帧就绪，画面会一直发暗且不会自己重绘）
      ready: window.__geo3dReady === true,
      texReady: window.__geo3dTexReady === true,
      // v2.10: 地形实际贴到的纹理尺寸（分块合成时 = 等效大纹理，如 [6592, 8192]）
      texSize: (window.__geo3dDebug && window.__geo3dDebug.texSize) || null,
      // v2.9 业务工具是否就位
      hasTools: ['measBtn', 'profBtn', 'glbBtn', 'clrBtn', 'scaleTx', 'needle', 'cursor']
        .every(id => !!document.getElementById(id)),
      // v2.9 各向同性检查：世界平面的宽高比必须等于地面范围的宽高比，
      // 否则 x/z 两个方向的「米/世界单位」不等，垂直夸张会在长边方向被压平、短边被拔高
      plane: window.__geo3dDebug ? (() => {
        const D = window.__geo3dDebug;
        return { WX: +D.WX.toFixed(2), WZ: +D.WZ.toFixed(2),
                 planeRatio: +(D.WX / D.WZ).toFixed(4),
                 rangeRatio: +(D.META.width_m / D.META.height_m).toFixed(4) };
      })() : null,
    };
  });
  if (shot) await page.screenshot({ path: shot });
  await browser.close();
  if (srv) srv.close();

  // 平面宽高比与地面范围宽高比必须一致（容差 1%，浮点与取整余量）
  const iso = !state.plane
    || Math.abs(state.plane.planeRatio - state.plane.rangeRatio) <= 0.01 * Math.max(1, state.plane.rangeRatio);
  const ok = state.hasCanvas && state.loadingHidden === true
    && state.ready === true && state.hasTools === true
    && iso && logs.length === 0;
  console.log(JSON.stringify({ ok, ...state, errors: logs }, null, 2));
  process.exit(ok ? 0 : 1);
})();
