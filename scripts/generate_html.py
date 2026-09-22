#!/usr/bin/env python3
"""
generate_html.py  (v2.4)
========================
把 prepare_terrain_data.py 生成的 terrain.json 渲染成自包含 Three.js 三维地形 HTML。

修复（相对 v1）:
  * 经纬度→像素映射改在墨卡托空间插值（v1 用纬度线性插值，低 zoom 时地标会偏移数十米）
  * 地标的球体与引线随垂直夸张系数一起更新（v1 只更新了标签，球体会浮空/陷地）
  * "显示地标" 开关真正生效（v1 每帧 updateLabels 会强制重新显示）
  * 更正 peak_u / peak_v 语义
  * 响应式布局，窄屏不再溢出

新增:
  * 垂直夸张系数自动推荐、地形底座、高程分层设色模式、加载/错误提示
  * v2.1: Three.js 默认**内嵌进 HTML**（--three auto，下载失败才回退 CDN），
    彻底解决沙箱/离线/预览面板加载不了 CDN 导致白屏的问题

依赖:
    pip install jinja2

用法:
    python generate_html.py --data terrain.json --out terrain.html
    python generate_html.py --data terrain.json --out t.html --exaggeration 5 --three auto
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

THREE_VERSION = "0.160.0"
CDNS = {
    "jsdelivr": (f"https://cdn.jsdelivr.net/npm/three@{THREE_VERSION}/build/three.module.js",
                 f"https://cdn.jsdelivr.net/npm/three@{THREE_VERSION}/examples/jsm/"),
    "unpkg": (f"https://unpkg.com/three@{THREE_VERSION}/build/three.module.js",
              f"https://unpkg.com/three@{THREE_VERSION}/examples/jsm/"),
    "esm.sh": (f"https://esm.sh/three@{THREE_VERSION}",
               f"https://esm.sh/three@{THREE_VERSION}/examples/jsm/"),
}
# 内嵌模式需要的两个文件（three 主包 + OrbitControls）
EMBED_FILES = {
    "three": f"https://cdn.jsdelivr.net/npm/three@{THREE_VERSION}/build/three.module.js",
    "orbit": f"https://cdn.jsdelivr.net/npm/three@{THREE_VERSION}/examples/jsm/controls/OrbitControls.js",
}

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{{ place }} · 真实三维地形</title>
<style>
  :root{
    --bg:#0d1117; --panel:rgba(18,24,32,.86); --line:rgba(255,255,255,.12);
    --txt:#e8eef5; --sub:#9fb0c0; --accent:#4ea1ff; --warn:#ffb454;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{height:100%;background:var(--bg);color:var(--txt);overflow:hidden;
    font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",Segoe UI,Roboto,sans-serif;
    -webkit-font-smoothing:antialiased}
  #app{position:fixed;inset:0}
  canvas{display:block;touch-action:none}
  .panel{position:fixed;background:var(--panel);backdrop-filter:blur(12px);
    -webkit-backdrop-filter:blur(12px);border:1px solid var(--line);border-radius:14px;
    padding:13px 15px;box-shadow:0 8px 30px rgba(0,0,0,.45);z-index:20}
  #info{top:16px;left:16px;max-width:330px}
  #info h1{font-size:16px;font-weight:700;letter-spacing:.4px;line-height:1.35;
    display:flex;align-items:center;gap:8px}
  #info h1 button{margin-left:auto;background:transparent;border:1px solid var(--line);
    color:var(--sub);border-radius:6px;font-size:11px;padding:2px 7px;cursor:pointer}
  #info h1 button:hover{color:var(--txt);border-color:var(--accent)}
  #info .sub{font-size:11.5px;color:var(--sub);margin-top:4px}
  #info .grid{display:grid;grid-template-columns:auto 1fr;gap:3px 10px;margin-top:10px;font-size:12px}
  #info .grid span{color:var(--sub);white-space:nowrap}
  #info .grid b{color:var(--accent);font-weight:600;font-variant-numeric:tabular-nums}
  #info .src{margin-top:10px;font-size:10.5px;color:var(--sub);line-height:1.55;
    border-top:1px dashed var(--line);padding-top:8px}
  #info.folded .grid,#info.folded .src,#info.folded .sub{display:none}
  #ctrl{bottom:16px;left:16px;width:250px;display:flex;flex-direction:column;gap:11px}
  #ctrl label{font-size:12px;color:var(--sub);display:flex;justify-content:space-between;margin-bottom:5px}
  #ctrl label b{color:var(--accent);font-variant-numeric:tabular-nums}
  #ctrl input[type=range]{width:100%;accent-color:var(--accent)}
  #ctrl .btns{display:flex;gap:7px;flex-wrap:wrap}
  #ctrl button{flex:1 1 46%;background:#1c2733;color:var(--txt);border:1px solid var(--line);
    border-radius:9px;padding:7px 6px;font-size:12px;cursor:pointer;transition:.15s;white-space:nowrap}
  #ctrl button:hover{background:#26384a;border-color:var(--accent)}
  #ctrl button.on{background:#1d4d7a;border-color:var(--accent)}
  #ctrl .chk{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--sub);cursor:pointer;user-select:none}
  #ctrl .chk input{accent-color:var(--accent);width:auto}
  #tip{bottom:16px;right:16px;font-size:11.5px;color:var(--sub);line-height:1.7;text-align:right}
  .lbl{position:fixed;transform:translate(-50%,-125%);pointer-events:none;color:#fff;
    font-size:12px;font-weight:600;padding:4px 9px;border-radius:8px;white-space:nowrap;
    box-shadow:0 4px 14px rgba(0,0,0,.45);display:none}
  .lbl:after{content:"";position:absolute;left:50%;top:100%;transform:translateX(-50%);
    border:6px solid transparent;border-top-color:var(--ar,#666)}
  #peakLabel{z-index:12;background:#c82828;--ar:#c82828}
  .lm{z-index:8}
  #legend{bottom:64px;right:16px;display:none;width:150px}
  #legend .bar{height:9px;border-radius:5px;margin:6px 0 4px;
    background:linear-gradient(90deg,#2e6b4f,#4f8f3a,#9db33a,#c9a227,#a86b32,#efeae3)}
  #legend .lg{display:flex;justify-content:space-between;font-size:10.5px;color:var(--sub);
    font-variant-numeric:tabular-nums}
  #loading{position:fixed;inset:0;display:flex;flex-direction:column;align-items:center;
    justify-content:center;gap:12px;background:var(--bg);z-index:60;color:var(--sub);font-size:13px}
  #loading .spin{width:26px;height:26px;border:2px solid rgba(255,255,255,.15);
    border-top-color:var(--accent);border-radius:50%;animation:sp .9s linear infinite}
  #loading.hide{opacity:0;pointer-events:none;transition:opacity .5s}
  #loading .err{max-width:520px;text-align:center;line-height:1.7;color:var(--warn)}
  @keyframes sp{to{transform:rotate(360deg)}}
  @media (max-width:820px){
    #info{max-width:calc(100vw - 32px);padding:10px 12px}
    #info h1{font-size:14px}
    #ctrl{width:calc(100vw - 32px)}
    #tip{display:none}
    #legend{display:none!important}
  }
  @media (max-height:560px){ #info .src{display:none} }
</style>
{% if embed_three %}
<!-- Three.js 已内嵌（离线可用，不依赖任何 CDN） -->
<script id="__three_src" type="text/plain">{{ three_src }}</script>
<script id="__orbit_src" type="text/plain">{{ orbit_src }}</script>
{% else %}
<script type="importmap">
{ "imports": {
  "three": "{{ three_url }}",
  "three/addons/": "{{ three_addons }}"
}}
</script>
{% endif %}
</head>
<body>
<div id="app"></div>
<div id="loading"><div class="spin" id="spin"></div><div id="lmsg">正在构建真实地形…</div></div>

<div id="info" class="panel">
  <h1>{{ place }} · 真实三维地形 <button id="fold">折叠</button></h1>
  <div class="sub">基于真实 DEM 高程逐像素还原 · 非程序化生成</div>
  <div class="grid">
    <span>中心坐标</span><b>{{ "%.4f"|format(center_lat) }}°N, {{ "%.4f"|format(center_lon) }}°E</b>
    <span>覆盖范围</span><b>{{ "%.1f"|format(width_m/1000) }} × {{ "%.1f"|format(height_m/1000) }} km</b>
    <span>高程范围</span><b>{{ "%.0f"|format(min_elev) }} – {{ "%.0f"|format(max_elev) }} m</b>
    <span>相对高差</span><b>{{ "%.0f"|format(max_elev-min_elev) }} m</b>
    <span>主峰海拔</span><b>{{ "%.0f"|format(peak_elev) }} m</b>
    <span>贴图分辨率</span><b>{{ "%.1f"|format(resolution) }} m/px</b>
  </div>
  <div class="src">
    高程：{{ src_dem }}<br>
    影像：{{ src_sat }}<br>
    地标：{{ src_poi }}
  </div>
</div>

<div id="ctrl" class="panel">
  <div>
    <label>垂直夸张 <b id="exagVal">{{ "%.1f"|format(exaggeration) }}×</b></label>
    <input id="exag" type="range" min="1" max="10" step="0.1" value="{{ exaggeration }}">
  </div>
  <div class="btns">
    <button id="reset">重置视角</button>
    <button id="trueScale">真实比例</button>
  </div>
  <div class="btns">
    <button id="modeBtn" class="{{ 'on' if not has_sat else '' }}">{{ '影像贴图' if has_sat else '高程设色' }}</button>
  </div>
  <div class="btns">
    <button id="rotateBtn">自动旋转</button>
    <button id="shotBtn">导出 PNG</button>
  </div>
  <div class="btns">
    <button id="contourBtn" class="on">等高线</button>
    <button id="wireBtn">网格线</button>
  </div>
  <label class="chk"><input id="lmToggle" type="checkbox" checked> 显示地标标注</label>
</div>

<div id="tip">左键拖拽旋转 · 滚轮缩放 · 右键平移</div>
<div id="legend" class="panel">
  <div style="font-size:11.5px;color:var(--sub)">高程设色</div>
  <div class="bar"></div>
  <div class="lg"><span>{{ "%.0f"|format(min_elev) }} m</span><span>{{ "%.0f"|format(max_elev) }} m</span></div>
</div>
<div id="peakLabel" class="lbl">▲ {{ peak_label }}</div>

<script type="module">
{% if embed_three %}
// 内嵌模式：把源码包成 Blob URL 再动态 import（OrbitControls 里的 'three' 裸标识符要改写）
const __unesc = s => s.replace(/<\\\/script/gi, '</script');
const __threeURL = URL.createObjectURL(new Blob(
  [__unesc(document.getElementById('__three_src').textContent)], {type: 'text/javascript'}));
const __orbitURL = URL.createObjectURL(new Blob(
  [__unesc(document.getElementById('__orbit_src').textContent)
     .replace(/from\s*(['"])three\1/g, 'from "' + __threeURL + '"')], {type: 'text/javascript'}));
const THREE = await import(__threeURL);
const { OrbitControls } = await import(__orbitURL);
{% else %}
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
{% endif %}

const META = {{ meta_json }};
const DEM_URL = "data:image/png;base64,{{ dem_b64 }}";
const SAT_URL = {{ ('"data:' + sat_mime + ';base64,' + sat_b64 + '"') if has_sat else 'null' }};
const LANDMARKS = {{ landmarks_json }};

const W = META.size_w, H = META.size_h;
const WORLD = 1000;
const S = WORLD / Math.max(META.width_m, META.height_m);
const BASE_H = WORLD * 0.18;
let exag = {{ exaggeration }};
let pendingExag = null;
let needNormals = false, normTimer = null;

// ---- 墨卡托空间映射（像素行号与墨卡托 Y 线性相关，不能用纬度线性插值）----
const MX0 = META.merc.x0, MX1 = META.merc.x1;
const MY0 = META.merc.y0, MY1 = META.merc.y1;
function mercY(lat){
  const r = lat * Math.PI / 180;
  return (1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2;
}
function lonToX(lon){ return (((lon + 180) / 360 - MX0) / (MX1 - MX0) - 0.5) * WORLD; }
function latToZ(lat){ return ((mercY(lat) - MY0) / (MY1 - MY0) - 0.5) * WORLD; }
function midY(){ return (META.min_elev + META.max_elev) * 0.5 * S * exag; }

let scene, camera, renderer, controls, terrain, plinth, baseElev = null, contourMesh = null;

function setMsg(t, isErr){
  const l = document.getElementById('lmsg');
  l.className = isErr ? 'err' : '';
  l.style.whiteSpace = 'pre-line';
  l.textContent = t;
  if (isErr) document.getElementById('spin').style.display = 'none';
}

// ---- 高程解码：优先用 Web Worker 异步解码（避免大网格 getImageData 阻塞主线程）----
const __DEM_WORKER_SRC = `
self.onmessage = async (ev) => {
  const {dataURL, W, H} = ev.data;
  try {
    const blob = await (await fetch(dataURL)).blob();
    const bmp = await createImageBitmap(blob);
    const c = new OffscreenCanvas(W, H);
    const ctx = c.getContext('2d');
    ctx.drawImage(bmp, 0, 0, W, H);
    const d = ctx.getImageData(0, 0, W, H).data;
    const e = new Float32Array(W * H);
    for (let i = 0, p = 0; i < W * H; i++, p += 4) e[i] = (d[p] * 256 + d[p + 1] + d[p + 2] / 256) - 32768;
    self.postMessage({e: e.buffer}, [e.buffer]);
  } catch (err) {
    self.postMessage({error: String((err && err.message) || err)});
  }
};
`;

function decodeDEMSync(url){
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const c = document.createElement('canvas');
      c.width = W; c.height = H;
      const ctx = c.getContext('2d', { willReadFrequently: true });
      ctx.drawImage(img, 0, 0, W, H);
      const d = ctx.getImageData(0, 0, W, H).data;
      const e = new Float32Array(W * H);
      for (let i = 0, p = 0; i < W * H; i++, p += 4){
        e[i] = (d[p] * 256 + d[p + 1] + d[p + 2] / 256) - 32768;
      }
      resolve(e);
    };
    img.onerror = () => reject(new Error('高程贴图解码失败'));
    img.src = url;
  });
}

function parseDEM(url){
  // 支持 Web Worker + OffscreenCanvas 时异步解码；否则/失败(含超时)时回退主线程同步解码
  if (typeof Worker !== 'undefined' && typeof OffscreenCanvas !== 'undefined' && typeof createImageBitmap === 'function'){
    return new Promise((resolve, reject) => {
      let done = false;
      let worker = null;
      const fallback = () => { if (!done){ done = true; decodeDEMSync(url).then(resolve, reject); } };
      try {
        const blob = new Blob([__DEM_WORKER_SRC], {type: 'text/javascript'});
        worker = new Worker(URL.createObjectURL(blob));
        const timer = setTimeout(() => { try { worker.terminate(); } catch(e){} fallback(); }, 8000);
        worker.onmessage = (ev) => {
          if (done) return;
          if (ev.data.error){ clearTimeout(timer); try { worker.terminate(); } catch(e){} fallback(); return; }
          done = true; clearTimeout(timer); try { worker.terminate(); } catch(e){}
          resolve(new Float32Array(ev.data.e));
        };
        worker.onerror = () => { clearTimeout(timer); fallback(); };
        worker.postMessage({dataURL: url, W, H});
      } catch(e){ fallback(); }
    });
  }
  return decodeDEMSync(url);
}

function sampleElev(lat, lon){
  const fi = ((lon + 180) / 360 - MX0) / (MX1 - MX0) * (W - 1);
  const fj = (mercY(lat) - MY0) / (MY1 - MY0) * (H - 1);
  const i0 = Math.max(0, Math.min(W - 2, Math.floor(fi)));
  const j0 = Math.max(0, Math.min(H - 2, Math.floor(fj)));
  const tx = fi - i0, ty = fj - j0;
  const a = baseElev[j0 * W + i0],       b = baseElev[j0 * W + i0 + 1];
  const c = baseElev[(j0 + 1) * W + i0], d = baseElev[(j0 + 1) * W + i0 + 1];
  return (a * (1 - tx) + b * tx) * (1 - ty) + (c * (1 - tx) + d * tx) * ty;
}

// ---- 高程分层设色 ----
const STOPS = [
  [0.00, 0x2e6b4f], [0.18, 0x4f8f3a], [0.38, 0x9db33a],
  [0.58, 0xc9a227], [0.76, 0xa86b32], [1.00, 0xefeae3]
];
function rampColor(t){
  t = Math.max(0, Math.min(1, t));
  for (let i = 0; i < STOPS.length - 1; i++){
    const a = STOPS[i], b = STOPS[i + 1];
    if (t <= b[0]){
      const k = (t - a[0]) / Math.max(1e-6, b[0] - a[0]);
      const ca = new THREE.Color(a[1]), cb = new THREE.Color(b[1]);
      return ca.lerp(cb, k);
    }
  }
  return new THREE.Color(STOPS[STOPS.length - 1][1]);
}
function buildColors(){
  const arr = new Float32Array(W * H * 3);
  const lo = META.min_elev, hi = Math.max(META.max_elev, lo + 1);
  const c = new THREE.Color();
  for (let i = 0; i < W * H; i++){
    c.copy(rampColor((baseElev[i] - lo) / (hi - lo)));
    arr[i * 3] = c.r; arr[i * 3 + 1] = c.g; arr[i * 3 + 2] = c.b;
  }
  return new THREE.BufferAttribute(arr, 3);
}

async function init(){
  scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0d1117);
  scene.fog = new THREE.Fog(0x0d1117, WORLD * 2.0, WORLD * 6.0);

  camera = new THREE.PerspectiveCamera(52, innerWidth / innerHeight, 1, WORLD * 30);
  renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance', preserveDrawingBuffer: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.setSize(innerWidth, innerHeight);
  document.getElementById('app').appendChild(renderer.domElement);

  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.maxPolarAngle = Math.PI * 0.495;
  controls.minDistance = WORLD * 0.15;
  controls.maxDistance = WORLD * 6;
  resetView();

  scene.add(new THREE.HemisphereLight(0xa8c6ff, 0x2b2f36, 0.75));
  const dir = new THREE.DirectionalLight(0xffffff, 1.35);
  dir.position.set(WORLD * 0.6, WORLD * 1.1, WORLD * 0.35);
  scene.add(dir);
  scene.add(new THREE.AmbientLight(0xffffff, 0.18));

  setMsg('解析高程数据…');
  baseElev = await parseDEM(DEM_URL);

  buildTerrain();
  buildPlinth();
  buildPeak();
  buildLandmarks();

  addEventListener('resize', onResize);
  document.getElementById('loading').classList.add('hide');
  animate();
}

function resetView(){
  const d = WORLD * 1.25, y = midY();
  camera.position.set(d * 0.62, y + d * 0.52, d * 0.78);
  controls.target.set(0, y, 0);
  controls.update();
}

function buildTerrain(){
  const geo = new THREE.PlaneGeometry(WORLD, WORLD, W - 1, H - 1);
  geo.rotateX(-Math.PI / 2);
  geo.setAttribute('color', buildColors());
  applyHeights(geo);
  geo.computeVertexNormals();

  let map = null;
  if (SAT_URL){
    map = new THREE.TextureLoader().load(SAT_URL);
    map.colorSpace = THREE.SRGBColorSpace;
    map.anisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy());
  }
  const mat = new THREE.MeshStandardMaterial({
    map: map,
    vertexColors: !map,
    roughness: 0.92,
    metalness: 0.04,
  });
  terrain = new THREE.Mesh(geo, mat);
  terrain.userData.map = map;
  scene.add(terrain);
  buildContours(geo);  // 等高线叠加层（与地形共用几何，随夸张系数联动）
}

// ---- 等高线叠加：由高程数组生成透明描线纹理，叠在地形上方（卫星/设色两种模式都生效）----
function buildContours(geo){
  const lo = META.min_elev, hi = Math.max(META.max_elev, lo + 1);
  const span = hi - lo;
  // 自动选"好看"的等高距：约 12 条线，取整到 10/20/50/100/200/500
  const rawStep = span / 12;
  const mag = Math.pow(10, Math.floor(Math.log10(Math.max(1, rawStep))));
  const norm = rawStep / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
  const resM = META.resolution_m || (span / Math.max(W, H));
  const band = Math.max(1.0, step * 0.04, resM * 1.6);  // 线宽≈1.6 像素(米)

  const c = document.createElement('canvas');
  c.width = W; c.height = H;
  const ctx = c.getContext('2d');
  const img = ctx.createImageData(W, H);
  const data = img.data;
  for (let i = 0; i < W * H; i++){
    const e = baseElev[i];
    let m = ((e - lo) % step + step) % step;
    const near = Math.min(m, step - m);
    const idx = i * 4;
    if (near < band){
      const a = (1 - near / band) * 120;
      data[idx] = 18; data[idx + 1] = 18; data[idx + 2] = 18; data[idx + 3] = a;
    } else {
      data[idx + 3] = 0;
    }
  }
  ctx.putImageData(img, 0, 0);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy());
  const mat = new THREE.MeshBasicMaterial({
    map: tex, transparent: true, depthWrite: false,
    polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2,
  });
  contourMesh = new THREE.Mesh(geo, mat);
  contourMesh.renderOrder = 2;
  contourMesh.visible = true;
  scene.add(contourMesh);
  const cb = document.getElementById('contourBtn');
  if (cb) cb.classList.toggle('on', true);
}

function applyHeights(geo){
  const pos = geo.attributes.position;
  const y = geo.attributes.position.array;
  for (let j = 0; j < H; j++){
    for (let i = 0; i < W; i++){
      y[(j * W + i) * 3 + 1] = baseElev[j * W + i] * S * exag;
    }
  }
  pos.needsUpdate = true;
}

function buildPlinth(){
  const g = new THREE.BoxGeometry(WORLD, BASE_H, WORLD);
  const m = new THREE.MeshStandardMaterial({ color: 0x12181f, roughness: 1, metalness: 0 });
  plinth = new THREE.Mesh(g, m);
  scene.add(plinth);
  updatePlinth();
}
function updatePlinth(){
  plinth.position.y = META.min_elev * S * exag - BASE_H / 2;
}

function buildPeak(){
  const px = (META.peak_u - 0.5) * WORLD;
  const pz = (META.peak_v - 0.5) * WORLD;
  window._peakPos = new THREE.Vector3(px, META.peak_elev * S * exag, pz);
}

function buildLandmarks(){
  window._lm = [];
  LANDMARKS.forEach(L => {
    const x = lonToX(L.lon), z = latToZ(L.lat);
    const sph = new THREE.Mesh(
      new THREE.SphereGeometry(WORLD * 0.008, 16, 12),
      new THREE.MeshBasicMaterial({ color: L.color })
    );
    scene.add(sph);
    const g = new THREE.BufferGeometry().setFromPoints(
      [new THREE.Vector3(), new THREE.Vector3()]);
    const line = new THREE.Line(g, new THREE.LineBasicMaterial({ color: L.color }));
    scene.add(line);

    const el = document.createElement('div');
    el.className = 'lbl lm';
    const hex = '#' + (L.color >>> 0).toString(16).padStart(6, '0');
    el.style.background = hex;
    el.style.setProperty('--ar', hex);
    el.textContent = L.name;
    document.body.appendChild(el);

    const o = { el, sph, line, top: new THREE.Vector3(), lat: L.lat, lon: L.lon };
    window._lm.push(o);
    updateLandmark(o);
  });
}

function updateLandmark(o){
  const x = lonToX(o.lon), z = latToZ(o.lat);
  const y = sampleElev(o.lat, o.lon) * S * exag;
  const h = WORLD * 0.045;
  o.sph.position.set(x, y, z);
  o.line.geometry.setFromPoints([new THREE.Vector3(x, y, z), new THREE.Vector3(x, y + h, z)]);
  o.line.geometry.attributes.position.needsUpdate = true;
  o.top.set(x, y + h, z);
}

function updateLandmarks(){
  (window._lm || []).forEach(updateLandmark);
}

function project(v){
  const p = v.clone().project(camera);
  if (p.z >= 1 || p.x < -1.05 || p.x > 1.05 || p.y < -1.05 || p.y > 1.05) return null;
  return { x: (p.x * 0.5 + 0.5) * innerWidth, y: (-p.y * 0.5 + 0.5) * innerHeight };
}

let showLM = true;
function updateLabels(){
  const peak = document.getElementById('peakLabel');
  const pp = window._peakPos ? project(window._peakPos) : null;
  if (pp){ peak.style.display = 'block'; peak.style.left = pp.x + 'px'; peak.style.top = pp.y + 'px'; }
  else peak.style.display = 'none';

  (window._lm || []).forEach(o => {
    if (!showLM){ o.el.style.display = 'none'; return; }   // 关键：开关必须在这里生效
    const p = project(o.top);
    if (p){ o.el.style.display = 'block'; o.el.style.left = p.x + 'px'; o.el.style.top = p.y + 'px'; }
    else o.el.style.display = 'none';
  });
}

function onResize(){
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
}

function animate(){
  requestAnimationFrame(animate);
  controls.update();
  if (pendingExag !== null){          // 滑块高频拖动时每帧只重算一次顶点
    exag = pendingExag; pendingExag = null;
    applyHeights(terrain.geometry);
    updatePlinth();
    updateLandmarks();
    buildPeak();
  }
  if (needNormals){                   // 法线开销大，拖完再算
    needNormals = false;
    terrain.geometry.computeVertexNormals();
  }
  updateLabels();
  renderer.render(scene, camera);
}

// ---- 交互 ----
document.getElementById('exag').addEventListener('input', e => {
  const v = parseFloat(e.target.value);
  document.getElementById('exagVal').textContent = v.toFixed(1) + '×';
  pendingExag = v;
  clearTimeout(normTimer);
  normTimer = setTimeout(() => { needNormals = true; }, 150);
});
document.getElementById('reset').addEventListener('click', resetView);
document.getElementById('trueScale').addEventListener('click', () => {
  const s = document.getElementById('exag');
  s.value = 1; s.dispatchEvent(new Event('input'));
});
const modeBtn = document.getElementById('modeBtn');
if (SAT_URL){
  let satMode = true;
  modeBtn.addEventListener('click', () => {
    satMode = !satMode;
    terrain.material.map = satMode ? terrain.material.map || terrain.userData.map : null;
    terrain.material.vertexColors = !satMode;
    terrain.material.needsUpdate = true;
    modeBtn.textContent = satMode ? '影像贴图' : '高程设色';
    modeBtn.classList.toggle('on', !satMode);
    document.getElementById('legend').style.display = satMode ? 'none' : 'block';
  });
} else {
  modeBtn.disabled = true;
  document.getElementById('legend').style.display = 'block';
}
const wireBtn = document.getElementById('wireBtn');
wireBtn.addEventListener('click', () => {
  terrain.material.wireframe = !terrain.material.wireframe;
  wireBtn.classList.toggle('on', terrain.material.wireframe);
});
const contourBtn = document.getElementById('contourBtn');
contourBtn.addEventListener('click', () => {
  if (!contourMesh) return;
  contourMesh.visible = !contourMesh.visible;
  contourBtn.classList.toggle('on', contourMesh.visible);
});
const rotateBtn = document.getElementById('rotateBtn');
let autoRot = false;
rotateBtn.addEventListener('click', () => {
  autoRot = !autoRot;
  controls.autoRotate = autoRot;
  controls.autoRotateSpeed = 0.9;
  rotateBtn.classList.toggle('on', autoRot);
  rotateBtn.textContent = autoRot ? '停止旋转' : '自动旋转';
});
const shotBtn = document.getElementById('shotBtn');
shotBtn.addEventListener('click', () => {
  renderer.render(scene, camera);   // 确保截到当前帧
  const url = renderer.domElement.toDataURL('image/png');
  const a = document.createElement('a');
  a.href = url;
  a.download = (META.place || 'terrain') + '_3d.png';
  document.body.appendChild(a); a.click(); a.remove();
});
document.getElementById('lmToggle').addEventListener('change', e => { showLM = e.target.checked; });
document.getElementById('fold').addEventListener('click', e => {
  const p = document.getElementById('info');
  p.classList.toggle('folded');
  e.target.textContent = p.classList.contains('folded') ? '展开' : '折叠';
});

init().catch(err => {
  console.error(err);
  setMsg('地形加载失败：' + err.message +
{% if embed_three %}
         '\nThree.js 已内嵌在本文件中，与网络无关；多半是当前浏览器未启用 WebGL。' +
         '请在浏览器设置里开启「使用硬件加速」，或换用 Chrome/Edge 打开。', true);
{% else %}
         '\n若提示无法解析 "three"，说明当前网络访问不了 {{ cdn_name }}，' +
         '重新生成时不加 --three cdn（默认内嵌）或换 --cdn unpkg 再试。', true);
{% endif %}
});
</script>
</body>
</html>
"""


