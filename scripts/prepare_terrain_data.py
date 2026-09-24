#!/usr/bin/env python3
"""
prepare_terrain_data.py  (v2.9.1)
===============================
三维真实地形数据准备脚本：输入地名 / 直接给经纬度 / 传 GeoJSON 文件，自动
  1. 地理编码（多源兜底 + 结果打分）；或按 GeoJSON 包围盒自动定位与自动选 zoom
  2. 下载 AWS Terrarium DEM 高程瓦片并拼接（按目标范围精确裁切）
  3. 下载 Esri World Imagery 卫星影像：源层级 = DEM zoom + --sat-extra（地名默认 → z17；自动封顶 z18，
     因实测 z19 无新细节），瓦片预算 --max-sat-tiles 兜底；
     纹理尺寸默认按「源像素」反推（夹在 [2048, 档位上限]），绝不比源更细（避免插值假细节）；
     处理顺序 = LANCZOS 缩放 → 两段 USM + 局部对比 + 微提饱和 → 烤入微 hillshade → JPEG 4:4:4 编码
     （--clear 只抬高纹理上限到 4096；实测此时纹理才是瓶颈，源抬到 z18 仅多 1.8% 细节却要 4 倍瓦片）
  4. 从 Overpass(OSM) 获取地标 POI（三端点**并发竞速**、不重试、带磁盘缓存）
  5. 解析 GeoJSON，把点/线/面作为「贴合地形的叠加图形」一并写进 terrain.json

v2.8 性能要点：三级磁盘缓存（瓦片 / 地标 POI 7 天 / 地理编码 30 天），同区域重跑从 71s 降到 ~2s；
日志末尾输出分阶段耗时（meta.timings 也有），一眼看出慢在下载还是 CPU。
v2.9 体积与工程化：--sat-format avif（实测比 JPEG 小 36%、比 WebP 小 26% 且编码更快）、
--dry-run（只算计划不下瓦片）、--cache-prune / --cache-max-mb（此前缓存只写不删）。
输出一个 terrain.json，供 generate_html.py 渲染成自包含 HTML。

依赖:
    pip install requests numpy Pillow

用法:
    # 两步
    python prepare_terrain_data.py --place "大蜀山" --zoom 14 --grid 3 --out terrain.json
    python generate_html.py --data terrain.json --out dashu.html

    # 一步到位
    python prepare_terrain_data.py --place "大蜀山" --out-html dashu.html

    # 最高清晰度（纹理 4096，源仍 z17——实测此时抬源层级只多 1.8% 细节）
    python prepare_terrain_data.py --place "大蜀山" --clear --out-html dashu.html

    # 地理编码失败时，直接给坐标（WGS84）
    python prepare_terrain_data.py --lat 31.8447 --lon 117.1661 --zoom 14 --out-html dashu.html

    # 传 GeoJSON：自动按包围盒定位、自动选 zoom、把图形叠到地形上
    python prepare_terrain_data.py --geojson 瑶海区.geojson --out-html 瑶海区三维地形.html
    python prepare_terrain_data.py --geojson a.geojson b.geojson --out-html out.html
    python prepare_terrain_data.py --geojson 高德导出.geojson --geojson-crs gcj02 --out-html out.html

数据来源:
    DEM:  AWS Terrarium  https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png
    SAT:  Esri World Imagery https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}
    POI:  Overpass API (OSM)
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageEnhance, ImageFilter
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    Retry = None

UA = "geo-3d-terrain/2.0 (terrain visualization skill)"
MIN_ZOOM, MAX_DEM_ZOOM, MAX_SAT_ZOOM = 3, 15, 19
# 影像「原生分辨率」上限：实测（合肥大蜀山）z18 原生约 0.51 m/px，z19 与 z18 放大后几乎无差别
# （高频细节 545→549），取 z19 只会多下 4 倍瓦片却不增加信息，故默认封顶 z18。
MAX_SAT_ZOOM_NATIVE = 18

# 清晰度档位：只填「用户没显式指定」的参数（None 才被预设填充，显式传参优先）
# clear 档也用 z17 源（sat_extra=3）：实测同为 4096 纹理时，z18 源只比 z17 源高 1.8%
# （△Lap 10957 vs 10761），却要 4 倍瓦片（2304 vs 576）——纹理才是此时瓶颈，源再细也被降采样平均掉。
# 想要那最后 2% 就显式 `--clear --sat-extra 4`。
SAT_PRESETS = {
    "standard": dict(sat_extra=3, max_sat_tiles=1200, tex_cap=2560, sharpen=1.35,
                     sat_clarity=0.35, jpeg_quality=85, sat_saturation=1.06),
    "clear": dict(sat_extra=3, max_sat_tiles=1200, tex_cap=4096, sharpen=1.8,
                  sat_clarity=0.50, jpeg_quality=86, sat_saturation=1.06),
}
# AVIF 的质量刻度与 JPEG 不同（同等 quality 下 AVIF 码率远低），未显式指定时按此取。
# 实测（2560² 真实地形纹理）：AVIF q70 = 2.20 MB，比 JPEG q85 4:4:4（3.44 MB）小 36%、
# 比 WebP q85（2.96 MB）小 26%，编码 618 ms —— 比 WebP 的 1337 ms 还快 2.2×。
AVIF_QUALITY = {"standard": 70, "clear": 72}
CACHE_DIR = Path(os.environ.get("GEO3D_CACHE", Path.home() / ".cache" / "geo-3d-terrain" / "tiles"))
POI_CACHE_DIR = CACHE_DIR.parent / "poi"      # 地标缓存（Overpass 是最慢一环，缓存收益最大）
USE_CACHE = True
NODATA_TTL = 24 * 3600  # 无数据(404)缓存有效期(秒)：过期后重新请求，避免瞬时限流 404 被记成永久无数据
POI_TTL = 7 * 24 * 3600        # 地标缓存有效期：POI 变化慢，7 天足够
GEO_TTL = 30 * 24 * 3600       # 地理编码缓存有效期：地名→坐标几乎不变，30 天
POI_TIMEOUT = 30               # 单端点客户端超时（服务端 25s + 余量）
POI_SERVER_TIMEOUT = 25        # Overpass 语句里的 [timeout:]：小 bbox 25s 绰绰有余（原 60s 是慢的主因）
CACHE_MAX_MB = 1024            # 缓存总盘上限（v2.9）：超出后按「最旧优先」清理，缓存可再生、丢了只是重下


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _progress(tag: str, done: int, total: int) -> None:
    """低频刷新进度，避免刷屏。"""
    if done % max(1, total // 10) == 0 or done == total:
        print(f"\r    {tag} {done}/{total}", end="", file=sys.stderr, flush=True)


# ==================================================================
# HTTP: 线程内复用 Session + 重试 + 磁盘缓存
# ==================================================================
_tls = threading.local()


def get_session() -> requests.Session:
    s = getattr(_tls, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA})
        if Retry is not None:
            retry = Retry(total=3, backoff_factor=0.6,
                          status_forcelist=[429, 500, 502, 503, 504],
                          allowed_methods=frozenset(["GET", "POST"]),
                          raise_on_status=False)
            ad = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32)
        else:
            ad = HTTPAdapter(pool_connections=32, pool_maxsize=32)
        s.mount("https://", ad)
        s.mount("http://", ad)
        _tls.s = s
    return s


def get_poi_session() -> requests.Session:
    """Overpass 专用 Session：**不挂重试**。

    瓦片下载的重试策略（Retry 3 次）对 Overpass 是灾难——它是慢查询而不是瞬时抖动，
    每次重试都要再等一个完整超时，实测把 30s 的查询放大成 101s。
    Overpass 侧已有「三端点并发竞速 + 磁盘缓存」兜底，不需要 urllib3 层面的重试。"""
    s = getattr(_tls, "poi", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA})
        ad = HTTPAdapter(max_retries=0, pool_connections=8, pool_maxsize=8)
        s.mount("https://", ad)
        s.mount("http://", ad)
        _tls.poi = s
    return s


def _cache_path(kind: str, z: int, x: int, y: int) -> Path:
    return CACHE_DIR / kind / str(z) / str(x) / f"{y}.bin"


def _cache_age(cp: Path) -> float:
    """缓存文件已存活秒数（用于无数据 TTL 判断）。"""
    try:
        return time.time() - cp.stat().st_mtime
    except Exception:
        return 1e9


def _cache_roots() -> list:
    """可清理的缓存根目录。**不含** three/（那是工具链源码，不是可再生缓存）。"""
    return [CACHE_DIR, POI_CACHE_DIR, _GEO_CACHE_DIR]


def cache_usage() -> tuple[int, int]:
    """返回 (文件数, 字节数)。"""
    n = sz = 0
    for root in _cache_roots():
        if not root.exists():
            continue
        for p in root.rglob("*"):
            try:
                if p.is_file():
                    n += 1
                    sz += p.stat().st_size
            except OSError:
                continue
    return n, sz


def prune_cache(max_age_days: float | None = None, max_mb: float | None = None) -> tuple[int, int]:
    """按「最旧优先」清理缓存，返回 (删除文件数, 释放字节数)。

    v2.9：缓存此前只写不删（实测跑几轮就 4161 个文件 / 70 MB 且持续增长），
    现在支持 `--cache-prune N`（清 N 天前的）与 `--cache-max-mb`（总量上限，超出按最旧删）。
    注意 404 空文件也在这里被清掉——它们本来就带 TTL，清了只会让下次重新探测，无副作用。
    """
    items: list[tuple[float, int, Path]] = []
    for root in _cache_roots():
        if not root.exists():
            continue
        for p in root.rglob("*"):
            try:
                if p.is_file():
                    st = p.stat()
                    items.append((st.st_mtime, st.st_size, p))
            except OSError:
                continue
    removed = freed = 0
    if max_age_days is not None:
        cut = time.time() - max_age_days * 86400.0
        keep = []
        for m, s, p in items:
            if m < cut:
                try:
                    p.unlink()
                    removed += 1
                    freed += s
                except OSError:
                    pass
            else:
                keep.append((m, s, p))
        items = keep
    if max_mb is not None:
        total = sum(s for _, s, _ in items)
        limit = max_mb * 1024.0 * 1024.0
        if total > limit:
            for m, s, p in sorted(items, key=lambda t: t[0]):   # 最旧的先删
                if total <= limit:
                    break
                try:
                    p.unlink()
                    removed += 1
                    freed += s
                    total -= s
                except OSError:
                    pass
    return removed, freed


def fetch_bytes(url: str, kind: str = "", z: int = 0, x: int = 0, y: int = 0,
                timeout: float = 30, use_cache: bool = True) -> bytes | None:
    """下载字节；None 表示无数据（404 或缺数据）。

    无数据(404)会被缓存为 0 字节标记，但带 TTL：过期后重新请求，
    防止限流导致的瞬时 404 被记成永久无数据。网络异常不缓存（每次回退重试）。"""
    cp = None
    if use_cache and USE_CACHE and kind:
        cp = _cache_path(kind, z, x, y)
        if cp.exists():
            data = cp.read_bytes()
            if data == b"":
                if _cache_age(cp) < NODATA_TTL:
                    return None  # 仍在有效期内，按无数据处理
                # 已过期，继续往下重新下载
            else:
                return data
    try:
        r = get_session().get(url, timeout=timeout)
        if r.status_code == 404:
            if cp:
                cp.parent.mkdir(parents=True, exist_ok=True)
                cp.write_bytes(b"")  # 标记无数据（带 TTL，不是永久）
            return None
        r.raise_for_status()
        data = r.content
        if cp:
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_bytes(data)
        return data
    except Exception:
        return None


# ==================================================================
# Web Mercator 工具
# ==================================================================
def lon_to_tile_x(lon: float, z: int) -> float:
    return (lon + 180.0) / 360.0 * (1 << z)


def lat_to_tile_y(lat: float, z: int) -> float:
    lat = max(-85.05112878, min(85.05112878, lat))
    r = math.radians(lat)
    return (1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * (1 << z)


def tile_x_to_lon(x: float, z: int) -> float:
    return x / (1 << z) * 360.0 - 180.0


def tile_y_to_lat(y: float, z: int) -> float:
    n = math.pi - 2 * math.pi * y / (1 << z)
    return math.degrees(math.atan(math.sinh(n)))


def merc_y(lat: float) -> float:
    """归一化墨卡托 Y (0..1)。像素行号与它线性相关，插值必须在墨卡托空间做。"""
    lat = max(-85.05112878, min(85.05112878, lat))
    r = math.radians(lat)
    return (1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2


def merc_y_to_lat(y: float) -> float:
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y))))


def haversine(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def ground_res_m(lat: float, z: int) -> float:
    """Web Mercator 在给定纬度、给定 zoom 下的地面分辨率（米/像素）。

    用于判断「纹理是否已细于源影像」——纹理比源更细就只是插值假细节。
    """
    return 156543.03392 * math.cos(math.radians(lat)) / float(1 << z)


# ==================================================================
# 地理编码：多源 + 打分
# ==================================================================
PREFERRED = {
    ("natural", "peak"), ("natural", "volcano"), ("natural", "mountain_range"),
    ("natural", "ridge"), ("natural", "hill"), ("natural", "cliff"),
    ("natural", "water"), ("natural", "wood"), ("natural", "peak"),
    ("tourism", "attraction"), ("tourism", "viewpoint"), ("tourism", "zoo"),
    ("leisure", "park"), ("leisure", "nature_reserve"),
    ("place", "mountain"), ("landuse", "forest"), ("waterway", "lake"),
}
PENALTY_KEYS = {"railway", "highway", "public_transport", "shop", "office", "craft"}
PENALTY_PAIRS = {("amenity", "bus_station"), ("amenity", "parking"), ("amenity", "fuel")}


def _score(query: str, name: str, key: str, value: str, importance: float) -> float:
    s = 0.0
    n, q = (name or "").strip(), (query or "").strip()
    if n and q:
        if n == q:
            s += 60
        elif q in n:
            s += 30
        elif n in q:
            s += 8
    if (key, value) in PREFERRED:
        s += 35
    if key == "natural":
        s += 12
    if key in PENALTY_KEYS or (key, value) in PENALTY_PAIRS:
        s -= 45
    s += importance * 20
    return s


def _norm_nominatim(items: list, query: str) -> list:
    out = []
    for it in items:
        try:
            lat, lon = float(it["lat"]), float(it["lon"])
        except Exception:
            continue
        name = it.get("display_name", "") or ""
        # Nominatim 的 name 在 display_name 第一段
        short = name.split(",")[0].strip()
        out.append({
            "name": short, "lat": lat, "lon": lon,
            "score": _score(query, short, it.get("class", "") or it.get("category", ""),
                            it.get("type", ""), float(it.get("importance", 0) or 0)),
            "src": "nominatim",
        })
    return out


def _norm_photon(items: list, query: str) -> list:
    out = []
    for f in items:
        pr = f.get("properties", {}) or {}
        geo = f.get("geometry", {}) or {}
        co = geo.get("coordinates") or []
        if len(co) < 2:
            continue
        lon, lat = float(co[0]), float(co[1])
        name = pr.get("name") or ""
        out.append({
            "name": name, "lat": lat, "lon": lon,
            "score": _score(query, name, pr.get("osm_key", ""), pr.get("osm_value", ""), 0.4),
            "src": "photon",
        })
    return out


def gcj02_to_wgs84(lon: float, lat: float) -> tuple[float, float]:
    """高德/腾讯返回 GCJ-02，需换算成 WGS84 才能对齐全球瓦片。"""
    a, ee = 6378245.0, 0.00669342162296594323

    def _t(lon_, lat_):
        dlat = _tlat(lon_ - 105.0, lat_ - 35.0)
        dlon = _tlon(lon_ - 105.0, lat_ - 35.0)
        rlat = lat_ / 180.0 * math.pi
        magic = math.sin(rlat)
        magic = 1 - ee * magic * magic
        smagic = math.sqrt(magic)
        dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * smagic) * math.pi)
        dlon = (dlon * 180.0) / (a / smagic * math.cos(rlat) * math.pi)
        return dlon, dlat

    def _tlat(x, y):
        ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
        ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
        ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
        return ret

    def _tlon(x, y):
        ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
        ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
        ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
        return ret

    wlon, wlat = lon, lat
    for _ in range(4):
        dlon, dlat = _t(wlon, wlat)
        wlon = lon - dlon
        wlat = lat - dlat
    return wlon, wlat


def geocode(query: str, providers: str = "auto", verbose: bool = True) -> tuple[float, float, str]:
    """返回 (lat, lon, 命中的名称)。多源并发，按打分取最优；结果缓存 30 天（地名→坐标几乎不变）。"""
    ck = _geo_cache_key(query, providers)
    hit = _geo_cache_get(ck)
    if hit is not None:
        lat, lon, nm = hit
        if verbose:
            log(f"    · 地理编码缓存命中：{nm} ({lat:.5f}, {lon:.5f})")
        return lat, lon, nm
    cands: list[dict] = []

    def try_amap():
        key = os.environ.get("AMAP_KEY")
        if not key:
            return []
        r = get_session().get("https://restapi.amap.com/v3/geocode/geo",
                              params={"address": query, "key": key, "city": os.environ.get("AMAP_CITY", "")},
                              timeout=20)
        r.raise_for_status()
        js = r.json()
        if js.get("status") != "1" or not js.get("geocodes"):
            return []
        out = []
        for g in js["geocodes"][:5]:
            lon, lat = [float(v) for v in g["location"].split(",")]
            lon, lat = gcj02_to_wgs84(lon, lat)
            out.append({"name": query, "lat": lat, "lon": lon, "score": 70, "src": "amap"})
        return out

    def try_nominatim():
        out = []
        for host in ("https://nominatim.openstreetmap.org", "https://nominatim.geocoding.ai"):
            try:
                r = get_session().get(host + "/search",
                                      params={"q": query, "format": "json", "limit": 5,
                                              "accept-language": "zh-CN,zh"},
                                      timeout=20)
                if r.status_code == 200:
                    out += _norm_nominatim(r.json(), query)
                    break
            except Exception:
                continue
        return out

    def try_photon():
        r = get_session().get("https://photon.komoot.io/api/",
                              params={"q": query, "limit": 8, "lang": "default"}, timeout=20)
        r.raise_for_status()
        return _norm_photon((r.json() or {}).get("features", []), query)

    table = {
        "amap": [try_amap],
        "nominatim": [try_nominatim],
        "photon": [try_photon],
    }
    if providers == "auto":
        fns = [try_amap, try_nominatim, try_photon]
    else:
        fns = []
        for p in providers.split(","):
            fns += table.get(p.strip(), [])

    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(f) for f in fns]
        for fu, f in zip(futs, fns):
            try:
                got = fu.result() or []
                cands += got
                if verbose:
                    log(f"    · 地理编码 {f.__name__}: 命中 {len(got)} 条")
            except Exception as e:
                if verbose:
                    log(f"    · 地理编码 {f.__name__} 失败: {type(e).__name__}")

    if not cands:
        raise RuntimeError(
            f"地理编码失败：「{query}」在所有数据源均无结果。\n"
            f"  建议：用更通用的名称重试（如「大蜀山 合肥」），或直接指定坐标 "
            f"--lat <纬度> --lon <经度>（WGS84）。"
        )

    cands.sort(key=lambda c: c["score"], reverse=True)
    best = cands[0]
    if verbose:
        log(f"    · 选中 [{best['src']}] {best['name']} @ ({best['lat']:.5f}, {best['lon']:.5f}) score={best['score']:.0f}")
    _geo_cache_put(ck, [best["lat"], best["lon"], best["name"]])
    return best["lat"], best["lon"], best["name"]


# ==================================================================
# 瓦片下载
# ==================================================================
def download_dem_tile(z: int, x: int, y: int) -> np.ndarray:
    x %= (1 << z)
    url = f"https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
    raw = fetch_bytes(url, "dem", z, x, y, timeout=45)
    if raw is None:
        return np.full((256, 256), np.nan, dtype=np.float32)  # 海洋/无数据
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    a = np.asarray(img, dtype=np.float32)
    return (a[:, :, 0] * 256.0 + a[:, :, 1] + a[:, :, 2] / 256.0 - 32768.0)


def download_sat_tile(z: int, x: int, y: int) -> Image.Image:
    x %= (1 << z)
    url = (f"https://server.arcgisonline.com/ArcGIS/rest/services/"
           f"World_Imagery/MapServer/tile/{z}/{y}/{x}")
    raw = fetch_bytes(url, "sat", z, x, y, timeout=45)
    if raw is None:
        return Image.new("RGB", (256, 256), (110, 118, 128))
    return Image.open(io.BytesIO(raw)).convert("RGB")


def mosaic_dem(z: int, tx0: int, ty0: int, nx: int, ny: int, workers: int) -> np.ndarray:
    tiles: dict[tuple[int, int], np.ndarray] = {}
    jobs = [(tx0 + i, ty0 + j) for j in range(ny) for i in range(nx)]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(download_dem_tile, z, x, y): (x, y) for x, y in jobs}
        done = 0
        for fu in futs:
            x, y = futs[fu]
            try:
                tiles[(x, y)] = fu.result()
            except Exception:
                tiles[(x, y)] = np.full((256, 256), np.nan, dtype=np.float32)
            done += 1
            _progress("DEM 瓦片", done, len(jobs))
    print("", file=sys.stderr)
    rows = [np.hstack([tiles[(tx0 + i, ty0 + j)] for i in range(nx)]) for j in range(ny)]
    return np.vstack(rows)


def mosaic_sat(z: int, tx0: int, ty0: int, nx: int, ny: int, workers: int,
               crop_px: tuple[int, int, int, int] | None = None) -> Image.Image:
    """下载并拼接影像瓦片。crop_px=(x0,y0,x1,y1) 时在 numpy 阶段先裁再转 Image，
    避免 z17 大范围下整幅马赛克（可达 ~7000² px ≈ 140 MB）多做两次全图拷贝。"""
    tiles: dict[tuple[int, int], Image.Image] = {}
    jobs = [(tx0 + i, ty0 + j) for j in range(ny) for i in range(nx)]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(download_sat_tile, z, x, y): (x, y) for x, y in jobs}
        done = 0
        for fu in futs:
            x, y = futs[fu]
            try:
                tiles[(x, y)] = fu.result()
            except Exception:
                tiles[(x, y)] = Image.new("RGB", (256, 256), (110, 118, 128))
            done += 1
            _progress("影像瓦片", done, len(jobs))
    print("", file=sys.stderr)
    full = np.vstack([np.hstack([np.asarray(tiles[(tx0 + i, ty0 + j)]) for i in range(nx)])
                      for j in range(ny)])
    if crop_px is not None:
        x0, y0, x1, y1 = crop_px
        full = full[max(0, y0):max(1, y1), max(0, x0):max(1, x1)]
    return Image.fromarray(full, mode="RGB")


# ==================================================================
# 图像处理
# ==================================================================
def box_blur(a: np.ndarray, r: int) -> np.ndarray:
    """可分离近似盒滤波，用于峰值去噪。"""
    if r <= 0:
        return a.copy()
    k = 2 * r + 1
    p = np.pad(a, r, mode="reflect")
    out = np.zeros_like(a)
    for dy in range(k):
        for dx in range(k):
            out += p[dy:dy + a.shape[0], dx:dx + a.shape[1]]
    return out / (k * k)


def despike(a: np.ndarray, thresh_m: float = 100.0, max_ratio: float = 0.01) -> tuple[np.ndarray, int]:
    """剔除 DEM 里的孤立坏点（数据源空洞/编码错误），返回 (清洗后, 修正像素数)。

    实测（合肥周边 z11）：整个范围里有 4 个像素是 -768 / -696 / -532 m（占 0.002%），
    但它们会把 min_elev 从 ~0 拉到 -768：相对高差虚增 60%，高程色带被压扁、
    自动垂直夸张变小、剖面图 y 轴被拉爆——一个坏点毁掉整张图。

    判据：与 3×3 邻域均值相差超过 thresh_m 才算坏点（真实陡崖在 30 m DEM 上
    很难达到这个量级）；**坏点占比超过 max_ratio 就整批放弃清洗**——那说明
    遇到的不是坏点而是真实地形（海岸线、峡谷），硬洗会把真地形抹平。
    """
    if thresh_m <= 0 or not np.isfinite(a).any():
        return a, 0
    out = a.astype(np.float32)
    total = 0
    # 迭代且逐轮放大邻域：坏点常成簇出现（实测 4 个坏点挤在 4×3 的小块里），
    # 3×3 邻域的均值本身也被污染，换完还是个"半深坑"；放大到 5×5 / 7×7 才够到干净像素
    for r in (1, 2, 3):
        m = box_blur(out, r)
        bad = np.abs(out - m) > float(thresh_m)
        n = int(bad.sum())
        if n == 0 or n > max_ratio * out.size:
            break
        out[bad] = m[bad]      # 用邻域均值顶上（比填常量自然，不留下"坑"）
        total += n
    return out, total


def downsample(a: np.ndarray, max_size: int) -> np.ndarray:
    h, w = a.shape
    f = max(1, int(math.ceil(max(h, w) / max_size)))
    if f == 1:
        return a
    nh, nw = h // f, w // f
    return a[:nh * f, :nw * f].reshape(nh, f, nw, f).mean(axis=(1, 3))


def decimate(a: np.ndarray, f: int) -> np.ndarray:
    """最近邻整块抽稀（LOD / 网格简化）：每 f 个像素取一个，用于网格顶点数超限时。

    只降网格几何密度，不动纹理（纹理由 --sat-max-size 独立决定），故简化后贴图仍清晰。"""
    f = max(1, int(f))
    if f == 1:
        return a
    h, w = a.shape
    return a[::f, ::f][: h // f, : w // f]


def encode_terrarium(dem: np.ndarray) -> str:
    """高程 -> Terrarium PNG -> base64（无 data: 前缀）。

    不用 optimize=True（会触发逐行滤波搜索，慢数倍）；compress_level=6 与 9
    对高熵高程数据体积差 <2%，速度却快得多。"""
    v = np.clip(np.round(dem) + 32768.0, 0.0, 65535.0)
    r = np.floor(v / 256.0).astype(np.uint8)
    g = np.floor(v % 256.0).astype(np.uint8)
    b = np.round((v - np.floor(v)) * 255.0).astype(np.uint8)
    img = Image.fromarray(np.stack([r, g, b], axis=-1), mode="RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=6)
    return base64.b64encode(buf.getvalue()).decode()


def compute_hillshade(elev: np.ndarray, azimuth: float = 315.0, altitude: float = 45.0,
                      z_factor: float = 1.0) -> np.ndarray:
    """经典 ESRI hillshade：返回与 elev 同形的 [0,1] 光照系数（0=无光, 1=正对光源）。

    用于把地形起伏"烤"进卫星贴图，让平面影像也有立体感。"""
    e = np.pad(elev, 1, mode="edge").astype(np.float32)
    dzdx = (e[1:-1, 2:] - e[1:-1, :-2]) / 2.0
    dzdy = (e[2:, 1:-1] - e[:-2, 1:-1]) / 2.0
    slope = np.arctan(z_factor * np.sqrt(dzdx ** 2 + dzdy ** 2))
    aspect = np.arctan2(dzdy, -dzdx)
    zenith = math.radians(90.0 - altitude)
    az = math.radians(azimuth)
    shade = (np.cos(zenith) * np.cos(slope)
             + np.sin(zenith) * np.sin(slope) * np.cos(az - aspect))
    return np.clip(shade, 0.0, 1.0)


def enhance_texture(img: "Image.Image", sharpen: float = 1.3, clarity: float = 0.35,
                    saturation: float = 1.0) -> "Image.Image":
    """让卫星贴图"看起来更清晰"的三件套（v2.6）。

    旧版只有 `ImageEnhance.Sharpness`——PIL 里它只是「原图 与 平滑图」的线性混合，
    对 2 m/px 级的卫星影像几乎看不出差别。这里换成真正的高频回加：

      1. 两段 USM：小半径(1.1)提微观边缘 + 中半径(2.6)提结构，带 threshold 避免放大平坦区噪点
      2. clarity：LAB 的 L 通道做大半径(8)高频回加，即"去灰雾"的局部对比，远处读图更利落
      3. saturation：轻微提饱和，抵消 JPEG 色度压缩带来的发灰

    `sharpen` 保留旧语义：1.0 = 不锐化，越大越锐；clarity 0 = 关闭。
    """
    amt = max(0.0, sharpen - 1.0)
    if amt > 0:
        img = img.filter(ImageFilter.UnsharpMask(radius=1.1, percent=int(round(110 * amt)),
                                                 threshold=2))
        img = img.filter(ImageFilter.UnsharpMask(radius=2.6, percent=int(round(58 * amt)),
                                                 threshold=3))
    if clarity > 0:
        lab = img.convert("LAB")
        L, A, B = lab.split()
        la = np.asarray(L, dtype=np.float32)
        base = np.asarray(L.filter(ImageFilter.GaussianBlur(8)), dtype=np.float32)
        # 只在亮度上回加高频，色度不动，避免出现彩边
        L = Image.fromarray(np.clip(la + clarity * (la - base), 0, 255).astype(np.uint8), mode="L")
        img = Image.merge("LAB", (L, A, B)).convert("RGB")
    if saturation and abs(saturation - 1.0) > 1e-3:
        img = ImageEnhance.Color(img).enhance(saturation)
    return img


def encode_sat(crop: "Image.Image", fmt: str, quality: int, subsampling: str = "444") -> tuple[str, str]:
    """编码卫星纹理，返回 (b64, mime)。默认 JPEG 4:4:4；webp / avif 需 `--sat-format` 显式开启。

    JPEG 默认 4:4:4 色度不抽样（`subsampling=0`）。旧版用 PIL 默认的 4:2:0，
    色度被减半，彩色地物（屋顶/绿地边界/道路）边缘会发糊，正是"不清晰"的来源之一。
    progressive 也保留：实测（2560² q85 444）去掉它编码只快 ~96 ms，文件却大 6.9%，得不偿失。

    兜底链（v2.9）：avif → avif(降级质量) → webp → jpeg(按配置) → jpeg(4:2:0) →
    jpeg(不 optimize) → 低质量 jpeg。**必须逐级降级而不是同参数重试**——实测 Pillow 12
    对部分大图做 4:4:4 编码会抛 `Suspension not allowed here / broken data stream`，
    同参数重来必然再失败；AVIF 在没编译 libavif 的环境下也会直接失败，故必须能落回 JPEG。
    """
    ss = 0 if subsampling == "444" else 2
    attempts: list[tuple[str, dict]] = []
    if fmt == "avif":
        attempts += [
            ("image/avif", dict(format="AVIF", quality=quality)),
            ("image/avif", dict(format="AVIF", quality=max(40, quality - 15))),
        ]
    if fmt == "webp":
        attempts.append(("image/webp", dict(format="WEBP", quality=quality, method=4)))
    attempts += [
        ("image/jpeg", dict(format="JPEG", quality=quality, optimize=True,
                            progressive=True, subsampling=ss)),
        ("image/jpeg", dict(format="JPEG", quality=quality, optimize=True,
                            progressive=True, subsampling=2)),
        ("image/jpeg", dict(format="JPEG", quality=quality, subsampling=2)),
        ("image/jpeg", dict(format="JPEG", quality=max(50, quality - 25), subsampling=2)),
    ]
    last: Exception | None = None
    for mime, kw in attempts:
        buf = io.BytesIO()
        try:
            crop.save(buf, **kw)
        except Exception as e:          # noqa: BLE001 - 编码失败必须降级而不是中断
            last = e
            continue
        if len(buf.getvalue()) > 512:
            return base64.b64encode(buf.getvalue()).decode(), mime
        last = RuntimeError("编码结果为空")
    raise RuntimeError(f"影像编码全部尝试失败：{last}")


# ==================================================================
# 地标（Overpass）
# ==================================================================
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]
# 地标分类：把 OSM 标签映射为「中文类别 + 图标 + 优先级」。
# 设计原则：不假设目标一定是山峰——平原/城区同样有湖泊、交通枢纽、城市片区、医院高校等核心地标。
# 优先级（数值越小越优先）：山峰/火山 > 城市/城镇 > 交通枢纽 > 城市公共设施 > 旅游/古迹 >
# 湖泊 > 公园 > 片区/街区/乡村 > 宗教场所 > 河流/运河 > 水塘（小水体降权，避免挤掉核心地标）。
LM_PRIORITY = [
    "山峰", "火山",
    "城市", "城镇",
    "机场", "车站",
    "高校", "医院", "政府",
    "景点", "观景台",
    "古迹",
    "湖泊",
    "公园",
    "片区", "街区", "乡村",
    "宗教场所",
    "河流", "运河",
    "地铁站",
    "水塘",
]
_LM_RANK = {c: i for i, c in enumerate(LM_PRIORITY)}

# 值得在地形图上标注的类型；酒店/餐饮/商铺/加油站等一律过滤，否则地标全是噪点
KEEP_TOURISM = {"attraction", "viewpoint", "museum", "zoo", "theme_park",
                "artwork", "gallery", "picnic_site", "wilderness_hut", "alpine_hut"}
KEEP_LEISURE = {"park", "nature_reserve", "garden", "golf_course", "stadium"}
KEEP_HISTORIC = {"monument", "memorial", "castle", "ruins", "tomb", "fort",
                 "archaeological_site", "shrine", "temple", "wayside_cross"}
KEEP_AMENITY = {"hospital", "university", "college", "townhall", "place_of_worship"}
KEEP_PLACE = {"city", "town", "suburb", "neighbourhood", "village", "hamlet"}
# 山名关键字：用于甄别 OSM 上被误标 natural=peak 的非山峰节点（如「草坡风貌区」）
_MOUNTAIN_KW = ("山", "峰", "岭", "顶", "岳", "岗", "粱",
                "mount", "peak", "mountain", "hill")


def _keep(tags: dict) -> bool:
    nat = tags.get("natural")
    if nat in ("peak", "volcano", "water"):
        return True
    if tags.get("waterway") in ("river", "canal"):
        return True
    if tags.get("aeroway") == "airport":
        return True
    if tags.get("railway") in ("station", "halt") or tags.get("station") == "subway":
        return True
    if "tourism" in tags:
        return tags["tourism"] in KEEP_TOURISM
    if "historic" in tags:
        v = tags["historic"]
        return v in KEEP_HISTORIC or v not in ("yes", "building", "house")
    if "leisure" in tags:
        return tags["leisure"] in KEEP_LEISURE
    if "amenity" in tags:
        return tags["amenity"] in KEEP_AMENITY
    if "place" in tags:
        return tags["place"] in KEEP_PLACE
    return False


def _lm_category(tags: dict) -> tuple[str, str, int]:
    """OSM 标签 → (中文类别, 图标, 优先级数值)。desc 用中文类别，不再暴露 raw OSM key。"""
    nat = tags.get("natural")
    if nat == "peak":
        return ("山峰", "🏔", _LM_RANK["山峰"])
    if nat == "volcano":
        return ("火山", "🌋", _LM_RANK["火山"])
    if nat == "water":
        # 小水体（pond/basin）降权为「水塘」，避免一堆池塘挤掉巢湖、枢纽等核心地标
        if tags.get("water") in ("pond", "basin", "wastewater"):
            return ("水塘", "💧", _LM_RANK["水塘"])
        return ("湖泊", "🌊", _LM_RANK["湖泊"])
    ww = tags.get("waterway")
    if ww == "river":
        return ("河流", "🌊", _LM_RANK["河流"])
    if ww == "canal":
        return ("运河", "🌊", _LM_RANK["运河"])
    if tags.get("aeroway") == "airport":
        return ("机场", "✈️", _LM_RANK["机场"])
    if tags.get("station") == "subway":
        return ("地铁站", "🚇", _LM_RANK["地铁站"])
    if tags.get("railway") in ("station", "halt"):
        return ("车站", "🚉", _LM_RANK["车站"])
    if "tourism" in tags:
        t = tags["tourism"]
        if t == "viewpoint":
            return ("观景台", "🔭", _LM_RANK["观景台"])
        return ("景点", "📍", _LM_RANK["景点"])
    if "historic" in tags:
        return ("古迹", "🏛", _LM_RANK["古迹"])
    if "leisure" in tags:
        return ("公园", "🌳", _LM_RANK["公园"])
    if "amenity" in tags:
        a = tags["amenity"]
        if a in ("university", "college"):
            return ("高校", "🎓", _LM_RANK["高校"])
        if a == "hospital":
            return ("医院", "🏥", _LM_RANK["医院"])
        if a == "townhall":
            return ("政府", "🏢", _LM_RANK["政府"])
        if a == "place_of_worship":
            return ("宗教场所", "⛪", _LM_RANK["宗教场所"])
        return ("地标", "📍", len(LM_PRIORITY))
    if "place" in tags:
        p = tags["place"]
        if p == "city":
            return ("城市", "🏙", _LM_RANK["城市"])
        if p == "town":
            return ("城镇", "🏘", _LM_RANK["城镇"])
        if p == "suburb":
            return ("片区", "🗺", _LM_RANK["片区"])
        if p == "neighbourhood":
            return ("街区", "📍", _LM_RANK["街区"])
        return ("乡村", "🏡", _LM_RANK["乡村"])
    return ("地标", "📍", len(LM_PRIORITY))


def _poi_cache_key(lon_w: float, lat_s: float, lon_e: float, lat_n: float, limit: int) -> str:
    import hashlib
    s = f"{lon_w:.4f},{lat_s:.4f},{lon_e:.4f},{lat_n:.4f},{limit}"
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:16]


_GEO_CACHE_DIR = CACHE_DIR.parent / "geocode"


def _geo_cache_key(query: str, providers: str) -> str:
    import hashlib
    return hashlib.md5(f"{providers}|{query.strip().lower()}".encode("utf-8")).hexdigest()[:16]


def _geo_cache_get(key: str) -> tuple[float, float, str] | None:
    """返回缓存的 (lat, lon, name)；无缓存或已过期返回 None。失败结果不缓存。"""
    if not USE_CACHE:
        return None
    fp = _GEO_CACHE_DIR / f"{key}.json"
    try:
        if not fp.exists() or time.time() - fp.stat().st_mtime > GEO_TTL:
            return None
        v = json.loads(fp.read_text(encoding="utf-8"))
        return float(v[0]), float(v[1]), str(v[2])
    except Exception:                # noqa: BLE001
        return None


def _geo_cache_put(key: str, val: list) -> None:
    if not USE_CACHE:
        return
    try:
        _GEO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (_GEO_CACHE_DIR / f"{key}.json").write_text(
            json.dumps(val, ensure_ascii=False), encoding="utf-8")
    except Exception:                # noqa: BLE001
        pass


def _poi_cache_get(key: str) -> list | None:
    if not USE_CACHE:
        return None
    fp = POI_CACHE_DIR / f"{key}.json"
    try:
        if not fp.exists():
            return None
        if time.time() - fp.stat().st_mtime > POI_TTL:
            return None
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:                # noqa: BLE001 - 缓存坏了就当没缓存
        return None


def _poi_cache_put(key: str, items: list) -> None:
    if not USE_CACHE:
        return
    try:
        POI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (POI_CACHE_DIR / f"{key}.json").write_text(
            json.dumps(items, ensure_ascii=False), encoding="utf-8")
    except Exception:                # noqa: BLE001
        pass


def _overpass_one(ep: str, q: str) -> tuple[dict, str] | None:
    """向单个 Overpass 端点发请求；成功返回 (json, endpoint)，失败返回 None。"""
    try:
        r = get_poi_session().post(ep, data={"data": q}, timeout=POI_TIMEOUT)
        if r.status_code == 200:
            return r.json(), ep
    except Exception:                # noqa: BLE001 - 单端点失败由竞速逻辑兜底
        return None
    return None


def fetch_landmarks(lon_w: float, lat_s: float, lon_e: float, lat_n: float,
                    limit: int = 12, min_sep_ratio: float = 0.05) -> list:
    if lon_e - lon_w <= 0 or lat_n - lat_s <= 0:
        return []
    # 先查磁盘缓存：同一区域反复重跑（调夸张系数/配色）不必每次都等 Overpass。
    # Overpass 是整个流程里最慢的一环（实测单端点可到 60s），缓存是性价比最高的优化。
    ck = _poi_cache_key(lon_w, lat_s, lon_e, lat_n, limit)
    cached = _poi_cache_get(ck)
    if cached is not None:
        log(f"    · 地标 {len(cached)} 个（磁盘缓存命中，跳过 Overpass）")
        return cached

    q = f"""[out:json][timeout:{POI_SERVER_TIMEOUT}];
