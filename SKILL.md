# Skill: 三维真实地形生成器 (Geo-3D-Terrain)　`v2.4`

> 触发场景：用户要求"生成 XX 的三维地形 / 3D 地形 / 立体地形图 / 地形演示网页"，
> 或需要在真实地形上叠加主峰与地标做可视化展示时。
> **一句话**：给个地名 → 出一个单文件 HTML，真实 DEM 高程 + 真实卫星影像 + OSM 地标，离线可看。

## 能力边界

- ✅ 生成**真实**地形（真实 DEM 高程 + 真实卫星影像），不是程序化噪声
- ✅ 自动地理编码、自动找主峰、自动拉取 OSM 地标
- ✅ 单文件交付，**Three.js 一并内嵌**，断网也能打开
- ❌ 不做三维建筑、不支持倾斜摄影/实景三维模型

## 脚本清单

| 脚本 | 作用 |
|------|------|
| `scripts/prepare_terrain_data.py` | 主流程：地理编码 → DEM → 影像 → 地标 → `terrain.json` →（可选）HTML |
| `scripts/generate_html.py` | 只做渲染：拿已有 `terrain.json` 重新出 HTML（改夸张系数时用它，不用重下瓦片） |
| `scripts/check_render.js` | 无头浏览器渲染校验 + 截图（**交付前必跑**） |

## 前置环境（首次使用必须检查）

Python 侧需要 `requests / numpy / Pillow / jinja2`。**不要污染用户全局环境**，用隔离 venv：

```bash
PY="C:/Users/wangch/.workbuddy/binaries/python/versions/3.13.12/python.exe"
VENV="C:/Users/wangch/.workbuddy/binaries/python/envs/geo3d/Scripts/python.exe"
"$PY" -m venv "C:/Users/wangch/.workbuddy/binaries/python/envs/geo3d" && \
"$VENV" -m pip install -q requests numpy Pillow jinja2
```

已装好则跳过安装，直接用 `$VENV` 执行脚本。

Node 侧（仅 Step 2b 校验需要，已具备则跳过）：

```bash
NODE_PATH="C:/Users/wangch/.workbuddy/binaries/node/workspace/node_modules"   # 已含 playwright-core
# 浏览器自动探测：本机 Chrome → Edge；找不到时交给 playwright-core 自己找
```

## 执行流程

### Step 1 — 生成（一条命令搞定）

```bash
cd <输出目录>
"$VENV" "<skill>/scripts/prepare_terrain_data.py" \
  --place "大蜀山" --zoom 14 --grid 3 \
  --out terrain.json --out-html 大蜀山三维地形.html
```

脚本会依次完成：地理编码 → 下载 DEM → 下载卫星影像 → 拉地标 → 写 JSON → 渲染 HTML。
全程有中文进度输出到 stderr（默认 zoom14+grid3 首次生成约 15–25 秒——v2.4 起 DEM 与影像**并行下载**，比旧版 60–90 秒快很多；瓦片有磁盘缓存，二次生成同区域可降到 ~3–7 秒）。

### Step 2 — 校验（必须做，别跳过）

**2a. 数据字段**（快，先跑）：

```bash
# HTML 是否真的生成、多大
ls -l 大蜀山三维地形.html
# 关键字段是否正确（高程范围应合理、主峰不为空、has_sat 为 true、sat_resolution_m 反映贴图清晰度）
"$VENV" -c "import json;d=json.load(open('terrain.json',encoding='utf-8'));m=d['meta'];print({k:m[k] for k in ['place','min_elev','max_elev','peak_elev','peak_name','has_sat','size_w','resolution_m','sat_resolution_m','size_sat_w','size_sat_h']});print('landmarks',len(d['landmarks']))"
```

**2b. 真实渲染校验**（关键！JSON 正常 ≠ 页面能看。JS 一旦运行时报错，
页面会永远停在「正在构建真实地形…」转圈，而 `terrain.json` 一切正常）：

```bash
cd <输出目录>
NODE_PATH="C:/Users/wangch/.workbuddy/binaries/node/workspace/node_modules" \
"C:/Users/wangch/.workbuddy/binaries/node/versions/22.22.2-3/node.exe" \
  "<skill>/scripts/check_render.js" "大蜀山三维地形.html" check.png
```

必须看到 `{"ok": true, "hasCanvas": true, "loadingHidden": true, "errors": []}`。
`ok:false` 时看 `errors` 里的 `[pageerror]`，那才是真正的白屏原因。截图可肉眼复核。
脚本会自动起临时 HTTP 服务（file:// 下 blob 动态 import 可能被拦），无需自己开服务器。

### Step 3 — 交付

用 `present_files` 打开该 HTML（会自动进入预览面板），并简述覆盖范围、高程、地标数量。

## 参数速查（prepare_terrain_data.py）

