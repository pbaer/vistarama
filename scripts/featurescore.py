"""View score V for map features: averaged along trails, at peaks and passes.

The viewer asks for these one feature at a time (hover a trail, peak or pass
and press V). Results are stored in `data/derived/<aoi>/feature_scores.json`,
keyed by OSM id, so they survive reloads and are shown again on hover without
recomputing.

Every stored score carries `version()`: the point score's own `version_tag`
(algorithm version plus a hash of its parameters) and this module's sampling
parameters. A score stored under any other version is treated as missing and
recomputed on request, so changing the method never leaves stale numbers on
the map.

* **Trails** are scored as whole trails, not OSM ways. OSM splits a named
  trail into many ways (median way length here is 216 m; Snow Lakes Trail is
  15 ways and 19.7 km), so the hovered way alone is usually a fragment. A
  trail is every way sharing the hovered way's name that connects to it
  through shared nodes, which keeps apart different trails that happen to
  share a name. Unnamed ways are scored on their own. Each way is sampled
  every TRAIL_SPACING_M along its full raw geometry (not the simplified tile
  copy), at the midpoint of each 50 m piece so every sample stands for the
  same length of trail. The result is the mean, with the per-sample values
  kept for later use. The score is stored once under the trail and aliased
  from each of its ways, so hovering any piece shows it.
* **Peaks** are scored at their summit: the OSM node snapped to the highest
  summit within PEAK_SNAP_M, exactly as the panorama snaps a click. OSM nodes
  routinely sit on a flank, and from Mount Stuart's node that hides half the
  peaks the summit sees.
* **Passes** are scored at the node itself. Snapping to a local maximum would
  climb out of the saddle onto one of its sides.

Scoring is CPU and memory-bandwidth bound: ~1.5 s a point on one core. A
persistent pool of POOL_WORKERS processes brings that to ~240 ms a point;
measured, more workers do not help (8, 16 and 24 all land within 10%), the
march being limited by memory bandwidth rather than cores.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TRAIL_SPACING_M = 50.0
PEAK_SNAP_M = 150.0
POOL_WORKERS = 8
# Themes that can be scored, and how their geometry becomes sample points.
SCORABLE = {"trails": "line", "peaks": "summit", "passes": "point"}

_pool: ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()
_store_lock = threading.Lock()
_raw_cache: dict = {}
_jobs: dict = {}                 # job id -> state dict
_jobs_lock = threading.Lock()


def _paths(slug: str) -> tuple[Path, Path]:
    base = ROOT / "data" / "interim" / slug
    return base / "dem_5070.tif", base / "landcover_5070.tif"


def version() -> str:
    """The point score's version plus this module's sampling parameters."""
    import vscore
    own = {"trail_spacing_m": TRAIL_SPACING_M, "peak_snap_m": PEAK_SNAP_M,
           "trail_sample": "midpoints", "trail_group": "same-name-connected",
           # the record layout: bump when records gain fields the viewer needs
           "record": 2, "elev_step_m": ELEV_STEP_M,
           "gain_threshold_m": GAIN_THRESHOLD_M}
    h = hashlib.sha1(json.dumps(own, sort_keys=True).encode()).hexdigest()
    return f"{vscore.version_tag()}-f{h[:6]}"


# ---- worker side ----------------------------------------------------------

def _warm(dem: str, lc: str) -> None:
    """Pool initializer: load the rasters once per worker process."""
    import vscore
    try:
        import panorama
        panorama._read_full(Path(dem))
        vscore.view_score(Path(dem), Path(lc), *_dem_centre(Path(dem)))
    except Exception:  # noqa: BLE001 — a cold first job is fine too
        pass


def _dem_centre(dem: Path) -> tuple[float, float]:
    import rasterio
    from pyproj import Transformer
    with rasterio.open(dem) as s:
        b = s.bounds
        t = Transformer.from_crs(s.crs, "EPSG:4326", always_xy=True)
        lon, lat = t.transform((b.left + b.right) / 2, (b.bottom + b.top) / 2)
    return lat, lon


def _score_point(args: tuple) -> float:
    import vscore
    dem, lc, lat, lon = args
    return float(vscore.view_score(Path(dem), Path(lc), lat, lon)["V"])


def _get_pool(slug: str) -> ProcessPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            dem, lc = _paths(slug)
            _pool = ProcessPoolExecutor(POOL_WORKERS, initializer=_warm,
                                        initargs=(str(dem), str(lc)))
        return _pool


# ---- geometry -------------------------------------------------------------

def _raw_feature(slug: str, theme: str, fid: str) -> dict | None:
    key = (slug, theme)
    idx = _raw_cache.get(key)
    if idx is None:
        src = ROOT / "data" / "raw" / "osm" / f"{slug}_{theme}.geojson"
        gj = json.loads(src.read_text(encoding="utf-8"))
        idx = {f.get("id"): f for f in gj.get("features", []) if f.get("id")}
        _raw_cache[key] = idx
    return idx.get(fid)


_groups_cache: dict = {}


def trail_group(slug: str, fid: str) -> list[str]:
    """The OSM ways making up the trail `fid` belongs to: same name, connected
    through shared nodes (union-find over shared vertices). Just [fid] for an
    unnamed way."""
    groups = _groups_cache.get(slug)
    if groups is None:
        _raw_feature(slug, "trails", fid)            # load the index
        idx = _raw_cache[(slug, "trails")]
        parent: dict = {}

        def find(a):
            while parent.setdefault(a, a) != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        by_vertex: dict = {}
        for wid, f in idx.items():
            name = (f.get("properties") or {}).get("name")
            if not name:
                continue
            parent.setdefault(wid, wid)
            for line in _lines(f["geometry"]):
                for c in line:
                    k = (name, round(c[0], 7), round(c[1], 7))
                    other = by_vertex.setdefault(k, wid)
                    if other != wid:
                        parent[find(wid)] = find(other)
        members: dict = {}
        for wid in list(parent):
            members.setdefault(find(wid), []).append(wid)
        groups = {}
        for ms in members.values():
            ms = sorted(ms)
            for wid in ms:
                groups[wid] = ms
        _groups_cache[slug] = groups
    return groups.get(fid, [fid])


def trail_groups(slug: str) -> dict[str, list[str]]:
    """Every grouped trail way's members (way id -> sorted member ids), as
    trail_group builds them. process.py writes these into the map tiles so the
    viewer can highlight a whole trail on hover; sharing this one function
    keeps what is highlighted identical to what is scored."""
    trail_group(slug, "")                          # builds the cache
    return _groups_cache[slug]


def group_key(members: list[str]) -> str:
    """Stable id for a trail: the way id itself, or a hash of its members."""
    return _group_key(members)


def _group_key(members: list[str]) -> str:
    if len(members) == 1:
        return members[0]
    return "trail:" + hashlib.sha1("|".join(members).encode()).hexdigest()[:12]


def _lines(geom: dict) -> list[list]:
    if geom["type"] == "LineString":
        return [geom["coordinates"]]
    if geom["type"] == "MultiLineString":
        return geom["coordinates"]
    raise ValueError(f"cannot sample a {geom['type']} as a trail")


def trail_samples(geom: dict, spacing: float = TRAIL_SPACING_M):
    """Points at the middle of each `spacing` piece along a line, plus its
    geodesic length. A line shorter than `spacing` gets one sample, halfway."""
    from pyproj import Geod
    g = Geod(ellps="WGS84")
    pts = []
    total = 0.0
    for line in _lines(geom):
        lons = [c[0] for c in line]
        lats = [c[1] for c in line]
        az, _baz, seg = g.inv(lons[:-1], lats[:-1], lons[1:], lats[1:])
        seg = list(seg) if hasattr(seg, "__iter__") else [seg]
        az = list(az) if hasattr(az, "__iter__") else [az]
        length = float(sum(seg))
        total += length
        if length <= 0:
            continue
        n = max(1, int(length // spacing))
        # midpoints of n equal-length pieces when the line divides evenly;
        # otherwise the pieces are `spacing` long and the remainder is shared
        # out at both ends, so the samples stay centred on the line
        step = spacing if length >= spacing else length
        first = (length - (n - 1) * step) / 2.0
        targets = [first + i * step for i in range(n)]
        acc, j = 0.0, 0
        for t in targets:
            while j < len(seg) - 1 and acc + seg[j] < t:
                acc += seg[j]
                j += 1
            lon, lat, _ = g.fwd(lons[j], lats[j], az[j], t - acc)
            pts.append((lat, lon))
    return pts, total


# ---- elevation ------------------------------------------------------------
#
# Elevation stats ride along with every score, for the ranked list: min and
# max over the trail, and how much climbing it holds. Sampled from the DEM
# every ELEV_STEP_M (a third of a cell, so bilinear interpolation is smooth),
# and climbing is counted with a GAIN_THRESHOLD_M hysteresis: a rise only
# counts once the profile has climbed that far from its last turning point,
# so 30 m DEM ripple on flat ground does not add up to phantom gain.
#
# Gain depends on direction, and a branching trail has no single direction,
# so each way contributes its climb in whichever direction climbs more: the
# trail as you would hike it uphill, segment by segment.

ELEV_STEP_M = 10.0
GAIN_THRESHOLD_M = 5.0
_dem_cache: dict = {}


def _dem(slug: str):
    """(dem, invalid mask, inverse affine, lon/lat -> DEM CRS transformer)."""
    hit = _dem_cache.get(slug)
    if hit is None:
        import numpy as np
        import panorama
        from pyproj import Transformer
        dem, crs, tf, _b, nodata = panorama._read_full(_paths(slug)[0])
        bad = ~np.isfinite(dem) | (dem <= panorama.ELEV_FLOOR_M)
        if nodata is not None:
            bad |= dem == nodata
        hit = (dem, bad, ~tf, Transformer.from_crs("EPSG:4326", crs, always_xy=True))
        _dem_cache[slug] = hit
    return hit


def _dem_z(slug: str, xs, ys):
    """Bilinear DEM height at projected coordinates, NaN off the data."""
    import numpy as np
    import panorama
    dem, bad, inv, _t = _dem(slug)
    fc, fr = inv * (np.asarray(xs, float), np.asarray(ys, float))
    z, ok = panorama._bilinear(dem, np.asarray(fc), np.asarray(fr), bad)
    return np.where(ok, z, np.nan)


def _project(slug: str, lons, lats):
    import numpy as np
    X, Y = _dem(slug)[3].transform(np.asarray(lons, float), np.asarray(lats, float))
    return np.asarray(X), np.asarray(Y)


def _climb(z) -> tuple[float, float]:
    up = down = 0.0
    base = None
    for v in z:
        if v != v:                       # NaN: off the DEM
            continue
        if base is None:
            base = v
        elif v - base >= GAIN_THRESHOLD_M:
            up += v - base
            base = v
        elif base - v >= GAIN_THRESHOLD_M:
            down += base - v
            base = v
    return up, down


def elevation_stats(slug: str, geoms: list[dict]) -> dict:
    """Min, max and uphill gain along one or more line geometries."""
    import numpy as np
    lo, hi, gain = math.inf, -math.inf, 0.0
    for geom in geoms:
        for line in _lines(geom):
            X, Y = _project(slug, [c[0] for c in line], [c[1] for c in line])
            # densify every ELEV_STEP_M in the equal-area grid, whose
            # distances are within a fraction of a percent of ground ones here
            seg = np.hypot(np.diff(X), np.diff(Y))
            n = np.maximum(1, np.ceil(seg / ELEV_STEP_M).astype(np.int64))
            si = np.repeat(np.arange(seg.size), n)
            within = np.arange(int(n.sum())) - np.repeat(np.cumsum(n) - n, n)
            t = (within + 1) / n[si]
            xs = np.concatenate([[X[0]], X[si] + (X[si + 1] - X[si]) * t])
            ys = np.concatenate([[Y[0]], Y[si] + (Y[si + 1] - Y[si]) * t])
            z = _dem_z(slug, xs, ys)
            if np.isfinite(z).any():
                lo = min(lo, float(np.nanmin(z)))
                hi = max(hi, float(np.nanmax(z)))
                up, down = _climb(z.tolist())
                gain += max(up, down)
    return {"ele_min_m": round(lo, 1) if lo < math.inf else None,
            "ele_max_m": round(hi, 1) if hi > -math.inf else None,
            "gain_m": round(gain)}


def _osm_ele(props: dict) -> float | None:
    try:
        return float(str(props.get("ele", "")).split()[0].replace(",", ""))
    except (ValueError, IndexError):
        return None


# ---- scoring backends -----------------------------------------------------

_gpu = None
_gpu_failed = False
_gpu_lock = threading.Lock()


def _gpu_scorer(slug: str):
    """The GPU scorer, created on first use, or None without a usable GPU."""
    global _gpu, _gpu_failed
    if _gpu is None and not _gpu_failed:
        try:
            import vscore_gpu
            dem, lc = _paths(slug)
            _gpu = vscore_gpu.GpuScorer(dem, lc)
        except Exception:  # noqa: BLE001 — no CuPy, no card, or a driver issue
            _gpu_failed = True
    return _gpu


def score_points(slug: str, pts: list, progress=None) -> list[float]:
    """V at each (lat, lon). The GPU when there is one (~1 ms a point), else
    the process pool (~240 ms a point). The two agree to ~1e-4."""
    g = _gpu_scorer(slug)
    if g is not None:
        with _gpu_lock:                  # one batch on the card at a time
            vals = g.score(pts, progress=progress)
        if any(v is None for v in vals):
            raise ValueError("a sample point is off the DEM")
        return vals
    dem, lc = _paths(slug)
    pool = _get_pool(slug)
    futs = {pool.submit(_score_point, (str(dem), str(lc), la, lo)): i
            for i, (la, lo) in enumerate(pts)}
    vals = [0.0] * len(pts)
    done = 0
    for f in as_completed(futs):
        vals[futs[f]] = f.result()
        done += 1
        if progress:
            progress(done, len(pts))
    return vals


# ---- items and records ----------------------------------------------------
#
# An item is one thing to score: a whole trail, a peak or a pass. Building it
# turns the feature into sample points; finishing it turns their V values
# into the stored record. The V key and the bulk job (score_all.py) both go
# through these, so their records are identical.

def build_item(slug: str, theme: str, fid: str) -> dict:
    feat = _raw_feature(slug, theme, fid)
    if feat is None:
        raise ValueError(f"{fid} is not in the raw {theme} data")
    props = feat.get("properties") or {}
    kind = SCORABLE[theme]
    item = {"slug": slug, "theme": theme, "fid": fid, "name": props.get("name"),
            "kind": kind}
    if kind == "line":
        members = trail_group(slug, fid)
        geoms = [_raw_feature(slug, theme, w)["geometry"] for w in members]
        pts, length, counts = [], 0.0, []
        for g in geoms:
            p, ln = trail_samples(g)
            pts += p
            length += ln
            counts.append(len(p))       # which samples belong to which way
        xs = [c[0] for g in geoms for line in _lines(g) for c in line]
        ys = [c[1] for g in geoms for line in _lines(g) for c in line]
        item.update({"key": _group_key(members), "members": members,
                     "geoms": geoms, "points": pts, "length_m": length,
                     "way_counts": counts,
                     "bbox": [min(xs), min(ys), max(xs), max(ys)]})
    else:
        lon, lat = feat["geometry"]["coordinates"][:2]
        moved = 0.0
        if kind == "summit":
            import panorama
            d, crs, tf, _b, nd = panorama._read_full(_paths(slug)[0])
            s = panorama.snap_to_local_max(d, tf, crs, lat, lon, nd, PEAK_SNAP_M)
            if s["snapped"]:
                lat, lon, moved = s["lat"], s["lon"], s["moved_m"]
        item.update({"key": fid, "points": [(lat, lon)], "snapped_m": moved,
                     "ele_osm": _osm_ele(props)})
    return item


def finish_record(item: dict, vals: list[float], elapsed_s: float) -> dict:
    rec = {"id": item["key"], "theme": item["theme"], "name": item["name"],
           "version": version(), "eye_m": 2.0, "kind": "line"
           if item["kind"] == "line" else "point"}
    if item["kind"] == "line":
        pts = item["points"]
        srt = sorted(vals)
        best = max(range(len(vals)), key=lambda i: vals[i])
        rec.update({
            "members": item["members"], "n_ways": len(item["members"]),
            "length_m": round(item["length_m"], 1),
            "spacing_m": TRAIL_SPACING_M, "n": len(vals),
            "V": sum(vals) / len(vals), "V_median": srt[len(srt) // 2],
            "V_min": srt[0], "V_max": srt[-1],
            "best": {"lat": pts[best][0], "lon": pts[best][1], "V": vals[best]},
            "bbox": [round(x, 6) for x in item["bbox"]],
            "samples": [[round(la, 6), round(lo, 6), round(v, 4)]
                        for (la, lo), v in zip(pts, vals)],
        })
        if len(item["members"]) > 1:
            rec["way_stats"] = way_stats(item["members"], item["way_counts"],
                                         rec["samples"])
        rec.update(elevation_stats(item["slug"], item["geoms"]))
    else:
        lat, lon = item["points"][0]
        X, Y = _project(item["slug"], [lon], [lat])
        z = _dem_z(item["slug"], X, Y)
        dem_ele = float(z[0]) if z[0] == z[0] else None
        ele = item["ele_osm"] if item["ele_osm"] is not None else dem_ele
        rec.update({"V": vals[0], "lat": lat, "lon": lon,
                    "snapped_m": item["snapped_m"],
                    "ele_m": None if ele is None else round(ele, 1),
                    "ele_dem_m": None if dem_ele is None else round(dem_ele, 1),
                    "ele_min_m": None if ele is None else round(ele, 1),
                    "ele_max_m": None if ele is None else round(ele, 1)})
    rec["computed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rec["elapsed_s"] = round(elapsed_s, 2)
    return rec


def way_stats(members: list[str], counts: list[int], samples: list) -> dict:
    """Per-way view-score stats of a multi-way trail: way id -> {n, V, best}.

    `samples` are the trail's [lat, lon, V] in member order and `counts` how
    many each member contributed, which is how trail_samples generated them.
    A way with no samples (zero length) is left out.
    """
    if sum(counts) != len(samples):
        raise ValueError(f"{sum(counts)} samples by way, {len(samples)} stored")
    out, k = {}, 0
    for wid, n in zip(members, counts):
        seg = samples[k:k + n]
        k += n
        if not seg:
            continue
        b = max(seg, key=lambda x: x[2])
        out[wid] = {"n": n, "V": round(sum(x[2] for x in seg) / n, 4),
                    "best": {"lat": b[0], "lon": b[1], "V": b[2]}}
    return out


def add_way_stats(slug: str) -> dict:
    """Fill in `way_stats` on stored multi-way trail records that predate it.

    No rescoring: trail_samples is deterministic, so re-running it per member
    gives exactly how many of the stored samples each way contributed, and
    the stored samples are split accordingly. A record whose per-way counts do
    not add up to its stored samples is left alone and reported.
    """
    p = _store_path(slug)
    done = skipped = 0
    bad = []
    with _store_lock, _FileLock(p.with_suffix(".lock")):
        data = _load(slug)
        ver = version()
        for key, rec in data["scores"].items():
            if (rec.get("kind") != "line" or rec.get("version") != ver
                    or rec.get("n_ways", 1) < 2 or "way_stats" in rec):
                skipped += 1
                continue
            counts = [len(trail_samples(_raw_feature(slug, "trails", w)["geometry"])[0])
                      for w in rec["members"]]
            try:
                rec["way_stats"] = way_stats(rec["members"], counts, rec["samples"])
                done += 1
            except ValueError as exc:
                bad.append((key, str(exc)))
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, p)
    return {"added": done, "skipped": skipped, "mismatched": bad}


def plan(slug: str, core_only: bool = True) -> list[tuple[str, str]]:
    """Every scorable item in the AOI as (theme, fid): peaks and passes inside
    the core box, and every trail with at least one way inside it, once per
    trail rather than once per way."""
    from shapely.geometry import box, shape
    aoi = json.loads((ROOT / "data" / "aoi" / f"{slug}.json").read_text("utf-8"))
    c = aoi["core"]
    core = box(c["west"], c["south"], c["east"], c["north"])
    out: list[tuple[str, str]] = []
    for theme in ("peaks", "passes"):
        _raw_feature(slug, theme, "")
        for fid, f in _raw_cache[(slug, theme)].items():
            if f["geometry"]["type"] != "Point":
                continue
            if not core_only or core.contains(shape(f["geometry"])):
                out.append((theme, fid))
    _raw_feature(slug, "trails", "")
    seen = set()
    for fid, f in _raw_cache[(slug, "trails")].items():
        if core_only and not core.intersects(shape(f["geometry"])):
            continue
        key = _group_key(trail_group(slug, fid))
        if key not in seen:
            seen.add(key)
            out.append(("trails", fid))
    return out


# ---- store ----------------------------------------------------------------
#
# One JSON file per AOI. Records live under their own key (a way or node id,
# or "trail:<hash>" for a multi-way trail) and each way of a trail is an alias
# of it. Written by the server (V key) and by score_all.py in another process,
# so writes take a lock directory, re-read the file, merge, and replace it
# atomically.

def _store_path(slug: str) -> Path:
    return ROOT / "data" / "derived" / slug / "feature_scores.json"


def _load(slug: str) -> dict:
    p = _store_path(slug)
    if not p.exists():
        return {"format": 1, "scores": {}, "aliases": {}}
    data = json.loads(p.read_text(encoding="utf-8"))
    data.setdefault("aliases", {})
    return data


class _FileLock:
    """Cross-process lock: mkdir is atomic on every platform. A lock older
    than STALE_S is assumed to belong to a crashed writer and is broken."""
    STALE_S = 120

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        while True:
            try:
                os.mkdir(self.path)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.STALE_S:
                        os.rmdir(self.path)
                        continue
                except FileNotFoundError:
                    continue
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            os.rmdir(self.path)
        except FileNotFoundError:
            pass


def save_many(slug: str, records: list[dict]) -> None:
    p = _store_path(slug)
    with _store_lock, _FileLock(p.with_suffix(".lock")):
        data = _load(slug)
        for record in records:
            data["scores"][record["id"]] = record
            # a trail's score lives once, under the trail; each of its ways
            # points at it, so hovering any piece of the trail finds it
            for wid in record.get("members", []):
                if wid != record["id"]:
                    data["aliases"][wid] = record["id"]
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, p)        # never leave a half-written store behind


_summary_cache: dict = {}


def summaries(slug: str) -> dict:
    """Every stored score valid under the current version, without per-sample
    detail: `items` by record key, `ids` mapping every OSM way or node id to
    its record key, and how many records were dropped as stale. Cached on the
    store's mtime, since the ranked list polls it while a bulk job runs."""
    p = _store_path(slug)
    stamp = (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
    hit = _summary_cache.get(slug)
    if hit and hit[0] == stamp:
        return hit[1]
    ver = version()
    data = _load(slug)
    items, ids, stale = {}, {}, 0
    for key, rec in data.get("scores", {}).items():
        if rec.get("version") != ver:
            stale += 1
            continue
        items[key] = {k: v for k, v in rec.items() if k not in ("samples", "members")}
        ids[key] = key
    for wid, key in data.get("aliases", {}).items():
        if key in items:
            ids[wid] = key
    out = {"version": ver, "items": items, "ids": ids, "stale": stale}
    _summary_cache[slug] = (stamp, out)
    return out


def stored(slug: str, fid: str) -> dict | None:
    data = _load(slug)
    key = data["aliases"].get(fid, fid)
    rec = data.get("scores", {}).get(key)
    return rec if rec and rec.get("version") == version() else None


# ---- jobs (the V key) -----------------------------------------------------

def _run(job: dict, slug: str, theme: str, fid: str) -> None:
    t0 = time.time()
    try:
        item = build_item(slug, theme, fid)
        job["total"] = len(item["points"])

        def progress(done, total):
            job["done"] = done
        vals = score_points(slug, item["points"], progress)
        job["done"] = job["total"]
        rec = finish_record(item, vals, time.time() - t0)
        save_many(slug, [rec])
        job["record"] = _summary(rec)
        job["state"] = "done"
    except Exception as exc:  # noqa: BLE001
        job["state"] = "error"
        job["error"] = str(exc)


def start(slug: str, theme: str, fid: str) -> dict:
    """Score a feature, unless a valid score is stored or one is running."""
    if theme not in SCORABLE:
        raise ValueError(f"cannot score {theme}")
    rec = stored(slug, fid)
    if rec:
        return {"state": "done", "record": _summary(rec)}
    # every way of a trail maps to the same job, so pressing V on two pieces
    # of one trail does not score it twice
    key = _group_key(trail_group(slug, fid)) if SCORABLE[theme] == "line" else fid
    with _jobs_lock:
        for jid, j in _jobs.items():
            if j["key"] == key and j["state"] == "running":
                return {"job": jid, **_public(j)}
        jid = uuid.uuid4().hex[:12]
        job = {"fid": fid, "key": key, "state": "running", "done": 0,
               "total": None, "started": time.time()}
        _jobs[jid] = job
    threading.Thread(target=_run, args=(job, slug, theme, fid), daemon=True).start()
    return {"job": jid, **_public(job)}


def _summary(rec: dict) -> dict:
    """A stored record without its per-sample detail. `ways` lists the trail's
    OSM ways, so the viewer can attach the score to every piece at once."""
    out = {k: v for k, v in rec.items() if k not in ("samples", "members")}
    if rec.get("members"):
        out["ways"] = rec["members"]
    return out


def _public(job: dict) -> dict:
    return {k: job[k] for k in ("fid", "state", "done", "total", "record", "error")
            if k in job}


def status(jid: str) -> dict | None:
    job = _jobs.get(jid)
    return None if job is None else {"job": jid, **_public(job)}
