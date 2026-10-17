"""The view score `V` at a single viewpoint.

Implements Part 1 of docs/visibility-metric.md:

    V = sum over visible terrain of  dOmega * c(class) * w(d) * d_km^gamma

computed by one outward DEM march per azimuth. Along each ray the running
maximum elevation angle is the horizon so far; whenever a sample rises above
it, the elevation band it sweeps is newly visible terrain at that sample's
distance, with solid angle dAlpha * (sin phi_new - sin phi_old). That is exact,
not a small-angle approximation, and it handles occlusion (hidden ground never
raises the max) and foreshortening (a cliff sweeps a tall band at one
distance, a plain at grazing incidence sweeps almost nothing) without a single
surface normal.

* `gamma` blends the two axes the doc describes: 0 is pure dominance (solid
  angle, rewards enclosure), 2 is pure extent (foreshortened visible area),
  1 is the balanced starting point.
* `w(d)` has two jobs only: ignore everything nearer than NEAR_CUTOFF_M, and
  taper with distance as exp(-d / D_HAZE_M).
* `c(class)` weights the land cover *at the visible terrain*, not at the
  observer.

The march uses the panorama's geometry (same DEM, same bilinear sampling, same
curvature and refraction term, same observer-centred azimuthal frame), so the
score describes exactly the terrain the 3D view draws.

Besides V, the result carries every intermediate the panel shows: the same
sum at gamma = 0 and 2, the score without haze, and V and solid angle broken
down by distance band, by land-cover class and by azimuth, plus the by-products
the doc lists (sky fraction, horizon profile, look-down, sightline, visible
relief) and how much of the march the data edge cut short.
"""
from __future__ import annotations

import math
import time
from pathlib import Path

import numpy as np
from pyproj import Transformer

import panorama

# ---- parameters (docs/visibility-metric.md, "Parameter inventory") ----------

# Extent vs dominance. The doc's worked comparison ranks col > summit ~ distant
# range > cirque >> clearing > plain at 1.0, and says to tune within 0.8-1.3.
GAMMA = 1.0

# Atmospheric taper. 60 km is the doc's starting value (50-80 km range). This
# is the score's own constant, deliberately independent of the panorama's haze
# slider: a score that moved with a display setting would not be comparable
# between points.
D_HAZE_M = 60_000.0

# Ignore the ground underfoot: the DEM cell you stand on has no meaningful
# local geometry. Terrain nearer than this still *occludes*; it just earns
# nothing.
NEAR_CUTOFF_M = 300.0

# How far each ray is marched. At gamma = 1 the per-steradian weight
# d * exp(-d / 60 km) peaks at 60 km and is still a third of that peak at
# 150 km, so stopping much short of this would cut real distant ranges off.
# Rays also stop at the DEM's edge; the result says how many did.
RANGE_M = 150_000.0

# Observer height above the ground. A standing person, matching the panorama's
# default eye height, and fixed rather than tied to the slider for the same
# reason as the haze.
EYE_HEIGHT_M = panorama.EYE_HEIGHT_M

# Rays and radial step, set by convergence. Against a 2880-ray, 15 m
# reference at four test points (Stuart summit, Colchuck Lake shore, Icicle
# Creek valley floor, Snoqualmie Pass), 720 x 30 m lands within 0.6% at all
# four. Ray count barely matters (360 is within 1.2%); halving the step moves
# V by a steady 0.4%, and doubling it to 60 m by up to 3.5%. The step is the
# DEM's own cell size, so finer would only re-read interpolated values.
N_AZ = 720
STEP_M = 30.0

# Positions are transformed exactly at these knots along each ray and
# interpolated linearly between them. In the observer-centred AEQD frame a ray
# is a straight line; its image in EPSG:5070 bends only by the grid
# convergence, which over 1 km is far below a millimetre.
KNOT_M = 1000.0

# Land-cover interest weights, by ESA WorldCover class, in the doc's four
# coarse groups plus built-up. The doc ranks glaciers above snow and ice, but
# there is no glacier raster here yet (RGI needs a login, OSM glaciers are
# vectors), so WorldCover's snow-and-ice class stands in for both.
CLASS_WEIGHTS = {
    70: ("Snow and ice", 1.0),
    60: ("Bare / sparse", 1.0),
    80: ("Permanent water", 1.0),
    100: ("Moss and lichen", 0.8),
    30: ("Grassland", 0.6),
    20: ("Shrubland", 0.6),
    90: ("Herbaceous wetland", 0.6),
    10: ("Tree cover", 0.3),
    40: ("Cropland", 0.3),
    95: ("Mangroves", 0.3),
    50: ("Built-up", -0.5),
}
# Where the land-cover raster has no class (outside its footprint).
UNKNOWN_WEIGHT = 0.5