| 参数 | 默认 | 说明 |
|------|------|------|
| `--place` | — | 地名，中文/英文均可。与 `--lat/--lon` 二选一 |
| `--lat` `--lon` | — | 直接给 WGS84 坐标，跳过地理编码（**最可靠的兜底**） |
| `--zoom` | 14 | DEM 层级 3–15。越大范围越小、越精细 |
| `--grid` | 3 | 覆盖 N×N 个瓦片。zoom14+grid3 ≈ 6.2 km；zoom12+grid3 ≈ 25 km |
| `--max-size` | 1024 | **网格**最大边长（像素），决定地形网格顶点数与几何平滑度 |
| `--max-verts` | 3000000 | 网格顶点数硬上限（W×H）。超出则按最近邻抽稀网格（纹理仍保持 `--sat-max-size` 清晰度），专治 `--max-size` 调大时的顶点爆炸 |
| `--sat-max-size` | 2048 | **影像纹理**最大边长（像素），独立于网格，直接决定贴图清晰度（核心清晰度参数） |
| `--sat-extra` | 2 | 影像额外超采样下限（DEM zoom + N），作为清晰度下限；实际 zoom 还会按 `--sat-max-size` 自动抬高 |
| `--max-sat-tiles` | 576 | 影像瓦片数上限，超出自动降低 zoom 控体积（想用 4096 纹理拿满原生清晰度时提到 ~600） |
| `--sat-format` | jpeg | 影像编码：`jpeg`=默认（体积小、兼容好）；`webp`=对平滑/照片类常更小，但本项目 hillshaded 高熵纹理下可能反而更大，按需选用 |
| `--jpeg-quality` | 85 | 影像质量（webp/jpeg 通用，70–95，越高越清晰、体积越大） |
| `--sharpen` | 1.3 | 纹理锐化强度（1.0=不锐化，>1 更锐），让贴图更 crisp |
| `--no-hillshade` | — | 不加立体光照；默认会把 DEM 微 hillshade 烤进贴图增强立体感 |
| `--hillshade-strength` | 0.5 | hillshade 强度（0–1，越大越立体；0 等于关闭） |
| `--hillshade-alt` | 45 | hillshade 光源高度角（度） |
| `--no-sat` / `--no-landmarks` | — | 跳过影像 / 跳过地标，用于快速或降级出图 |
| `--geocoder` | auto | `auto` = amap(需 AMAP_KEY) → nominatim → photon 依次兜底 |
| `--out-html` | — | 一步生成 HTML（不加则只出 JSON，需再跑 generate_html.py） |
| `--exaggeration` | auto | 垂直夸张系数，默认按"相对高差≈世界宽度18%"自动推荐 |
| `--three` | auto | Three.js 引入方式：`auto`=优先内嵌（离线可看）→失败回退 CDN；`cdn`=强制外链；`embed`=强制内嵌，下不到就报错 |
| `--cdn` | auto | 仅 `--three cdn` 时生效（jsdelivr → unpkg → esm.sh 择优） |
| `--no-cache` | — | 禁用瓦片磁盘缓存（默认缓存在 `~/.cache/geo-3d-terrain`） |

单独重渲染 HTML（改夸张系数时很有用，不用重下瓦片）：

```bash
"$VENV" "<skill>/scripts/generate_html.py" --data terrain.json --out t.html --exaggeration 5
```

### 调参建议

- **想提高影像清晰度（贴图更锐）**：核心是 `--sat-max-size`（默认 2048，约 3 m/px；提到 4096 约 1.5 m/px）。它已与网格解耦，调大只增纹理像素、不增网格顶点。配套 `--sat-extra` 与瓦片预算会自动抬高 zoom，保证源影像原生像素够铺满纹理。
- **清晰度上不去？查 `sat_resolution_m`**：若想用 `--sat-max-size 4096` 拿满原生清晰度，需同时把 `--max-sat-tiles` 提到 ~600（grid3 在 zoom17 约 576 块瓦片），否则会被瓦片预算压回低 zoom 变成放大糊。
- **体积/速度权衡**（实测 zoom14+grid3）：默认 2048 纹理单文件约 3.3 MB（其中 Three.js 内嵌约 1.3 MB、影像 JPEG 约 1.3 MB），首次生成约 15–25 s、同区域缓存命中约 3–7 s；`--sat-max-size 4096` + `--jpeg-quality 90` 可能 4–6 MB、瓦片更多稍慢。WebP 对**平滑/照片类**影像常比 JPEG 小约 30%，但叠加 hillshade 的高熵纹理下反而更大，故默认用 JPEG；想试 WebP 加 `--sat-format webp`。
- **平原/湖区**（如合肥翡翠湖、长三角）：相对高差极小，自动夸张会顶到 10×，观感仍可能偏平，可手动 `--exaggeration` 再调；这类区域更推荐缩到 zoom 13–14 看整体地貌。
- **山地**：默认 10× 往往过头，用 `--exaggeration 2~3` 更接近真实比例。
- **想看更大范围**：降 `--zoom`（12 看 25 km、11 看 50 km）而不是狂加 `--grid`，否则瓦片数暴涨、体积失控。
- **网格顶点爆炸**：`--max-size` 调大（如 2048/4096）会让顶点数到数百万，老设备会卡。可设 `--max-verts`（默认 3M）自动抽稀网格——纹理仍由 `--sat-max-size` 决定，清晰度不受影响。

