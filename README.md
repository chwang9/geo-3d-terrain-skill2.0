# 三维真实地形生成器 (Geo-3D-Terrain)

> 输入任意地名（或经纬度），自动生成该地点的**真实三维地形网页** —— DEM 高程逐像素还原 + 卫星影像叠加 + Three.js 渲染，输出单文件 HTML。

## ✨ v2 效果特性

- 真实高程（AWS Terrarium / SRTM）+ 真实卫星影像（Esri World Imagery），非程序化生成
- 垂直夸张实时调节（1×–10×，默认按相对高差自动推荐）
- 三种显示：卫星影像贴图 / 高程分层设色 / 网格线
- 自动标注主峰（含 OSM 峰名）+ OSM 重要地标（已过滤酒店餐饮等噪点）
- 地形底座、雾效、墨卡托精确配准、响应式布局（窄屏不溢出）
- 瓦片磁盘缓存 + 并发下载 + 重试，重跑几乎瞬间完成

## 🚀 快速使用

### 1. 准备环境（隔离 venv，不污染全局）

```bash
PY="C:/Users/wangch/.workbuddy/binaries/python/versions/3.13.12/python.exe"
VENV="C:/Users/wangch/.workbuddy/binaries/python/envs/geo3d/Scripts/python.exe"
"$PY" -m venv "C:/Users/wangch/.workbuddy/binaries/python/envs/geo3d"
"$VENV" -m pip install -q requests numpy Pillow jinja2
```

### 2. 一条命令出图

```bash
"$VENV" scripts/prepare_terrain_data.py \
  --place "大蜀山" --zoom 14 --grid 3 \
  --out terrain.json --out-html 大蜀山.html
```

浏览器打开 `大蜀山.html` 即可。典型耗时 20–40 秒，输出约 400 KB。

### 3. 只改样式时重渲染（不重新下载）

```bash
"$VENV" scripts/generate_html.py --data terrain.json --out t.html --exaggeration 6 --cdn unpkg
```

## 📋 参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--place` | — | 地名（中英文均可），与 `--lat/--lon` 二选一 |
| `--lat` `--lon` | — | 直接给 WGS84 坐标，跳过地理编码 |
| `--zoom` | 14 | DEM 层级（3–15）。z14+grid3 ≈ 6.2 km，z12+grid3 ≈ 25 km |
| `--grid` | 3 | 覆盖 N×N 个瓦片 |
| `--max-size` | 1024 | DEM/影像最大边长（像素），超出自动降采样 |
| `--sat-extra` | 2 | 影像超采样级别（zoom+N），影像比高程更清晰 |
| `--max-sat-tiles` | 64 | 影像瓦片上限，超出自动降低超采样 |
| `--no-sat` / `--no-landmarks` | — | 跳过影像 / 跳过地标（降级快跑） |
| `--geocoder` | auto | amap(需 `AMAP_KEY`) → nominatim → photon 依次兜底 |
| `--out-html` | — | 一步生成 HTML |
| `--exaggeration` | auto | 垂直夸张，缺省时按"高差≈世界宽度18%"推荐 |
| `--cdn` | auto | Three.js CDN，auto 做连通性择优 |
| `--workers` | 8 | 并发下载线程数 |
| `--no-cache` | — | 禁用缓存（默认 `~/.cache/geo-3d-terrain`） |

## 🔧 工作原理

```
地名 → 多源地理编码(打分选优) → 瓦片范围计算
                                     ↓
               DEM 瓦片(并发+缓存) ──┐
               影像瓦片(超采样+裁切) ─┼→ 拼接/降采样/统计峰值
               Overpass POI(多端点) ─┘
                                     ↓
                          base64 内嵌 → 单文件 HTML
```

### 数据来源

| 类型 | 来源 | 备注 |
|------|------|------|
| DEM | AWS Terrarium `elevation-tiles-prod` | `elev = R*256 + G + B/256 - 32768`，z ≤ 15 |
| 影像 | Esri World Imagery | 默认超采样 2 级后按 bbox 精确裁切对齐 |
| 地标 | OSM / Overpass | 必须带 User-Agent，否则 406 |

### v2 相对 v1 修复的关键 bug

| 问题 | 说明 |
|------|------|
| 经纬度→像素用纬度线性插值 | 墨卡托投影下应为墨卡托 Y 线性插值；低 zoom 时地标偏移可达数十米 |
| 地标球体不跟随夸张系数 | 只更新了标签，球体浮空/陷地 |
| "显示地标"开关无效 | 每帧 `updateLabels` 强制重新显示 |
| Overpass 缺 User-Agent | 返回 406，地标静默为空 |
| 地理编码单点依赖 Nominatim | 国内不可达即整个流程失败；v2 三源兜底 + 打分 |
| 偶数 `--grid` 瓦片数错位 | `tx1 = tx_c+half+1` 导致 grid=4 取到 5 个瓦片 |
| 图例与操作提示重叠 | 已分层定位 |

## ⚠️ 注意事项

- 高德/腾讯坐标为 GCJ-02，脚本已换算为 WGS84 再取瓦片
- 海洋/无数据区域用最低高程填充；整块无数据会明确报错
- Three.js 走 CDN，离线环境打开会白屏（DEM/影像已内嵌，仅引擎需联网）
- 地标数量与质量取决于 OSM 数据覆盖，可能为空（不影响地形生成）

## 📄 License

MIT