(
  nwr["natural"="peak"]({lat_s},{lon_w},{lat_n},{lon_e});
  nwr["natural"="volcano"]({lat_s},{lon_w},{lat_n},{lon_e});
  nwr["natural"="water"]({lat_s},{lon_w},{lat_n},{lon_e});
  nwr["waterway"="river"]({lat_s},{lon_w},{lat_n},{lon_e});
  nwr["waterway"="canal"]({lat_s},{lon_w},{lat_n},{lon_e});
  nwr["place"~"city|town|suburb|neighbourhood|village|hamlet"]({lat_s},{lon_w},{lat_n},{lon_e});
  nwr["aeroway"="airport"]({lat_s},{lon_w},{lat_n},{lon_e});
  nwr["railway"="station"]({lat_s},{lon_w},{lat_n},{lon_e});
  nwr["railway"="halt"]({lat_s},{lon_w},{lat_n},{lon_e});
  nwr["station"="subway"]({lat_s},{lon_w},{lat_n},{lon_e});
  nwr["amenity"="hospital"]({lat_s},{lon_w},{lat_n},{lon_e});
  nwr["amenity"="university"]({lat_s},{lon_w},{lat_n},{lon_e});
  nwr["amenity"="college"]({lat_s},{lon_w},{lat_n},{lon_e});
  nwr["amenity"="townhall"]({lat_s},{lon_w},{lat_n},{lon_e});
  nwr["amenity"="place_of_worship"]({lat_s},{lon_w},{lat_n},{lon_e});
  nwr["tourism"]({lat_s},{lon_w},{lat_n},{lon_e});
  nwr["historic"]({lat_s},{lon_w},{lat_n},{lon_e});
  nwr["leisure"="park"]({lat_s},{lon_w},{lat_n},{lon_e});
);
out center tags;"""
    palette = [0xffcf4d, 0xff9f43, 0xff5252, 0x4ecdc4, 0x45b7d1,
               0x96ceb4, 0xffeaa7, 0xdda0dd, 0x74b9ff, 0xa29bfe,
               0x55efc4, 0xfd79a8]
    # 并发竞速：三个端点同时发，谁先成功用谁（串行试的代价是慢端点累加，实测可达 60s+）
    data = None
    try:
        with ThreadPoolExecutor(max_workers=len(OVERPASS_ENDPOINTS)) as ex:
            futs = {ex.submit(_overpass_one, ep, q): ep for ep in OVERPASS_ENDPOINTS}
            for fu in as_completed(futs):
                got = fu.result()
                if got is not None:
                    data, ep = got
                    log(f"    · 地标来源 {ep.split('/')[2]}")
                    break
                log(f"    · Overpass {futs[fu].split('/')[2]} 无结果")
    except Exception as e:                      # noqa: BLE001
        log(f"    · Overpass 并发异常: {type(e).__name__}")
    if data is None:
        log("    · 地标获取失败，跳过（不影响地形生成）")
        return []

    seen, items = set(), []
    for el in data.get("elements", []):
        tags = el.get("tags", {}) or {}
        name = tags.get("name:zh") or tags.get("name") or ""
        if not name:
            continue
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue
        if not _keep(tags):
            continue
        kn = name.strip().lower()
        if kn in seen:
            continue
        seen.add(kn)
        cat, icon, rank = _lm_category(tags)
        items.append({
            "name": name.strip(), "lat": float(lat), "lon": float(lon),
            "desc": cat, "icon": icon, "rank": rank,
            "ele": float(tags["ele"]) if "ele" in tags and _is_num(tags["ele"]) else None,
        })

    items.sort(key=lambda d: (d["rank"], -(d["ele"] or -9999)))
    span = max(lon_e - lon_w, lat_n - lat_s) * min_sep_ratio

    def _far(it, kept) -> bool:
        # 与已选地标「两维都近」才算近邻（任一维度远即可保留），沿用原 min_sep 语义
        return all(abs(it["lon"] - k["lon"]) > span or abs(it["lat"] - k["lat"]) > span
                   for k in kept)

    # 配额 + 轮询：先保证每类（城市/城镇/枢纽/水体/公园…）都有代表，再按 rank 回填。
    # 否则严格 rank 排序会让某一大类（如地铁站、小池塘）刷屏，挤掉核心地标。
    QUOTA = {"山峰": 2, "火山": 1, "城市": 2, "城镇": 2, "机场": 1, "车站": 2,
             "地铁站": 1, "高校": 1, "医院": 1, "政府": 1, "景点": 1, "观景台": 1,
             "古迹": 1, "湖泊": 3, "公园": 2, "片区": 2, "街区": 2, "乡村": 1,
             "宗教场所": 1, "河流": 1, "运河": 1, "水塘": 1}
    kept, counts = [], {}
    by_rank = sorted(items, key=lambda d: (d["rank"], -(d["ele"] or -9999)))
    # 轮询分配（循环游标）：每轮从「当前游标」往后找下一个尚有配额且能放下（min_sep）的类别放一个，
    # 游标前移。这样类别间真正交替，前 N 个优先类别（含湖泊/公园）都能保底出现，
    # 而不会被城市/车站等数量多的大类连续占满名额挤掉。
    ncat = len(LM_PRIORITY)
    start = 0
    for _ in range(limit):
        placed = False
        for step in range(ncat):
            ci = (start + step) % ncat
            cat = LM_PRIORITY[ci]
            if counts.get(cat, 0) >= QUOTA.get(cat, 1):
                continue
            cand = next((it for it in by_rank
                         if it["desc"] == cat and it not in kept and _far(it, kept)), None)
            if cand is not None:
                kept.append(cand)
                counts[cat] = counts.get(cat, 0) + 1
                start = (ci + 1) % ncat
                placed = True
                break
        if not placed:
            break
    for i, it in enumerate(kept):
        it["color"] = palette[i % len(palette)]
        it.pop("rank", None)
    _poi_cache_put(ck, kept)
    return kept


def _is_num(s) -> bool:
    try:
        float(str(s).replace("m", "").strip())
        return True
    except Exception:
        return False


# ==================================================================
# GeoJSON：解析 / 包围盒 / 简化 / 拍平成叠加图元
# ==================================================================
# 叠加图形默认配色（按顺序循环）
OV_COLORS = [0xffcf4d, 0x4ecdc4, 0xff6b6b, 0x45b7d1, 0x96ceb4, 0xdda0dd,
             0x74b9ff, 0xa29bfe, 0x55efc4, 0xfd79a8, 0xff9f43, 0x7bed9f,
             0xffd93d, 0x6c5ce7, 0x00cec9, 0xe17055]
# 识别名称用的属性键（按顺序优先）
NAME_KEYS = ("name", "NAME", "Name", "名称", "title", "TITLE", "label", "LABEL", "id", "ID")


def _isnum(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _fnum(v):
    """宽松数字解析：'2' / 2 / '2px' / '0.3' → float，失败返回 None。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(re.sub(r"[^0-9eE.+-]", "", str(v)) or "nan")
    except Exception:
        return None