# Bands for the distance breakdown, km.
DIST_BANDS_KM = (0.3, 1, 3, 10, 30, 60, 100, 150)

# Rays are marched this many azimuths at a time, to bound memory: one chunk is
# CHUNK_AZ x (RANGE_M / STEP_M) samples in several float64 arrays.
CHUNK_AZ = 120


def _class_lut() -> np.ndarray:
    lut = np.full(256, UNKNOWN_WEIGHT, dtype=np.float64)
    for code, (_name, w) in CLASS_WEIGHTS.items():
        lut[code] = w
    return lut


def view_score(dem_path: Path, landcover_path: Path | None,
               lat: float, lon: float, *, gamma: float = GAMMA,
               n_az: int = N_AZ, step_m: float = STEP_M,
               range_m: float = RANGE_M) -> dict:
    """V at (lat, lon), with the breakdown the viewer's score panel shows.

    The point is used as given: callers that want the snapped summit should
    pass the panorama's own (already snapped) coordinates.
    """
    from rasterio.enums import Resampling

    t0 = time.perf_counter()
    dem, dem_crs, dem_tf, dem_bounds, dem_nodata = panorama._read_full(dem_path)
    H, W = dem.shape
    dem_bad = ~np.isfinite(dem) | (dem <= panorama.ELEV_FLOOR_M)
    if dem_nodata is not None:
        dem_bad |= dem == dem_nodata

    lc = None
    if landcover_path and Path(landcover_path).exists():
        # the panorama's own land-cover read: nearest-neighbour onto the DEM
        # grid, cached, so the class at a hit is the one the 3D view colours
        lc, _ = panorama._read_window(Path(landcover_path), dem_bounds,
                                      dem.shape, Resampling.nearest)
    lut = _class_lut()

    inv = ~dem_tf
    to_dem = Transformer.from_crs("EPSG:4326", dem_crs, always_xy=True)
    ox, oy = to_dem.transform(lon, lat)
    oc, orr = inv * (ox, oy)
    gz, gok = panorama._bilinear(dem, np.array([oc]), np.array([orr]), dem_bad)
    if not bool(gok[0]):
        raise ValueError("viewpoint is outside the DEM or on a void")
    ground = float(gz[0])
    eye_z = ground + EYE_HEIGHT_M

    # ray geometry: azimuths x distances
    az = np.arange(n_az) * (360.0 / n_az)
    d = np.arange(1, int(range_m // step_m) + 1) * step_m
    n_d = d.size
    d_alpha = 2.0 * math.pi / n_az
    drop = (1.0 - panorama.REFRACTION_K) * d ** 2 / (2.0 * panorama.EARTH_R)
    d_km = d / 1000.0
    cutoff = d >= NEAR_CUTOFF_M
    haze = np.exp(-d / D_HAZE_M)
    w_d = np.where(cutoff, haze, 0.0)
    pow_g = d_km ** gamma
    pow_0 = np.ones_like(d_km)
    pow_2 = d_km ** 2

    # exact positions at knots, linear in between
    aeqd = (f"+proj=aeqd +lat_0={lat} +lon_0={lon} +x_0=0 +y_0=0 "
            f"+datum=WGS84 +units=m +no_defs")
    a2d = Transformer.from_crs(aeqd, dem_crs, always_xy=True)
    knots = np.arange(0.0, d[-1] + KNOT_M, KNOT_M)
    kk = np.minimum((d // KNOT_M).astype(np.int64), knots.size - 2)
    kf = (d - knots[kk]) / KNOT_M

    # accumulators
    v_az = np.zeros(n_az)                 # V per azimuth
    omega_az = np.zeros(n_az)             # visible solid angle beyond cutoff
    horizon_end = np.zeros(n_az)          # final running max, radians
    edge_km = np.full(n_az, np.nan)       # where the data ran out, if it did
    band_edges = np.asarray(DIST_BANDS_KM) * 1000.0
    n_band = len(DIST_BANDS_KM) - 1
    band_idx = np.clip(np.searchsorted(band_edges, d, side="right") - 1,
                       0, n_band - 1)
    v_band = np.zeros(n_band)
    om_band = np.zeros(n_band)
    alive_band = np.zeros(n_band)         # (ray, sample) pairs that had data
    n_in_band = np.bincount(band_idx, minlength=n_band) * float(n_az)
    v_class = {}
    om_class = {}
    dominance = extent = v_nohaze = om_weighted_c = 0.0
    lookdown = (math.inf, None, None)     # (angle rad, dist m, az deg)
    z_vis_min, z_vis_max = math.inf, -math.inf
    max_sight = 0.0
    hist_d = []                           # (distance, omega) for the median

    for a0 in range(0, n_az, CHUNK_AZ):
        a = az[a0:a0 + CHUNK_AZ]
        n = a.size
        ar = np.radians(a)
        kx = np.sin(ar)[:, None] * knots[None, :]
        ky = np.cos(ar)[:, None] * knots[None, :]
        X, Y = a2d.transform(kx.ravel(), ky.ravel())
        X = np.asarray(X).reshape(n, -1)
        Y = np.asarray(Y).reshape(n, -1)
        xs = X[:, kk] + (X[:, kk + 1] - X[:, kk]) * kf
        ys = Y[:, kk] + (Y[:, kk + 1] - Y[:, kk]) * kf
        fc, fr = inv * (xs.ravel(), ys.ravel())
        z, ok = panorama._bilinear(dem, np.asarray(fc), np.asarray(fr), dem_bad)
        z = z.reshape(n, n_d)
        ok = ok.reshape(n, n_d)

        # a ray ends at its first missing sample: there is no terrain beyond
        # the data edge to see, rather than terrain at some invented height
        alive = np.logical_and.accumulate(ok, axis=1)
        dead_at = np.where(alive.all(axis=1), -1, np.argmin(alive, axis=1))
        edge_km[a0:a0 + n] = np.where(dead_at >= 0, d[dead_at] / 1000.0, np.nan)

        phi = np.arctan2(z - drop[None, :] - eye_z, d[None, :])
        phi = np.where(alive, phi, -math.pi / 2)
        run = np.maximum.accumulate(phi, axis=1)
        prev = np.empty_like(run)
        prev[:, 0] = -math.pi / 2
        prev[:, 1:] = run[:, :-1]
        ds = np.sin(run) - np.sin(prev)          # >= 0; nonzero = newly visible
        horizon_end[a0:a0 + n] = run[:, -1]

        if lc is not None:
            cols = np.clip(np.floor(fc).astype(np.int64), 0, W - 1)
            rows = np.clip(np.floor(fr).astype(np.int64), 0, H - 1)
            cls = lc.ravel()[rows * W + cols].reshape(n, n_d)
        else:
            cls = np.zeros((n, n_d), dtype=np.uint8)
        cw = lut[cls]

        om = d_alpha * ds * cutoff[None, :]                 # sr, beyond cutoff
        base = d_alpha * ds * cw * w_d[None, :]             # sr, weighted
        contrib = base * pow_g[None, :]

        v_az[a0:a0 + n] = contrib.sum(axis=1)
        omega_az[a0:a0 + n] = om.sum(axis=1)
        dominance += float((base * pow_0[None, :]).sum())
        extent += float((base * pow_2[None, :]).sum())
        v_nohaze += float((d_alpha * ds * cw * cutoff[None, :]
                           * pow_g[None, :]).sum())
        om_weighted_c += float((om * cw).sum())
        np.add.at(v_band, band_idx, contrib.sum(axis=0))
        np.add.at(alive_band, band_idx, alive.sum(axis=0))
        np.add.at(om_band, band_idx, om.sum(axis=0))
        for code in np.unique(cls[om > 0]):
            m = cls == code
            v_class[int(code)] = v_class.get(int(code), 0.0) + float(contrib[m].sum())
            om_class[int(code)] = om_class.get(int(code), 0.0) + float(om[m].sum())

        vis = (om > 0)
        if vis.any():
            # lowest visible angle beyond the cutoff: the bottom edge of the
            # lowest newly visible band. Strongly negative = standing on an edge.
            low = np.where(vis, prev, np.inf)
            i = np.unravel_index(np.argmin(low), low.shape)
            if low[i] < lookdown[0]:
                lookdown = (float(low[i]), float(d[i[1]]), float(a[i[0]]))
            zv = z[vis]
            z_vis_min = min(z_vis_min, float(zv.min()))
            z_vis_max = max(z_vis_max, float(zv.max()))
            far = np.where(vis.any(axis=0))[0]
            max_sight = max(max_sight, float(d[far[-1]]))
            hist_d.append(om.sum(axis=0))

    V = float(v_az.sum())
    omega = float(omega_az.sum())
    sky = float((d_alpha * (1.0 - np.sin(horizon_end))).sum())

    # omega-weighted median distance of the visible terrain
    hist = np.sum(hist_d, axis=0) if hist_d else np.zeros(n_d)
    cum = np.cumsum(hist)
    med_d = float(d[np.searchsorted(cum, cum[-1] / 2)]) if cum[-1] > 0 else 0.0

    def share(x):
        return x / V if V else 0.0

    by_distance = [{
        "from_km": DIST_BANDS_KM[i], "to_km": DIST_BANDS_KM[i + 1],
        "omega_sr": om_band[i], "v": v_band[i], "share": share(v_band[i]),
        # fraction of this band the DEM covers; below 1 the band is cut short
        # by the data edge and its V is a lower bound
        "coverage": alive_band[i] / n_in_band[i] if n_in_band[i] else 0.0,
    } for i in range(n_band)]

    by_class = []
    for code in sorted(om_class, key=lambda c: -v_class.get(c, 0.0)):
        name, wgt = CLASS_WEIGHTS.get(code, ("No class", UNKNOWN_WEIGHT))
        by_class.append({"code": code, "name": name, "weight": wgt,
                         "omega_sr": om_class[code], "v": v_class[code],
                         "share": share(v_class[code])})

    # 1-degree bins for the client's direction chart
    per_deg = n_az // 360 if n_az % 360 == 0 else None
    if per_deg:
        v_deg = v_az.reshape(360, per_deg).sum(axis=1)
        hz_deg = np.degrees(horizon_end.reshape(360, per_deg).max(axis=1))
    else:
        idx = (az // 1).astype(int)
        v_deg = np.bincount(idx, weights=v_az, minlength=360)
        hz_deg = np.full(360, -90.0)
        np.maximum.at(hz_deg, idx, np.degrees(horizon_end))

    truncated = np.isfinite(edge_km)
    unknown = sum(v["omega_sr"] for v in by_class if v["code"] not in CLASS_WEIGHTS)

    return {
        "lat": lat, "lon": lon,
        "ground_m": round(ground, 1), "eye_m": EYE_HEIGHT_M,
        "V": V, "units": "sr·km",
        "params": {
            "gamma": gamma, "d_haze_km": D_HAZE_M / 1000.0,
            "near_cutoff_m": NEAR_CUTOFF_M, "range_km": range_m / 1000.0,
            "n_az": n_az, "step_m": step_m,
            "refraction_k": panorama.REFRACTION_K,
            "class_weights": [{"code": c, "name": n, "weight": w}
                              for c, (n, w) in CLASS_WEIGHTS.items()],
            "unknown_weight": UNKNOWN_WEIGHT,
        },
        "axes": {
            "dominance_sr": dominance,          # same sum at gamma = 0
            "extent_km2": extent,               # same sum at gamma = 2
            "v_no_haze": v_nohaze,
        },
        "visible": {
            "omega_sr": omega,
            "omega_frac_sphere": omega / (4 * math.pi),
            "sky_frac_sphere": sky / (4 * math.pi),
            "mean_class_weight": om_weighted_c / omega if omega else 0.0,
            "haze_factor": V / v_nohaze if v_nohaze else 0.0,
            "median_dist_km": med_d / 1000.0,
            "max_sightline_km": max_sight / 1000.0,
            "lookdown_deg": (math.degrees(lookdown[0])
                             if math.isfinite(lookdown[0]) else None),
            "lookdown_dist_km": (lookdown[1] / 1000.0
                                 if lookdown[1] is not None else None),
            "lookdown_az": lookdown[2],
            "relief_min_m": z_vis_min if math.isfinite(z_vis_min) else None,
            "relief_max_m": z_vis_max if math.isfinite(z_vis_max) else None,
            "horizon_min_deg": float(np.degrees(horizon_end.min())),
            "horizon_max_deg": float(np.degrees(horizon_end.max())),
        },
        "by_distance": by_distance,
        "by_class": by_class,
        "by_azimuth": {
            "bin_deg": 1,
            "v": [round(float(x), 6) for x in v_deg],
            "horizon_deg": [round(float(x), 3) for x in hz_deg],
        },
        "coverage": {
            "truncated_frac": float(truncated.mean()),
            "nearest_edge_km": (float(np.nanmin(edge_km))
                                if truncated.any() else None),
            "median_edge_km": (float(np.nanmedian(edge_km))
                               if truncated.any() else None),
            "unknown_class_omega_frac": unknown / omega if omega else 0.0,
        },
        "elapsed_ms": round((time.perf_counter() - t0) * 1000.0),
    }
