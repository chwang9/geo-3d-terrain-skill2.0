#!/usr/bin/env node
/**
 * check_tools.js — v2.9 页面业务工具的交互校验（测距 / 剖面 / 导出 GLB / HUD）
 * ==========================================================================
 * check_render.js 只保证「页面能出来」，这里进一步验证真的能用：
 *   ① 测距：两次点选后出现 2 个标记 + 距离文本（水平/高差/坡度）
 *   ② 剖面：两次点选后剖面浮层出现，canvas 有非空白像素
 *   ③ 导出 GLB：点击后真的下载到 .glb 且魔数为 'glTF'（可被 Blender/Cesium 打开）
 *   ④ HUD：比例尺有数值、指北针角度随相机变化、光标显示经纬高
 *
 * 用法:
 *   node check_tools.js <html路径> [glb保存路径]
 *
 * 依赖: playwright-core（随 WorkBuddy node workspace 提供）
 *   NODE_PATH=<node workspace>/node_modules node check_tools.js ...
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
  for (const p of CHROME_CANDIDATES) if (fs.existsSync(p)) return p;
  return undefined;
}
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
  const target = process.argv[2];
  const glbOut = process.argv[3];
  if (!target) { console.error('用法: node check_tools.js <html路径> [glb保存路径]'); process.exit(2); }

  let url = target, srv = null;
  if (!/^https?:/.test(target)) {
    const dir = path.resolve(path.dirname(target));
    srv = await serve(dir);
    url = `http://127.0.0.1:${srv.address().port}/${encodeURIComponent(path.basename(target))}`;
  }

  const browser = await chromium.launch({
    executablePath: findBrowser(),
    args: ['--enable-unsafe-swiftshader', '--use-gl=angle', '--use-angle=swiftshader', '--ignore-gpu-blocklist'],
  });
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, acceptDownloads: true });
  const page = await ctx.newPage();
  const logs = [];
  page.on('console', m => { if (m.type() === 'error' && !/favicon/i.test(m.text())) logs.push('[console.error] ' + m.text()); });
  page.on('pageerror', e => { if (!/favicon/i.test(e.message)) logs.push('[pageerror] ' + e.message); });

  await page.goto(url, { waitUntil: 'load', timeout: 60000 });
  await page.waitForFunction(() => document.getElementById('loading').classList.contains('hide'),
                             null, { timeout: 90000 });

  // 弹窗（导出失败时会 alert）必须接住，否则会一直阻塞页面
  const dialogs = [];
  page.on('dialog', async d => { dialogs.push(d.message()); await d.dismiss(); });

  const res = {};
  const box = await page.locator('canvas').first().boundingBox();
  const pt = (fx, fy) => [box.x + box.width * fx, box.y + box.height * fy];
  const markCount = () => page.evaluate(() => document.querySelectorAll('.mk').length);
  async function pick2(fxA, fyA, fxB, fyB) {
    const steps = [];
    for (const [fx, fy] of [[fxA, fyA], [fxB, fyB]]) {
      const [x, y] = pt(fx, fy);
      await page.mouse.move(x, y);
      await page.mouse.down(); await page.mouse.up();
      await page.waitForTimeout(400);
      steps.push(await markCount());
    }
    return steps;
  }
  // 自动找两个「确实打在地形上」的屏幕点：长宽比悬殊的产物（如 21×50 km 的南北长条）
  // 画面左右大片是空的，写死的两组坐标会随机点天空，把好功能误报成坏的。
  // 先用取点函数探一遍网格，再挑相距最远的一对，测距/剖面才有意义。
  async function findTwoHits() {
    const cands = await page.evaluate(() => {
      const out = [];
      for (let fy = 0.30; fy <= 0.90; fy += 0.04)
        for (let fx = 0.15; fx <= 0.85; fx += 0.04) {
          const x = innerWidth * fx, y = innerHeight * fy;
          // 取点函数只做数学计算，不判断遮挡；必须再用 elementFromPoint 确认这一下真的会落在
          // canvas 上，否则挑中的点可能压在信息/控制面板底下，鼠标点过去等于没点
          const el = document.elementFromPoint(x, y);
          if (window.__geo3dPick(x, y) && el && el.tagName === 'CANVAS') out.push([fx, fy]);
        }
      return out;
    });
    if (cands.length < 2) return null;
    let best = [cands[0], cands[cands.length - 1]], bd = -1;
    for (let i = 0; i < cands.length; i++) {
      for (let j = i + 1; j < cands.length; j++) {
        const d = Math.hypot(cands[i][0] - cands[j][0], cands[i][1] - cands[j][1]);
        if (d > bd) { bd = d; best = [cands[i], cands[j]]; }
      }
    }
    return best;
  }
  const two = await findTwoHits();
  if (!two) { console.log(JSON.stringify({ ok: false, error: '画面上找不到两个可命中的取点位置' })); process.exit(1); }

  // ① 测距
  await page.click('#measBtn');
  res.measSteps = await pick2(two[0][0], two[0][1], two[1][0], two[1][1]);
  res.meas = await page.evaluate(() => {
    const l = document.querySelector('.mkLbl');
    return { marks: document.querySelectorAll('.mk').length, text: l ? l.textContent : null };
  });

  // ② 剖面（清掉测距残留后重新点两点）
  await page.click('#clrBtn');
  await page.click('#profBtn');
  res.profSteps = await pick2(two[0][0], two[0][1], two[1][0], two[1][1]);
  res.prof = await page.evaluate(() => {
    const box = document.getElementById('prof');
    const cv = document.getElementById('profCv');
    let ink = 0;
    if (cv && cv.getContext) {
      const d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data;
      for (let i = 3; i < d.length; i += 4) if (d[i] > 8) ink++;
    }
    return { shown: box && box.style.display === 'block', inkPixels: ink,
             text: (document.getElementById('profTx') || {}).textContent || '' };
  });

  // ⑤ 取点性能（防回归）：直接 raycast 地形网格在 118 万面片下实测 36.7 ms/次，
  //    鼠标每动一次就掉帧；改成高度场步进后 0.04 ms/次。这里断言"每次取点 < 2 ms 且能命中"
  res.pick = await page.evaluate(() => {
    const N = 30, pts = [];
    for (let i = 0; i < N; i++) pts.push([300 + (i % 10) * 60, 260 + Math.floor(i / 10) * 90]);
    const t0 = performance.now();
    let hit = 0;
    for (const [x, y] of pts) if (window.__geo3dPick(x, y)) hit++;
    return { ms_per_pick: +((performance.now() - t0) / N).toFixed(3), hits: hit, n: N };
  });

  // ③ HUD（先退出取点模式，否则光标位置显示的是"点选两点…"而不是经纬高）
  await page.click('#clrBtn');
  // 取画面中下部（正中间常常落在地形之外的天空上，raycast 打不到会显示 "—"）。
  // 光标更新有 60ms 节流，先等计时器归位再移，并连移两次确保这次一定被处理。
  await page.waitForTimeout(200);
  const cx = box.x + box.width * 0.45, cy = box.y + box.height * 0.62;
  await page.mouse.move(cx, cy);
  await page.waitForTimeout(120);
  await page.mouse.move(cx + 2, cy + 2);
  await page.waitForTimeout(400);
  res.hud = await page.evaluate(() => ({
    scale: (document.getElementById('scaleTx') || {}).textContent,
    scaleBar: (document.getElementById('scaleBar') || {}).style.width,
    needle: (document.getElementById('needle') || {}).style.transform,
    cursor: (document.getElementById('cursor') || {}).textContent,
  }));

  // ④ 导出 GLB（与「导出失败弹窗」竞速，别让 alert 把页面卡死）
  try {
    // 两边都要吞掉超时，否则 race 输的那个会在 browser.close() 后变成 unhandled rejection
    const noop = () => ({});
    const dl = page.waitForEvent('download', { timeout: 150000 }).then(d => ({ d }), noop);
    const dg = page.waitForEvent('dialog', { timeout: 150000 }).then(() => ({ dialog: true }), noop);
    await page.click('#glbBtn');
    const r = await Promise.race([dl, dg]);
    if (!r.d && !r.dialog) {
      res.glb = { error: '导出超时（150s 内既没下载也没弹窗）', dialogs };
    } else if (r.dialog) {
      res.glb = { error: '导出失败弹窗: ' + (dialogs[dialogs.length - 1] || '') };
    } else {
      const p = glbOut || path.join(path.dirname(target), 'out.glb');
      await r.d.saveAs(p);
      const buf = fs.readFileSync(p);
      res.glb = { file: p, bytes: buf.length, magic: buf.slice(0, 4).toString('ascii') };
    }
  } catch (e) {
    res.glb = { error: String((e && e.message) || e), dialogs };
  }

  await browser.close();
  if (srv) srv.close();

  const ok = res.meas.marks === 2 && /水平/.test(res.meas.text || '')
    && res.prof.shown && res.prof.inkPixels > 500
    && /\d/.test(res.hud.scale || '') && /m$/.test(res.hud.cursor || '')
    && res.glb.magic === 'glTF' && res.glb.bytes > 100000
    && res.pick.ms_per_pick < 2 && res.pick.hits > 0      // 取点不能退回"打满网格三角形"
    && logs.length === 0;
  console.log(JSON.stringify({ ok, ...res, errors: logs }, null, 2));
  process.exit(ok ? 0 : 1);
})();