def pick_cdn(choice: str) -> tuple[str, str, str]:
    """返回 (name, three_url, addons_url)。auto 时做一次连通性择优。"""
    if choice != "auto":
        if choice not in CDNS:
            raise SystemExit(f"未知 CDN: {choice}，可选 {list(CDNS)}")
        a, b = CDNS[choice]
        return choice, a, b
    try:
        import requests
        for name in ("jsdelivr", "unpkg", "esm.sh"):
            try:
                r = requests.get(CDNS[name][0], timeout=8,
                                 headers={"User-Agent": "geo-3d-terrain/2.0"})
                if r.status_code == 200:
                    return name, CDNS[name][0], CDNS[name][1]
            except Exception:
                continue
    except ImportError:
        pass
    return "jsdelivr", CDNS["jsdelivr"][0], CDNS["jsdelivr"][1]


def fetch_embed_sources(verbose: bool = True) -> tuple[str, str] | None:
    """下载并缓存 three.module.js / OrbitControls.js 源码，返回 (three_src, orbit_src)。
    失败返回 None（调用方应回退 CDN）。缓存目录 ~/.cache/geo-3d-terrain/three/。"""
    cache_dir = Path.home() / ".cache" / "geo-3d-terrain" / "three" / THREE_VERSION
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = []
    try:
        import requests
    except ImportError:
        return None
    for key, fname in (("three", "three.module.js"), ("orbit", "OrbitControls.js")):
        fp = cache_dir / fname
        if fp.exists() and fp.stat().st_size > 10000:
            out.append(fp.read_text(encoding="utf-8"))
            continue
        ok = False
        for base in (f"https://cdn.jsdelivr.net/npm/three@{THREE_VERSION}",
                     f"https://unpkg.com/three@{THREE_VERSION}"):
            url = (base + "/build/" + fname) if key == "three" else (base + "/examples/jsm/controls/" + fname)
            try:
                r = requests.get(url, timeout=25, headers={"User-Agent": "geo-3d-terrain/2.1"})
                if r.status_code == 200 and len(r.content) > 10000:
                    txt = r.content.decode("utf-8", "replace")
                    fp.write_text(txt, encoding="utf-8")
                    out.append(txt)
                    ok = True
                    break
            except Exception:
                continue
        if not ok:
            return None
    if verbose:
        print(f"    Three.js 已内嵌（{len(out[0]) // 1024} KB + {len(out[1]) // 1024} KB），离线可用",
              file=sys.stderr)
    return out[0], out[1]


