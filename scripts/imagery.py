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

ROOT = Path(__file__).resolve().parents[1]

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
NAIP_RES = 2.0          # native is 0.6 m; see pick_naip_resolution() for why

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
LOG_PATH = ROOT / "data" / "imagery.log"


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
# NAIP
# --------------------------------------------------------------------------

_TOKENS: dict = {}
_TOKEN_LOCK = threading.Lock()


def pc_token(collection: str, force: bool = False) -> str:
    """A currently-valid SAS token, refreshed before it expires.

    These tokens last about an hour, and a full NAIP mosaic takes longer than
    that. Fetching one at the start and reusing it meant the back third of the
    run got HTTP 403 — 63 of 187 quads — and because each quad had its own
    except clause the job reported success with a third of the AOI missing.
    """
    with _TOKEN_LOCK:
        tok, exp = _TOKENS.get(collection, (None, 0.0))
        if tok and not force and time.time() < exp:
            return tok
        r = requests.get(PC_SAS.format(collection=collection),
                         headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        tok = r.json().get("token")
        if not tok:
            raise RuntimeError(f"no SAS token in response: {r.text[:200]}")
        # refresh 10 minutes early rather than trusting the stated expiry
        _TOKENS[collection] = (tok, time.time() + 50 * 60)
        log(f"SAS token for {collection} refreshed")
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
        dt = f["properties"]["datetime"]
        month = int(dt[5:7])
        summer = 0 if 6 <= month <= 9 else 1        # summer first
        return (summer, -int(dt[:4]))

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


def build_naip_mosaic(feats: list, bbox, dst: Path, res: float = NAIP_RES,
                      fill_gaps: bool = False) -> Path:
    """Warp every NAIP quad into one metric-CRS mosaic at `res`.

    Reading through the COGs' internal overviews is what makes this affordable:
    the source is 0.6 m and the target is 2 m, so GDAL fetches the 2.4 m
    overview level and never transfers the full-resolution pixels. Downloading
    these quads at native resolution would be ~40 GB for this AOI.
    """
    if dst.exists() and not fill_gaps:
        log(f"skip {dst.name} (present)")
        return dst
    from rasterio.warp import calculate_default_transform, reproject

    token = pc_token("naip")
    crs = "EPSG:5070"
    west, south, east, north = transform_bounds("EPSG:4326", crs, *bbox, densify_pts=21)
    west, south = math.floor(west / res) * res, math.floor(south / res) * res
    east, north = math.ceil(east / res) * res, math.ceil(north / res) * res
    width, height = int(round((east - west) / res)), int(round((north - south) / res))
    transform = rasterio.transform.from_origin(west, north, res, res)
    log(f"NAIP mosaic {width}x{height} @{res:g}m {crs} from {len(feats)} quads")

    # Deliberately UNCOMPRESSED, unlike every other raster the pipeline writes.
    #
    # Quads arrive in whatever order the network returns them and land in
    # scattered windows across a 37500x33500 destination. Writing that into a
    # DEFLATE TIFF means GDAL cannot flush a block until it can compress it in
    # order, so it pins every dirty block in the cache instead: measured 7.1 GB
    # resident with 88 kB actually on disk, heading for an out-of-memory kill.
    # Uncompressed blocks are evicted straight to disk, so memory stays flat.
    # The cost is ~3.8 GB on disk for this AOI, which is a fine trade — and the
    # tiler and the panorama both read it faster for being uncompressed.
    profile = {
        "driver": "GTiff", "dtype": "uint8", "count": 3, "nodata": 0,
        "width": width, "height": height, "crs": crs, "transform": transform,
        "tiled": True, "blockxsize": 512, "blockysize": 512,
        "BIGTIFF": "YES",
    }
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".part.tif")
    done = 0
    failed: list[str] = []
    # "w+", not "w": quads overlap, so each one has to read back what is
    # already there and fill only the gaps. A "w" dataset raises
    # "UnsupportedOperation: not readable" on the first read — and because each
    # quad is wrapped in its own try/except, that failed silently per quad and
    # produced an empty mosaic rather than an error.
    # Fetch and warp quads concurrently, write them serially.
    #
    # The blob host caps a *single connection* at ~0.8 MB/s, but that is not a
    # real bandwidth ceiling: measured aggregate was 2.9 MB/s over 4 connections
    # and 6.7 MB/s over 12. Sequentially this AOI would take hours. Only the
    # writes are serialised — a GDAL dataset must not be written from two
    # threads at once — and they are cheap next to the transfers.
    def fetch(f, retry: bool = True):
        """Download one quad and warp it into destination-grid pixels."""
        try:
            return _fetch_one(f, pc_token("naip"))
        except Exception as e:  # noqa: BLE001
            # Retry any read failure with a fresh token, not just a literal
            # "403": GDAL wraps the real status and most of these surface as
            # "RasterioIOError: Read failed. See previous exception", which a
            # 403-only test walked straight past. That left 23 quads missing.
            if retry:
                try:
                    return _fetch_one(f, pc_token("naip", force=True))
                except Exception:  # noqa: BLE001
                    raise e from None
            raise

    def _fetch_one(f, token):
        with rasterio.open("/vsicurl/" + f["assets"]["image"]["href"] + "?" + token) as s:
            qw, qs, qe, qn = transform_bounds(s.crs, crs, *s.bounds, densify_pts=21)
            c0 = max(0, int((qw - west) / res) - 1)
            r0 = max(0, int((north - qn) / res) - 1)
            c1 = min(width, int((qe - west) / res) + 2)
            r1 = min(height, int((north - qs) / res) + 2)
            if c1 <= c0 or r1 <= r0:
                return None, None
            win = Window(c0, r0, c1 - c0, r1 - r0)

            # Decimate on read, then warp the small array. Handing
            # `rasterio.band(...)` to reproject instead pulls every 0.6 m pixel
            # over the wire — 150 s per quad — because reproject reads the band
            # at full resolution. `out_shape` is what makes GDAL serve the
            # request from the COG's internal overview pyramid, which is the
            # entire reason these files are COGs.
            dec = max(1, int(res / s.res[0]))
            sh, sw = max(1, s.height // dec), max(1, s.width // dec)
            src_arr = s.read(indexes=[1, 2, 3], out_shape=(3, sh, sw),
                             resampling=Resampling.average)
            src_tf = s.transform * rasterio.Affine.scale(
                s.width / sw, s.height / sh)

            dest = np.zeros((3, r1 - r0, c1 - c0), dtype=np.uint8)
            for b in range(3):
                reproject(
                    source=src_arr[b], destination=dest[b],
                    src_transform=src_tf, src_crs=s.crs, src_nodata=0,
                    dst_transform=rasterio.windows.transform(win, transform),
                    dst_crs=crs, dst_nodata=0,
                    resampling=Resampling.bilinear, num_threads=2,
                )
            return win, dest

    t0 = time.time()
    # north-to-south, then west-to-east: completions still arrive out of order,
    # but the writes stay loosely raster-scan, which keeps the block cache
    # working on a moving band of the image rather than all of it
    ordered = sorted(feats, key=lambda f: (-f["bbox"][3], f["bbox"][0]))
    # Resume mode: reopen the existing mosaic and only fetch quads that left a
    # hole. These runs take over an hour, so losing all of it to a handful of
    # transient read failures — or to the machine sleeping — is not acceptable.
    if fill_gaps:
        tmp = dst
        log(f"fill-gaps: reopening {dst.name}, skipping quads already filled")
    with rasterio.open(tmp, "r+" if fill_gaps else "w+",
                       **({} if fill_gaps else profile)) as out:
        if fill_gaps:
            todo = []
            for f in ordered:
                qw, qs, qe, qn = f["bbox"][0], f["bbox"][1], f["bbox"][2], f["bbox"][3]
                bw, bs, be, bn = transform_bounds("EPSG:4326", crs, qw, qs, qe, qn,
                                                  densify_pts=21)
                c0 = max(0, int((bw - west) / res)); r0 = max(0, int((north - bn) / res))
                c1 = min(width, int((be - west) / res) + 1)
                r1 = min(height, int((north - bs) / res) + 1)
                if c1 <= c0 or r1 <= r0:
                    continue
                w0 = Window(c0, r0, c1 - c0, r1 - r0)
                # Fill *fraction*, not "has any data". NAIP quads overlap, so a
                # quad that failed outright still has most of its window
                # painted by its neighbours — `.any()` called every one of the
                # 187 already-done and found nothing to fix.
                band = out.read(1, window=w0)
                if band.size and (band > 0).mean() >= 0.99:
                    continue
                todo.append(f)
            log(f"fill-gaps: {len(todo)} of {len(ordered)} quads still empty")
            ordered = todo
        with cf.ThreadPoolExecutor(WORKERS) as ex:
            futures = {ex.submit(fetch, f): f for f in ordered}
            for fut in cf.as_completed(futures):
                f = futures[fut]
                try:
                    win, dest = fut.result()
                except Exception as e:  # noqa: BLE001
                    failed.append(f"{f['id']}: {type(e).__name__}: {e}")
                    if len(failed) <= 5:
                        log(f"  quad {f['id']} failed: {type(e).__name__}: {e}")
                    continue
                if win is not None:
                    prev = out.read(window=win)
                    fill = (prev.sum(axis=0) == 0) & (dest.sum(axis=0) > 0)
                    prev[:, fill] = dest[:, fill]
                    out.write(prev, window=win)
                done += 1
                if done % 10 == 0 or done == len(feats):
                    el = time.time() - t0
                    eta = el / max(done, 1) * (len(feats) - done)
                    log(f"  {done}/{len(feats)} quads, {len(failed)} failed, "
                        f"{el / 60:.1f} min elapsed, ~{eta / 60:.1f} min left")

    # A per-quad except is right for one bad quad and dangerous for a
    # systematic fault: opening the output "w" instead of "w+" made every
    # single read fail, and without this check that produced a blank mosaic and
    # a cheerful exit code.
    if done == 0 and not fill_gaps:
        raise RuntimeError(
            f"every one of {len(feats)} NAIP quads failed; first errors:\n  "
            + "\n  ".join(failed[:5]))
    if failed and len(failed) > len(feats) * 0.1:
        log(f"WARNING: {len(failed)}/{len(feats)} quads failed — mosaic has holes")
    if not fill_gaps:
        tmp.replace(dst)
    log(f"NAIP mosaic complete: {done}/{len(feats)} quads, {len(failed)} failed")
    return dst


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slug", default="alpine-lakes")
    ap.add_argument("--source", choices=["s2", "naip", "both"], default="both")
    ap.add_argument("--years", default="2023,2024")
    ap.add_argument("--naip-res", type=float, default=NAIP_RES)
    ap.add_argument("--fill-gaps", action="store_true",
                    help="reopen an existing NAIP mosaic and fetch only the "
                         "quads that are still empty")
    args = ap.parse_args()

    aoi = json.loads((ROOT / "data" / "aoi" / f"{args.slug}.json").read_text("utf-8"))
    buf = aoi["buffered"]
    core = aoi["core"]
    buf_bbox = (buf["west"], buf["south"], buf["east"], buf["north"])
    core_bbox = (core["west"], core["south"], core["east"], core["north"])
    interim = ROOT / "data" / "interim" / args.slug
    raw = ROOT / "data" / "raw" / args.slug
    raw.mkdir(parents=True, exist_ok=True)

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
                          res=args.naip_res, fill_gaps=args.fill_gaps)
        (raw / "naip_source.json").write_text(json.dumps({
            "collection": "naip", "endpoint": PC_SEARCH,
            "resolution_m": args.naip_res, "native_resolution_m": 0.6,
            "quads": len(feats),
            "licence": "Public domain (USDA NAIP, US Government work)",
            "attribution": "USDA National Agriculture Imagery Program",
        }, indent=2), encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