def _iter_pts(coords):
    """递归产出坐标结构里所有 [lon, lat] 叶节点。"""
    if isinstance(coords, (list, tuple)):
        if len(coords) >= 2 and _isnum(coords[0]) and _isnum(coords[1]):
            yield float(coords[0]), float(coords[1])
        else:
            for c in coords:
                yield from _iter_pts(c)


def _map_leaf(coords, fn):
    """递归把坐标叶节点 [lon,lat(,alt)] 交给 fn 变换，保留第三维。"""
    if isinstance(coords, list):
        if len(coords) >= 2 and _isnum(coords[0]) and _isnum(coords[1]):
            x, y = fn(float(coords[0]), float(coords[1]))
            return [x, y] + list(coords[2:])
        return [_map_leaf(c, fn) for c in coords]
    return coords


def _geom_parts(g) -> list:
    """任意 GeoJSON Geometry → [(kind, coords), ...]，kind ∈ polygon|line|point。"""
    if not isinstance(g, dict):
        return []
    t = g.get("type")
    c = g.get("coordinates")
    if t == "GeometryCollection":
        out = []
        for sub in (g.get("geometries") or []):
            out += _geom_parts(sub)
        return out
    if t == "Polygon":
        return [("polygon", c or [])]
    if t == "MultiPolygon":
        return [("polygon", poly) for poly in (c or [])]
    if t == "LineString":
        return [("line", c or [])]
    if t == "MultiLineString":
        return [("line", ln) for ln in (c or [])]
    if t == "Point":
        return [("point", [c])] if c else []
    if t == "MultiPoint":
        return [("point", [p]) for p in (c or [])]
    return []