## 页面交互（生成出来的 HTML 自带）

垂直夸张滑块（1–10×）· 重置视角 / 真实比例 · 影像贴图 ↔ 高程分层设色 · 网格线 ·
等高线（自动按高程范围生成约 12 条描线，卫星/设色两种模式都叠加）· 自动旋转（绕中心匀速转）·
导出 PNG（一键把当前视角存为图片）· 地标标注开关 · 信息面板折叠 ·
左键旋转 / 滚轮缩放 / 右键平移 · 窄屏自适应。

## 失败处理（按优先级）

1. **地理编码失败**（三国源都无结果）
   → 先换个更通用的名字重试（"大蜀山 合肥"）；仍失败就直接问用户要经纬度，或用常识坐标，然后 `--lat/--lon` 重跑。**不要**因为失败就放弃。
2. **Overpass 地标为空**
   → 不是致命错误，地形照常生成，只是没地标。可缩小 `--grid` 或换 `--zoom` 重试一次。
3. **该区域无高程数据**（远海/无数据）
   → 换地点或降 zoom。
4. **网络整体超时**
   → 加 `--no-sat --no-landmarks` 只出高程，先保证有成果。
5. **打开 HTML 白屏 / 一直转圈**
   → **先跑 Step 2b 的 `check_render.js`**，看 `[pageerror]`，不要靠猜。
   两种已知原因：
   - JS 运行时错误 → 页面卡在加载动画。这类 bug 在浏览器控制台之外看不见，必须靠 2b 抓。
   - WebGL 不可用（沙箱/无硬件加速）→ 提示语会显示"当前浏览器未启用 WebGL"，换 Chrome/Edge 或开硬件加速。
   注意：**Three.js 默认已内嵌进 HTML，白屏基本与网络无关**；只有显式用了
   `--three cdn` 时才需要考虑 CDN 可达性（换 `--cdn unpkg`）。

## 技术要点

- 数据源：DEM = AWS Terrarium（SRTM/GDEM，`elev = R*256 + G + B/256 - 32768`，z≤15）；影像 = Esri World Imagery；POI = OSM/Overpass
- **影像清晰度与网格解耦**（v2.2）：网格尺寸由 `--max-size` 决定（控顶点数），影像纹理由 `--sat-max-size` 独立决定（控贴图清晰度）。旧版把影像强制 resize 成网格尺寸导致贴图被压回 ~6 m/px；新版按目标纹理尺寸自动抬高影像 zoom（`needed = ceil(log2(sat_max_size / (grid*256)))`），纹理可精细到 1.5 m/px。页面"贴图分辨率"显示 `sat_resolution_m`。
- **纹理增强**（v2.3）：新增 `--sat-format webp` 选项（默认仍 JPEG——实测叠加 hillshade 后 WebP 常比 JPEG 更大，故不默认）；`--sharpen` 轻微锐化让贴图更 crisp；默认会把 DEM 重采样到纹理尺寸算出的微 hillshade 直接"烤"进卫星贴图（`factor = (1-0.5k) + k·shade`，k=`--hillshade-strength`，默认 0.5），让平面影像也有立体感。hillshade 与 3D 材质光照叠加，故强度保持克制。
- **等高线叠加**（v2.4）：HTML 端由高程数组生成透明描线纹理（自动挑 10/20/50/100/200/500 m 的等高距，约 12 条），叠在地形上方（`polygonOffset` 避免 z-fighting）。卫星贴图模式和高程设色模式都生效，可用「等高线」按钮开关。线宽按 DEM 分辨率自适应约 1.6 像素。
- **DEM 异步解码**（v2.4）：JS 端高程解码默认放进 **Web Worker**（`OffscreenCanvas` + `createImageBitmap` 解 PNG，结果用 transferable ArrayBuffer 回传），不再在主线程 `getImageData` 阻塞——4096 级大网格也不会卡 UI。不支持/超时(8s)/报错时**自动回退主线程同步解码**，保证兼容。
- **DEM 与 SAT 并行下载**（v2.4）：先算好边界与影像 zoom（纯数学），再用两个线程池**并发**抓 DEM 与卫星瓦片；瓦片抓完后再串行做 hillshade/锐化/编码（依赖高程）。首跑省掉一半网络等待。
- **网格 LOD / 顶点上限**（v2.4）：`--max-verts`（默认 3M）硬上限。超出时按最近邻整块抽稀网格（`decimate`），纹理仍由 `--sat-max-size` 独立决定，所以简化后贴图依旧清晰，几何开销可控。
- **404 缓存 TTL**（v2.4）：无数据(404)瓦片仍缓存为 0 字节标记，但带 24h TTL；过期后重新请求，防止限流导致的瞬时 404 被记成永久无数据。网络异常不缓存。
- **坐标插值必须在墨卡托空间做**（像素行号与墨卡托 Y 线性相关，与纬度非线性）——v1 用纬度线性插值，低 zoom 时地标会偏几十米，v2 已修正
- 高德/腾讯返回 GCJ-02，脚本内已换算为 WGS84 再取瓦片
- Overpass 必须带 User-Agent，否则返回 406（v1 缺 UA 导致地标静默为空）
- 峰值先在平滑图上定位再取原始高程，避免噪点误判
- 输出是单文件 HTML，DEM / 影像 / **Three.js 全部内嵌**，真·离线可看（v2.3 默认约 3.3 MB，其中 Three.js 约 1.27 MB、影像 JPEG 约 1.3 MB；`--sat-max-size 4096` + `--jpeg-quality 90` 可到 4–6 MB）。
  内嵌实现：把 `three.module.js` 与 `OrbitControls.js` 放进 `<script type="text/plain">`，
  运行时包成 Blob URL 动态 `import`；OrbitControls 里的裸标识符 `from 'three'`
  需改写成 three 的 Blob URL。源码中的 `</script` 必须转义，否则会提前闭合节点。
