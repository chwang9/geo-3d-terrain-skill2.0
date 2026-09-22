#!/usr/bin/env python3
"""
prepare_terrain_data.py  (v2.4)
===============================
三维真实地形数据准备脚本：输入地名（或直接给经纬度），自动
  1. 地理编码（多源兜底 + 结果打分）
  2. 下载 AWS Terrarium DEM 高程瓦片并拼接
  3. 下载 Esri World Imagery 卫星影像（按目标纹理分辨率自动选 zoom，裁切对齐；可叠微 hillshade / 锐化，默认 JPEG 编码、可选 WebP）
  4. 从 Overpass(OSM) 获取地标 POI
输出一个 terrain.json，供 generate_html.py 渲染成自包含 HTML。

依赖:
    pip install requests numpy Pillow

用法:
    # 两步
    python prepare_terrain_data.py --place "大蜀山" --zoom 14 --grid 3 --out terrain.json
    python generate_html.py --data terrain.json --out dashu.html

    # 一步到位
    python prepare_terrain_data.py --place "大蜀山" --out-html dashu.html

    # 地理编码失败时，直接给坐标（WGS84）
    python prepare_terrain_data.py --lat 31.8447 --lon 117.1661 --zoom 14 --out-html dashu.html

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
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageEnhance
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    Retry = None

UA = "geo-3d-terrain/2.0 (terrain visualization skill)"
MIN_ZOOM, MAX_DEM_ZOOM, MAX_SAT_ZOOM = 3, 15, 19
CACHE_DIR = Path(os.environ.get("GEO3D_CACHE", Path.home() / ".cache" / "geo-3d-terrain" / "tiles"))
USE_CACHE = True
NODATA_TTL = 24 * 3600  # 无数据(404)缓存有效期(秒)：过期后重新请求，避免瞬时限流 404 被记成永久无数据


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


def _cache_path(kind: str, z: int, x: int, y: int) -> Path:
    return CACHE_DIR / kind / str(z) / str(x) / f"{y}.bin"


def _cache_age(cp: Path) -> float:
    """缓存文件已存活秒数（用于无数据 TTL 判断）。"""
    try:
        return time.time() - cp.stat().st_mtime
    except Exception:
        return 1e9


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
    """返回 (lat, lon, 命中的名称)。多源并发，按打分取最优。"""
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
                cands += fu.result() or []
                if verbose:
                    log(f"    · 地理编码 {f.__name__}: 命中 {len(fu.result() or [])} 条")
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


def mosaic_dem(z: int, tx0: int, ty0: int, n: int, workers: int) -> np.ndarray:
    tiles: dict[tuple[int, int], np.ndarray] = {}
    jobs = [(tx0 + i, ty0 + j) for j in range(n) for i in range(n)]
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
    rows = [np.hstack([tiles[(tx0 + i, ty0 + j)] for i in range(n)]) for j in range(n)]
    return np.vstack(rows)


def mosaic_sat(z: int, tx0: int, ty0: int, nx: int, ny: int, workers: int) -> Image.Image:
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
    rows = [np.hstack([np.asarray(tiles[(tx0 + i, ty0 + j)]) for i in range(nx)]) for j in range(ny)]
    return Image.fromarray(np.vstack(rows), mode="RGB")


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
    """高程 -> Terrarium PNG -> base64（无 data: 前缀）。"""
    v = np.clip(np.round(dem) + 32768.0, 0.0, 65535.0)
    r = np.floor(v / 256.0).astype(np.uint8)
    g = np.floor(v % 256.0).astype(np.uint8)
    b = np.round((v - np.floor(v)) * 255.0).astype(np.uint8)
    img = Image.fromarray(np.stack([r, g, b], axis=-1), mode="RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True, compress_level=9)
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


def encode_sat(crop: "Image.Image", fmt: str, quality: int) -> tuple[str, str]:
    """编码卫星纹理：优先 WebP（更小），失败回退 JPEG。返回 (b64, mime)。"""
    buf = io.BytesIO()
    mime = "image/jpeg"
    try:
        if fmt == "webp":
            crop.save(buf, format="WEBP", quality=quality, method=4)
            mime = "image/webp"
        else:
            crop.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
    except Exception:
        buf = io.BytesIO()
        crop.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
        mime = "image/jpeg"
    return base64.b64encode(buf.getvalue()).decode(), mime


# ==================================================================
# 地标（Overpass）
# ==================================================================
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]
LM_PRIORITY = [
    ("natural", "peak"), ("natural", "volcano"), ("historic", None),
    ("tourism", None), ("amenity", "place_of_worship"), ("leisure", None),
]
# 值得在地形图上标注的类型；酒店/餐饮/商铺等一律过滤，否则地标全是噪点
KEEP_TOURISM = {"attraction", "viewpoint", "museum", "zoo", "theme_park",
                "artwork", "gallery", "picnic_site", "wilderness_hut", "alpine_hut"}
KEEP_LEISURE = {"park", "nature_reserve", "garden", "golf_course", "stadium"}
KEEP_HISTORIC = {"monument", "memorial", "castle", "ruins", "tomb", "fort",
                 "archaeological_site", "shrine", "temple", "wayside_cross"}


def _keep(tags: dict) -> bool:
    if tags.get("natural") in ("peak", "volcano"):
        return True
    if tags.get("amenity") == "place_of_worship":
        return True
    if "tourism" in tags:
        return tags["tourism"] in KEEP_TOURISM
    if "historic" in tags:
        v = tags["historic"]
        return v in KEEP_HISTORIC or v != "yes" and v not in ("building", "house")
    if "leisure" in tags:
        return tags["leisure"] in KEEP_LEISURE
    return False


def _lm_rank(tags: dict) -> int:
    for i, (k, v) in enumerate(LM_PRIORITY):
        if k in tags and (v is None or tags.get(k) == v):
            return i
    return len(LM_PRIORITY)


def fetch_landmarks(lon_w: float, lat_s: float, lon_e: float, lat_n: float,
                    limit: int = 12, min_sep_ratio: float = 0.05) -> list:
    if lon_e - lon_w <= 0 or lat_n - lat_s <= 0:
        return []
    q = f"""[out:json][timeout:60];