def auto_exaggeration(meta: dict) -> float:
    """让相对高差约占世界宽度的 18%，兼顾起伏观感与真实性。"""
    relief = max(1.0, meta["max_elev"] - meta["min_elev"])
    world_w = 1000.0
    s = world_w / max(1.0, meta["width_m"], meta["height_m"])
    raw = 0.18 * world_w / max(1e-6, relief * s)
    return round(min(10.0, max(1.0, raw)), 1)


def render(data: dict, out_path: str, exaggeration: float | None = None,
           cdn: str = "auto", place_override: str | None = None,
           verbose: bool = True, three: str = "auto") -> str:
    from jinja2 import Template

    meta = data["meta"]
    place = place_override or meta.get("place") or meta.get("matched_name") or "地形"
    if exaggeration is None:
        exaggeration = auto_exaggeration(meta)

    has_sat = bool(meta.get("has_sat") and data.get("sat_b64"))

    # three: auto = 尽量内嵌（离线可看），cdn = 强制走 CDN，embed = 强制内嵌
    embed_three = False
    three_src = orbit_src = ""
    if three in ("auto", "embed"):
        got = fetch_embed_sources(verbose=verbose)
        if got:
            embed_three = True
            # </script 会提前闭合 text/plain 节点，必须转义
            three_src, orbit_src = (s.replace("</script", "<\\/script") for s in got)
        elif three == "embed":
            raise SystemExit("内嵌 Three.js 失败：下载不到源码，请检查网络或改用 --three cdn")
        elif verbose:
            print("    ! 内嵌 Three.js 失败，回退 CDN（离线打开会白屏）", file=sys.stderr)

    cdn_name, three_url, three_addons = pick_cdn(cdn)

    peak_name = (meta.get("peak_name") or "").strip()
    peak_label = f"{peak_name} {meta['peak_elev']:.0f} m" if peak_name else f"主峰 {meta['peak_elev']:.0f} m"

    src = meta.get("sources", {}) or {}
    html = Template(HTML_TEMPLATE).render(
        place=place,
        center_lat=meta["center_lat"], center_lon=meta["center_lon"],
        width_m=meta["width_m"], height_m=meta["height_m"],
        min_elev=meta["min_elev"], max_elev=meta["max_elev"],
        peak_elev=meta["peak_elev"], peak_label=peak_label,
        resolution=meta.get("sat_resolution_m") or meta.get("resolution_m", 0),
        exaggeration=exaggeration,
        has_sat=has_sat,
        dem_b64=data["dem_b64"],
        sat_b64=data.get("sat_b64", ""),
        sat_mime=data.get("sat_mime", "image/jpeg"),
        meta_json=json.dumps(meta, ensure_ascii=False),
        landmarks_json=json.dumps(data.get("landmarks", []), ensure_ascii=False),
        three_url=three_url, three_addons=three_addons, cdn_name=cdn_name,
        embed_three=embed_three, three_src=three_src, orbit_src=orbit_src,
        src_dem=src.get("dem", "AWS Terrarium"),
        src_sat=src.get("sat") or "未使用",
        src_poi=src.get("poi", "OpenStreetMap"),
    )

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    if verbose:
        size_kb = Path(out_path).stat().st_size / 1024
        print(f"[✓] HTML 已生成: {out_path}  ({size_kb:.0f} KB)", file=sys.stderr)
        mode = "Three.js 内嵌（离线可用）" if embed_three else f"CDN {cdn_name}（需联网）"
        print(f"    垂直夸张 {exaggeration}×  ·  {mode}", file=sys.stderr)
    return out_path


def main():
    ap = argparse.ArgumentParser(description="生成三维地形 HTML（v2）",
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--data", required=True, help="terrain.json 路径")
    ap.add_argument("--out", default="terrain.html", help="输出 HTML 路径")
    ap.add_argument("--exaggeration", type=float, default=None, help="垂直夸张系数（默认自动推荐）")
    ap.add_argument("--cdn", default="auto", help="CDN 源 auto|jsdelivr|unpkg|esm.sh（仅 --three cdn 时生效）")
    ap.add_argument("--three", default="auto", choices=["auto", "embed", "cdn"],
                    help="Three.js 引入方式：auto=优先内嵌, embed=强制内嵌, cdn=外链")
    ap.add_argument("--place-override", default=None, help="覆盖标题地名")
    args = ap.parse_args()

    with open(args.data, "r", encoding="utf-8") as f:
        data = json.load(f)
    render(data, args.out, exaggeration=args.exaggeration, cdn=args.cdn,
           place_override=args.place_override, three=args.three)


if __name__ == "__main__":
    main()