- **不要在模块顶层（`init()` 之外）碰 `terrain` / `controls` 等异步创建的变量**——
  它们在 `await parseDEM()` 之后才存在。v2 曾在顶层写 `terrain.userData.map = ...`，
  导致每次打开都 `Cannot read properties of undefined` 白屏（v2.1 已修）

## 更新日志

- **v2.4**（2026-09-22）性能与读图增强：① HTML 新增 **等高线叠加**（自动等高距、卫星/设色两模式通用、「等高线」按钮开关）；② DEM 解码默认挪到 **Web Worker 异步**执行（OffscreenCanvas 解 PNG，transferable 回传），4096 大网格不卡主线程，失败时回退同步解码；③ DEM 与卫星影像 **并行下载**（网络阶段并发，省首跑时间）；④ 新增 **`--max-verts`** 网格顶点硬上限 + 最近邻抽稀（LOD，防顶点爆炸，纹理仍清晰）；⑤ 404 无数据缓存加 **24h TTL**，防瞬时限流 404 被记成永久无数据。
- **v2.3**（2026-09-22）影像增强 + 交互：① 卫星纹理新增 **WebP** 编码选项（`--sat-format`，默认仍 JPEG——实测叠加 hillshade 的高熵纹理下 WebP 反而更大）；② 纹理 `--sharpen` 轻微锐化；③ 默认把 **微 hillshade 烤进贴图**（`--no-hillshade` / `--hillshade-strength` / `--hillshade-alt` 可调），平面影像也有立体感；④ HTML 新增 **自动旋转** 与 **导出 PNG** 按钮（`preserveDrawingBuffer` 已开）。
- **v2.2**（2026-09-22）影像清晰度优化：新增 `--sat-max-size`（默认 2048，原 1024），**影像纹理与网格分辨率解耦**——纹理可独立精细到 ~1.5 m/px 而不增加网格顶点；脚本按目标纹理尺寸自动抬高影像 zoom；页面"贴图分辨率"改显示 `sat_resolution_m`；`--max-sat-tiles` 默认 64→576、`--jpeg-quality` 默认 82→85。
- **v2.1**（2026-09-21）修复必现白屏：模块顶层访问异步变量 `terrain` 的 JS 错误；
  Three.js 改为默认内嵌（`--three auto`），彻底摆脱 CDN 依赖；
  新增 `check_render.js` 无头渲染校验并把 Step 2 拆成 2a/2b。
- **v2** 墨卡托空间插值修正地标偏移、地标随夸张系数联动、地标开关生效、垂直夸张自动推荐、
  地形底座、高程分层设色、响应式布局。
- **v1** 首个可用版本。

## 不要做的事

- 不要用 `pip install` 装到用户全局 Python
- 不要在地理编码失败后直接告诉用户"做不到"而不尝试 `--lat/--lon`
- 不要谎报已完整生成——Step 2 的校验必须真的跑过
- 不要只跑 2a（JSON 字段）就交付，**2b 的渲染校验也必跑**；否则用户打开白屏才发现