(
  nwr["natural"="peak"]({lat_s},{lon_w},{lat_n},{lon_e});
  nwr["natural"="volcano"]({lat_s},{lon_w},{lat_n},{lon_e});
  nwr["tourism"]({lat_s},{lon_w},{lat_n},{lon_e});
  nwr["historic"]({lat_s},{lon_w},{lat_n},{lon_e});
  nwr["amenity"="place_of_worship"]({lat_s},{lon_w},{lat_n},{lon_e});
  nwr["leisure"="park"]({lat_s},{lon_w},{lat_n},{lon_e});
);
out center tags;"""
    palette = [0xffcf4d, 0xff9f43, 0xff5252, 0x4ecdc4, 0x45b7d1,
               0x96ceb4, 0xffeaa7, 0xdda0dd, 0x74b9ff, 0xa29bfe,
               0x55efc4, 0xfd79a8]
    data = None
    for ep in OVERPASS_ENDPOINTS:
        try:
            r = get_session().post(ep, data={"data": q}, timeout=70)
            if r.status_code == 200:
                data = r.json()
                log(f"    · 地标来源 {ep.split('/')[2]}")
                break
            log(f"    · Overpass {ep.split('/')[2]} 返回 {r.status_code}")
        except Exception as e:
            log(f"    · Overpass {ep.split('/')[2]} 失败: {type(e).__name__}")
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
        cat = (tags.get("natural") or tags.get("historic") or tags.get("tourism")
               or tags.get("amenity") or tags.get("leisure") or "poi")
        items.append({
            "name": name.strip(), "lat": float(lat), "lon": float(lon),
            "desc": cat, "rank": _lm_rank(tags),
            "ele": float(tags["ele"]) if "ele" in tags and _is_num(tags["ele"]) else None,
        })

    items.sort(key=lambda d: (d["rank"], -(d["ele"] or -9999)))
    span = max(lon_e - lon_w, lat_n - lat_s) * min_sep_ratio
    kept = []
    for it in items:
        if all(abs(it["lon"] - k["lon"]) > span or abs(it["lat"] - k["lat"]) > span for k in kept):
            kept.append(it)
        if len(kept) >= limit:
            break
    for i, it in enumerate(kept):
        it["color"] = palette[i % len(palette)]
        it.pop("rank", None)
    return kept


def _is_num(s) -> bool:
    try:
        float(str(s).replace("m", "").strip())
        return True
    except Exception:
        return False


# ==================================================================
# 主流程
# ==================================================================
def build(args) -> dict:
    t0 = time.time()

    # ---- 1. 定位 ----
    if args.lat is not None and args.lon is not None:
        lat, lon, matched = float(args.lat), float(args.lon), args.place or "自定义坐标"
        log(f"[1/5] 使用指定坐标 ({lat:.6f}, {lon:.6f})")
    else:
        log(f"[1/5] 地理编码「{args.place}」…")
        lat, lon, matched = geocode(args.place, args.geocoder)

    zoom = max(MIN_ZOOM, min(MAX_DEM_ZOOM, args.zoom))
    if zoom != args.zoom:
        log(f"    · zoom 已夹紧到 {zoom}（DEM 最高支持 {MAX_DEM_ZOOM}）")

    grid = max(1, args.grid)
    tx_c = int(math.floor(lon_to_tile_x(lon, zoom)))
    ty_c = int(math.floor(lat_to_tile_y(lat, zoom)))
    half = grid // 2
    tx0, ty0 = tx_c - half, ty_c - half
    # 注意：tx1 = tx0 + grid，保证偶数 grid 也严格取 grid 个瓦片
    tx1, ty1 = tx0 + grid, ty0 + grid
    ty0 = max(0, ty0)
    ty1 = min((1 << zoom), ty1)

    lon_w = tile_x_to_lon(tx0, zoom)
    lon_e = tile_x_to_lon(tx1, zoom)
    lat_n = tile_y_to_lat(ty0, zoom)
    lat_s = tile_y_to_lat(ty1, zoom)
    width_m = haversine(lon_w, lat_n, lon_e, lat_n)
    height_m = haversine(lon_w, lat_n, lon_w, lat_s)
    merc = {"x0": (lon_w + 180.0) / 360.0, "x1": (lon_e + 180.0) / 360.0,
            "y0": merc_y(lat_n), "y1": merc_y(lat_s)}
    log(f"    · 范围 {width_m / 1000:.2f} × {height_m / 1000:.2f} km  (zoom {zoom}, {grid}×{grid} 瓦片)")

    # ---- 2 & 3. DEM 与卫星影像并行下载（网络阶段并发，省首跑时间）----
    # 影像 zoom 预算（纯数学，只依赖范围，不依赖 DEM），先算好再并发下瓦片
    sat_zoom, sx0, sy0, nx, ny = zoom, 0, 0, 0, 0
    fx0, fy0, fx1, fy1 = 0.0, 0.0, 0.0, 0.0
    if not args.no_sat:
        span_tiles = max(1e-6, grid * 256.0)  # 该范围在 DEM zoom 下的像素跨度
        needed = int(math.ceil(math.log2(max(1.0, args.sat_max_size) / span_tiles)))
        sat_zoom = min(MAX_SAT_ZOOM, zoom + max(args.sat_extra, needed))
        fx0, fx1 = lon_to_tile_x(lon_w, sat_zoom), lon_to_tile_x(lon_e, sat_zoom)
        fy0, fy1 = lat_to_tile_y(lat_n, sat_zoom), lat_to_tile_y(lat_s, sat_zoom)
        while True:
            nxx = int(math.ceil(fx1)) - int(math.floor(fx0))
            nyy = int(math.ceil(fy1)) - int(math.floor(fy0))
            if nxx * nyy <= args.max_sat_tiles or sat_zoom <= zoom:
                break
            sat_zoom -= 1
            fx0, fx1 = lon_to_tile_x(lon_w, sat_zoom), lon_to_tile_x(lon_e, sat_zoom)
            fy0, fy1 = lat_to_tile_y(lat_n, sat_zoom), lat_to_tile_y(lat_s, sat_zoom)
        sx0, sy0 = int(math.floor(fx0)), int(math.floor(fy0))
        nx = int(math.ceil(fx1)) - sx0
        ny = int(math.ceil(fy1)) - sy0
        log(f"    · 影像 zoom {sat_zoom}，{nx}×{ny} 瓦片（与 DEM 并行下载）")

    def _fetch_dem():
        return mosaic_dem(zoom, tx0, ty0, grid, args.workers)

    def _fetch_sat():
        return mosaic_sat(sat_zoom, sx0, sy0, nx, ny, args.workers)

    log(f"[2/5] 并行下载 DEM ({grid * grid} 块) + 卫星影像…")
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_dem = ex.submit(_fetch_dem)
        f_sat = ex.submit(_fetch_sat) if not args.no_sat else None
        dem_raw = f_dem.result()
        sat_mos = f_sat.result() if f_sat is not None else None

    if np.isnan(dem_raw).all():
        raise RuntimeError("该区域无高程数据（可能位于远海或数据缺失区），请更换地点或降低 zoom。")
    fill = float(np.nanmin(dem_raw))
    dem = np.nan_to_num(dem_raw, nan=fill)
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
    log(f"    · 高程 {float(dem.min()):.0f} – {float(dem.max()):.0f} m，主峰 {peak_elev:.0f} m（网格 {W}×{H}）")

    dem_b64 = encode_terrarium(dem)

    # ---- 3. 卫星影像后处理（裁切/锐化/hillshade 烤入，依赖 dem 完成）----
    sat_b64, sat_w, sat_h, sat_mime = "", 0, 0, ""
    if sat_mos is not None:
        log("[3/5] 处理卫星影像（裁切/锐化/hillshade）…")
        cx0 = (fx0 - sx0) * 256.0
        cy0 = (fy0 - sy0) * 256.0
        cw = (fx1 - fx0) * 256.0
        ch = (fy1 - fy0) * 256.0
        box = (int(round(cx0)), int(round(cy0)),
               int(round(cx0 + cw)), int(round(cy0 + ch)))
        box = (max(0, box[0]), max(0, box[1]),
               min(sat_mos.width, box[2]), min(sat_mos.height, box[3]))
        crop = sat_mos.crop(box)
        # 纹理长边对齐 sat_max_size（与网格 W/H 解耦）
        long_edge = max(crop.width, crop.height)
        scale = args.sat_max_size / max(1, long_edge)
        sat_w = max(2, int(round(crop.width * scale)))
        sat_h = max(2, int(round(crop.height * scale)))
        crop = crop.resize((sat_w, sat_h), Image.LANCZOS)
        # 锐化（微妙提升贴图 crispness）
        if args.sharpen != 1.0:
            crop = ImageEnhance.Sharpness(crop).enhance(args.sharpen)
        # 叠微 hillshade：用 DEM 重采样到纹理尺寸，把起伏"烤"进贴图增强立体感
        if not args.no_hillshade:
            shade = compute_hillshade(dem, altitude=args.hillshade_alt)
            shade = np.asarray(Image.fromarray(shade).resize((sat_w, sat_h), Image.BILINEAR),
                               dtype=np.float32)
            k = args.hillshade_strength
            factor = np.clip((1.0 - 0.5 * k) + k * shade, 0.45, 1.45)
            arr = np.asarray(crop, dtype=np.float32) * factor[..., None]
            crop = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), mode="RGB")
            log(f"    · 已叠 hillshade（强度 {k:.2f}，光照角 {args.hillshade_alt:.0f}°）")
        sat_b64, sat_mime = encode_sat(crop, args.sat_format, args.jpeg_quality)
        log(f"    · 影像编码 {sat_mime.split('/')[-1].upper()}，纹理 {sat_w}×{sat_h}")
    else:
        log("[3/5] 跳过卫星影像（--no-sat）")

    # ---- 4. 地标 ----
    landmarks = []
    if not args.no_landmarks:
        log("[4/5] 获取地标 POI…")
        landmarks = fetch_landmarks(lon_w, lat_s, lon_e, lat_n, limit=args.landmarks)
        log(f"    · 地标 {len(landmarks)} 个")
    else:
        log("[4/5] 跳过地标（--no-landmarks）")

    peak_name = ""
    if landmarks:
        pk = [l for l in landmarks if l.get("desc") == "peak"]
        if pk:
            peak_name = max(pk, key=lambda l: l.get("ele") or -9999)["name"]

    # 与主峰标注重复的地标去掉，避免峰顶叠两个标签
    if landmarks:
        plon = merc["x0"] + pi / max(1, W - 1) * (merc["x1"] - merc["x0"])
        plat = merc_y_to_lat(merc["y0"] + pj / max(1, H - 1) * (merc["y1"] - merc["y0"]))
        keep = []
        for l in landmarks:
            d = haversine(l["lon"], l["lat"], plon * 360 - 180, plat)
            if d < max(250.0, width_m * 0.02) and (l["name"] == peak_name or not peak_name):
                continue
            keep.append(l)
        if len(keep) != len(landmarks):
            log(f"    · 去除 {len(landmarks) - len(keep)} 个与主峰重复的地标")
        landmarks = keep

    # ---- 5. 组装 ----
    meta = {
        "place": args.place or matched,
        "matched_name": matched,
        "center_lat": lat,
        "center_lon": lon,
        "zoom": zoom,
        "sat_zoom": sat_zoom,
        "grid": grid,
        "size_w": int(W),
        "size_h": int(H),
        "size": int(W),
        "width_m": width_m,
        "height_m": height_m,
        "min_elev": float(dem.min()),
        "max_elev": float(dem.max()),
        "peak_elev": peak_elev,
        "peak_name": peak_name,
        "peak_u": float(pi) / max(1, W - 1),   # u -> 东西 (x)
        "peak_v": float(pj) / max(1, H - 1),   # v -> 南北 (z)
        "bounds": {"lon_w": lon_w, "lat_n": lat_n, "lon_e": lon_e, "lat_s": lat_s},
        "merc": merc,  # 像素行号与墨卡托 Y 线性相关，插值必须在墨卡托空间做
        "has_sat": bool(sat_b64),
        "resolution_m": max(width_m, height_m) / max(1, W),
        "sat_resolution_m": (max(width_m, height_m) / max(1, sat_w) if sat_w else None),
        "size_sat_w": sat_w,
        "size_sat_h": sat_h,
        "sources": {
            "dem": "AWS Terrarium (SRTM/GDEM)",
            "sat": "Esri World Imagery" if sat_b64 else "",
            "poi": "OpenStreetMap / Overpass",
        },
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    log(f"[5/5] 完成，用时 {time.time() - t0:.1f}s")
    return {"meta": meta, "dem_b64": dem_b64, "sat_b64": sat_b64,
            "sat_mime": sat_mime or "image/jpeg", "landmarks": landmarks}


def main():
    p = argparse.ArgumentParser(description="三维真实地形数据准备（v2）",
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--place", help="地名（中文/英文均可）")
    p.add_argument("--lat", type=float, help="直接指定中心纬度 WGS84（与 --lon 同时使用）")
    p.add_argument("--lon", type=float, help="直接指定中心经度 WGS84")
    p.add_argument("--zoom", type=int, default=14, help="DEM 瓦片层级 (3-15)")
    p.add_argument("--grid", type=int, default=3, help="瓦片网格数 N（覆盖 N×N 个瓦片）")
    p.add_argument("--max-size", type=int, default=1024, help="DEM/网格最大边长（像素，控制网格顶点数）")
    p.add_argument("--max-verts", type=int, default=3000000, help="网格顶点数硬上限（超出则按最近邻抽稀网格，纹理仍保持 sat-max-size 清晰度；防止顶点爆炸）")
    p.add_argument("--sat-max-size", type=int, default=2048,
                   help="卫星影像纹理最大边长（像素，独立于网格，决定贴图清晰度；越大越清晰、体积越大）")
    p.add_argument("--sat-extra", type=int, default=2, help="卫星影像额外超采样下限（zoom+N，作为清晰度下限）")
    p.add_argument("--max-sat-tiles", type=int, default=576, help="影像瓦片数上限（超出则自动降低 zoom 控体积）")
    p.add_argument("--sat-format", default="jpeg", choices=["webp", "jpeg"],
                   help="影像编码：jpeg=默认(体积小、兼容好)；webp=对平滑/照片类常更小，但叠加 hillshade 的高熵纹理下可能反而更大，按需选用")
    p.add_argument("--jpeg-quality", type=int, default=85, help="影像质量（webp/jpeg 通用，70-95）")
    p.add_argument("--sharpen", type=float, default=1.3, help="纹理锐化强度（1.0=不锐化，>1 更锐）")
    p.add_argument("--no-hillshade", action="store_true", help="不叠加 hillshade 立体光照")
    p.add_argument("--hillshade-strength", type=float, default=0.5, help="hillshade 强度（0-1，越大越立体）")
    p.add_argument("--hillshade-alt", type=float, default=45.0, help="hillshade 光源高度角（度）")
    p.add_argument("--landmarks", type=int, default=12, help="最多地标数")
    p.add_argument("--no-sat", action="store_true", help="不下载卫星影像")
    p.add_argument("--no-landmarks", action="store_true", help="不获取地标")
    p.add_argument("--geocoder", default="auto", help="地理编码源: auto|amap|nominatim|photon")
    p.add_argument("--workers", type=int, default=8, help="并发下载线程数")
    p.add_argument("--no-cache", action="store_true", help="禁用瓦片磁盘缓存")
    p.add_argument("--out", default="terrain.json", help="输出 JSON 路径")
    p.add_argument("--out-html", default=None, help="同时生成 HTML（一步到位）")
    p.add_argument("--exaggeration", type=float, default=None, help="初始垂直夸张系数（默认自动）")
    p.add_argument("--cdn", default="auto", help="Three.js CDN: auto|jsdelivr|unpkg|esm.sh（仅 --three cdn 时生效）")
    p.add_argument("--three", default="auto", choices=["auto", "embed", "cdn"],
                   help="Three.js 引入方式：auto=优先内嵌(离线可看), embed=强制内嵌, cdn=外链")
    args = p.parse_args()

    if not args.place and (args.lat is None or args.lon is None):
        p.error("需提供 --place，或同时提供 --lat 与 --lon")
    global USE_CACHE
    USE_CACHE = not args.no_cache

    data = build(args)
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
        log(f"    影像纹理  {m['size_sat_w']}×{m['size_sat_h']}，贴图分辨率 {m['sat_resolution_m']:.2f} m/px")
    log(f"    地标      {len(data['landmarks'])} 个")

    if args.out_html:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import generate_html
        generate_html.render(data, args.out_html,
                             exaggeration=args.exaggeration, cdn=args.cdn,
                             three=args.three)
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