def _parse_color(v):
    """'#rrggbb' / 'rrggbb' / '#rgb' / 'rgb(r,g,b)' / 0xrrggbb / int → int(0xRRGGBB)。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v & 0xFFFFFF
    if isinstance(v, float):
        return int(v) & 0xFFFFFF
    s = str(v).strip()
    if not s:
        return None
    low = s.lower()
    if low.startswith("rgb"):
        nums = re.findall(r"\d+", s)
        if len(nums) >= 3:
            r, g, b = (int(n) for n in nums[:3])
            return ((r & 255) << 16) | ((g & 255) << 8) | (b & 255)
        return None
    if s.startswith("#"):
        s = s[1:]
    if re.fullmatch(r"[0-9a-fA-F]{6}", s):
        return int(s, 16)
    if re.fullmatch(r"[0-9a-fA-F]{3}", s):
        return int("".join(ch * 2 for ch in s), 16)
    return None


def load_geojson_raw(paths, crs: str = "wgs84", name_key: str | None = None) -> list:
    """读取一个或多个 GeoJSON 文件（FeatureCollection / Feature / 裸 Geometry）。

    返回 [{kind, coords, name, props, file}, ...]，坐标统一为 WGS84。"""
    out = []
    keys = ([name_key] if name_key else []) + list(NAME_KEYS)
    do_gcj = crs.lower() in ("gcj02", "gcj-02", "gcj_02", "amap", "高德", "tencent", "腾讯")
    for p in paths:
        path = Path(p)
        if not path.exists():
            raise RuntimeError(f"GeoJSON 文件不存在：{p}")
        try:
            gj = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as e:
            raise RuntimeError(f"GeoJSON 解析失败（{p}）：{e}")
        t = gj.get("type") if isinstance(gj, dict) else None
        if t == "FeatureCollection":
            feats = gj.get("features") or []
        elif t == "Feature":
            feats = [gj]
        elif t:
            feats = [{"type": "Feature", "geometry": gj, "properties": {}}]
        else:
            raise RuntimeError(f"不是合法的 GeoJSON（顶层缺少 type）：{p}")
        for ft in feats:
            if not isinstance(ft, dict):
                continue
            props = ft.get("properties")
            if not isinstance(props, dict):
                props = {}
            nm = ""
            for k in keys:
                if k and props.get(k) not in (None, ""):
                    nm = str(props[k]).strip()
                    break
            for kind, coords in _geom_parts(ft.get("geometry")):
                if do_gcj:
                    coords = _map_leaf(coords, gcj02_to_wgs84)
                if not any(True for _ in _iter_pts(coords)):
                    continue
                out.append({"kind": kind, "coords": coords, "name": nm,
                            "props": props, "file": path.name})
    return out


def raw_bbox(raw: list) -> tuple[float, float, float, float]:
    """返回 (lon_w, lat_s, lon_e, lat_n)。"""
    xs, ys = [], []
    for it in raw:
        for x, y in _iter_pts(it["coords"]):
            xs.append(x)
            ys.append(y)
    if not xs:
        raise RuntimeError("GeoJSON 中没有任何坐标点。")
    return min(xs), min(ys), max(xs), max(ys)


def _rdp(pts: list, tol: float) -> list:
    """Douglas-Peucker 抽稀（迭代实现，避免深递归爆栈）。"""
    if len(pts) < 3 or tol <= 0:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = pts[i]
        bx, by = pts[j]
        dx, dy = bx - ax, by - ay
        seg = math.hypot(dx, dy)
        dmax, idx = -1.0, -1
        for k in range(i + 1, j):
            px, py = pts[k]
            if seg < 1e-12:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(dy * (px - ax) - dx * (py - ay)) / seg
            if d > dmax:
                dmax, idx = d, k
        if dmax > tol and idx > 0:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [p for p, k in zip(pts, keep) if k]


def _simplify_ring(pts: list, tol: float) -> list:
    if len(pts) < 4:
        return pts
    closed = pts[0] == pts[-1]
    core = pts[:-1] if closed else pts
    core = _rdp(core, tol)
    if len(core) < 3 and closed:
        return pts
    return core + [core[0]] if closed else core


def simplify_raw(raw: list, tol: float) -> list:
    """按容差（度）抽稀所有线/面要素。"""
    if tol <= 0:
        return raw
    out = []
    for it in raw:
        if it["kind"] == "point":
            out.append(it)
            continue
        it = dict(it)
        if it["kind"] == "polygon":
            it["coords"] = [[[x, y] for x, y in _simplify_ring(list(_iter_pts(r)), tol)]
                            for r in (it["coords"] or [])]
        else:
            pts = list(_iter_pts(it["coords"]))
            it["coords"] = [[x, y] for x, y in _rdp(pts, tol)]
        out.append(it)
    return out


def count_pts(raw: list) -> int:
    return sum(1 for it in raw for _ in _iter_pts(it["coords"]))


def flatten_overlays(raw: list, force_color=None, max_items: int = 4000) -> list:
    """raw → 供 HTML 直接消费的叠加图元列表（点/线/面 + 颜色 + 名称）。"""
    out = []
    ci = 0
    for it in raw:
        props = it.get("props") or {}
        stroke = None
        for k in ("stroke", "color", "strokeColor", "stroke_color", "colour"):
            stroke = _parse_color(props.get(k))
            if stroke is not None:
                break
        fill = None
        for k in ("fill", "fillColor", "fill_color", "fillColour"):
            fill = _parse_color(props.get(k))
            if fill is not None:
                break
        base = force_color if force_color is not None else (
            stroke if stroke is not None else OV_COLORS[ci % len(OV_COLORS)])
        width = None
        for k in ("strokeWidth", "stroke_width", "weight", "lineWidth", "line_width"):
            width = _fnum(props.get(k))
            if width is not None:
                break
        fo = None
        for k in ("fillOpacity", "fill_opacity", "opacity"):
            fo = _fnum(props.get(k))
            if fo is not None:
                break
        name = it.get("name") or ""

        if it["kind"] == "point":
            r = None
            for k in ("radius", "r"):
                r = _fnum(props.get(k))
                if r is not None:
                    break
            for x, y in _iter_pts(it["coords"]):
                out.append({"kind": "point", "lon": x, "lat": y, "name": name,
                            "color": base, "width": width, "r": r})
            ci += 1
        elif it["kind"] == "line":
            pts = list(_iter_pts(it["coords"]))
            if len(pts) >= 2:
                out.append({"kind": "line", "path": [[a, b] for a, b in pts],
                            "name": name, "color": base, "width": width})
                ci += 1
        else:
            rings = it["coords"] or []
            if not rings:
                continue
            outer = list(_iter_pts(rings[0]))
            if len(outer) < 3:
                continue
            holes = []
            for r in rings[1:]:
                hp = list(_iter_pts(r))
                if len(hp) >= 3:
                    holes.append([[a, b] for a, b in hp])
            out.append({"kind": "polygon",
                        "outer": [[a, b] for a, b in outer],
                        "holes": holes,
                        "name": name, "color": base,
                        "fill": fill if fill is not None else base,
                        "fillOpacity": 0.22 if fo is None else max(0.0, min(1.0, fo)),
                        "width": width})
            ci += 1
        if len(out) >= max_items:
            log(f"    · 叠加图元已达上限 {max_items}，其余忽略")
            break
    return out


def fit_zoom(lon_w: float, lat_s: float, lon_e: float, lat_n: float,
             max_tiles: int, max_size: int = 1024) -> int:
    """选 DEM zoom：在「下采样后不浪费瓦片」的前提下尽量精细。

    原始长边超过 max_size 时会被降采样 2 倍，多下的瓦片等于白下，
    所以优先找 raw 长边 ≤ max_size 的最高 zoom；范围大到连最低 zoom 都
    超预算时，退回瓦片数不超预算的最高 zoom。"""
    fallback = MIN_ZOOM
    for z in range(MAX_DEM_ZOOM, MIN_ZOOM - 1, -1):
        nx = int(math.ceil(lon_to_tile_x(lon_e, z))) - int(math.floor(lon_to_tile_x(lon_w, z)))
        ny = int(math.ceil(lat_to_tile_y(lat_s, z))) - int(math.floor(lat_to_tile_y(lat_n, z)))
        nx, ny = max(1, nx), max(1, ny)
        if nx * ny > max_tiles:
            continue
        if max(nx, ny) * 256 <= max_size:
            return z
        fallback = z
    return fallback


def biggest_name(overlays: list) -> str:
    """取面积最大要素的名字，用作默认标题。"""
    best, best_area = "", -1.0
    for ov in overlays:
        if not ov.get("name"):
            continue
        pts = ov.get("outer") or ov.get("path") or []
        if len(pts) < 2:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        area = (max(xs) - min(xs)) * (max(ys) - min(ys))
        if area > best_area:
            best_area, best = area, ov["name"]
    return best


# ==================================================================
# 主流程
# ==================================================================
def _crop_box(fx0, fy0, fx1, fy1, tx0, ty0, h, w):
    """把瓦片拼图裁到精确范围（瓦片边界对齐时退化为恒等裁切）。"""
    x0 = int(round((fx0 - tx0) * 256.0))
    x1 = int(round((fx1 - tx0) * 256.0))
    y0 = int(round((fy0 - ty0) * 256.0))
    y1 = int(round((fy1 - ty0) * 256.0))
    x0 = max(0, min(w - 1, x0))
    x1 = max(x0 + 1, min(w, x1))
    y0 = max(0, min(h - 1, y0))
    y1 = max(y0 + 1, min(h, y1))
    return x0, y0, x1, y1


# 实测每纹理像素的编码体积（字节/px，2560² 真实地形纹理 = 6.55 Mpx）：
# JPEG q85 4:4:4 = 3.44 MB / 230 ms；WebP q85 m4 = 2.96 MB / 1337 ms；AVIF q70 = 2.20 MB / 618 ms。
# 即 AVIF 比 JPEG 小 36%、比 WebP 小 26%，且编码比 WebP 快 2.2×（只比 JPEG 慢 388 ms）。
_BYTES_PER_PX = {"jpeg": 0.525, "webp": 0.452, "avif": 0.336}
_TILE_PER_SEC = 30   # 冷跑吞吐估计（保守值）：v2.10 实测包河区 z16 2520 块仅 40.8s ≈ 62 tile/s
                     #（12 并发 keep-alive），旧值 6.6 会把耗时高估 9 倍；取一半作保守估计
_THREE_MB = 1.25      # 内嵌 Three.js + OrbitControls 的体积（实测 1.24 MB）

# v2.10 分块纹理（大范围自动 2×2）：单张纹理上限（standard 2560 / --clear 4096）会把
# 16 km 以上的范围压到 >4 m/px；分块后等效长边翻倍，HTML 端把各块合成一张大
# CanvasTexture，地形网格/叠加/导出等其余管线零改动。体积约 4×，故配体积预算自动降质量。
TILED_TILE_BUDGET = 3600   # 分块模式下影像瓦片预算（普通模式仍用 --max-sat-tiles）
TILED_MAX_MB = 24.0        # 分块纹理 base64 后的默认体积预算（--tiled-max-mb）
_TILED_Q_LADDER = {"jpeg": [80, 76, 72], "avif": [64, 58, 52], "webp": [80, 74]}


def _tiled_quality(args, mpx: float) -> tuple[int, float]:
    """分块纹理的体积预算：返回（采用的编码质量, 预计 base64 后 MB）。

    从当前质量起按阶梯降档，取第一个估出体积 ≤ --tiled-max-mb 的档；
    全超预算就用最低档并如实返回估计值（日志会提示）。"""
    base_q = max(1, args.jpeg_quality)
    bpp = _BYTES_PER_PX.get(args.sat_format, _BYTES_PER_PX["jpeg"])
    cand = [base_q] + [q for q in _TILED_Q_LADDER.get(args.sat_format, [80, 76, 72])
                       if q < base_q]
    est = 0.0
    for q in cand:
        est = mpx * bpp * (q / base_q) ** 1.7 * 4.0 / 3.0
        if est <= getattr(args, "tiled_max_mb", TILED_MAX_MB):
            return q, est
    return cand[-1], est


def _dry_run_report(args, zoom, dem_box, sat_info, width_m, height_m, tiling: int = 1) -> None:
    """`--dry-run`：把「要下多少、要多久、多大」先算出来，一块瓦片都不下。

    大范围（尤其 --clear）跑一次要好几分钟，先 dry-run 看一眼预算，比盲目开跑靠谱。
    """
    tx0, ty0, gx, gy = dem_box
    n_dem = gx * gy
    hit = 0
    total = n_dem
    if USE_CACHE:
        for j in range(gy):
            for i in range(gx):
                if _cache_path("dem", zoom, tx0 + i, ty0 + j).exists():
                    hit += 1
    log("")
    log("[dry-run] 只算计划，不下载任何瓦片")
    log(f"    范围      {width_m / 1000:.2f} × {height_m / 1000:.2f} km")
    log(f"    DEM       z{zoom}，{gx}×{gy} = {n_dem} 瓦片"
        + (f"（已缓存 {hit}）" if USE_CACHE else "（--no-cache）"))
    px = 0
    if sat_info:
        sz, sx0, sy0, nx, ny, sat_max = sat_info
        n_sat = nx * ny
        total += n_sat
        hits = 0
        if USE_CACHE:
            for j in range(ny):
                for i in range(nx):
                    if _cache_path("sat", sz, sx0 + i, sy0 + j).exists():
                        hits += 1
            hit += hits
        # 纹理长边 = sat_max，短边按长宽比缩放（两个方向都可能更长，故取 min(aspect, 1/aspect)）
        aspect = (height_m / width_m) if width_m > 0 else 1.0
        px = sat_max * sat_max * min(aspect, 1.0 / max(1e-9, aspect))
        log(f"    影像      z{sz}，{nx}×{ny} = {n_sat} 瓦片"
            + (f"（已缓存 {hits}）" if USE_CACHE else "（--no-cache）")
            + f" → 纹理 {sat_max}px" + (f"（{tiling}×{tiling} 分块，HTML 端合成）" if tiling > 1 else ""))
    miss = max(0, total - hit)
    secs = miss / _TILE_PER_SEC
    if not args.no_landmarks and (args.landmarks is None or args.landmarks > 0):
        secs += 16.0     # Overpass 冷查实测 ~16 s（v2.8 并发竞速后），命中缓存则 ~0
    secs += 6.0          # DEM/影像的 CPU 处理与编码（实测 5–8 s）
    if tiling > 1:       # 分块纹理：按体积预算选档后估体积（与正式生成同一套逻辑）
        _, est_mb = _tiled_quality(args, px / 1048576.0)
        mb_img = est_mb
    else:
        mb_img = px * _BYTES_PER_PX.get(args.sat_format, _BYTES_PER_PX["jpeg"]) / 1048576.0 * 4.0 / 3.0
    mb = mb_img + _THREE_MB + 0.15 + 0.06
    log(f"    网格      ≤ {args.max_size}×{args.max_size}（--max-size）")
    log(f"    预计下载  {total} 块，其中 {miss} 块需要联网"
        f" ≈ {secs:.0f} s（按 {_TILE_PER_SEC} tile/s 冷跑实测）")
    log(f"    预计产物  HTML ≈ {mb:.1f} MB（影像 {mb_img:.1f} MB base64 + Three.js {_THREE_MB:.1f} MB）")
    for f in ("webp", "avif"):
        if f != args.sat_format:
            alt = px * _BYTES_PER_PX[f] / 1048576.0 * 4.0 / 3.0 + _THREE_MB + 0.21
            log(f"              换 --sat-format {f} 约 {alt:.1f} MB")


def build(args) -> dict:
    t0 = time.time()
    # 分阶段计时（诊断用：慢在"下载"还是"CPU 处理"一眼可见，写进 meta.timings）
    _marks: list[tuple[str, float]] = [("start", time.perf_counter())]

    def mark(tag: str) -> None:
        _marks.append((tag, time.perf_counter()))

    # v3.0.0：分块判定提前到 zoom 选择之前——分块与否决定 DEM 网格密度（--max-size）。
    # 触发只看「范围长边 / 档位纹理上限」> 4 m/px（standard ≈ >10 km、--clear ≈ >16 km），
    # 与源影像计划解耦，比 v2.10 按源像素反推的判定更简单、更可预测。
    tiling = 1
    tiling_decided = False
    if args.max_size is None:
        args.max_size = 1024

    def decide_tiling(lo_w: float, lo_e: float, la_n: float, la_s: float) -> None:
        nonlocal tiling, tiling_decided
        long_pre = max(haversine(lo_w, la_n, lo_e, la_n),
                       haversine(lo_w, la_n, lo_w, la_s))
        if args.sat_tiling is not None:
            tiling = args.sat_tiling
        elif (not args.no_sat and args.sat_max_size is None
              and long_pre / max(1, args.tex_cap) > 4.0):
            tiling = 2
        if tiling > 1 and args.max_size <= 1024:
            # 分块模式下网格同步增密：DEM zoom 随 fit_zoom 抬 1 级，几何分辨率约 3×
            args.max_size = 2048
            log(f"    · 分块纹理 {tiling}×{tiling}：网格密度自动加倍"
                f"（--max-size 1024→2048；显式给 --max-size 可覆盖）")
        tiling_decided = True

    overlays: list = []
    ov_files = [str(p) for p in (getattr(args, "geojson", None) or [])]

    # ---- 0. GeoJSON（可选）：解析 + 包围盒 ----
    raw, geom_bbox = [], None
    if ov_files:
        log(f"[0/5] 读取 GeoJSON（{len(ov_files)} 个文件）…")
        raw = load_geojson_raw(ov_files, args.geojson_crs, args.geojson_name_key)
        if not raw:
            raise RuntimeError("GeoJSON 中没有可解析的几何要素"
                               "（支持 Point/MultiPoint、LineString/MultiLineString、"
                               "Polygon/MultiPolygon、GeometryCollection）。")
        geom_bbox = raw_bbox(raw)
        log(f"    · 要素 {len(raw)} 个（{count_pts(raw)} 点），包围盒 "
            f"({geom_bbox[0]:.5f}, {geom_bbox[1]:.5f}) → ({geom_bbox[2]:.5f}, {geom_bbox[3]:.5f})")

    # ---- 1. 定位 & 范围 ----
    if geom_bbox is not None:
        lon_w, lat_s, lon_e, lat_n = geom_bbox
        if args.lat is not None and args.lon is not None:
            log("    ! 已给 --geojson，--lat/--lon 被忽略（范围以 GeoJSON 包围盒为准）")
        pad = max(0.0, args.geojson_padding) / 100.0
        dx = (lon_e - lon_w) * pad or 2e-4
        dy = (lat_n - lat_s) * pad or 2e-4
        lon_w -= dx; lon_e += dx; lat_s -= dy; lat_n += dy
        lon_w = max(-180.0, lon_w); lon_e = min(180.0, lon_e)
        lat_s = max(-85.0, lat_s); lat_n = min(85.0, lat_n)
        lat, lon = (lat_n + lat_s) / 2.0, (lon_e + lon_w) / 2.0
        matched = args.place or Path(ov_files[0]).stem
        log(f"[1/5] 以 GeoJSON 包围盒定位（外扩 {args.geojson_padding:g}%），中心 ({lat:.5f}, {lon:.5f})")

        decide_tiling(lon_w, lon_e, lat_n, lat_s)   # v3.0.0：先定分块/网格密度，再选 zoom

        if args.zoom:
            zoom = max(MIN_ZOOM, min(MAX_DEM_ZOOM, args.zoom))
            if zoom != args.zoom:
                log(f"    · zoom 已夹紧到 {zoom}（DEM 最高支持 {MAX_DEM_ZOOM}）")
        else:
            zoom = fit_zoom(lon_w, lat_s, lon_e, lat_n, args.max_dem_tiles, args.max_size)
            log(f"    · 自动 zoom {zoom}（DEM 瓦片预算 {args.max_dem_tiles}，"
                f"网格长边 ≤ {args.max_size} px 不浪费下采样）")

        fx0, fy0 = lon_to_tile_x(lon_w, zoom), lat_to_tile_y(lat_n, zoom)
        fx1, fy1 = lon_to_tile_x(lon_e, zoom), lat_to_tile_y(lat_s, zoom)
        tx0, ty0 = int(math.floor(fx0)), int(math.floor(fy0))
        tx1, ty1 = int(math.ceil(fx1)), int(math.ceil(fy1))
        ty0, ty1 = max(0, ty0), min(1 << zoom, ty1)
        # 瓦片范围被夹到极区后，分数边界也要跟着夹，否则裁切框落在已下载的瓦片之外，
        # 顶部/底部会多出一整行 nan（NaN 又被 nanmin 当最低值填进去，形成假的"深沟"）
        fy0, fy1 = max(fy0, float(ty0)), min(fy1, float(ty1))
        grid_x, grid_y = max(1, tx1 - tx0), max(1, ty1 - ty0)
        if grid_x * grid_y > args.max_dem_tiles:
            log(f"    ! {grid_x}×{grid_y} = {grid_x * grid_y} 块 DEM 瓦片超过预算 "
                f"{args.max_dem_tiles}（因 --zoom 被固定），生成会变慢")
    else:
        if args.lat is not None and args.lon is not None:
            lat, lon, matched = float(args.lat), float(args.lon), args.place or "自定义坐标"
            log(f"[1/5] 使用指定坐标 ({lat:.6f}, {lon:.6f})")
        else:
            log(f"[1/5] 地理编码「{args.place}」…")
            lat, lon, matched = geocode(args.place, args.geocoder)

        zoom = max(MIN_ZOOM, min(MAX_DEM_ZOOM, args.zoom or 14))
        if args.zoom and zoom != args.zoom:
            log(f"    · zoom 已夹紧到 {zoom}（DEM 最高支持 {MAX_DEM_ZOOM}）")

        grid = max(1, args.grid or 3)
        tx_c = int(math.floor(lon_to_tile_x(lon, zoom)))
        ty_c = int(math.floor(lat_to_tile_y(lat, zoom)))
        half = grid // 2
        tx0, ty0 = tx_c - half, ty_c - half
        # 注意：tx1 = tx0 + grid，保证偶数 grid 也严格取 grid 个瓦片
        tx1, ty1 = tx0 + grid, ty0 + grid
        ty0 = max(0, ty0)
        ty1 = min((1 << zoom), ty1)
        grid_x, grid_y = tx1 - tx0, ty1 - ty0
        fx0, fy0, fx1, fy1 = float(tx0), float(ty0), float(tx1), float(ty1)

    lon_w = tile_x_to_lon(fx0, zoom)
    lon_e = tile_x_to_lon(fx1, zoom)
    lat_n = tile_y_to_lat(fy0, zoom)
    lat_s = tile_y_to_lat(fy1, zoom)
    if not tiling_decided:              # 地名/坐标模式：此时才有精确范围
        decide_tiling(lon_w, lon_e, lat_n, lat_s)
    width_m = haversine(lon_w, lat_n, lon_e, lat_n)
    height_m = haversine(lon_w, lat_n, lon_w, lat_s)
    merc = {"x0": (lon_w + 180.0) / 360.0, "x1": (lon_e + 180.0) / 360.0,
            "y0": merc_y(lat_n), "y1": merc_y(lat_s)}
    log(f"    · 范围 {width_m / 1000:.2f} × {height_m / 1000:.2f} km  "
        f"(zoom {zoom}, {grid_x}×{grid_y} 瓦片)")

    # ---- GeoJSON 抽稀（按 DEM 分辨率定容差，避免叠加图形比地形还密）----
    if raw:
        res_m = max(width_m, height_m) / max(1.0, min(args.max_size, max(grid_x, grid_y) * 256))
        tol = 0.0 if args.no_geojson_simplify else res_m * 0.35 / 111320.0
        raw = simplify_raw(raw, tol) if tol > 0 else raw
        npts = count_pts(raw)
        budget = args.geojson_max_points
        while npts > budget and tol < 1.0:
            tol = (tol if tol > 0 else res_m * 0.35 / 111320.0) * 3
            raw = simplify_raw(raw, tol)
            npts = count_pts(raw)
        overlays = flatten_overlays(raw, force_color=_parse_color(args.geojson_color))
        if not overlays:
            raise RuntimeError("GeoJSON 要素不完整（面至少需要 3 个点、线至少 2 个点），无法叠加。")
        if tol > 0:
            log(f"    · 图形抽稀容差 {tol * 111320:.1f} m，共 {len(overlays)} 个图元 / {npts} 点")
        else:
            log(f"    · 叠加图形 {len(overlays)} 个图元 / {npts} 点")
        if matched == Path(ov_files[0]).stem and not args.place:
            nm = biggest_name(overlays)
            if nm:
                matched = nm

    # ---- 2 & 3. DEM 与卫星影像并行下载（网络阶段并发，省首跑时间）----
    # 影像 zoom 预算（纯数学，只依赖范围，不依赖 DEM），先算好再并发下瓦片
    # 注意：gx0..gy1 是影像自己的分数瓦片坐标，不要覆盖 DEM 的 fx0..fy1（裁剪还要用）
    sat_zoom, sx0, sy0, nx, ny = zoom, 0, 0, 0, 0
    gx0, gy0, gx1, gy1 = 0.0, 0.0, 0.0, 0.0
    sat_budget_hit = False
    if not args.no_sat:
        def _sat_span(z: int) -> tuple[float, float, float, float, int]:
            a0, a1 = lon_to_tile_x(lon_w, z), lon_to_tile_x(lon_e, z)
            b0, b1 = lat_to_tile_y(lat_n, z), lat_to_tile_y(lat_s, z)
            n = (int(math.ceil(a1)) - int(math.floor(a0))) * (int(math.ceil(b1)) - int(math.floor(b0)))
            return a0, a1, b0, b1, n

        if args.sat_zoom is not None:      # 显式钉死：不做预算收敛，用户负责
            sat_zoom = max(MIN_ZOOM, min(MAX_SAT_ZOOM, args.sat_zoom))
            gx0, gx1, gy0, gy1, _ = _sat_span(sat_zoom)
            log(f"    · 影像 zoom 由 --sat-zoom 钉在 z{sat_zoom}")
        else:
            cap = min(MAX_SAT_ZOOM, args.sat_zoom_max)
            sat_zoom = min(cap, zoom + args.sat_extra)   # 先吃到"超采样下限"，再看瓦片预算
            while True:
                gx0, gx1, gy0, gy1, ntile = _sat_span(sat_zoom)
                if ntile <= args.max_sat_tiles or sat_zoom <= zoom:
                    break
                sat_zoom -= 1
                sat_budget_hit = True
            if sat_budget_hit:
                log(f"    · 影像 zoom 受瓦片预算(--max-sat-tiles {args.max_sat_tiles})压到 z{sat_zoom}；"
                    f"想要更细可调大 --max-sat-tiles，或用 --sat-zoom 钉死层级")
        sx0, sy0 = int(math.floor(gx0)), int(math.floor(gy0))
        nx = int(math.ceil(gx1)) - sx0
        ny = int(math.ceil(gy1)) - sy0

        # 纹理尺寸：默认由「源像素」反推，避免纹理比源影像更细（那是插值假细节）
        src_px = max(nx, ny) * 256.0
        if args.sat_max_size is None:
            tex_cap = args.tex_cap          # SAT_PRESETS: standard=2560 / clear=4096
            sat_max_size = int(round(min(tex_cap, max(2048.0, src_px)) / 8.0) * 8)
            tex_src = "自动"
        else:
            sat_max_size = args.sat_max_size
            tex_src = "指定"

        # ---- 分块影像计划（tiling 已在定位阶段判定，见 decide_tiling）----
        if tiling > 1:
            if args.sat_max_size is None:
                # 等效长边 = 档位上限 × N（--clear：2×2=8192 / 3×3=12288）
                sat_max_size = int(round(args.tex_cap * tiling / 8.0) * 8)
            tex_src = (f"自动，分块 {tiling}×{tiling}（等效长边）" if args.sat_max_size is None
                       else f"指定，分块 {tiling}×{tiling}")
            if args.sat_zoom is None:
                tiled_budget = max(args.max_sat_tiles, TILED_TILE_BUDGET * (tiling - 1))
                z_before = sat_zoom
                while sat_zoom < min(MAX_SAT_ZOOM, args.sat_zoom_max):
                    if _sat_span(sat_zoom + 1)[4] > tiled_budget:
                        break
                    sat_zoom += 1
                    s = _sat_span(sat_zoom)
                    span_px = max(int(math.ceil(s[1])) - int(math.floor(s[0])),
                                  int(math.ceil(s[3])) - int(math.floor(s[2]))) * 256
                    if span_px >= sat_max_size:
                        break
                if sat_zoom != z_before:
                    log(f"    · 分块纹理需要更细的源：影像 zoom 抬到 z{sat_zoom}"
                        f"（瓦片预算放宽到 {tiled_budget}，冷跑约 "
                        f"{tiled_budget / _TILE_PER_SEC:.0f}s 封顶，实际按未缓存块数计）")
            # 分块体积约为单张的 4 倍：用户没显式选格式时默认 AVIF（实测比 JPEG 小 36%）
            if not getattr(args, "_fmt_explicit", True) and args.sat_format == "jpeg":
                args.sat_format = "avif"
                if not getattr(args, "_q_explicit", True):
                    args.jpeg_quality = AVIF_QUALITY["clear" if args.clear else "standard"]
                log("    · 分块体积约为单张 4×：编码格式自动改用 AVIF（比 JPEG 小 36%；"
                    "--sat-format jpeg 可改回，老浏览器解码失败会自动回退高程设色）")
            # zoom 抬升后按新层级重算影像瓦片范围（sat_crop_px 在后面要用 gx0..gy1）
            gx0, gx1, gy0, gy1, _ = _sat_span(sat_zoom)
            sx0, sy0 = int(math.floor(gx0)), int(math.floor(gy0))
            nx = int(math.ceil(gx1)) - sx0
            ny = int(math.ceil(gy1)) - sy0
            src_px = max(nx, ny) * 256.0

        src_mpp = ground_res_m(lat, sat_zoom)
        # 纹理长边对应范围的**长边**（height_m 可能大于 width_m），分母也要取 max(sat_w, sat_h)；
        # v2.9 前这里写成 width_m / sat_w，纵向长范围时会低估 m/px，把"纹理还够用"误报成"已放大插值"
        tex_mpp = max(width_m, height_m) / max(1, sat_max_size)
        up = tex_mpp < src_mpp * 0.98
        log(f"    · 影像 zoom {sat_zoom}，{nx}×{ny} 瓦片；源 {src_mpp:.2f} m/px"
            f" → 纹理 {sat_max_size}px（{tex_src}，{tex_mpp:.2f} m/px）"
            + (f"（{tiling}×{tiling} 分块，HTML 端合成）" if tiling > 1 else "")
            + ("  [放大插值，细节已到顶]" if up else "  [源更细，纹理还有余量]"))
        if up and args.sat_zoom is None and sat_zoom < min(MAX_SAT_ZOOM, args.sat_zoom_max):
            log(f"    · 提示：纹理已细于源影像，提升主要靠加 --clear（提高纹理上限与瓦片预算）")

    # ---- dry-run：算完计划就停，一块瓦片都不下 ----
    if getattr(args, "dry_run", False):
        _dry_run_report(args, zoom, (tx0, ty0, grid_x, grid_y),
                        (sat_zoom, sx0, sy0, nx, ny, sat_max_size) if not args.no_sat else None,
                        width_m, height_m,
                        tiling=(tiling if not args.no_sat else 1))
        return None

    # 影像裁剪框（瓦片像素坐标）：拼接时直接裁，省一次全图 Image 拷贝
    sat_crop_px = None
    if not args.no_sat:
        bcx = (gx0 - sx0) * 256.0
        bcy = (gy0 - sy0) * 256.0
        bcw = (gx1 - gx0) * 256.0
        bch = (gy1 - gy0) * 256.0
        sat_crop_px = (max(0, int(round(bcx))), max(0, int(round(bcy))),
                       min(nx * 256, int(round(bcx + bcw))), min(ny * 256, int(round(bcy + bch))))

    def _fetch_dem():
        return mosaic_dem(zoom, tx0, ty0, grid_x, grid_y, args.workers)

    def _fetch_sat():
        return mosaic_sat(sat_zoom, sx0, sy0, nx, ny, args.workers, sat_crop_px)

    mark("定位准备")
    log(f"[2/5] 并行下载 DEM ({grid_x}×{grid_y}={grid_x * grid_y} 块) + 卫星影像…")
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_dem = ex.submit(_fetch_dem)
        f_sat = ex.submit(_fetch_sat) if not args.no_sat else None
        dem_raw = f_dem.result()
        sat_mos = f_sat.result() if f_sat is not None else None
    mark("下载")

    # 裁到精确范围（地名模式下瓦片边界对齐，退化为恒等裁切）
    cx0, cy0, cx1, cy1 = _crop_box(fx0, fy0, fx1, fy1, tx0, ty0,
                                   dem_raw.shape[0], dem_raw.shape[1])
    if (cx0, cy0, cx1, cy1) != (0, 0, dem_raw.shape[1], dem_raw.shape[0]):
        dem_raw = dem_raw[cy0:cy1, cx0:cx1]
        log(f"    · 高程裁切到目标范围 {cx1 - cx0}×{cy1 - cy0} px")

    if np.isnan(dem_raw).all():
        raise RuntimeError("该区域无高程数据（可能位于远海或数据缺失区），请更换地点或降低 zoom。")
    # 先清洗坏点再定 fill：坏点会把 nanmin 拉成异常值，导致整片无数据区被填成"深沟"
    nan_mask = np.isnan(dem_raw)
    tmp = np.where(nan_mask, 0.0, dem_raw).astype(np.float32)
    tmp, n_bad = despike(tmp, thresh_m=args.despike)
    if n_bad:
        log(f"    · 已修正 {n_bad} 个高程坏点（与邻域高差 > {args.despike:g} m 的数据源异常值；"
            f"不修会拉歪 min_elev、压扁色带并放大相对高差）")
    fill = float(tmp[~nan_mask].min()) if (~nan_mask).any() else 0.0
    dem = np.where(nan_mask, fill, tmp)
    dem = downsample(dem, args.max_size)

    # ---- 网格 LOD：顶点数超限时按最近邻抽稀（纹理仍保持 sat_max_size 清晰度）----
    H0, W0 = dem.shape
    if W0 * H0 > args.max_verts:
        f_ = int(math.ceil(math.sqrt(W0 * H0 / args.max_verts)))
        dem = decimate(dem, f_)
        log(f"    · 顶点超限 {W0 * H0 / 1e6:.1f}M>{args.max_verts / 1e6:.1f}M，网格按 {f_}× 抽稀为 {dem.shape[1]}×{dem.shape[0]}")

    H, W = dem.shape

    # 峰值：先在平滑图上定位，再取原始高程，避免噪点誤判
    sm = box_blur(dem, 2)
    pj, pi = np.unravel_index(int(np.argmax(sm)), sm.shape)
    peak_elev = float(dem[pj, pi])
    # min/max 只算一次（meta 与日志共用，避免整表扫两遍）
    dem_max = float(dem.max())
    # v3.0.0：高程下限按 0.1% 分位截断。成片水体伪影（实测巢湖 -16 m，占 0.02%）不是孤立坏点，
    # despike 的邻域判定修不到它，却会把 min_elev 拉歪、色带压扁、相对高差虚增四成；
    # 只截 min 侧——山峰是真实点状极值，max 侧截断会把主峰抹掉。
    dem_min = max(float(dem.min()), float(np.percentile(dem, 0.1)))
    if dem_min > float(dem.min()) + 0.5:
        log(f"    · 高程下限按 0.1% 分位截断：{float(dem.min()):.0f} → {dem_min:.0f} m"
            f"（成片低值伪影会被 min_elev 拉歪色带，despike 管不到）")
    log(f"    · 高程 {dem_min:.0f} – {dem_max:.0f} m，主峰 {peak_elev:.0f} m（网格 {W}×{H}）")

    dem_b64 = encode_terrarium(dem)
    mark("高程")

    # ---- 3. 卫星影像后处理（裁切/锐化/hillshade 烤入，依赖 dem 完成）----
    sat_b64, sat_w, sat_h, sat_mime = "", 0, 0, ""
    sat_tiles: list[dict] = []
    if sat_mos is not None:
        log("[3/5] 处理卫星影像（缩放/锐化/hillshade）…")
        crop = sat_mos  # 已在拼接阶段裁到精确范围
        # 纹理长边对齐 sat_max_size（与网格 W/H 解耦）
        long_edge = max(crop.width, crop.height)
        scale = sat_max_size / max(1, long_edge)
        sat_w = max(2, int(round(crop.width * scale)))
        sat_h = max(2, int(round(crop.height * scale)))
        crop = crop.resize((sat_w, sat_h), Image.LANCZOS)
        if scale > 1.02:
            log(f"    · 纹理被放大 {scale:.2f}× 插值（源影像像素不够铺满纹理，细节到此为止）")
        # 分块纹理体积约 4×：按 --tiled-max-mb 预算自动降质量（普通单张模式维持旧行为）
        if tiling > 1:
            q_use, est_mb = _tiled_quality(args, sat_w * sat_h / 1048576.0)
            if q_use != args.jpeg_quality:
                log(f"    · 分块纹理超体积预算（--tiled-max-mb {args.tiled_max_mb:g} MB）："
                    f"编码质量 q{args.jpeg_quality} → q{q_use}（预计影像 ~{est_mb:.0f} MB）")
                args.jpeg_quality = q_use
        # hillshade 整幅先算好（灰度小图再放大），分块时逐块裁切，保证跨块光照连续
        shade_img = None
        if not args.no_hillshade:
            shade = compute_hillshade(dem, altitude=args.hillshade_alt)
            shade_img = Image.fromarray(shade).resize((sat_w, sat_h), Image.BILINEAR)

        def _apply_shade(im: "Image.Image", box) -> "Image.Image":
            sh = np.asarray(shade_img.crop(box), dtype=np.float32)
            k = args.hillshade_strength
            factor = np.clip((1.0 - 0.5 * k) + k * sh, 0.45, 1.45)
            arr = np.asarray(im, dtype=np.float32) * factor[..., None]
            return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), mode="RGB")

        need_enh = (args.sharpen != 1.0 or args.sat_clarity > 0
                    or abs(args.sat_saturation - 1.0) > 1e-3)

        if tiling > 1:
            # v2.10 分块编码：USM/局部对比是局部算子，每块带 48px 余量增强后再裁到块范围，
            # 跨块无缝；逐块编码还天然规避了 Pillow 对超大图 4:4:4 编码的偶发失败（v2.8 踩过）。
            # v3.0.0：逐块增强+编码并行化（局部操作、共享输入只读，实测 4 块 AVIF 16.8s → ~6s）。
            tn = tiling
            xs = [min(sat_w, round(i * sat_w / tn)) for i in range(tn + 1)]
            ys = [min(sat_h, round(i * sat_h / tn)) for i in range(tn + 1)]
            log(f"    · 分块纹理 {tn}×{tn}，等效 {sat_w}×{sat_h}px"
                f"（贴图分辨率 {max(width_m, height_m) / max(1, sat_w, sat_h):.2f} m/px）")
            m = 48   # 增强余量：USM r2.6 + clarity 高斯 r8 的波及范围，48px 绰绰有余
            jobs = []
            for j in range(tn):
                for i in range(tn):
                    x0, x1, y0, y1 = xs[i], xs[i + 1], ys[j], ys[j + 1]
                    bx = (max(0, x0 - m), max(0, y0 - m), min(sat_w, x1 + m), min(sat_h, y1 + m))
                    jobs.append((x0, x1, y0, y1, bx))

            def _encode_tile(job):
                x0, x1, y0, y1, bx = job
                tile = crop.crop(bx)
                if need_enh:
                    tile = enhance_texture(tile, args.sharpen, args.sat_clarity,
                                           args.sat_saturation)
                if shade_img is not None:
                    tile = _apply_shade(tile, bx)
                tile = tile.crop((x0 - bx[0], y0 - bx[1], x1 - bx[0], y1 - bx[1]))
                return encode_sat(tile, args.sat_format, args.jpeg_quality, args.subsampling)

            with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as pool:
                encoded = list(pool.map(_encode_tile, jobs))
            for k, (b64, mime) in enumerate(encoded):
                sat_tiles.append({"b64": b64, "mime": mime})
                log(f"    · 块 {k + 1}/{len(encoded)}: "
                    f"{mime.split('/')[-1].upper()} {len(b64) * 3 / 4 / 1048576:.1f} MB")
            sat_mime = sat_tiles[0]["mime"]
        else:
            # 两段 USM + 局部对比 + 微提饱和（v2.6：替换旧版偏弱的 ImageEnhance.Sharpness）
            if need_enh:
                crop = enhance_texture(crop, args.sharpen, args.sat_clarity, args.sat_saturation)
                log(f"    · 贴图增强：锐化 {args.sharpen:.2f} / 局部对比 {args.sat_clarity:.2f}"
                    f" / 饱和 {args.sat_saturation:.2f}")
            # 叠微 hillshade：用 DEM 重采样到纹理尺寸，把起伏"烤"进贴图增强立体感
            if shade_img is not None:
                crop = _apply_shade(crop, (0, 0, sat_w, sat_h))
                log(f"    · 已叠 hillshade（强度 {args.hillshade_strength:.2f}，"
                    f"光照角 {args.hillshade_alt:.0f}°）")
            sat_b64, sat_mime = encode_sat(crop, args.sat_format, args.jpeg_quality, args.subsampling)
            log(f"    · 影像编码 {sat_mime.split('/')[-1].upper()}"
                f"（q{args.jpeg_quality}/{args.subsampling}），纹理 {sat_w}×{sat_h}")
    else:
        log("[3/5] 跳过卫星影像（--no-sat）")
    mark("影像")

    # ---- 4. 地标 ----
    # 传 GeoJSON 时默认不拉 OSM 地标（用户的图形本身就是主角），显式 --landmarks N 可开
    lm_limit = args.landmarks if args.landmarks is not None else (0 if overlays else 16)
    landmarks = []
    if args.no_landmarks or lm_limit <= 0:
        if args.no_landmarks:
            log("[4/5] 跳过地标（--no-landmarks）")
        else:
            log("[4/5] 跳过地标（GeoJSON 模式下默认关闭；需要时加 --landmarks 8）")
    else:
        log("[4/5] 获取地标 POI…")
        landmarks = fetch_landmarks(lon_w, lat_s, lon_e, lat_n, limit=lm_limit)
        log(f"    · 地标 {len(landmarks)} 个")

    # 「主峰」判定（v3.1.0 泛化）：仅当目标真是山峰时才叫主峰，平原/城区不强行套用。
    # 真实山峰 = natural=peak/volcano 地标，且 (有实测海拔 或 名称含山名关键字)，
    # 且其位置靠近 DEM 最高格——以此过滤 OSM 上把观景台/风貌区误标 natural=peak 的节点
    # （实测 包河区 有「草坡风貌区」误标，旧逻辑直接把它当主峰显示「▲ 主峰 57m」）。
    peak_name = ""
    peak_lon = peak_lat = None
    peaks = [l for l in landmarks if l.get("desc") in ("山峰", "火山")]
    if peaks:
        plon_dem = (merc["x0"] + pi / max(1, W - 1) * (merc["x1"] - merc["x0"])) * 360.0 - 180.0
        plat_dem = merc_y_to_lat(merc["y0"] + pj / max(1, H - 1) * (merc["y1"] - merc["y0"]))
        thr = max(1500.0, width_m * 0.05)
        cand = []
        for l in peaks:
            has_ele = l.get("ele") is not None
            name_kw = any(k in (l["name"] or "") for k in _MOUNTAIN_KW)
            near = haversine(l["lon"], l["lat"], plon_dem, plat_dem) <= thr
            if (has_ele or name_kw) and near:
                cand.append(l)
        if cand:
            peak_name = max(cand, key=lambda l: l.get("ele") or -9999)["name"]
            for l in cand:
                if l["name"] == peak_name:
                    peak_lon, peak_lat = l["lon"], l["lat"]
                    break

    # 去重：仅当存在主峰时，删掉「与主峰同名且贴近主峰位置」的地标
    # （DEM 最高点标记已代表它，避免峰顶叠两个标签）。
    # 旧逻辑 `not peak_name` 会在「非山区无主峰」时把最高格附近所有地标全删掉——已修复。
    if landmarks and peak_name:
        keep = []
        for l in landmarks:
            d = haversine(l["lon"], l["lat"], peak_lon, peak_lat)
            if d < max(400.0, width_m * 0.02) and l["name"] == peak_name:
                continue
            keep.append(l)
        if len(keep) != len(landmarks):
            log(f"    · 去除 {len(landmarks) - len(keep)} 个与主峰重复的地标")
        landmarks = keep
    mark("地标")

    # ---- 5. 组装 ----
    meta = {
        "place": args.place or matched,
        "matched_name": matched,
        "center_lat": lat,
        "center_lon": lon,
        "zoom": zoom,
        "sat_zoom": sat_zoom,
        "grid": max(grid_x, grid_y),
        "grid_x": grid_x,
        "grid_y": grid_y,
        "size_w": int(W),
        "size_h": int(H),
        "size": int(W),
        "width_m": width_m,
        "height_m": height_m,
        "min_elev": dem_min,
        "max_elev": dem_max,
        "peak_elev": peak_elev,
        "peak_name": peak_name,
        "peak_u": float(pi) / max(1, W - 1),   # u -> 东西 (x)
        "peak_v": float(pj) / max(1, H - 1),   # v -> 南北 (z)
        "bounds": {"lon_w": lon_w, "lat_n": lat_n, "lon_e": lon_e, "lat_s": lat_s},
        "merc": merc,  # 像素行号与墨卡托 Y 线性相关，插值必须在墨卡托空间做
        "has_sat": bool(sat_b64 or sat_tiles),
        "resolution_m": max(width_m, height_m) / max(1, W),
        "sat_resolution_m": (max(width_m, height_m) / max(1, sat_w, sat_h) if sat_w else None),
        "sat_native_m": (ground_res_m(lat, sat_zoom) if sat_b64 else None),
        "sat_upscaled": bool((sat_b64 or sat_tiles) and sat_w and
                             (max(width_m, height_m) / max(1, sat_w, sat_h))
                             < ground_res_m(lat, sat_zoom) * 0.98),
        "clear_mode": bool(args.clear),
        "size_sat_w": sat_w,
        "size_sat_h": sat_h,
        "sat_tiling": ([tiling, tiling] if sat_tiles else None),
        "has_overlays": bool(overlays),
        "overlay_count": len(overlays),
        "geojson_files": [Path(p).name for p in ov_files],
        "sources": {
            "dem": "AWS Terrarium (SRTM/GDEM)",
            "sat": "Esri World Imagery" if (sat_b64 or sat_tiles) else "",
            "poi": "OpenStreetMap / Overpass" if landmarks else "",
        },
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    mark("end")
    timings = {}
    for i in range(1, len(_marks)):
        timings[_marks[i][0]] = round(_marks[i][1] - _marks[i - 1][1], 2)
    meta["timings"] = timings
    total = time.time() - t0
    brk = "  ".join(f"{k} {v:.1f}s" for k, v in timings.items())
    log(f"[5/5] 完成，总用时 {total:.1f}s（{brk}）")
    return {"meta": meta, "dem_b64": dem_b64, "sat_b64": sat_b64,
            "sat_mime": sat_mime or "image/jpeg", "sat_tiles": sat_tiles,
            "landmarks": landmarks, "overlays": overlays}


def main():
    p = argparse.ArgumentParser(description="三维真实地形数据准备（v3.1.0）",
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--place", help="地名（中文/英文均可）；传 --geojson 时用作标题，可省")
    p.add_argument("--lat", type=float, help="直接指定中心纬度 WGS84（与 --lon 同时使用）")
    p.add_argument("--lon", type=float, help="直接指定中心经度 WGS84")
    p.add_argument("--geojson", nargs="+", default=None,
                   help="GeoJSON 文件（可给多个）。自动按包围盒定位+自动选 zoom，并把点/线/面叠加到地形上")
    p.add_argument("--geojson-crs", default="wgs84", choices=["wgs84", "gcj02"],
                   help="GeoJSON 坐标系：wgs84=默认(GPS/OSM/GeoJSON 标准)；gcj02=高德/腾讯导出（会转 WGS84）")
    p.add_argument("--geojson-padding", type=float, default=6.0,
                   help="包围盒外扩百分比（让图形四周留点地形余量）")
    p.add_argument("--geojson-color", default=None,
                   help="强制所有叠加图形使用该颜色（如 #ff5252），默认读 properties.color 或自动配色")
    p.add_argument("--geojson-name-key", default=None,
                   help="从该属性键取图形名称（默认自动尝试 name/NAME/名称/title/id）")
    p.add_argument("--geojson-max-points", type=int, default=200000,
                   help="叠加图形坐标点总数上限（超出自动加大抽稀容差控体积）")
    p.add_argument("--no-geojson-simplify", action="store_true", help="不做几何抽稀（原样渲染，文件会更大）")
    p.add_argument("--max-dem-tiles", type=int, default=64,
                   help="GeoJSON 模式下 DEM 瓦片数预算（自动选 zoom 用；越大越精细也越慢）")
    p.add_argument("--zoom", type=int, default=None, help="DEM 瓦片层级 (3-15)；默认 地名=14 / GeoJSON=自动")
    p.add_argument("--grid", type=int, default=None, help="瓦片网格数 N（覆盖 N×N 个瓦片）默认 3；GeoJSON 模式下由包围盒决定")
    p.add_argument("--max-size", type=int, default=None,
                   help="DEM/网格最大边长（像素，控制网格顶点数）；默认 1024，"
                        "分块纹理模式下自动加倍到 2048（DEM zoom 抬 1 级、几何分辨率约 3×），显式给值则按给定值")
    p.add_argument("--max-verts", type=int, default=3000000, help="网格顶点数硬上限（超出则按最近邻抽稀网格，纹理仍保持 sat-max-size 清晰度；防止顶点爆炸）")
    p.add_argument("--sat-max-size", type=int, default=None,
                   help="卫星影像纹理最大边长（像素，独立于网格，决定贴图清晰度）；默认自动："
                        "min(档位上限, max(2048, 源像素))，即按源影像像素反推、小范围保底 2048。"
                        "档位上限 standard=2560 / --clear=4096。显式给值则完全按给定值")
    p.add_argument("--sat-extra", type=int, default=None,
                   help="影像超采样下限（DEM zoom + N，决定源影像层级）；默认 standard=3 / --clear=3"
                        "（即地名模式源 z17）；想要 z18 原生层显式给 4（实测仅多 1.8%% 细节，却要 4 倍瓦片）")
    p.add_argument("--max-sat-tiles", type=int, default=None,
                   help="影像瓦片数上限（超出则自动降 zoom 控体积）；默认 standard=1200 / --clear=1200")
    p.add_argument("--sat-zoom", type=int, default=None,
                   help="钉死影像 zoom（不做瓦片预算收敛）；不填则自动。自动封顶 z18（实测 z19 无新细节）")
    p.add_argument("--sat-zoom-max", type=int, default=MAX_SAT_ZOOM_NATIVE,
                   help="自动选层时的影像 zoom 上限")
    p.add_argument("--sat-tiling", type=int, default=None, choices=[1, 2, 3],
                   help="影像纹理分块 N（N×N 块，HTML 端合成一张等效大纹理）。"
                        "默认 auto：范围长边/档位纹理上限 > 4 m/px（standard 约 >10 km、"
                        "--clear 约 >16 km）自动开 2×2，等效长边 = 档位上限×N（--clear 时 "
                        "2×2=8192 / 3×3=12288）；给 1 强制单张。显式 3 供市域级范围用，"
                        "等效 12288px 需 GPU maxTextureSize ≥ 12288（桌面独显一般 16384）")
    p.add_argument("--tiled-max-mb", type=float, default=TILED_MAX_MB,
                   help=f"分块纹理的影像体积预算 MB（base64 后，默认 {TILED_MAX_MB:.0f}）；"
                        "超预算自动降编码质量（jpeg 最低 q72 / avif 最低 q52）")
    p.add_argument("--clear", action="store_true",
                   help="最高清晰度模式：纹理上限拉到 4096（1.52 m/px）+ 更强锐化/局部对比 + q86，"
                        "文件约 14 MB、首跑约 3 分钟。源层级仍取 z17——实测此时纹理才是瓶颈，"
                        "源 z18 只多 1.8%% 细节却要 4 倍瓦片，要那 2%% 请显式再加 --sat-extra 4")
    p.add_argument("--sat-clarity", type=float, default=None,
                   help="贴图局部对比「去灰雾」强度 0-1（0=关）；默认 standard=0.35 / --clear=0.5")
    p.add_argument("--sat-saturation", type=float, default=None,
                   help="贴图饱和度（1.0=不改，0=完全去色）；默认 1.06，轻微提饱和可抵消压缩发灰。"
                        "显式给 0 也会生效（不会被引擎当空值覆盖）")
    p.add_argument("--sat-format", default=None, choices=["avif", "webp", "jpeg"],
                   help="影像编码：jpeg=默认(兼容最好、编码最快)；avif=实测(2560² 真实纹理)比 jpeg 小 36%%、"
                        "比 webp 小 26%%，且编码比 webp 快 2.2×（需 Chrome85+/Safari16+ 才能解码，"
                        "老浏览器解码失败会自动回退高程设色）；webp=兼容性居中但编码最慢(1.3s)")
    p.add_argument("--jpeg-quality", type=int, default=None,
                   help="影像质量（webp/jpeg 通用，70-95）；默认 standard=85 / --clear=86")
    p.add_argument("--subsampling", default="444", choices=["444", "420"],
                   help="JPEG 色度抽样：444=默认(色度不降采样，彩色地物边缘更锐、体积略大)；420=更小但彩色边缘发糊")
    p.add_argument("--sharpen", type=float, default=None,
                   help="贴图锐化强度（1.0=不锐化，>1 更锐，两段 USM 实现）；默认 standard=1.35 / --clear=1.8")
    p.add_argument("--despike", type=float, default=100.0,
                   help="高程坏点清洗阈值（米）：与 3×3 邻域均值相差超过该值的孤立像素判为数据源异常值，"
                        "用邻域均值顶上（实测每幅图常见 1–10 个 -700 m 级坏点，会拉歪色带与相对高差）；"
                        "0=关闭。坏点占比超过 1%% 时自动放弃清洗（那大概率是真实陡崖/海岸线）")
    p.add_argument("--no-hillshade", action="store_true", help="不叠加 hillshade 立体光照")
    p.add_argument("--hillshade-strength", type=float, default=0.5, help="hillshade 强度（0-1，越大越立体）")
    p.add_argument("--hillshade-alt", type=float, default=45.0, help="hillshade 光源高度角（度）")
    p.add_argument("--landmarks", type=int, default=None,
                   help="最多地标数；默认 地名模式=12 / GeoJSON 模式=0（不拉 OSM 地标）。给 0 即关闭")
    p.add_argument("--no-sat", action="store_true", help="不下载卫星影像")
    p.add_argument("--no-landmarks", action="store_true", help="不获取地标")
    p.add_argument("--geocoder", default="auto", help="地理编码源: auto|amap|nominatim|photon")
    p.add_argument("--workers", type=int, default=12, help="并发下载线程数（实测 16 吞吐到顶 8 tile/s，超过 24 被 Esri 限流反降）")
    p.add_argument("--no-cache", action="store_true",
                   help="禁用全部磁盘缓存：瓦片 / 地标 POI / 地理编码（默认都开，目录 ~/.cache/geo-3d-terrain）")
    p.add_argument("--cache-prune", type=float, default=None, metavar="DAYS",
                   help="只清缓存：删掉 DAYS 天前更新的缓存文件后退出（0=全清）。"
                        "缓存可再生，删了只是下次重下；three/ 工具链源码不在清理范围内")
    p.add_argument("--cache-max-mb", type=float, default=CACHE_MAX_MB,
                   help=f"缓存总盘上限 MB（默认 {CACHE_MAX_MB:.0f}），超出部分按最旧优先删除")
    p.add_argument("--dry-run", action="store_true",
                   help="只算计划不下瓦片：打印层级/瓦片数/预计耗时/预计产物体积后退出")
    p.add_argument("--out", default="terrain.json", help="输出 JSON 路径")
    p.add_argument("--out-html", default=None, help="同时生成 HTML（一步到位）")
    p.add_argument("--exaggeration", type=float, default=None, help="初始垂直夸张系数（默认自动）")
    p.add_argument("--cdn", default="auto", help="Three.js CDN: auto|jsdelivr|unpkg|esm.sh（仅 --three cdn 时生效）")
    p.add_argument("--three", default="auto", choices=["auto", "embed", "cdn"],
                   help="Three.js 引入方式：auto=优先内嵌(离线可看), embed=强制内嵌, cdn=外链")
    args = p.parse_args()

    # 缓存清理是纯维护操作，必须在「必须有地名」校验之前处理，
    # 否则 `--cache-prune 7` 这种不带地名的用法会被误判为参数缺失（v2.9 踩过）。
    if args.cache_prune is not None:
        n0, s0 = cache_usage()
        days = None if args.cache_prune <= 0 else args.cache_prune
        removed, freed = prune_cache(max_age_days=days,
                                     max_mb=None if days is not None else args.cache_max_mb)
        n1, s1 = cache_usage()
        log(f"[✓] 缓存清理：删除 {removed} 个文件、释放 {freed / 1048576:.1f} MB"
            f"（{n0} 个 {s0 / 1048576:.1f} MB → {n1} 个 {s1 / 1048576:.1f} MB）")
        return 0

    if not args.geojson and not args.place and (args.lat is None or args.lon is None):
        p.error("需提供 --place、--geojson，或同时提供 --lat 与 --lon")

    # 清晰度档位落位：None 的参数才吃预设，显式指定的一律以用户为准
    preset = SAT_PRESETS["clear" if args.clear else "standard"]
    q_explicit = args.jpeg_quality is not None   # 用户显式给过质量就别被档位覆盖
    for k, v in preset.items():
        if getattr(args, k, None) is None:
            setattr(args, k, v)
    # 显式标记：--sat-format / --jpeg-quality 是否由用户给过（分块纹理的自动决策只动「没给过」的）
    args._fmt_explicit = args.sat_format is not None
    args._q_explicit = q_explicit
    if args.sat_format is None:
        args.sat_format = "jpeg"
    # AVIF 的质量刻度与 JPEG 不同，没显式指定时按档位给 AVIF 专用值
    if args.sat_format == "avif" and not q_explicit:
        args.jpeg_quality = AVIF_QUALITY["clear" if args.clear else "standard"]
    if args.sat_saturation is None:      # 不能写 `or 1.0`——用户显式给 0（去色）会被悄悄改回 1.0
        args.sat_saturation = 1.0

    global USE_CACHE
    USE_CACHE = not args.no_cache

    data = build(args)
    if data is None:          # --dry-run：只打了计划，没有产物
        return 0
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    m = data["meta"]
    kb = os.path.getsize(args.out) / 1024
    log("")
    log(f"[✓] {args.out}  ({kb:.0f} KB)")
    log(f"    地名      {m['place']}" + (f"  (匹配: {m['matched_name']})" if m["matched_name"] != m["place"] else ""))
    log(f"    中心      {m['center_lat']:.5f}, {m['center_lon']:.5f}")
    log(f"    覆盖      {m['width_m']:.0f} × {m['height_m']:.0f} m")
    log(f"    高程      {m['min_elev']:.0f} – {m['max_elev']:.0f} m，主峰 {m['peak_elev']:.0f} m")
    log(f"    网格      {m['size_w']}×{m['size_h']}，高程分辨率 {m['resolution_m']:.1f} m/px")
    if m.get('sat_resolution_m'):
        tn = (m.get("sat_tiling") or [1, 1])[0]
        log(f"    影像纹理  {m['size_sat_w']}×{m['size_sat_h']}"
            + (f"（{tn}×{tn} 分块合成，等效长边 {max(m['size_sat_w'], m['size_sat_h'])}px）" if tn > 1 else "")
            + f"，贴图分辨率 {m['sat_resolution_m']:.2f} m/px"
            + (f"，源影像 z{m['sat_zoom']}（{m['sat_native_m']:.2f} m/px）" if m.get("sat_native_m") else ""))
        if m.get("sat_upscaled"):
            log("              ⚠ 纹理已细于源影像（放大插值）；想真提升用 --clear 或 --max-sat-tiles")
    log(f"    地标      {len(data['landmarks'])} 个")
    if m.get("has_overlays"):
        kinds = {}
        for ov in data["overlays"]:
            kinds[ov["kind"]] = kinds.get(ov["kind"], 0) + 1
        log(f"    叠加图形  {m['overlay_count']} 个（" +
            "，".join(f"{ {'polygon': '面', 'line': '线', 'point': '点'}.get(k, k)}{v}" for k, v in kinds.items()) +
            f"）来自 {', '.join(m['geojson_files'])}")

    if args.out_html:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import generate_html
        generate_html.render(data, args.out_html,
                             exaggeration=args.exaggeration, cdn=args.cdn,
                             three=args.three)
    # 收尾：缓存超过上限就按最旧优先清一清（缓存可再生，删了只是下次重下）
    if USE_CACHE and args.cache_max_mb > 0:
        removed, freed = prune_cache(max_mb=args.cache_max_mb)
        if removed:
            log(f"    · 缓存超 {args.cache_max_mb:.0f} MB 上限，已清理 {removed} 个最旧文件"
                f"（释放 {freed / 1048576:.1f} MB）")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("\n[!] 已中断")
        sys.exit(130)
    except Exception as e:
        log(f"\n[✗] 失败: {e}")
        sys.exit(1)
