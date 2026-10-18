"""True-colour satellite imagery acquisition.

Two sources, deliberately layered rather than chosen once:

  * **Sentinel-2 L2A** (10 m, ESA/Copernicus, free and open) — global, so it is
    the only option that will still work when the AOI moves to the Alps or the
    Pyrenees. This is the base layer everywhere.
  * **NAIP** (0.6 m, USDA, public domain) — United States only, but sixteen
    times the linear resolution. Used where it exists, which for now is the
    whole AOI.

Both are reached anonymously over plain HTTPS. That is not a given in this
corner of the world and it drove the choice of endpoint: Earth Search hands
out `s3://naip-analytic/...` hrefs for NAIP, and that bucket is requester-pays,
so it needs AWS credentials and bills the caller. Planetary Computer serves the
same imagery from Azure blob storage with a free, account-less SAS token.

Sentinel-2 is taken from the `visual` asset — the True Colour Image, already
8-bit and already stretched. Deriving RGB from the raw B04/B03/B02 reflectance
bands means inventing a stretch that has to hold up across snow, rock, conifer
and water simultaneously, and the ESA product does that job well enough that
there is nothing to gain by redoing it.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import threading
import json
import math
import re
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import rasterio
import requests
from rasterio.enums import Resampling
from rasterio.merge import merge
from rasterio.warp import transform_bounds
from rasterio.windows import Window

from aoi import AOI_DIR, DATA


HEADERS = {
    "User-Agent": "vistarama/0.1 "
                  "(hiking scenery research; contact via repo)"
}
TIMEOUT = 180

EARTH_SEARCH = "https://earth-search.aws.element84.com/v1/search"
PC_SEARCH = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
PC_SAS = "https://planetarycomputer.microsoft.com/api/sas/v1/token/{collection}"

# Late summer: snow off everything below the permanent icefields, rivers down,
# sun still high enough that north faces are not solid black. Going later than
# early October risks termination dust on the high country.
SEASON = ("07-15", "10-05")

S2_RES = 10.0
# NAIP at its native 0.6 m (the finest Washington has been flown at; 2011-2017
# were 1 m), stored as JPEG in YCbCr. The source quads are 4-band DEFLATE,
# which barely compresses photographs (a 2023 quad is 458 MB against 442 MB
# raw), so the mosaic is where the size is won: JPEG at this quality is a
# small fraction of the raw size, with losses below what the eye picks out in
# terrain texture. An earlier build stored 2 m uncompressed (7.7 GB) and threw
# away nine-tenths of the detail.
NAIP_RES = 0.6
NAIP_JPEG_QUALITY = 88
# The blob host gives one connection only ~1-2 MB/s, so throughput comes from
# connections in flight: each quad's share of a strip is fetched as column
# chunks of NAIP_CHUNK_COLS, each on its own connection.
NAIP_FETCH_WORKERS = 48
NAIP_CHUNK_COLS = 2048

# Rows of the destination mosaic built per pass. The full AOI at 10 m is ~1.1 GB
# as a 3-band uint8 array, which is more than worth holding in memory at once.
STRIP_ROWS = 2048

# Concurrent quad downloads. The limit here is the host's per-connection rate,
# not local CPU, so this is set well above core count.
WORKERS = 12


# Progress goes to a file as well as stdout, unbuffered, because these runs are
# long and stdout is not always reachable — a pipe through `tail` buffers the
# whole run, and a job killed by the machine sleeping leaves nothing behind at
# all. The log file is the one place progress can always be read from.
LOG_PATH = DATA / "imagery.log"


def log(msg: str) -> None:
    line = f"[imagery] {msg}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
    except OSError:
        pass


def _post(url: str, body: dict) -> dict:
    r = requests.post(url, json=body, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    ct = r.headers.get("content-type", "")
    if "json" not in ct:
        # An HTML body with a 200 is how these APIs report an auth wall. The
        # RGI download in this project failed exactly this way.
        raise RuntimeError(f"{url} returned {ct}, not JSON: {r.text[:200]}")
    return r.json()


# --------------------------------------------------------------------------
# Sentinel-2
# --------------------------------------------------------------------------

def _nodata_pct(f) -> float:
    return float(f["properties"].get("s2:nodata_pixel_percentage", 0.0) or 0.0)


def pick_s2_date(bbox, years: list[int], max_cloud: float = 15.0,
                 min_cover: float = 0.999) -> tuple[str, list]:
    """Find one acquisition date that covers the whole bbox, cloud-free.

    A single date matters more than it might seem. Mosaicking two dates puts a
    seam through the imagery wherever the tiles meet — different sun angle,
    different snowline, different atmospheric haze — and that seam lands in the
    middle of the panorama. Sentinel-2's 5-day revisit over a 9-tile footprint
    makes a fully clear single date likely enough to just ask for one.

    Two traps, both of which produced a two-thirds-covered mosaic before they
    were handled:

    * **Cloud cover says nothing about data coverage.** A granule clipped by
      the edge of the swath is 92% nodata and still reports 0.001% cloud,
      because the cloud percentage is computed over the pixels that exist. So
      `s2:nodata_pixel_percentage` has to be read too.
    * **One MGRS tile can have several scenes on one date**, from adjacent
      orbits, each holding a different slice of it. Keeping only the "best"
      scene per tile therefore throws away the half of the tile that the other
      scene was carrying. Every scene on the chosen date is returned, fullest
      first, and the merge composites them.

    Coverage is judged by unioning the scenes' STAC footprints, not by any
    nodata percentage. Those footprints are the real data outlines — their
    areas track `s2:nodata_pixel_percentage` to the decimal — so the union is
    an exact answer to "is any of the AOI missing", which no combination of
    per-scene percentages can give once scenes overlap.
    """
    from shapely.geometry import box, shape
    from shapely.ops import unary_union

    aoi = box(*bbox)
    best = None
    for year in years:
        got = _post(EARTH_SEARCH, {
            "collections": ["sentinel-2-l2a"], "bbox": list(bbox),
            "datetime": f"{year}-{SEASON[0]}T00:00:00Z/{year}-{SEASON[1]}T23:59:59Z",
            "query": {"eo:cloud_cover": {"lt": max_cloud}},
            "limit": 500,
        })
        feats = got.get("features", [])
        byday = defaultdict(list)
        for f in feats:
            byday[f["properties"]["datetime"][:10]].append(f)
        log(f"{year}: {len(feats)} scenes on {len(byday)} dates")
        for d, scenes in byday.items():
            union = unary_union([shape(f["geometry"]) for f in scenes])
            cover = union.intersection(aoi).area / aoi.area
            if cover < min_cover:
                continue
            worst_cloud = max(f["properties"].get("eo:cloud_cover", 100.0)
                              for f in scenes)
            if best is None or worst_cloud < best[0]:
                best = (worst_cloud, d, scenes, cover)
    if best is None:
        raise RuntimeError(
            "no single date covers the bbox; widen SEASON, raise max_cloud, "
            "lower min_cover, or add years")
    worst_cloud, date, feats, cover = best
    feats.sort(key=_nodata_pct)          # fullest first: merge is first-wins
    log(f"chose {date}: {len(feats)} scenes over "
        f"{len({f['id'].split('_')[1] for f in feats})} tiles, "
        f"{cover * 100:.2f}% AOI coverage, worst cloud {worst_cloud:.3f}%")
    return date, feats


def build_s2_mosaic(feats: list, bbox, dst: Path) -> Path:
    """Merge the `visual` COGs over the bbox, in the tiles' own CRS.

    Every Sentinel-2 tile here is EPSG:32610 and they share a common 10 m grid,
    so this is a pixel copy with no resampling at all — the reprojection to the
    metric CRS happens once, afterwards, instead of once per tile.
    """
    if dst.exists():
        log(f"skip {dst.name} (present)")
        return dst

    epsgs = {f["properties"].get("proj:epsg") for f in feats}
    if len(epsgs) != 1:
        raise RuntimeError(
            f"tiles span multiple CRSs {epsgs}; merging them needs a warp per "
            f"tile, which this function deliberately does not do")
    epsg = epsgs.pop()
    hrefs = [f["assets"]["visual"]["href"] for f in feats]

    west, south, east, north = transform_bounds(
        "EPSG:4326", f"EPSG:{epsg}", *bbox, densify_pts=21)
    # snap outward to the 10 m grid so the merge stays a pixel copy
    west, south = math.floor(west / S2_RES) * S2_RES, math.floor(south / S2_RES) * S2_RES
    east, north = math.ceil(east / S2_RES) * S2_RES, math.ceil(north / S2_RES) * S2_RES
    width = int(round((east - west) / S2_RES))
    height = int(round((north - south) / S2_RES))
    log(f"mosaic {width}x{height} @{S2_RES:g}m EPSG:{epsg} from {len(hrefs)} tiles")

    srcs = []
    try:
        for h in hrefs:
            srcs.append(rasterio.open(h.replace("https://", "/vsicurl/https://")))
        profile = {
            "driver": "GTiff", "dtype": "uint8", "count": 3, "nodata": 0,
            "width": width, "height": height, "crs": f"EPSG:{epsg}",
            "transform": rasterio.transform.from_origin(west, north, S2_RES, S2_RES),
            "tiled": True, "blockxsize": 512, "blockysize": 512,
            "compress": "deflate", "predictor": 2, "BIGTIFF": "IF_SAFER",
        }
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".part.tif")
        with rasterio.open(tmp, "w", **profile) as out:
            for r0 in range(0, height, STRIP_ROWS):
                rows = min(STRIP_ROWS, height - r0)
                strip_n = north - r0 * S2_RES
                strip_s = strip_n - rows * S2_RES
                arr, _ = merge(srcs, bounds=(west, strip_s, east, strip_n),
                               res=S2_RES, nodata=0, indexes=[1, 2, 3])
                out.write(arr[:, :rows, :width], window=Window(0, r0, width, rows))
                log(f"  rows {r0}-{r0 + rows} of {height}")
        tmp.replace(dst)
    finally:
        for s in srcs:
            s.close()
    return dst


# --------------------------------------------------------------------------
# Sentinel-2: a seasonal median composite, for areas no single date covers
# --------------------------------------------------------------------------

# Scene classification (SCL) classes kept as ground: dark areas (2, which is
# also terrain shadow, so north faces must keep it), vegetation, bare, water,
# unclassified and snow/ice (11: summer snowfields and glaciers are real).
# Dropped: no data, saturated, cloud shadow, cloud, thin cirrus.
S2_SCL_KEEP = np.array([False, False, True, False, True, True, True, True,
                        False, False, False, True])
# Clear looks per pixel that make the median stable. Scenes are read clearest
# first and a block stops reading once every pixel has this many, which in a
# Cascades summer is after a handful of scenes rather than every one.
S2_MIN_LOOKS = 7
# ... on this share of the block's pixels: a few never get there (a summit's
# cloud cap, glacier ice the classifier calls cloud), and waiting on them read
# every scene there was. And never more than S2_MAX_SCENES per block.
S2_LOOKS_SHARE = 0.99
S2_MAX_SCENES = 24
S2_COMPOSITE_BLOCK = 2048
# One warp at a time, single-threaded, and never on a sliver. The composite
# reads scenes on 16 threads, and GDAL's warper corrupted the heap (Windows
# fatal exception 0xc0000374, inside rasterio's reproject) every ~150 blocks
# at 10 m, and within two blocks at 60 m, where reads are decimated and scene
# edges leave windows of a pixel or two. Serialising alone did not stop it;
# the warper's own worker threads and the degenerate windows are gone too. A
# block's warp is ~0.1 s against seconds of network reads.
_S2_WARP_LOCK = threading.Lock()
S2_JPEG_QUALITY = 90


def _stac_search(url: str, body: dict) -> list:
    """Every feature of a STAC search, following its paging links."""
    def post(b):
        # the public STAC APIs answer a busy moment with 502/504; wait it out
        for attempt in range(6):
            try:
                return _post(url, b)
            except (requests.HTTPError, requests.ConnectionError, requests.Timeout) as e:
                if attempt == 5:
                    raise
                log(f"STAC search: {type(e).__name__}: {str(e)[:100]}; retrying")
                time.sleep(10 * 2 ** attempt)

    feats = []
    while True:
        page = post(body)
        feats.extend(page.get("features", []))
        nxt = next((l for l in page.get("links", []) if l.get("rel") == "next"), None)
        if not nxt:
            return feats
        if nxt.get("body"):
            body = {**body, **nxt["body"]}
            continue
        # GET-style paging: follow hrefs until there is no next link
        href = nxt["href"]
        while href:
            got = requests.get(href, headers=HEADERS, timeout=TIMEOUT)
            got.raise_for_status()
            page = got.json()
            feats.extend(page.get("features", []))
            nxt = next((l for l in page.get("links", []) if l.get("rel") == "next"), None)
            href = nxt["href"] if nxt else None
        return feats


def build_s2_composite(bbox, dst: Path, res: float, year: int,
                       max_cloud: float = 50.0, quality: int = S2_JPEG_QUALITY) -> Path:
    """A per-pixel median of clear Sentinel-2 looks over one season, in
    EPSG:5070 at `res`, JPEG/YCbCr.

    One date cannot cover western Washington cloud-free, and a mosaic of
    dates puts a seam wherever two meet; the median of each pixel's clear
    looks has none. The `visual` (true-colour) asset is used, the same
    product as the single-date Alpine Lakes layer, with each scene's SCL band
    as the cloud mask. Built in blocks; each block is written once and the
    file closed after it (see build_naip_mosaic for why), and a rerun resumes
    after the last block recorded. Where a pixel has no clear look at all it
    takes the clearest scene that has data there.
    """
    from pyproj import Transformer
    from rasterio.warp import reproject
    from shapely.geometry import box as sbox, shape

    if dst.exists():
        log(f"skip {dst.name} (present)")
        return dst
    crs = "EPSG:5070"
    west, south, east, north = transform_bounds("EPSG:4326", crs, *bbox, densify_pts=21)
    west, south = math.floor(west / res) * res, math.floor(south / res) * res
    east, north = math.ceil(east / res) * res, math.ceil(north / res) * res
    width, height = int(round((east - west) / res)), int(round((north - south) / res))
    B = S2_COMPOSITE_BLOCK
    nbx, nby = (width + B - 1) // B, (height + B - 1) // B

    feats = _stac_search(EARTH_SEARCH, {
        "collections": ["sentinel-2-l2a"], "bbox": list(bbox),
        "datetime": f"{year}-{SEASON[0]}T00:00:00Z/{year}-{SEASON[1]}T23:59:59Z",
        "query": {"eo:cloud_cover": {"lt": max_cloud}}, "limit": 100})
    feats = [f for f in feats if "visual" in f["assets"] and "scl" in f["assets"]]
    feats.sort(key=lambda f: (f["properties"].get("eo:cloud_cover", 100.0), _nodata_pct(f)))
    log(f"S2 composite {dst.name}: {width}x{height} @{res:g}m, {nbx}x{nby} blocks, "
        f"{len(feats)} scenes {year} {SEASON[0]}..{SEASON[1]} under {max_cloud:g}% cloud")
    to_ll = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    shapes = [shape(f["geometry"]) for f in feats]

    part = dst.with_name(dst.stem + ".part.tif")
    prog_path = dst.with_name(dst.stem + ".progress.json")
    settings = {"res": res, "width": width, "height": height, "block": B, "year": year,
                "max_cloud": max_cloud, "quality": quality}
    done = 0
    if part.exists() and prog_path.exists():
        prog = json.loads(prog_path.read_text(encoding="utf-8"))
        if prog.get("settings") == settings:
            done = prog["blocks_done"]
            log(f"S2 composite: resuming at block {done} of {nbx * nby}")
    if done == 0:
        part.unlink(missing_ok=True)
        profile = {"driver": "GTiff", "dtype": "uint8", "count": 3, "nodata": 0,
                   "width": width, "height": height, "crs": crs,
                   "transform": rasterio.transform.from_origin(west, north, res, res),
                   "tiled": True, "blockxsize": 512, "blockysize": 512,
                   "compress": "JPEG", "photometric": "YCBCR", "jpeg_quality": quality,
                   "BIGTIFF": "YES"}
        dst.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(part, "w", **profile):
            pass

    def read_scene(f, bx0, by0, bw, bh):
        """(rgb, keep, has) of one scene on the block grid, or None if it has
        nothing there."""
        dst_tf = rasterio.transform.from_origin(west + bx0 * res, north - by0 * res, res, res)
        b_w, b_n = west + bx0 * res, north - by0 * res
        b_e, b_s = b_w + bw * res, b_n - bh * res
        out = {}
        for key, bands in (("visual", [1, 2, 3]), ("scl", [1])):
            href = f["assets"][key]["href"].replace("https://", "/vsicurl/https://")
            for attempt in range(5):
                try:
                    with rasterio.open(href) as sds:
                        bw_, bs_, be_, bn_ = transform_bounds(crs, sds.crs, b_w, b_s, b_e, b_n,
                                                              densify_pts=21)
                        m = 2 * sds.res[0]
                        win = rasterio.windows.from_bounds(bw_ - m, bs_ - m, be_ + m, bn_ + m,
                                                           sds.transform)
                        win = win.round_offsets().round_lengths()
                        full = Window(0, 0, sds.width, sds.height)
                        if not rasterio.windows.intersect(win, full):
                            return None
                        win = win.intersection(full)
                        dec = max(1, int(res / sds.res[0] + 1e-6))
                        shp = (len(bands), max(1, int(win.height) // dec),
                               max(1, int(win.width) // dec))
                        # a sliver of a scene at its edge: nothing worth a
                        # warp, and degenerate warps are where GDAL fell over
                        if shp[1] < 2 or shp[2] < 2:
                            return None
                        src = sds.read(indexes=bands, window=win, out_shape=shp,
                                       resampling=Resampling.average if key == "visual"
                                       else Resampling.nearest)
                        src_tf = sds.window_transform(win) * rasterio.Affine.scale(
                            win.width / shp[2], win.height / shp[1])
                        arr = np.zeros((len(bands), bh, bw), dtype=np.uint8)
                        with _S2_WARP_LOCK:
                            reproject(source=src, destination=arr, src_transform=src_tf,
                                      src_crs=sds.crs, src_nodata=0, dst_transform=dst_tf,
                                      dst_crs=crs, dst_nodata=0,
                                      resampling=Resampling.bilinear if key == "visual"
                                      else Resampling.nearest, num_threads=1)
                        out[key] = arr
                    break
                except Exception as e:  # noqa: BLE001 - retried, then the scene is skipped
                    if attempt == 4:
                        log(f"  S2 {f['id']} {key}: giving up: {type(e).__name__}: {str(e)[:120]}")
                        return None
                    time.sleep(3 * 2 ** attempt)
        rgb, scl = out["visual"], out["scl"][0]
        has = rgb.any(axis=0)
        if not has.any():
            return None
        keep = has & S2_SCL_KEEP[np.minimum(scl, 11)]
        return rgb, keep, has

    t0 = time.time()
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_MULTIRANGE="YES",
                      GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES", GDAL_CACHEMAX=1024), \
            cf.ThreadPoolExecutor(16) as ex:
        for k in range(done, nbx * nby):
            by, bx = divmod(k, nbx)
            bx0, by0 = bx * B, by * B
            bw, bh = min(B, width - bx0), min(B, height - by0)
            # the block's footprint in lon/lat, to pick the scenes that reach it
            xs = [west + bx0 * res, west + (bx0 + bw) * res]
            ys = [north - (by0 + bh) * res, north - by0 * res]
            lon, lat = to_ll.transform([xs[0], xs[1], xs[1], xs[0]],
                                       [ys[0], ys[0], ys[1], ys[1]])
            foot = sbox(min(lon), min(lat), max(lon), max(lat))
            cands = [f for f, sh in zip(feats, shapes) if sh.intersects(foot)]
            stack, keeps = [], []
            fallback = np.zeros((3, bh, bw), dtype=np.uint8)
            fb_has = np.zeros((bh, bw), dtype=bool)
            looks = np.zeros((bh, bw), dtype=np.uint16)
            read = 0
            # clearest first, eight at a time, until nearly every pixel with
            # data has S2_MIN_LOOKS clear looks or the scenes run out
            for i in range(0, len(cands), 8):
                for got in ex.map(lambda f: read_scene(f, bx0, by0, bw, bh), cands[i:i + 8]):
                    read += 1
                    if got is None:
                        continue
                    rgb, keep, has = got
                    fill = has & ~fb_has
                    fallback[:, fill] = rgb[:, fill]
                    fb_has |= has
                    if keep.any():
                        stack.append(rgb)
                        keeps.append(keep)
                        looks += keep
                if read >= S2_MAX_SCENES or (fb_has.any() and np.mean(
                        looks[fb_has] >= S2_MIN_LOOKS) >= S2_LOOKS_SHARE):
                    break
            out = fallback
            if stack:
                st = np.stack(stack)                              # n, 3, h, w
                kp = np.stack(keeps)                              # n, h, w
                n = kp.sum(axis=0)
                st = np.where(kp[:, None], st, np.uint8(255))     # invalid sorts last
                st.sort(axis=0)
                idx = (np.maximum(n.astype(np.int64), 1) - 1) // 2
                med = np.take_along_axis(st, np.broadcast_to(idx, (1, 3) + idx.shape), axis=0)[0]
                out = np.where(n[None] > 0, med, fallback).astype(np.uint8)
            data = out.any(axis=0)
            out[:, data] = np.maximum(out[:, data], 1)
            with rasterio.open(part, "r+") as dsw:
                dsw.write(out, window=Window(bx0, by0, bw, bh))
            prog_path.write_text(json.dumps({"settings": settings, "blocks_done": k + 1}),
                                 encoding="utf-8")
            el = time.time() - t0
            left = el / (k + 1 - done) * (nbx * nby - k - 1)
            med_looks = int(np.median(looks[fb_has])) if fb_has.any() else 0
            log(f"  block {k + 1}/{nbx * nby}: {read} of {len(cands)} scenes read, "
                f"median {med_looks} clear looks, {data.mean():.0%} data, "
                f"{el / 60:.1f} min, ~{left / 60:.0f} min left")
    log("S2 composite: building overviews")
    with rasterio.Env(COMPRESS_OVERVIEW="JPEG", PHOTOMETRIC_OVERVIEW="YCBCR",
                      JPEG_QUALITY_OVERVIEW=str(quality), INTERLEAVE_OVERVIEW="PIXEL",
                      GDAL_NUM_THREADS="ALL_CPUS", BIGTIFF_OVERVIEW="YES"):
        with rasterio.open(part, "r+") as ds:
            ds.build_overviews([2, 4, 8, 16, 32, 64], Resampling.average)
            ds.update_tags(ns="rio_overview", resampling="average")
    part.replace(dst)
    prog_path.unlink(missing_ok=True)
    log(f"S2 composite complete: {dst.name}, {dst.stat().st_size / 1e9:.2f} GB, "
        f"{(time.time() - t0) / 60:.0f} min")
    return dst


# --------------------------------------------------------------------------
# NAIP
# --------------------------------------------------------------------------

_TOKENS: dict = {}
_TOKEN_LOCK = threading.Lock()


def pc_token(collection: str, failed: str | None = None) -> str:
    """A currently-valid SAS token, refreshed before it expires.

    These tokens last about an hour, and a full NAIP mosaic takes longer than
    that. Fetching one at the start and reusing it meant the back third of the
    run got HTTP 403 — 63 of 187 quads — and because each quad had its own
    except clause the job reported success with a third of the AOI missing.

    The token is kept until five minutes before the expiry the service states.
    A caller whose read failed passes the token it used as `failed`, and the
    token is refreshed only if that is still the current one: with 48 workers
    fetching at once, a token expiring made all of them force a refresh, and
    the token service answered the flood with HTTP 429 for minutes. Now one
    failure refreshes, and the rest pick up the new token. A 429 is waited out
    (for as long as the service says) while holding the lock, so the other
    workers wait for the same token instead of asking again.
    """
    with _TOKEN_LOCK:
        tok, exp = _TOKENS.get(collection, (None, 0.0))
        if tok and time.time() < exp and (failed is None or failed != tok):
            return tok
        for attempt in range(12):
            r = requests.get(PC_SAS.format(collection=collection),
                             headers=HEADERS, timeout=TIMEOUT)
            if r.status_code != 429:
                break
            wait = 30.0
            m = re.search(r"in (\d+) seconds", r.text)
            if m:
                wait = float(m.group(1)) + 2
            elif r.headers.get("Retry-After", "").isdigit():
                wait = float(r.headers["Retry-After"]) + 2
            log(f"SAS token endpoint rate-limited; waiting {wait:.0f} s")
            time.sleep(wait)
        r.raise_for_status()
        body = r.json()
        tok = body.get("token")
        if not tok:
            raise RuntimeError(f"no SAS token in response: {r.text[:200]}")
        exp = time.time() + 45 * 60
        try:
            from datetime import datetime
            stated = datetime.fromisoformat(body["msft:expiry"].replace("Z", "+00:00"))
            exp = stated.timestamp() - 5 * 60
        except (KeyError, ValueError):
            pass
        _TOKENS[collection] = (tok, exp)
        log(f"SAS token for {collection} refreshed, valid for "
            f"{(exp - time.time()) / 60:.0f} min")
        return tok


def search_naip(bbox, prefer_years: tuple[int, int] = (2021, 2025)) -> list:
    """Every NAIP quad intersecting the bbox, newest acquisition per quad.

    NAIP is flown state by state over a multi-year cycle and quads within one
    bbox routinely come from different years and different months. Preferring
    the newest is not obviously right — a late-October quad carries fresh snow
    that an August quad from two years earlier does not — so summer months win
    ties over recency.
    """
    feats, token = [], None
    body = {"collections": ["naip"], "bbox": list(bbox), "limit": 500}
    while True:
        page = _post(PC_SEARCH, body)
        feats.extend(page.get("features", []))
        nxt = next((l for l in page.get("links", []) if l.get("rel") == "next"), None)
        if not nxt:
            break
        body = {**body, **(nxt.get("body") or {})}
    log(f"NAIP: {len(feats)} quads intersect the bbox")

    def rank(f):
        # finest ground sample first (0.6 m flights over 1 m ones), then
        # summer, then newest: a 2017 July quad must not beat a 2023 October
        # one at 0.6 m when the point of the mosaic is resolution
        dt = f["properties"]["datetime"]
        month = int(dt[5:7])
        summer = 0 if 6 <= month <= 9 else 1        # summer first
        return (float(f["properties"].get("gsd") or 9.9), summer, -int(dt[:4]))

    best = {}
    for f in feats:
        # quad id without the acquisition suffix, so revisits collapse
        key = "_".join(f["id"].split("_")[:5])
        if key not in best or rank(f) < rank(best[key]):
            best[key] = f
    chosen = list(best.values())
    months = defaultdict(int)
    for f in chosen:
        months[f["properties"]["datetime"][:7]] += 1
    log(f"NAIP: {len(chosen)} quads after dedupe; acquisition months: "
        + ", ".join(f"{k}:{v}" for k, v in sorted(months.items())))
    return chosen


def _strip_has_data(path: Path, r0: int, r1: int) -> bool:
    """Whether rows r0..r1 of the mosaic hold any imagery: one row from each
    512-row band of tiles, first band only (each read decodes a whole row of
    tiles, so more rows would only repeat the work)."""
    with rasterio.open(path) as ds:
        rows = range(r0 + 256, r1, 512) or [r0]
        for r in rows:
            if ds.read(1, window=Window(0, r, ds.width, 1)).any():
                return True
    return False


def build_naip_mosaic(feats: list, bbox, dst: Path, res: float = NAIP_RES,
                      quality: int = NAIP_JPEG_QUALITY) -> Path:
    """Mosaic the NAIP quads into one EPSG:5070 GeoTIFF at `res`, JPEG/YCbCr.

    Built in strips of STRIP_ROWS rows, top to bottom, and each strip written
    to the file exactly once. That is what makes a compressed output possible:
    the earlier build wrote quad by quad into scattered, overlapping windows,
    and a compressed TIFF cannot do that (GDAL pins every dirty block in memory
    until it can compress it, or leaves the old copy behind as dead space when
    a block is rewritten), so it had to be stored uncompressed.

    For each strip, every quad crossing it is read over just that band, at
    full resolution (or decimated through the COG overviews when `res` is
    coarser than the source), warped onto the strip, and the quads merged in
    rank order, each filling only what is still empty. Only the part of the
    strip inside `bbox` is fetched: the mosaic is a rectangle in EPSG:5070 and
    the lon/lat box is a slanted quadrilateral inside it, so its corners would
    be a third of the transfer for imagery nobody looks at. Quads for the next
    strip download while the current one is merged and written.

    Resumable: the strips done so far are recorded beside the part file, and a
    rerun continues from the next one. Overviews (JPEG too) are built at the
    end, then the part file replaces `dst`.
    """
    from rasterio.warp import reproject
    from shapely.geometry import box as sbox
    from shapely.ops import transform as stransform
    from pyproj import Transformer

    crs = "EPSG:5070"
    west, south, east, north = transform_bounds("EPSG:4326", crs, *bbox, densify_pts=21)
    west, south = math.floor(west / res) * res, math.floor(south / res) * res
    east, north = math.ceil(east / res) * res, math.ceil(north / res) * res
    width, height = int(round((east - west) / res)), int(round((north - south) / res))
    transform = rasterio.transform.from_origin(west, north, res, res)
    to5070 = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    core = stransform(to5070, sbox(*bbox).segmentize(0.002))
    n_strips = (height + STRIP_ROWS - 1) // STRIP_ROWS

    part = dst.with_name(dst.stem + ".part.tif")
    prog_path = dst.with_name(dst.stem + ".progress.json")
    settings = {"res": res, "quality": quality, "width": width, "height": height,
                "strip_rows": STRIP_ROWS, "quads": sorted(f["id"] for f in feats)}
    first = 0
    if part.exists() and prog_path.exists():
        prog = json.loads(prog_path.read_text(encoding="utf-8"))
        if prog.get("settings") == settings and prog.get("flushed"):
            first = prog["strips_done"]
            # trust the record only if the last strip it names is on disk
            if first and not _strip_has_data(part, (first - 1) * STRIP_ROWS,
                                             min(height, first * STRIP_ROWS)):
                log(f"NAIP: strip {first} is recorded but empty on disk; starting over")
                first = 0
            else:
                log(f"NAIP: resuming at strip {first} of {n_strips}")
    if first == 0:
        part.unlink(missing_ok=True)
    log(f"NAIP mosaic {width}x{height} @{res:g}m {crs}, JPEG q{quality}, "
        f"{n_strips} strips of {STRIP_ROWS} rows, from {len(feats)} quads")

    # quad footprints in the mosaic CRS, for picking the quads a strip needs
    foot = {}
    for f in feats:
        fw, fs_, fe, fn = f["bbox"]
        foot[f["id"]] = transform_bounds("EPSG:4326", crs, fw, fs_, fe, fn, densify_pts=21)
    order = sorted(feats, key=lambda f: (float(f["properties"].get("gsd") or 9.9),
                                         0 if 6 <= int(f["properties"]["datetime"][5:7]) <= 9 else 1,
                                         -int(f["properties"]["datetime"][:4]), f["id"]))

    def strip_plan(i):
        """(rows, columns) of strip i to fill, and the quads that cross it."""
        r0 = i * STRIP_ROWS
        r1 = min(height, r0 + STRIP_ROWS)
        yn, ys = north - r0 * res, north - r1 * res
        band = core.intersection(sbox(west, ys, east, yn))
        if band.is_empty:
            return r0, r1, None, []
        bx0, _b, bx1, _t = band.bounds
        c0 = max(0, int((bx0 - west) / res) - 2)
        c1 = min(width, int((bx1 - west) / res) + 3)
        xw, xe = west + c0 * res, west + c1 * res
        quads = [f for f in order
                 if foot[f["id"]][0] < xe and foot[f["id"]][2] > xw
                 and foot[f["id"]][1] < yn and foot[f["id"]][3] > ys]
        return r0, r1, (c0, c1), quads

    def chunks(f, c0, c1):
        """The strip columns quad f can reach, in NAIP_CHUNK_COLS pieces."""
        fw, _fs, fe, _fn = foot[f["id"]]
        qc0 = max(c0, int((fw - west) / res) - 2)
        qc1 = min(c1, int((fe - west) / res) + 3)
        return [(a, min(qc1, a + NAIP_CHUNK_COLS)) for a in range(qc0, qc1, NAIP_CHUNK_COLS)]

    def fetch(f, r0, r1, qc0, qc1):
        """A piece of one quad's share of a strip, warped onto the strip grid:
        (col0, array) or None. Retried with a fresh token; every failure is
        logged, so a run that keeps retrying says why."""
        last = None
        used = None
        for attempt in range(8):
            try:
                # after a failure, a fresh token if nobody has fetched one yet
                used = pc_token("naip", failed=used if attempt else None)
                return _fetch_one(f, r0, r1, qc0, qc1, used)
            except Exception as e:  # noqa: BLE001 — retried, then raised
                last = e
                log(f"  retry {attempt + 1}: {f['id']} rows {r0}-{r1} cols {qc0}-{qc1}: "
                    f"{type(e).__name__}: {str(e)[:160]}")
                time.sleep(min(60, 3 * 2 ** attempt))
        raise RuntimeError(f"{f['id']} strip rows {r0}-{r1}: {type(last).__name__}: {last}")

    def _fetch_one(f, r0, r1, qc0, qc1, token):
        if qc1 <= qc0:
            return None
        dst_tf = rasterio.transform.from_origin(west + qc0 * res, north - r0 * res, res, res)
        with rasterio.open("/vsicurl/" + f["assets"]["image"]["href"] + "?" + token) as sds:
            # the source window covering this piece of the strip, with a margin
            # for the resampling kernel
            bw, bs, be, bn = transform_bounds(
                crs, sds.crs, west + qc0 * res, north - r1 * res,
                west + qc1 * res, north - r0 * res, densify_pts=21)
            m = 4 * sds.res[0]
            win = rasterio.windows.from_bounds(bw - m, bs - m, be + m, bn + m, sds.transform)
            win = win.round_offsets().round_lengths()
            full = Window(0, 0, sds.width, sds.height)
            # The footprints that pick quads for a strip are lon/lat boxes, a
            # little larger than the image, so a piece can miss it entirely.
            # That is no data, not a failed read (rasterio raises on an empty
            # intersection, which the retries used to mistake for one).
            if not rasterio.windows.intersect(win, full):
                return None
            win = win.intersection(full)
            if win.width <= 0 or win.height <= 0:
                return None
            dec = max(1, int(res / sds.res[0] + 1e-6))
            shape = (3, max(1, int(win.height) // dec), max(1, int(win.width) // dec))
            src = sds.read(indexes=[1, 2, 3], window=win, out_shape=shape,
                           resampling=Resampling.average)
            src_tf = sds.window_transform(win) * rasterio.Affine.scale(
                win.width / shape[2], win.height / shape[1])
            out = np.zeros((3, r1 - r0, qc1 - qc0), dtype=np.uint8)
            reproject(source=src, destination=out,
                      src_transform=src_tf, src_crs=sds.crs, src_nodata=0,
                      dst_transform=dst_tf, dst_crs=crs, dst_nodata=0,
                      resampling=Resampling.cubic, num_threads=2)
        return qc0, out

    profile = {
        "driver": "GTiff", "dtype": "uint8", "count": 3, "nodata": 0,
        "width": width, "height": height, "crs": crs, "transform": transform,
        "tiled": True, "blockxsize": 512, "blockysize": 512,
        "compress": "JPEG", "photometric": "YCBCR", "jpeg_quality": quality,
        "BIGTIFF": "YES",
    }
    dst.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    # The file is closed after every strip, and only then is the strip
    # recorded as done. GDAL holds written blocks in its cache and writes the
    # tile index (where each block sits) only on close, so a build stopped
    # with the file open lost every strip since it was opened: the first
    # complete-looking 0.6 m build had its northern two-thirds empty, after
    # two stops and resumes, and reported success. Reopening costs nothing
    # next to a strip, and keeps the JPEG settings (its tables live in the
    # file).
    if first == 0:
        with rasterio.open(part, "w", **profile):
            pass
    with rasterio.Env(GDAL_CACHEMAX=2048, GDAL_NUM_THREADS="ALL_CPUS",
                      GDAL_HTTP_MULTIRANGE="YES", GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
                      GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", VSI_CACHE="FALSE"), \
            cf.ThreadPoolExecutor(NAIP_FETCH_WORKERS) as ex:

        def submit(i):
            r0, r1, cols, quads = strip_plan(i)
            futs = [] if cols is None else [
                (f, ex.submit(fetch, f, r0, r1, a, b))
                for f in quads for a, b in chunks(f, cols[0], cols[1])]
            return r0, r1, cols, futs

        expected = {}                                   # strip -> fill written
        pending = submit(first) if first < n_strips else None
        for i in range(first, n_strips):
            r0, r1, cols, futs = pending
            pending = submit(i + 1) if i + 1 < n_strips else None   # prefetch
            strip = np.zeros((3, r1 - r0, width), dtype=np.uint8)
            filled = np.zeros((r1 - r0, width), dtype=bool)
            for f, fut in futs:                      # rank order: first wins
                got = fut.result()
                if got is None:
                    continue
                qc0, arr = got
                has = arr.any(axis=0)
                tgt = filled[:, qc0:qc0 + arr.shape[2]]
                take = has & ~tgt
                view = strip[:, :, qc0:qc0 + arr.shape[2]]
                view[:, take] = arr[:, take]
                tgt |= take
            # 0 means "no data": lift real black pixels to 1 so they stay data
            strip[:, filled] = np.maximum(strip[:, filled], 1)
            with rasterio.open(part, "r+") as out:
                out.write(strip, window=Window(0, r0, width, r1 - r0))
            prog_path.write_text(json.dumps({"settings": settings, "strips_done": i + 1,
                                             "flushed": True}), encoding="utf-8")
            expected[i] = float(filled.mean())
            el = time.time() - t0
            eta = el / (i + 1 - first) * (n_strips - i - 1)
            log(f"  strip {i + 1}/{n_strips}: {len({f['id'] for f, _ in futs})} quads, "
                f"{filled.mean():.0%} filled, {el / 60:.1f} min elapsed, ~{eta / 60:.0f} min left")

    # Every strip must read back the way it was written. A strip that comes
    # back empty means the file lost it, and that must fail the build rather
    # than ship a mosaic with holes (which is what the first one did).
    missing = [i + 1 for i in range(n_strips)
               if expected.get(i, 1.0) > 0.01
               and not _strip_has_data(part, i * STRIP_ROWS, min(height, (i + 1) * STRIP_ROWS))]
    if missing:
        raise RuntimeError(f"NAIP: strips {missing} read back empty; the mosaic is incomplete")
    log("NAIP: building JPEG overviews")
    with rasterio.Env(COMPRESS_OVERVIEW="JPEG", PHOTOMETRIC_OVERVIEW="YCBCR",
                      JPEG_QUALITY_OVERVIEW=str(quality), INTERLEAVE_OVERVIEW="PIXEL",
                      GDAL_NUM_THREADS="ALL_CPUS", GDAL_CACHEMAX=4096, BIGTIFF_OVERVIEW="YES"):
        with rasterio.open(part, "r+") as ds:
            ds.build_overviews([2, 4, 8, 16, 32, 64, 128], Resampling.average)
            ds.update_tags(ns="rio_overview", resampling="average")
    part.replace(dst)
    prog_path.unlink(missing_ok=True)
    log(f"NAIP mosaic complete: {dst.name}, {dst.stat().st_size / 1e9:.1f} GB, "
        f"{(time.time() - t0) / 60:.0f} min")
    return dst


# Work tiles for a region's NAIP: each NAIP box is cut into near-square
# pieces of about NAIP_TILE_DEG, each built as its own mosaic. A region is
# ~70,000 km² and ~16 hours of streaming; as one file a failure late in the run
# would cost hours and the file would pass 100 GB. A tile is ~500 km², about
# seven minutes, and a rerun skips every tile already on disk.
NAIP_TILE_DEG = 0.25


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def naip_work_tiles(region: dict) -> list[dict]:
    """The region's NAIP work tiles, box by box, south to north and west to
    east, leaving out any tile wholly inside a mosaic it reuses."""
    reuse = region.get("naip_reuse", [])

    def covered(t):
        return any(r["west"] <= t[0] and r["south"] <= t[1]
                   and t[2] <= r["east"] and t[3] <= r["north"] for r in reuse)

    out = []
    for b in region["naip_boxes"]:
        w, s_, e, n = b["west"], b["south"], b["east"], b["north"]
        nx = max(1, math.ceil((e - w) / NAIP_TILE_DEG - 1e-9))
        ny = max(1, math.ceil((n - s_) / NAIP_TILE_DEG - 1e-9))
        dx, dy = (e - w) / nx, (n - s_) / ny
        for j in range(ny):
            for i in range(nx):
                t = (round(w + i * dx, 6), round(s_ + j * dy, 6),
                     round(w + (i + 1) * dx, 6), round(s_ + (j + 1) * dy, 6))
                if not covered(t):
                    out.append({"box": b["name"], "id": f"{_slug(b['name'])}_{j:02d}_{i:02d}",
                                "bbox": t})
    return out


def write_naip_vrt(slug: str, region: dict, res: float = NAIP_RES) -> Path:
    """interim/<slug>/imagery_naip_5070.vrt: every built work tile, plus the
    mosaics the region reuses, as one 3-band mosaic.

    Every NAIP mosaic here is EPSG:5070 at 0.6 m with its corner snapped to a
    multiple of 0.6 m, so they share one grid and the VRT only places them:
    no resampling. GDAL serves a VRT's decimated reads from its sources'
    overviews, so the tiler and the panorama read it as they read one file."""
    from xml.sax.saxutils import escape
    src_dir = DATA / "interim" / slug / "naip"
    paths = [DATA / r["mosaic"] for r in region.get("naip_reuse", [])
             if (DATA / r["mosaic"]).exists()]
    paths += sorted(p for p in src_dir.glob("*.tif") if not p.name.endswith(".part.tif"))
    if not paths:
        raise RuntimeError(f"no NAIP mosaics for {slug}")
    info = []
    for p in paths:
        with rasterio.open(p) as ds:
            if ds.crs.to_epsg() != 5070 or abs(ds.res[0] - res) > 1e-9:
                raise RuntimeError(f"{p.name}: {ds.crs} @ {ds.res}, expected EPSG:5070 @ {res}")
            info.append((p, ds.bounds, ds.width, ds.height, ds.crs))
    west = min(b.left for _, b, *_ in info)
    north = max(b.top for _, b, *_ in info)
    east = max(b.right for _, b, *_ in info)
    south = min(b.bottom for _, b, *_ in info)
    width, height = int(round((east - west) / res)), int(round((north - south) / res))
    bands = []
    for band, interp in ((1, "Red"), (2, "Green"), (3, "Blue")):
        srcs = []
        for p, b, w, h, _ in info:
            xo, yo = int(round((b.left - west) / res)), int(round((north - b.top) / res))
            srcs.append(
                f'    <ComplexSource>\n'
                f'      <SourceFilename relativeToVRT="0">{escape(str(p))}</SourceFilename>\n'
                f'      <SourceBand>{band}</SourceBand>\n'
                f'      <SrcRect xOff="0" yOff="0" xSize="{w}" ySize="{h}"/>\n'
                f'      <DstRect xOff="{xo}" yOff="{yo}" xSize="{w}" ySize="{h}"/>\n'
                f'      <NODATA>0</NODATA>\n'
                f'    </ComplexSource>\n')
        bands.append(f'  <VRTRasterBand dataType="Byte" band="{band}">\n'
                     f'    <NoDataValue>0</NoDataValue>\n'
                     f'    <ColorInterp>{interp}</ColorInterp>\n' + "".join(srcs)
                     + '  </VRTRasterBand>\n')
    dst = DATA / "interim" / slug / "imagery_naip_5070.vrt"
    dst.write_text(
        f'<VRTDataset rasterXSize="{width}" rasterYSize="{height}">\n'
        f'  <SRS>{escape(info[0][4].to_wkt())}</SRS>\n'
        f'  <GeoTransform>{west:.4f}, {res}, 0, {north:.4f}, 0, {-res}</GeoTransform>\n'
        + "".join(bands)
        # virtual overviews: GDAL answers them from the sources' own pyramids,
        # and they let readers that check for overviews (the panorama's
        # texture picker) use the VRT at coarse scales
        + '  <OverviewList resampling="average">2 4 8 16 32 64 128</OverviewList>\n'
        + '</VRTDataset>\n', encoding="utf-8")
    log(f"NAIP VRT {dst.name}: {len(paths)} mosaics, {width}x{height} @{res:g}m")
    return dst


def build_naip_region(slug: str, region: dict, res: float = NAIP_RES,
                      quality: int = NAIP_JPEG_QUALITY) -> list[Path]:
    """Build every NAIP work tile of a region into interim/<slug>/naip/.

    A tile already built is skipped, and one with no NAIP at all (open water,
    Canada) leaves an .empty marker so it is not searched again. A tile that
    fails is logged and the run moves on; rerunning retries it."""
    tiles = naip_work_tiles(region)
    out_dir = DATA / "interim" / slug / "naip"
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"NAIP region {slug}: {len(tiles)} work tiles in "
        f"{len(region['naip_boxes'])} boxes")
    built, failed, t0 = [], [], time.time()
    for k, t in enumerate(tiles, 1):
        dst = out_dir / f"{t['id']}.tif"
        empty = out_dir / f"{t['id']}.empty"
        if dst.exists() or empty.exists():
            if dst.exists():
                built.append(dst)
            continue
        log(f"NAIP work tile {k}/{len(tiles)}: {t['id']} ({t['box']}) {t['bbox']}, "
            f"{(time.time() - t0) / 3600:.1f} h elapsed")
        try:
            feats = search_naip(t["bbox"])
            if not feats:
                empty.write_text("no NAIP quads", encoding="utf-8")
                continue
            built.append(build_naip_mosaic(feats, t["bbox"], dst, res=res, quality=quality))
        except Exception as e:  # noqa: BLE001 — one tile must not stop the run
            failed.append(t["id"])
            log(f"NAIP work tile {t['id']} FAILED: {type(e).__name__}: {str(e)[:300]}")
    log(f"NAIP region {slug}: {len(built)} tiles on disk, {len(failed)} failed"
        + (f" ({', '.join(failed)}); rerun to retry" if failed else "")
        + f", {(time.time() - t0) / 3600:.1f} h")
    if built:
        write_naip_vrt(slug, region, res)
    return built


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slug", default="alpine-lakes")
    ap.add_argument("--source", choices=["s2", "naip", "both", "naip-region", "s2-composite"],
                    default="both")
    ap.add_argument("--s2-near-km", type=float, default=30.0,
                    help="s2-composite: 10 m out to this far past the core")
    ap.add_argument("--years", default="2023,2024")
    ap.add_argument("--naip-res", type=float, default=NAIP_RES)
    ap.add_argument("--naip-quality", type=int, default=NAIP_JPEG_QUALITY,
                    help="JPEG quality of the NAIP mosaic")
    args = ap.parse_args()

    aoi = json.loads((AOI_DIR / f"{args.slug}.json").read_text("utf-8"))
    buf = aoi["buffered"]
    core = aoi["core"]
    buf_bbox = (buf["west"], buf["south"], buf["east"], buf["north"])
    core_bbox = (core["west"], core["south"], core["east"], core["north"])
    interim = DATA / "interim" / args.slug
    raw = DATA / "raw" / args.slug
    raw.mkdir(parents=True, exist_ok=True)

    if args.source == "s2-composite":
        year = int(args.years.split(",")[0])
        # 10 m over the core and a margin; 60 m over the far field, which only
        # the panorama's distant levels see (they need ~150 m/texel)
        dlat = args.s2_near_km / 111.0
        dlon = dlat / math.cos(math.radians((core["south"] + core["north"]) / 2))
        near = (core["west"] - dlon, core["south"] - dlat,
                core["east"] + dlon, core["north"] + dlat)
        build_s2_composite(near, interim / "imagery_s2_5070.tif", S2_RES, year)
        build_s2_composite(buf_bbox, interim / "imagery_s2far_5070.tif", 60.0, year)
        (raw / "s2_source.json").write_text(json.dumps({
            "method": f"per-pixel median of clear looks (SCL), {year} {SEASON[0]}..{SEASON[1]}",
            "collection": "sentinel-2-l2a", "asset": "visual", "endpoint": EARTH_SEARCH,
            "near": {"bbox": near, "res_m": S2_RES}, "far": {"bbox": buf_bbox, "res_m": 60.0},
            "licence": "Copernicus Sentinel data - free and open",
            "attribution": f"Contains modified Copernicus Sentinel data {year}",
        }, indent=2), encoding="utf-8")
        return 0

    if args.source == "naip-region":
        build_naip_region(args.slug, aoi, res=args.naip_res, quality=args.naip_quality)
        return 0

    if args.source in ("s2", "both"):
        years = [int(y) for y in args.years.split(",")]
        date, feats = pick_s2_date(buf_bbox, years)
        build_s2_mosaic(feats, buf_bbox, raw / "s2_visual_utm.tif")
        (raw / "s2_source.json").write_text(json.dumps({
            "date": date, "collection": "sentinel-2-l2a", "asset": "visual",
            "endpoint": EARTH_SEARCH,
            "scenes": sorted(f["id"] for f in feats),
            "cloud_cover": {f["id"]: f["properties"].get("eo:cloud_cover")
                            for f in feats},
            "licence": "Copernicus Sentinel data — free and open",
            "attribution": f"Contains modified Copernicus Sentinel data {date[:4]}",
        }, indent=2), encoding="utf-8")

    if args.source in ("naip", "both"):
        feats = search_naip(core_bbox)
        build_naip_mosaic(feats, core_bbox, interim / "imagery_naip_5070.tif",
                          res=args.naip_res, quality=args.naip_quality)
        (raw / "naip_source.json").write_text(json.dumps({
            "collection": "naip", "endpoint": PC_SEARCH,
            "resolution_m": args.naip_res, "native_resolution_m": 0.6,
            "jpeg_quality": args.naip_quality,
            "quads": len(feats),
            "licence": "Public domain (USDA NAIP, US Government work)",
            "attribution": "USDA National Agriculture Imagery Program",
        }, indent=2), encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
