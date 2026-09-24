#!/usr/bin/env python3
"""
generate_html.py  (v2.9.1)
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
  * v2.4: 等高线叠加、自动旋转、导出 PNG
  * v2.5: **GeoJSON 叠加图形**——把点/线/面按真实高程"贴"到地形表面（面填充 + 描边丝带
    + 点标记 + 名称标签），随垂直夸张滑块实时联动；支持把 .geojson 直接拖进页面即时叠加
  * v2.6: 信息面板补显「源影像 z17（1.01 m/px）」——从 meta 的 sat_zoom / sat_native_m 读，
  * v2.7: 去掉 WebGLRenderer 的 preserveDrawingBuffer（每帧 GPU 开销，只为导出 PNG 保留；
    导出前本就显式 render 再 toDataURL，无需常驻缓冲），交互帧率更低耗电更省
  * v2.8: **按需渲染**——相机未移动、未拖滑块、未开自动旋转时不提交 GPU 绘制
    （实测静止 3 秒 0 次 draw，拖拽 1 秒 496 次，交互不受影响）；标签位置复用临时向量、
    坐标未变时不写 DOM，去掉每帧的临时对象与样式重算
    与贴图分辨率并列，方便一眼判断清晰度瓶颈在源侧还是纹理侧
  * v2.9: **业务工具**——测距（水平/高差/坡度/直线）、两点高程剖面（含累计爬升）、
    导出 GLB（真实比例 1×，内嵌 GLTFExporter + TextureUtils，离线也能导出）、
    底部 HUD（比例尺 / 光标经纬高 / 指北针）；AVIF 纹理解码失败时自动回退高程设色并提示
  * v2.9.1: 世界平面按米数走（WX/WZ），修掉长条范围下垂直夸张的各向异性失真；
    取点改高度场射线步进（实测 36.7 → 0.04 ms/次）；GLB 导出器只装配一次，
    未内嵌时按钮直接禁用；暴露 window.__geo3dPick / __geo3dDebug 供校验脚本断言

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
  .ovl{position:fixed;transform:translate(-50%,-50%);pointer-events:none;z-index:9;
    font-size:11.5px;font-weight:600;padding:3px 8px;border-radius:999px;white-space:nowrap;
    background:rgba(9,13,19,.82);color:#e9f1fa;border:1px solid rgba(255,255,255,.35);
    box-shadow:0 3px 12px rgba(0,0,0,.5);display:none}
  #drop{position:fixed;inset:0;z-index:70;display:none;align-items:center;justify-content:center;
    background:rgba(8,12,18,.72);backdrop-filter:blur(3px);color:#e8eef5;font-size:15px;
    border:2px dashed rgba(78,161,255,.75);border-radius:0}
  #drop.on{display:flex}
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
  /* ---- v2.9 底部 HUD：比例尺 / 指北针 / 光标经纬高 ---- */
  #hud{position:fixed;left:12px;bottom:12px;z-index:55;display:flex;align-items:flex-end;gap:12px;
       font-size:11.5px;color:var(--sub);pointer-events:none;text-shadow:0 1px 3px #000}
  #scale .bar{height:6px;border:1px solid var(--sub);border-top:none;min-width:40px}
  #scale .tx{display:block;text-align:center;margin-bottom:2px;white-space:nowrap}
  #compass{position:fixed;right:12px;bottom:12px;width:48px;height:48px;border-radius:50%;
           border:1px solid var(--line);background:rgba(13,17,23,.55);z-index:55;
           display:flex;align-items:center;justify-content:center;pointer-events:none}
  #compass .n{position:absolute;top:2px;font-size:9px;color:var(--sub)}
  #compass .nd{width:2px;height:17px;border-radius:1px;transform-origin:50% 100%;
               background:linear-gradient(to top,rgba(230,237,243,.15),#e6edf3)}
  #prof{position:fixed;left:50%;transform:translateX(-50%);bottom:14px;z-index:65;padding:10px 12px;display:none}
  #prof .hd{display:flex;justify-content:space-between;align-items:center;gap:16px;
            font-size:12px;color:var(--txt);margin-bottom:6px}
  #prof .hd button{background:transparent;border:1px solid var(--line);color:var(--sub);
                   border-radius:4px;padding:0 6px;cursor:pointer}
  #prof canvas{width:100%;height:auto;display:block;border-radius:4px;background:#0b1017}
  #prof .sub{margin-top:6px;font-size:11px}
  .mk{position:fixed;z-index:56;transform:translate(-50%,-50%);width:9px;height:9px;border-radius:50%;
      background:#ffd166;border:1.5px solid #1a1a1a;box-shadow:0 0 0 2px rgba(0,0,0,.35)}
  .mkLbl{position:fixed;z-index:56;font-size:11px;color:#ffd166;background:rgba(13,17,23,.75);
         border:1px solid var(--line);border-radius:4px;padding:2px 6px;white-space:nowrap}
  @media (max-height:560px){ #info .src{display:none} }
</style>
{% if embed_three %}
<!-- Three.js 已内嵌（离线可用，不依赖任何 CDN） -->
<script id="__three_src" type="text/plain">{{ three_src }}</script>
<script id="__orbit_src" type="text/plain">{{ orbit_src }}</script>
{% if gltf_src %}<script id="__gltf_src" type="text/plain">{{ gltf_src }}</script>
<script id="__texutils_src" type="text/plain">{{ texutils_src }}</script>
{% endif %}{% else %}
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
    <span>最高海拔</span><b>{{ "%.0f"|format(peak_elev) }} m</b>
    <span>贴图分辨率</span><b>{{ "%.1f"|format(resolution) }} m/px{% if sat_native_m %} · 源 z{{ sat_zoom }}（{{ "%.2f"|format(sat_native_m) }} m/px）{% endif %}</b>
{% if has_overlays %}    <span>叠加图形</span><b>{{ ov_count }} 个</b>
{% endif %}  </div>
  <div class="src">
    高程：{{ src_dem }}<br>
    影像：{{ src_sat }}<br>
    地标：{{ src_poi }}
{% if has_overlays %}    <br>图形：{{ ov_source }}
{% endif %}  </div>
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
  <div class="btns">
    <button id="measBtn">测距</button>
    <button id="profBtn">剖面</button>
  </div>
  <div class="btns">
    <button id="glbBtn"{% if not has_gltf %} disabled title="未内嵌 GLB 导出器（离线导出不可用）"{% endif %}>导出 GLB</button>
    <button id="clrBtn">清除标记</button>
  </div>
{% if has_overlays %}  <div class="btns">
    <button id="ovBtn" class="on">图形叠加</button>
    <button id="ovNameBtn" class="on">图形名称</button>
  </div>
{% endif %}  <label class="chk"><input id="lmToggle" type="checkbox" checked> 显示地标标注</label>
</div>

<div id="tip">左键拖拽旋转 · 滚轮缩放 · 右键平移{% if has_overlays %}<br>可将 .geojson 文件拖入页面即时叠加{% endif %}</div>
<div id="drop"><div>松手即把 GeoJSON 叠到地形上</div></div>
<div id="legend" class="panel">
  <div style="font-size:11.5px;color:var(--sub)">高程设色</div>
  <div class="bar"></div>
  <div class="lg"><span>{{ "%.0f"|format(min_elev) }} m</span><span>{{ "%.0f"|format(max_elev) }} m</span></div>
</div>
<div id="peakLabel" class="lbl">▲ {{ peak_label }}</div>

<div id="hud">
  <div id="scale"><span class="tx" id="scaleTx">—</span><div class="bar" id="scaleBar"></div></div>
  <div id="cursor">—</div>
</div>
<div id="compass"><div class="n">N</div><div class="nd" id="needle"></div></div>
<div id="prof" class="panel">
  <div class="hd"><span>高程剖面</span><button id="profClose">关闭</button></div>
  <canvas id="profCv" width="660" height="170"></canvas>
  <div class="sub" id="profTx"></div>
</div>

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
const SAT_MIME = "{{ sat_mime }}";
// v2.10 分块纹理：非空时按 META.sat_tiling 网格合成一张大 CanvasTexture（等效长边可达 8192px）
const SAT_TILES = {{ sat_tiles_json }};
const LANDMARKS = {{ landmarks_json }};
const OVERLAYS = {{ overlays_json }};

const W = META.size_w, H = META.size_h;
const WORLD = 1000;                       // 场景尺度单位（相机距离/雾/光照都按它算）
const S = WORLD / Math.max(META.width_m, META.height_m);
// 世界平面的**实际**宽高：v2.9 前平面恒为 WORLD×WORLD 正方形，但 x 方向 1 单位 = width_m/WORLD 米、
// z 方向 1 单位 = height_m/WORLD 米，两者不等时垂直方向（1 单位 = 1/S 米）只能和其中一个对上，
// 于是长边方向的坡会被"压平"短边方向的坡会被"拔高"（GeoJSON 长条范围下肉眼可见失真）。
// 现在让平面尺寸也按米数走：x/z 两个方向统一为 1 单位 = 1/S 米，垂直夸张才在各方向一致。
const WX = META.width_m * S;
const WZ = META.height_m * S;
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
function lonToX(lon){ return (((lon + 180) / 360 - MX0) / (MX1 - MX0) - 0.5) * WX; }
function latToZ(lat){ return ((mercY(lat) - MY0) / (MY1 - MY0) - 0.5) * WZ; }
function midY(){ return (META.min_elev + META.max_elev) * 0.5 * S * exag; }

let scene, camera, renderer, controls, terrain, plinth, baseElev = null, contourMesh = null;
let ovGroup = null; const ovMeshes = [], ovLabels = [];
let showOv = true, showOvName = true;
const OV_LIFT = WORLD * 0.0012;

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
  renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
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

  setMsg('加载卫星影像…');
  const satMap = await loadSatTexture();
  buildTerrain(satMap);
  buildPlinth();
  buildPeak();
  buildLandmarks();
  buildOverlays(OVERLAYS);
  setupTools();          // 依赖 renderer / terrain，必须在它们就绪后挂事件

  addEventListener('resize', onResize);
  document.getElementById('loading').classList.add('hide');
  window.__geo3dReady = true;      // 校验脚本等这个标记，别改成异步后再设
  // 调试钩子（体积可忽略）：控制台里可做取点/渲染相关的手工检查与性能测量
  window.__geo3dDebug = { THREE, META, WX, WZ, S, M_PER_UNIT,
                          get scene(){ return scene; }, get terrain(){ return terrain; },
                          get camera(){ return camera; },
                          get texSize(){ const im = terrain && terrain.userData.map && terrain.userData.map.image;
                                         return im ? [im.width, im.height] : null; } };
  animate();
}

function resetView(){
  const d = WORLD * 1.25, y = midY();
  camera.position.set(d * 0.62, y + d * 0.52, d * 0.78);
  controls.target.set(0, y, 0);
  controls.update();
}

// 卫星纹理必须**先加载完再建地形**：v2.8 的按需渲染只在「相机动了/有 dirty 标记」时才画，
// 若首帧先以无贴图状态渲染，纹理就绪后不会再重绘，用户看到的就是一片发暗的地形
// （实测静止首帧画面平均亮度 22.6，手动拖一下相机才升到 45.4）。
function loadSatTexture(){
  if (!SAT_URL && !SAT_TILES.length) return Promise.resolve(null);
  if (!SAT_TILES.length) return new Promise(resolve => {
    let done = false;
    const finish = tex => { markDirty(); if (!done){ done = true; resolve(tex); } };
    try{
      new THREE.TextureLoader().load(SAT_URL, tex => {
        tex.colorSpace = THREE.SRGBColorSpace;
        tex.anisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy());
        finish(tex);
      }, undefined, () => {
        // AVIF 体积小但老浏览器解不了（Chrome<85 / Safari<16）：回退高程设色，别白屏
        console.warn('卫星纹理解码失败：' + SAT_MIME);
        const tip = document.getElementById('tip');
        if (tip) tip.textContent = '当前浏览器无法解码 ' + SAT_MIME
          + ' 纹理，已回退高程设色（如需影像贴图，重新生成时改用 --sat-format jpeg）';
        const mb = document.getElementById('modeBtn');
        if (mb && !mb.disabled) mb.click();
        finish(null);
      });
      setTimeout(() => finish(null), 20000);   // 兜底：解码再慢也不能卡住整个 init
    } catch(e){ finish(null); }
  });
  // v2.10 分块纹理：解码全部块 → 按 sat_tiling 网格拼到一张 canvas（等效大纹理）。
  // canvas 尺寸受 maxTextureSize 保护：超限就整体等比缩小，老 GPU 不会白屏只会稍糊。
  return new Promise(resolve => {
    let done = false;
    const finish = tex => { markDirty(); if (!done){ done = true; resolve(tex); } };
    const loadImg = src => new Promise((ok, bad) => {
      const im = new Image();
      im.onload = () => ok(im);
      im.onerror = () => bad(new Error('tile decode failed'));
      im.src = src;
    });
    const tn = META.sat_tiling || [1, 1];
    const FW = META.size_sat_w || 0, FH = META.size_sat_h || 0;
    Promise.all(SAT_TILES.map(t => loadImg('data:' + t.mime + ';base64,' + t.b64)))
      .then(imgs => {
        const maxTex = renderer.capabilities.maxTextureSize || 4096;
        const scale = Math.min(1, maxTex / Math.max(FW, FH));
        const c = document.createElement('canvas');
        c.width = Math.max(2, Math.round(FW * scale));
        c.height = Math.max(2, Math.round(FH * scale));
        const ctx = c.getContext('2d');
        imgs.forEach((im, i) => {
          const cx = i % tn[0], cy = Math.floor(i / tn[0]);
          const x0 = Math.round(cx * FW / tn[0]), x1 = Math.round((cx + 1) * FW / tn[0]);
          const y0 = Math.round(cy * FH / tn[1]), y1 = Math.round((cy + 1) * FH / tn[1]);
          ctx.drawImage(im, Math.round(x0 * scale), Math.round(y0 * scale),
                        Math.round((x1 - x0) * scale), Math.round((y1 - y0) * scale));
        });
        const tex = new THREE.CanvasTexture(c);
        tex.colorSpace = THREE.SRGBColorSpace;
        tex.anisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy());
        finish(tex);
      })
      .catch(() => {
        console.warn('分块卫星纹理解码失败');
        const tip = document.getElementById('tip');
        if (tip) tip.textContent = '当前浏览器无法解码分块卫星纹理，已回退高程设色';
        const mb = document.getElementById('modeBtn');
        if (mb && !mb.disabled) mb.click();
        finish(null);
      });
  });
}

function buildTerrain(map){
  const geo = new THREE.PlaneGeometry(WX, WZ, W - 1, H - 1);
  geo.rotateX(-Math.PI / 2);
  geo.setAttribute('color', buildColors());
  applyHeights(geo);
  geo.computeVertexNormals();

  const mat = new THREE.MeshStandardMaterial({
    map: map,
    vertexColors: !map,
    roughness: 0.92,
    metalness: 0.04,
  });
  terrain = new THREE.Mesh(geo, mat);
  terrain.userData.map = map;
  // 给外部校验脚本用：v2.9 踩过「按需渲染下纹理就绪不再重绘 → 首屏一片发暗」的坑，
  // 有这个标记后 check_render.js 就能断言"建地形时纹理确实已经就位"
  window.__geo3dTexReady = !!(map && map.image && map.image.width > 1);
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
  const g = new THREE.BoxGeometry(WX, BASE_H, WZ);
  const m = new THREE.MeshStandardMaterial({ color: 0x12181f, roughness: 1, metalness: 0 });
  plinth = new THREE.Mesh(g, m);
  scene.add(plinth);
  updatePlinth();
}
function updatePlinth(){
  plinth.position.y = META.min_elev * S * exag - BASE_H / 2;
}

function buildPeak(){
  const px = (META.peak_u - 0.5) * WX;
  const pz = (META.peak_v - 0.5) * WZ;
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
    el.textContent = ((L.icon ? L.icon + " " : "") + L.name);
    el.title = L.name + (L.desc ? "（" + L.desc + "）" : "");
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

// ==================================================================
// GeoJSON 叠加图形：点/线/面按真实高程"贴"到地形表面，随夸张系数联动
// ==================================================================
function newGroup(){
  const g = new THREE.Group();
  g.renderOrder = 3;
  scene.add(g);
  return g;
}

function clearGroup(){
  if (!ovGroup) return;
  scene.remove(ovGroup);
  ovGroup.traverse(o => {
    if (o.geometry) o.geometry.dispose();
    if (o.material) o.material.dispose();
  });
  ovGroup = null;
  ovMeshes.length = 0;
  ovLabels.forEach(o => o.el.remove());
  ovLabels.length = 0;
}

// 沿线加密：让折线采样密度接近 DEM 分辨率，才能贴着地形起伏走
function densify(pts){
  const step = Math.max(1, (META.resolution_m || 30) * 1.5);
  const out = [pts[0]];
  for (let i = 0; i < pts.length - 1; i++){
    const a = pts[i], b = pts[i + 1];
    const kx = 111320 * Math.cos(a[1] * Math.PI / 180), ky = 110540;
    const d = Math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * ky);
    const n = Math.min(3000, Math.max(1, Math.ceil(d / step)));
    for (let k = 1; k <= n; k++){
      const t = k / n;
      out.push([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]);
    }
  }
  return out;
}

function llToXZ(ll){
  const XZ = new Float32Array(ll.length * 2);
  for (let i = 0; i < ll.length; i++){
    XZ[i * 2] = lonToX(ll[i][0]);
    XZ[i * 2 + 1] = latToZ(ll[i][1]);
  }
  return XZ;
}

// 把一条线做成有宽度的"丝带"（LineBasicMaterial 的 linewidth 在多数平台被忽略）
function ribbonMesh(ll, colorHex, widthPx, lift){
  const n = ll.length;
  if (n < 2) return null;
  const XZ = llToXZ(ll);
  const nor = new Float32Array(n * 2);
  for (let i = 0; i < n; i++){
    const a = Math.max(0, i - 1) * 2, b = Math.min(n - 1, i + 1) * 2;
    let dx = XZ[b] - XZ[a], dz = XZ[b + 1] - XZ[a + 1];
    const L = Math.hypot(dx, dz) || 1;
    nor[i * 2] = -dz / L; nor[i * 2 + 1] = dx / L;
  }
  const half = Math.max(WORLD * 0.0012, WORLD * 0.0035 * Math.min(2.5, Math.max(0.5, widthPx / 2))) / 2;
  const tri = [], triLL = [];
  for (let i = 0; i < n - 1; i++){
    const p0 = i * 2, p1 = (i + 1) * 2;
    const A = [XZ[p0] + nor[p0] * half, XZ[p0 + 1] + nor[p0 + 1] * half];
    const B = [XZ[p0] - nor[p0] * half, XZ[p0 + 1] - nor[p0 + 1] * half];
    const C = [XZ[p1] + nor[p1] * half, XZ[p1 + 1] + nor[p1 + 1] * half];
    const D = [XZ[p1] - nor[p1] * half, XZ[p1 + 1] - nor[p1 + 1] * half];
    tri.push(A, B, C, B, D, C);
    triLL.push(ll[i], ll[i], ll[i + 1], ll[i], ll[i + 1], ll[i + 1]);
  }
  const m = tri.length;
  if (!m) return null;
  const pos = new Float32Array(m * 3);
  const lls = new Float32Array(m * 2);
  for (let i = 0; i < m; i++){
    pos[i * 3] = tri[i][0]; pos[i * 3 + 1] = 0; pos[i * 3 + 2] = tri[i][1];
    lls[i * 2] = triLL[i][0]; lls[i * 2 + 1] = triLL[i][1];
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  const mat = new THREE.MeshBasicMaterial({
    color: colorHex, side: THREE.DoubleSide, transparent: true, opacity: 0.96,
    depthWrite: false, polygonOffset: true, polygonOffsetFactor: -4, polygonOffsetUnits: -4,
  });
  const mesh = new THREE.Mesh(geo, mat);
  mesh.userData.ll = lls;
  mesh.userData.lift = lift === undefined ? OV_LIFT : lift;
  mesh.renderOrder = 4;
  return mesh;
}

function trimRing(ring){
  const n = ring.length;
  if (n > 1 && ring[0][0] === ring[n - 1][0] && ring[0][1] === ring[n - 1][1]) return ring.slice(0, -1);
  return ring;
}

// 面填充：用 ShapeUtils 三角化后逐顶点采样高程
function fillMesh(outer, holes, colorHex, opacity, lift){
  const o = trimRing(outer);
  if (o.length < 3) return null;
  const hs = (holes || []).map(trimRing).filter(h => h.length >= 3);
  const c2 = o.map(p => new THREE.Vector2(lonToX(p[0]), latToZ(p[1])));
  const h2 = hs.map(h => h.map(p => new THREE.Vector2(lonToX(p[0]), latToZ(p[1]))));
  let faces;
  try { faces = THREE.ShapeUtils.triangulateShape(c2, h2); }
  catch (e){ faces = []; }
  if (!faces || !faces.length) return null;
  const llAll = o.concat(...hs);
  const m = faces.length * 3;
  const pos = new Float32Array(m * 3);
  const lls = new Float32Array(m * 2);
  for (let i = 0; i < faces.length; i++){
    for (let k = 0; k < 3; k++){
      const vi = i * 3 + k;
      const p = llAll[faces[i][k]];
      if (!p){ continue; }
      pos[vi * 3] = lonToX(p[0]);
      pos[vi * 3 + 1] = 0;
      pos[vi * 3 + 2] = latToZ(p[1]);
      lls[vi * 2] = p[0];
      lls[vi * 2 + 1] = p[1];
    }
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  const mat = new THREE.MeshBasicMaterial({
    color: colorHex, side: THREE.DoubleSide, transparent: true,
    opacity: Math.max(0.05, Math.min(0.9, opacity)),
    depthWrite: false, polygonOffset: true, polygonOffsetFactor: -3, polygonOffsetUnits: -3,
  });
  const mesh = new THREE.Mesh(geo, mat);
  mesh.userData.ll = lls;
  mesh.userData.lift = lift === undefined ? OV_LIFT * 0.6 : lift;
  mesh.renderOrder = 3;
  return mesh;
}

// 逐顶点重算 y（夸张系数变化时调）；点标记只挪 position
function resampleHeights(mesh){
  const ll = mesh.userData.ll;
  if (!ll || !ll.length) return;
  const lift = mesh.userData.lift || 0;
  if (mesh.userData.isPoint){
    mesh.position.set(lonToX(ll[0]), sampleElev(ll[1], ll[0]) * S * exag + lift, latToZ(ll[1]));
    return;
  }
  const pos = mesh.geometry.attributes.position;
  if (!pos) return;
  const y = pos.array;
  for (let i = 0; i < ll.length / 2; i++){
    y[i * 3 + 1] = sampleElev(ll[i * 2 + 1], ll[i * 2]) * S * exag + lift;
  }
  pos.needsUpdate = true;
  mesh.geometry.computeBoundingSphere();
}

function addOvLabel(anchorLL, text, colorHex){
  const el = document.createElement('div');
  el.className = 'ovl';
  el.style.borderColor = colorHex;
  el.style.color = '#fff';
  el.textContent = text;
  document.body.appendChild(el);
  const o = { el, ll: anchorLL };
  ovLabels.push(o);
  return o;
}

function hexOf(c){ return '#' + (c >>> 0).toString(16).padStart(6, '0'); }

function buildOverlays(list){
  clearGroup();
  if (!list || !list.length) return;
  ovGroup = newGroup();
  list.forEach(ov => {
    const colorHex = hexOf(ov.color === undefined ? 0x4ea1ff : ov.color);
    const wpx = ov.width === undefined || ov.width === null ? 2 : ov.width;
    if (ov.kind === 'polygon'){
      const fillHex = hexOf(ov.fill === undefined || ov.fill === null ? ov.color : ov.fill);
      const fm = fillMesh(ov.outer, ov.holes, fillHex, ov.fillOpacity, OV_LIFT * 0.6);
      if (fm){ ovGroup.add(fm); ovMeshes.push(fm); }
      const rings = [ov.outer].concat(ov.holes || []);
      rings.forEach(ring => {
        const rm = ribbonMesh(densify(ring), colorHex, wpx, OV_LIFT);
        if (rm){ ovGroup.add(rm); ovMeshes.push(rm); }
      });
      if (ov.name){
        let best = ov.outer[0], bestE = -1e9;
        ov.outer.forEach(p => { const e = sampleElev(p[1], p[0]); if (e > bestE){ bestE = e; best = p; } });
        addOvLabel(best, ov.name, colorHex);
      }
    } else if (ov.kind === 'line'){
      const rm = ribbonMesh(densify(ov.path), colorHex, wpx, OV_LIFT);
      if (rm){ ovGroup.add(rm); ovMeshes.push(rm); }
      if (ov.name) addOvLabel(ov.path[Math.floor(ov.path.length / 2)], ov.name, colorHex);
    } else {
      const r = Math.max(WORLD * 0.004, WORLD * 0.006 * Math.min(2.5, Math.max(0.5, (ov.r || 6) / 6)));
      const sph = new THREE.Mesh(
        new THREE.SphereGeometry(r, 14, 10),
        new THREE.MeshBasicMaterial({ color: colorHex })
      );
      const llf = new Float32Array([ov.lon, ov.lat]);
      sph.userData.ll = llf;
      sph.userData.lift = OV_LIFT;
      sph.userData.isPoint = true;
      ovGroup.add(sph); ovMeshes.push(sph);
      if (ov.name) addOvLabel([ov.lon, ov.lat], ov.name, colorHex);
    }
  });
  ovMeshes.forEach(resampleHeights);
  ovGroup.visible = showOv;
}

function updateOverlayHeights(){ ovMeshes.forEach(resampleHeights); }

const _ovv = new THREE.Vector3();
function updateOvLabels(){
  ovLabels.forEach(o => {
    if (!showOv || !showOvName){ o.el.style.display = 'none'; return; }
    _ovv.set(lonToX(o.ll[0]), sampleElev(o.ll[1], o.ll[0]) * S * exag + OV_LIFT * 8, latToZ(o.ll[1]));
    place(o.el, project(_ovv));
  });
}

// 复用同一个临时向量 + 复用返回对象，避免每帧每标签都产生垃圾
const _pv = new THREE.Vector3();
const _pp = { x: 0, y: 0 };
function project(v){
  const p = _pv.copy(v).project(camera);
  if (p.z >= 1 || p.x < -1.05 || p.x > 1.05 || p.y < -1.05 || p.y > 1.05) return null;
  _pp.x = (p.x * 0.5 + 0.5) * innerWidth;
  _pp.y = (-p.y * 0.5 + 0.5) * innerHeight;
  return _pp;
}

// 位置没变就别写 DOM（style 写入会触发样式重算，逐帧写纯属浪费）
function place(el, p){
  if (!p){ if (el.style.display !== 'none') el.style.display = 'none'; return; }
  if (el.style.display !== 'block') el.style.display = 'block';
  const x = Math.round(p.x), y = Math.round(p.y);
  if (el._px !== x){ el._px = x; el.style.left = x + 'px'; }
  if (el._py !== y){ el._py = y; el.style.top = y + 'px'; }
}

let showLM = true;
function updateLabels(){
  const peak = document.getElementById('peakLabel');
  place(peak, window._peakPos ? project(window._peakPos) : null);

  (window._lm || []).forEach(o => {
    if (!showLM){ o.el.style.display = 'none'; return; }   // 关键：开关必须在这里生效
    place(o.el, project(o.top));
  });

  updateOvLabels();
}

function onResize(){
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
  markDirty();
}

// 按需渲染：相机没动、没在拖滑块、没开自动旋转时，不再提交 GPU 绘制（省电、降温、风扇不转）
let dirty = true;
const _lastCam = new THREE.Matrix4();
function markDirty(){ dirty = true; }

function animate(){
  requestAnimationFrame(animate);
  controls.update();
  camera.updateMatrixWorld();
  const moved = !_lastCam.equals(camera.matrixWorld);
  if (!(moved || autoRot || pendingExag !== null || needNormals || dirty)) return;
  if (moved) _lastCam.copy(camera.matrixWorld);
  dirty = false;
  if (pendingExag !== null){          // 滑块高频拖动时每帧只重算一次顶点
    exag = pendingExag; pendingExag = null;
    applyHeights(terrain.geometry);
    updatePlinth();
    updateLandmarks();
    updateOverlayHeights();
    if (mkLine) resampleHeights(mkLine);   // 测量线也要跟着贴地
    buildPeak();
  }
  if (needNormals){                   // 法线开销大，拖完再算
    needNormals = false;
    terrain.geometry.computeVertexNormals();
  }
  updateLabels();
  updateMarks();
  updateHud();
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
    markDirty();
  });
} else {
  modeBtn.disabled = true;
  document.getElementById('legend').style.display = 'block';
}
const wireBtn = document.getElementById('wireBtn');
wireBtn.addEventListener('click', () => {
  terrain.material.wireframe = !terrain.material.wireframe;
  wireBtn.classList.toggle('on', terrain.material.wireframe);
  markDirty();
});
const contourBtn = document.getElementById('contourBtn');
contourBtn.addEventListener('click', () => {
  if (!contourMesh) return;
  contourMesh.visible = !contourMesh.visible;
  contourBtn.classList.toggle('on', contourMesh.visible);
  markDirty();
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
document.getElementById('lmToggle').addEventListener('change', e => { showLM = e.target.checked; markDirty(); });

// ================= v2.9 业务工具：测距 / 剖面 / 导出 GLB / 指北针 / 比例尺 =================
// 世界坐标 → 经纬度（lonToX / latToZ 的逆变换，仍在墨卡托空间做，别用纬度线性插值）
function xToLon(x){ return (((x / WX + 0.5) * (MX1 - MX0) + MX0) * 360) - 180; }
function zToLat(z){
  const my = (z / WZ + 0.5) * (MY1 - MY0) + MY0;
  return (2 * Math.atan(Math.exp(Math.PI * (1 - 2 * my))) - Math.PI / 2) * 180 / Math.PI;
}
function hDist(a, b){
  const R = 6371008.8, p = Math.PI / 180;
  const dLat = (b.lat - a.lat) * p, dLon = (b.lon - a.lon) * p;
  const s = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * p) * Math.cos(b.lat * p) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(s)));
}
const _ray = new THREE.Raycaster();
const _ndc = new THREE.Vector2();
const _hitP = new THREE.Vector3();
// 世界坐标处的地面高度（线性插值；渲染顶点用的是网格原值，亚像素差异不影响取点）
function groundY(x, z){ return sampleElev(zToLat(z), xToLon(x)) * S * exag; }

// 高度场射线求交：**不用** raycaster 直接打地形网格。
// 地形是规则高度场，沿射线「粗步进 + 二分」即可定位交点；而 Mesh.raycast 没有 BVH，
// 768² 网格就是约 118 万个三角形要逐个测，单次几十毫秒——鼠标一动就掉帧（v2.9 修）。
function pickAt(cx, cy){
  if (!baseElev || !terrain) return null;
  _ndc.set((cx / innerWidth) * 2 - 1, -(cy / innerHeight) * 2 + 1);
  _ray.setFromCamera(_ndc, camera);
  const o = _ray.ray.origin, d = _ray.ray.direction;
  // 先把射线裁进地形 AABB，步进区间才有界
  const lo = [-WX / 2, META.min_elev * S * exag - 1, -WZ / 2];
  const hi = [ WX / 2, META.max_elev * S * exag + 1,  WZ / 2];
  let t0 = 0, t1 = 1e9;
  for (let k = 0; k < 3; k++){
    const od = d.getComponent(k), oo = o.getComponent(k);
    if (Math.abs(od) < 1e-9){
      if (oo < lo[k] || oo > hi[k]) return null;
      continue;
    }
    let ta = (lo[k] - oo) / od, tb = (hi[k] - oo) / od;
    if (ta > tb){ const t = ta; ta = tb; tb = t; }
    if (ta > t0) t0 = ta;
    if (tb < t1) t1 = tb;
  }
  if (t1 <= t0) return null;
  const gap = t => (o.y + d.y * t) - groundY(o.x + d.x * t, o.z + d.z * t);
  if (gap(t0) <= 0) return mkHit(o, d, t0);        // 相机已经在地形里（贴地视角）
  const N = 128, dt = (t1 - t0) / N;
  let ta = t0;
  for (let i = 1; i <= N; i++){
    const tb = t0 + dt * i;
    if (gap(tb) <= 0){                              // 已穿过地面 → 二分收敛
      let a = ta, b = tb;
      for (let k = 0; k < 22; k++){
        const m = (a + b) * 0.5;
        if (gap(m) > 0) a = m; else b = m;
      }
      return mkHit(o, d, (a + b) * 0.5);
    }
    ta = tb;
  }
  return null;                                      // 射线从地形上方掠过，没打到
}
function mkHit(o, d, t){
  const p = _hitP.set(o.x + d.x * t, o.y + d.y * t, o.z + d.z * t);
  const lon = xToLon(p.x), lat = zToLat(p.z);
  return { lon, lat, elev: sampleElev(lat, lon), pos: p.clone() };
}
window.__geo3dPick = pickAt;   // 调试/校验钩子：可在控制台手测取点与耗时
const fmtM = m => m >= 1000 ? (m / 1000).toFixed(2) + ' km' : m.toFixed(0) + ' m';

let pickMode = null;                 // 'dist' | 'prof' | null
const mkEls = [];                    // [{el, pos, ll}]
let mkLine = null, mkLbl = null;
function clearMarks(){
  mkEls.forEach(o => o.el.remove());
  mkEls.length = 0;
  if (mkLine){ scene.remove(mkLine); mkLine.geometry.dispose(); mkLine.material.dispose(); mkLine = null; }
  if (mkLbl){ mkLbl.remove(); mkLbl = null; }
  document.getElementById('prof').style.display = 'none';
}
function addMark(ll){
  const el = document.createElement('div');
  el.className = 'mk';
  document.body.appendChild(el);
  mkEls.push({ el, pos: ll.pos, ll });
  if (mkEls.length > 2) { const o = mkEls.shift(); o.el.remove(); }   // 只保留最近两点
}
function setPick(mode){
  pickMode = (pickMode === mode) ? null : mode;
  document.getElementById('measBtn').classList.toggle('on', pickMode === 'dist');
  document.getElementById('profBtn').classList.toggle('on', pickMode === 'prof');
  if (pickMode) clearMarks();
}
// 屏幕点击（拖拽旋转时不应被当成取点，故按下—抬起位移超过 4px 就不算）
// 注意：canvas 是 init() 里才创建的，绑定必须等 init，不能在模块顶层直接碰 renderer（v2.1 白屏坑）
let _dn = null;
function setupTools(){
  const cv = renderer.domElement;
  cv.addEventListener('pointerdown', e => { _dn = [e.clientX, e.clientY]; });
  cv.addEventListener('pointerup', e => {
    if (!pickMode || !_dn) return;
    if (Math.hypot(e.clientX - _dn[0], e.clientY - _dn[1]) > 4) return;
    const ll = pickAt(e.clientX, e.clientY);
    if (!ll) return;
    addMark(ll);
    if (mkEls.length === 2) finishPick();
  });
}
function finishPick(){
  const a = mkEls[0].ll, b = mkEls[1].ll;
  if (mkLine){ scene.remove(mkLine); mkLine.geometry.dispose(); mkLine.material.dispose(); }
  mkLine = ribbonMesh([[a.lon, a.lat], [b.lon, b.lat]], 0xffd166, 3, OV_LIFT * 2);
  if (mkLine){ scene.add(mkLine); resampleHeights(mkLine); }
  const d = hDist(a, b), dz = b.elev - a.elev;
  const txt = '水平 ' + fmtM(d) + ' · 高差 ' + (dz >= 0 ? '+' : '') + dz.toFixed(0) + ' m'
            + ' · 坡度 ' + (Math.atan2(Math.abs(dz), d) * 100).toFixed(1) + ' %'
            + ' · 直线 ' + fmtM(Math.hypot(d, dz));
  if (!mkLbl){
    mkLbl = document.createElement('div');
    mkLbl.className = 'mkLbl';
    document.body.appendChild(mkLbl);
  }
  mkLbl.textContent = txt;
  mkLbl._pos = new THREE.Vector3((a.pos.x + b.pos.x) / 2, (a.pos.y + b.pos.y) / 2 + WORLD * 0.02,
                                 (a.pos.z + b.pos.z) / 2);
  if (pickMode === 'prof') drawProfile(a, b);
  markDirty();
}
function drawProfile(a, b){
  const N = 260, es = [], ds = [];
  let acc = 0, climb = 0;
  for (let i = 0; i <= N; i++){
    const t = i / N;
    const lon = a.lon + (b.lon - a.lon) * t, lat = a.lat + (b.lat - a.lat) * t;
    const p = { lon, lat }, e = sampleElev(lat, lon);
    es.push(e);
    if (i > 0){ acc += hDist({ lon: a.lon + (b.lon - a.lon) * (i - 1) / N, lat: a.lat + (b.lat - a.lat) * (i - 1) / N }, p); ds.push(acc); }
    if (i > 0) climb += Math.max(0, e - es[i - 1]);
  }
  const lo = Math.min(...es), hi = Math.max(...es);
  const cv = document.getElementById('profCv'), ctx = cv.getContext('2d');
  const w = cv.width, h = cv.height, pad = 18;
  ctx.clearRect(0, 0, w, h);
  const sx = x => pad + (w - pad * 2) * (ds.length ? Math.min(1, x / ds[ds.length - 1]) : 0);
  const sy = e => (h - pad) - (h - pad * 2) * ((e - lo) / Math.max(1e-6, hi - lo));
  // 填充 + 描边
  const grad = ctx.createLinearGradient(0, pad, 0, h - pad);
  grad.addColorStop(0, 'rgba(255,209,102,.35)');
  grad.addColorStop(1, 'rgba(255,209,102,.02)');
  ctx.beginPath(); ctx.moveTo(sx(0), h - pad);
  for (let i = 0; i < es.length; i++) ctx.lineTo(sx(i === 0 ? 0 : ds[i - 1]), sy(es[i]));
  ctx.lineTo(sx(ds[ds.length - 1]), h - pad); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();
  ctx.beginPath();
  for (let i = 0; i < es.length; i++){
    const x = sx(i === 0 ? 0 : ds[i - 1]), y = sy(es[i]);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  }
  ctx.strokeStyle = '#ffd166'; ctx.lineWidth = 1.6; ctx.stroke();
  // 端点与极值标注
  ctx.fillStyle = '#e6edf3'; ctx.font = '11px system-ui,sans-serif';
  ctx.fillText(hi.toFixed(0) + ' m', 4, pad - 4);
  ctx.fillText(lo.toFixed(0) + ' m', 4, h - 4);
  ctx.fillText(fmtM(ds[ds.length - 1] || 0), w - pad - 46, h - 4);
  document.getElementById('profTx').textContent =
    '起点 ' + a.elev.toFixed(0) + ' m → 终点 ' + b.elev.toFixed(0) + ' m，'
    + '最高 ' + hi.toFixed(0) + ' m / 最低 ' + lo.toFixed(0) + ' m，累计爬升 ' + climb.toFixed(0) + ' m'
    + '（剖面按当前地形采样，垂直夸张 ' + exag.toFixed(1) + '× 不影响高程数值）';
  document.getElementById('prof').style.display = 'block';
}
document.getElementById('measBtn').addEventListener('click', () => setPick('dist'));
document.getElementById('profBtn').addEventListener('click', () => setPick('prof'));
document.getElementById('clrBtn').addEventListener('click', () => { clearMarks(); pickMode = null;
  document.getElementById('measBtn').classList.remove('on');
  document.getElementById('profBtn').classList.remove('on'); markDirty(); });
document.getElementById('profClose').addEventListener('click', () => {
  document.getElementById('prof').style.display = 'none'; });

// ---- HUD：比例尺 / 指北针 / 光标经纬高 ----
// 平面尺寸改为按米数走之后，x/z/y 三个方向 1 世界单位都等于 1/S 米，比例尺才是准的
// （v2.9 前用 (width_m+height_m)/2/WORLD，长条范围下会系统性偏小）
const M_PER_UNIT = 1 / S;
function updateHud(){
  const d = camera.position.distanceTo(controls.target);
  const vh = 2 * d * Math.tan(camera.fov * Math.PI / 360);          // 视口高度（世界单位）
  const mpp = vh / innerHeight * M_PER_UNIT;                        // 米/像素
  let target = 110 * mpp;                                           // 目标：比例尺约 110px 宽
  const mag = Math.pow(10, Math.floor(Math.log10(Math.max(1e-6, target))));
  const nm = target / mag;
  const nice = (nm <= 1 ? 1 : nm <= 2 ? 2 : nm <= 5 ? 5 : 10) * mag;
  document.getElementById('scaleTx').textContent = fmtM(nice);
  document.getElementById('scaleBar').style.width = Math.max(30, nice / mpp).toFixed(0) + 'px';
  const fx = controls.target.x - camera.position.x, fz = controls.target.z - camera.position.z;
  const ang = Math.atan2(-fx, -fz) * 180 / Math.PI;                 // 北在 -Z 方向
  document.getElementById('needle').style.transform = 'rotate(' + ang.toFixed(1) + 'deg)';
}
let curTimer = null;
addEventListener('pointermove', e => {
  if (curTimer) return;
  curTimer = setTimeout(() => {
    curTimer = null;
    const el = document.getElementById('cursor');
    const ll = (terrain && !pickMode) ? pickAt(e.clientX, e.clientY) : null;
    el.textContent = ll ? ll.lat.toFixed(5) + '°N, ' + ll.lon.toFixed(5) + '°E · ' + ll.elev.toFixed(0) + ' m'
                        : (pickMode ? '点选两点…' : '—');
  }, 60);
});
function updateMarks(){
  mkEls.forEach(o => place(o.el, project(o.pos)));
  if (mkLbl && mkLbl._pos) place(mkLbl, project(mkLbl._pos));
}

// ---- 导出 GLB（地形 + 叠加层，可导入 Blender / Cesium / 数字孪生平台）----
let __exporterP = null;      // 导出器只装配一次（Blob URL 别反复创建，否则每次点导出都漏一串 URL）
async function getExporter(){
  if (__exporterP) return __exporterP;
  __exporterP = (async () => {
{% if embed_three %}{% if has_gltf %}
  // GLTFExporter 里有一句 `from './../utils/TextureUtils.js'`：Blob URL 下相对路径解析不了，
  // 必须先把依赖包成 Blob URL，再把这句改写成绝对 URL，否则导出会直接抛
  // "Failed to resolve module specifier"。
  const __texURL = URL.createObjectURL(new Blob(
    [__unesc(document.getElementById('__texutils_src').textContent)
       .replace(/from\s*(['"])three\1/g, 'from "' + __threeURL + '"')], {type: 'text/javascript'}));
  const url = URL.createObjectURL(new Blob(
    [__unesc(document.getElementById('__gltf_src').textContent)
       .replace(/from\s*(['"])three\1/g, 'from "' + __threeURL + '"')
       .split("'./../utils/TextureUtils.js'").join('"' + __texURL + '"')], {type: 'text/javascript'}));
  return (await import(url)).GLTFExporter;
{% else %}
  throw new Error('未内嵌 GLB 导出器（生成时下载不到 GLTFExporter.js），联网重生成一次即可');
{% endif %}{% else %}
  return (await import('three/addons/exporters/GLTFExporter.js')).GLTFExporter;
{% endif %}
  })().catch(e => { __exporterP = null; throw e; });   // 失败要能重试，别把坏 Promise 缓存住
  return __exporterP;
}
const glbBtn = document.getElementById('glbBtn');
{% if embed_three and not has_gltf %}
glbBtn.disabled = true;      // 没内嵌导出器时直接禁用，别让用户点了才弹失败
glbBtn.title = '未内嵌 GLB 导出器（生成时下载不到 GLTFExporter.js），联网重生成一次即可';
{% else %}
glbBtn.title = '导出真实比例（垂直夸张 1×）的地形 + 叠加层，贴图最长边限制 2048';
{% endif %}
glbBtn.addEventListener('click', async () => {
  const old = glbBtn.textContent;
  const back = exag;
  glbBtn.disabled = true; glbBtn.textContent = '导出中…';
  try{
    const GLTFExporter = await getExporter();
    // 画面上的垂直夸张只是观感，写进 GLB 会误导下游（Cesium/Blender 里地形会"拔高"），
    // 故导出前临时回到 1×，导出完再恢复。
    if (back !== 1){
      exag = 1;
      applyHeights(terrain.geometry);
      terrain.geometry.computeVertexNormals();
      updateOverlayHeights();
      if (mkLine) resampleHeights(mkLine);
    }
    const objs = [terrain];
    if (ovGroup && showOv) objs.push(ovGroup);
    const buf = await new Promise((res, rej) =>
      // v3.0.0：导出贴图 2048→4096。分块纹理等效 8192px，压到 2048 细节损失 4×；
      // 4096 的 GLB 约大 3-4 MB，但进 Blender/Cesium 后明显更清晰，值得。
      new GLTFExporter().parse(objs, res, rej, {binary: true, maxTextureSize: 4096}));
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([buf], {type: 'model/gltf-binary'}));
    a.download = (META.place || 'terrain') + '.glb';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  } catch(err){
    alert('导出 GLB 失败：' + ((err && err.message) || err));
  } finally{
    // 交回 animate 恢复（避免重入），并把法线一并重算，否则恢复后光照是错的
    if (back !== 1){ pendingExag = back; needNormals = true; markDirty(); }
    glbBtn.disabled = false; glbBtn.textContent = old;
  }
});

// ---- GeoJSON 叠加开/关 + 名称开/关 ----
const ovBtn = document.getElementById('ovBtn');
const ovNameBtn = document.getElementById('ovNameBtn');
if (ovBtn){
  ovBtn.addEventListener('click', () => {
    showOv = !showOv;
    if (ovGroup) ovGroup.visible = showOv;
    ovBtn.classList.toggle('on', showOv);
    if (!showOv) ovLabels.forEach(o => { o.el.style.display = 'none'; });
    markDirty();
  });
}
if (ovNameBtn){
  ovNameBtn.addEventListener('click', () => {
    showOvName = !showOvName;
    ovNameBtn.classList.toggle('on', showOvName);
    if (!showOvName) ovLabels.forEach(o => { o.el.style.display = 'none'; });
    markDirty();
  });
}

// ---- 把 .geojson 拖进页面即时叠加（坐标须落在当前地形范围内）----
const OV_PALETTE = [0xffcf4d, 0x4ecdc4, 0xff6b6b, 0x45b7d1, 0x96ceb4, 0xdda0dd,
                    0x74b9ff, 0xa29bfe, 0x55efc4, 0xfd79a8, 0xff9f43, 0x7bed9f];
function iterPts(c, out){
  if (Array.isArray(c)){
    if (c.length >= 2 && typeof c[0] === 'number' && typeof c[1] === 'number') out.push(c);
    else c.forEach(x => iterPts(x, out));
  }
  return out;
}
function geoParts(g, out){
  if (!g || !g.type) return out;
  const t = g.type, c = g.coordinates;
  if (t === 'GeometryCollection'){ (g.geometries || []).forEach(x => geoParts(x, out)); return out; }
  if (t === 'Polygon') out.push({ kind: 'polygon', outer: c[0], holes: c.slice(1) });
  else if (t === 'MultiPolygon') c.forEach(p => out.push({ kind: 'polygon', outer: p[0], holes: p.slice(1) }));
  else if (t === 'LineString') out.push({ kind: 'line', path: c });
  else if (t === 'MultiLineString') c.forEach(l => out.push({ kind: 'line', path: l }));
  else if (t === 'Point') out.push({ kind: 'point', lon: c[0], lat: c[1] });
  else if (t === 'MultiPoint') c.forEach(p => out.push({ kind: 'point', lon: p[0], lat: p[1] }));
  return out;
}
function parseGeoJSONText(txt){
  const gj = JSON.parse(txt);
  const feats = gj.type === 'FeatureCollection' ? (gj.features || [])
    : gj.type === 'Feature' ? [gj]
    : [{ type: 'Feature', geometry: gj, properties: {} }];
  const out = [];
  let ci = 0;
  feats.forEach(ft => {
    const props = (ft && ft.properties) || {};
    const nm = props.name || props.NAME || props['名称'] || props.title || '';
    const parts = geoParts(ft.geometry, []);
    parts.forEach(p => {
      p.name = nm;
      p.color = OV_PALETTE[ci % OV_PALETTE.length];
      p.fill = p.color;
      p.fillOpacity = 0.22;
      out.push(p);
      ci++;
    });
  });
  return out;
}
const dropEl = document.getElementById('drop');
let dragDepth = 0;
addEventListener('dragenter', e => { e.preventDefault(); dragDepth++; if (dropEl) dropEl.classList.add('on'); });
addEventListener('dragover', e => { e.preventDefault(); });
addEventListener('dragleave', e => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth && dropEl) dropEl.classList.remove('on'); });
addEventListener('drop', e => {
  e.preventDefault();
  dragDepth = 0;
  if (dropEl) dropEl.classList.remove('on');
  const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
  if (!f) return;
  const fr = new FileReader();
  fr.onload = () => {
    try {
      const list = parseGeoJSONText(String(fr.result));
      if (!list.length){ setMsg('该 GeoJSON 没有可叠加的几何要素', true); return; }
      showOv = true; showOvName = true;
      if (ovBtn) ovBtn.classList.add('on');
      if (ovNameBtn) ovNameBtn.classList.add('on');
      buildOverlays(list);
      markDirty();
      document.getElementById('loading').classList.add('hide');
    } catch (err){
      setMsg('GeoJSON 解析失败：' + err.message, true);
    }
  };
  fr.readAsText(f);
});
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


# 内嵌进 HTML 的源文件：(key, 文件名, CDN 子路径, 最小字节数)
# min_size 不能一刀切：TextureUtils.js 只有约 5 KB，用 10 KB 的判据会把它误判成"下载失败"。
EMBED_FILES = (
    ("three", "three.module.js", "build/", 10000),
    ("orbit", "OrbitControls.js", "examples/jsm/controls/", 10000),
    # 导出 GLB 用（内嵌才能保证离线也能导出；两者合计约 80 KB，产物 +1.3%）。
    # 注意 GLTFExporter 里有一句相对路径 import './../utils/TextureUtils.js'，
    # Blob URL 下解析不了相对路径，必须连这个文件一起内嵌并在运行时改写成绝对 URL。
    ("gltf", "GLTFExporter.js", "examples/jsm/exporters/", 10000),
    ("texutils", "TextureUtils.js", "examples/jsm/utils/", 1000),
)


def fetch_embed_sources(verbose: bool = True) -> tuple[str, str, str, str] | None:
    """下载并缓存 three / OrbitControls / GLTFExporter / TextureUtils 源码。

    前两个是渲染必需，缺一不可；后两个只用于「导出 GLB」，下载失败时仅禁用该按钮
    （返回 gltf/texutils 为空串），不影响主流程。缓存目录 ~/.cache/geo-3d-terrain/three/。
    """
    cache_dir = Path.home() / ".cache" / "geo-3d-terrain" / "three" / THREE_VERSION
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = []
    try:
        import requests
    except ImportError:
        return None
    for key, fname, sub, min_size in EMBED_FILES:
        fp = cache_dir / fname
        if fp.exists() and fp.stat().st_size > min_size:
            out.append(fp.read_text(encoding="utf-8"))
            continue
        ok = False
        for base in (f"https://cdn.jsdelivr.net/npm/three@{THREE_VERSION}",
                     f"https://unpkg.com/three@{THREE_VERSION}"):
            url = f"{base}/{sub}{fname}"
            try:
                r = requests.get(url, timeout=25, headers={"User-Agent": "geo-3d-terrain/2.9"})
                if r.status_code == 200 and len(r.content) > min_size:
                    txt = r.content.decode("utf-8", "replace")
                    fp.write_text(txt, encoding="utf-8")
                    out.append(txt)
                    ok = True
                    break
            except Exception:
                continue
        if not ok:
            if key in ("gltf", "texutils"):
                out.append("")          # 导出 GLB 用不了，主流程照常
                continue
            return None                 # three / orbit 缺失：整体回退 CDN
    if verbose:
        extra = f" + GLB 导出 {(len(out[2]) + len(out[3])) // 1024} KB" if (out[2] and out[3]) \
            else "（未内嵌 GLB 导出器）"
        print(f"    Three.js 已内嵌（{len(out[0]) // 1024} KB + {len(out[1]) // 1024} KB{extra}），离线可用",
              file=sys.stderr)
    return out[0], out[1], out[2], out[3]


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

    sat_tiles = data.get("sat_tiles") or []
    has_sat = bool(meta.get("has_sat") and (data.get("sat_b64") or sat_tiles))

    # three: auto = 尽量内嵌（离线可看），cdn = 强制走 CDN，embed = 强制内嵌
    embed_three = False
    three_src = orbit_src = gltf_src = texutils_src = ""
    if three in ("auto", "embed"):
        got = fetch_embed_sources(verbose=verbose)
        if got:
            embed_three = True
            # </script 会提前闭合 text/plain 节点，必须转义
            three_src, orbit_src, gltf_src, texutils_src = (s.replace("</script", "<\\/script") for s in got)
        elif three == "embed":
            raise SystemExit("内嵌 Three.js 失败：下载不到源码，请检查网络或改用 --three cdn")
        elif verbose:
            print("    ! 内嵌 Three.js 失败，回退 CDN（离线打开会白屏）", file=sys.stderr)

    cdn_name, three_url, three_addons = pick_cdn(cdn)

    peak_name = (meta.get("peak_name") or "").strip()
    peak_label = (f"{peak_name} {meta['peak_elev']:.0f} m" if peak_name
                  else f"最高点 {meta['peak_elev']:.0f} m")

    src = meta.get("sources", {}) or {}
    overlays = data.get("overlays") or []
    ov_files = meta.get("geojson_files") or []
    if ov_files:
        ov_source = "，".join(ov_files)
    else:
        ov_source = "页面内拖入 .geojson"
    html = Template(HTML_TEMPLATE).render(
        place=place,
        center_lat=meta["center_lat"], center_lon=meta["center_lon"],
        width_m=meta["width_m"], height_m=meta["height_m"],
        min_elev=meta["min_elev"], max_elev=meta["max_elev"],
        peak_elev=meta["peak_elev"], peak_label=peak_label,
        resolution=meta.get("sat_resolution_m") or meta.get("resolution_m", 0),
        sat_native_m=meta.get("sat_native_m"),
        sat_zoom=meta.get("sat_zoom"),
        exaggeration=exaggeration,
        has_sat=has_sat,
        dem_b64=data["dem_b64"],
        sat_b64=data.get("sat_b64", ""),
        sat_mime=data.get("sat_mime", "image/jpeg"),
        sat_tiles_json=json.dumps([
            {"mime": t.get("mime", "image/jpeg"), "b64": t.get("b64", "")}
            for t in (data.get("sat_tiles") or [])], ensure_ascii=False),
        meta_json=json.dumps(meta, ensure_ascii=False),
        landmarks_json=json.dumps(data.get("landmarks", []), ensure_ascii=False),
        overlays_json=json.dumps(overlays, ensure_ascii=False),
        has_overlays=bool(overlays),
        ov_count=len(overlays),
        ov_source=ov_source,
        three_url=three_url, three_addons=three_addons, cdn_name=cdn_name,
        embed_three=embed_three, three_src=three_src, orbit_src=orbit_src,
        gltf_src=gltf_src, texutils_src=texutils_src,
        has_gltf=bool(gltf_src and texutils_src),
        src_dem=src.get("dem", "AWS Terrarium"),
        src_sat=src.get("sat") or "未使用",
        src_poi=src.get("poi") or "未使用",
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
    ap = argparse.ArgumentParser(description="生成三维地形 HTML（v3.1.0）",
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
