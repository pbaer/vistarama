"""360-degree terrain panorama from an observer point.

This is the same outward ray-march the view score `V` needs (see
docs/visibility-metric.md), rendered instead of integrated — so it doubles as a
visual check on that machinery. For each azimuth we step outward, track the
running maximum elevation angle, and everything the running max sweeps past is
terrain that is visible; everything above it is sky.

Geometry notes:

* Earth curvature and atmospheric refraction are applied as the standard
  drop = (1 - k) * d^2 / (2R) with k = 0.13. At 100 km this is ~650 m, i.e. the
  difference between seeing a distant range and not.
* Ray endpoints are computed geodetically on the WGS84 ellipsoid and only then
  projected, so azimuths stay true. Stepping in the projected CRS directly
  would bend them by the grid convergence, which at -121 deg in EPSG:5070 is
  substantial.
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from pyproj import Geod, Transformer
from rasterio.windows import from_bounds

EARTH_R = 6_371_000.0
REFRACTION_K = 0.13
EYE_HEIGHT_M = 2.0

# A snap candidate must clear its highest neighbour by at least this, so that
# sub-metre DEM ripple on flat ground is not mistaken for a summit.
MIN_LOCAL_RISE_M = 1.0

# ...measured against the terrain at this radius, NOT against the adjacent
# cell. Summits are flat on top at 30 m posting: Dragontail's summit cell is
# 2681.5 m with a 2681.2 m cell beside it, so requiring a 1 m rise over the
# immediate neighbour rejected the peak outright — and with it the whole
# Stuart Range. A rise measured over ~3 cells keeps the noise rejection this
# threshold is for while letting real summits through.
SUMMIT_TEST_RADIUS_M = 90.0

# Target arc spacing between adjacent mesh vertices on the inner rings. Half a
# DEM cell — fine enough that bilinear interpolation looks smooth, without
# spending vertices on detail the 30 m source cannot contain.
TARGET_ARC_M = 15.0

# Where the geometric radial progression starts, and how many evenly spaced
# rings cover the ground between the observer and it. The step (25/16 = 1.6 m)
# is deliberately on the order of the eye height rather than of the DEM cell:
# these rings exist so that no triangle is large enough to straddle the camera,
# not to add detail the source data does not have.
NEAR_RING_M = 25.0
NEAR_RINGS = 16

# Radial spacing as a multiple of the azimuthal spacing at the same radius.
# 1.0 gives square triangles. Raising it is the cheapest way to trade silhouette
# sharpness for vertices — cost scales as 1/RADIAL_ISOTROPY — but it buys that
# by blunting exactly the tangential ridges the mesh is there to show.
RADIAL_ISOTROPY = 1.0

# Angular error budget for the adaptive mesh, in degrees of silhouette error.
# The pane runs ~0.047 deg per screen pixel (60 deg over ~1272 px), so this is
# sub-pixel. Measured against a 30 m reference from Mount Stuart at 120 km:
#
#   tol    rings   vertices     mean err   p95 err
#   0.01    1754    3.49 M      0.07 px    0.32 px
#   0.04     913    1.77 M      0.22 px    0.77 px   <- default
#   0.08     620    1.17 M      0.42 px    1.40 px
#
# 0.04 sits on the knee: still under a pixel at p95, and it matches the
# uniform mesh's accuracy with about a third fewer vertices.
SAG_TOL_DEG = 0.04
# Probe grid used to decide the spacing (not to build the mesh). Coarse on
# purpose: this is a measurement of how much structure the terrain has.
PROBE_AZ = 256
PROBE_STEP_M = 30.0            # the DEM's own cell size; finer buys nothing
# Ceiling on how far the adaptive law may stretch a step past the uniform
# isotropic spacing. Flat ground would otherwise produce single triangles tens
# of kilometres long, which interpolate the *texture* badly even where the
# geometry is fine.
MAX_STEP_FACTOR = 8.0
# How much finer than a texture's own texel a source may be before it is
# skipped in favour of a coarser one. Only relevant for sources without an
# overview pyramid, where deep decimation means reading every full-res pixel.
MAX_DECIM = 4.0
# Threads GDAL may use for a texture warp. Profiling the four LOD levels put
# ~55% of the time in reproject and ~40% in the windowed read, so this is the
# one knob that touches the dominant cost. Measured on the 4096 px level:
# 4 threads 5.18 s, 8 threads 4.78 s, 16 threads 3.98 s, output bit-identical.
# Halved rather than matched to the core count, since four levels are usually
# in flight at once.
WARP_THREADS = max(4, min(16, (os.cpu_count() or 8) // 2))
ELEV_FLOOR_M = -500.0

# Radius searched by the manual "snap to local max" button, as opposed to the
# automatic snap that runs on every click at a much tighter radius.
MANUAL_SNAP_RADIUS_M = 500.0

GEOD = Geod(ellps="WGS84")

# Muted natural palette, keyed by ESA WorldCover class
TERRAIN_RGB = {
    10: (72, 98, 64),     # tree cover
    20: (124, 128, 84),   # shrubland
    30: (150, 154, 98),   # grassland
    40: (166, 156, 108),  # cropland
    50: (138, 130, 126),  # built-up
    60: (158, 148, 136),  # bare / sparse
    70: (236, 240, 246),  # snow and ice
    80: (78, 112, 140),   # permanent water
    90: (112, 130, 112),  # herbaceous wetland
    95: (92, 128, 100),   # mangroves
    100: (176, 176, 148),  # moss and lichen
}
DEFAULT_RGB = (140, 140, 130)

HAZE_RGB = np.array([176, 190, 205], dtype=np.float32)
SKY_TOP = np.array([92, 132, 190], dtype=np.float32)
SKY_HORIZON = np.array([196, 214, 232], dtype=np.float32)


def _step_distances(range_m: float) -> np.ndarray:
    """Range samples that are dense near the observer and coarse far away.

    Near terrain fills most of the view and needs DEM-resolution sampling;
    at 40 km a 150 m step is still far finer than one output column.
    """
    d = [30.0]
    while d[-1] < range_m:
        d.append(d[-1] + min(150.0, max(25.0, d[-1] * 0.006)))
    return np.asarray(d, dtype=np.float64)


def _bilinear(arr: np.ndarray, fc: np.ndarray, fr: np.ndarray,
              invalid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Bilinearly sample `arr` at fractional cell coordinates.

    Nearest-cell sampling is what makes a rendered heightfield look like
    Minecraft: close to the viewer a polar mesh has sub-metre angular spacing,
    so dozens of vertices fall inside one 30 m DEM cell, take identical
    heights, and produce flat-topped blocks with step edges between them.
    Interpolating between cell *centres* gives a continuous surface instead.

    Contributing corners that are nodata are dropped and the remaining weights
    renormalised, so an interpolated value never gets dragged toward a -999999
    sentinel; only a point with all four corners invalid comes back unusable.
    """
    H, W = arr.shape
    c = fc - 0.5                      # cell centres sit at integer+0.5
    r = fr - 0.5
    c0 = np.floor(c).astype(np.int64)
    r0 = np.floor(r).astype(np.int64)
    tx = (c - c0).astype(np.float64)
    ty = (r - r0).astype(np.float64)

    flat_arr = arr.ravel()
    flat_bad = invalid.ravel()
    acc = np.zeros(fc.shape, dtype=np.float64)
    wsum = np.zeros(fc.shape, dtype=np.float64)
    for dr, dc, w in ((0, 0, (1 - tx) * (1 - ty)), (0, 1, tx * (1 - ty)),
                      (1, 0, (1 - tx) * ty), (1, 1, tx * ty)):
        rr = np.clip(r0 + dr, 0, H - 1)
        cc = np.clip(c0 + dc, 0, W - 1)
        oob = (r0 + dr < 0) | (r0 + dr >= H) | (c0 + dc < 0) | (c0 + dc >= W)
        idx = rr * W + cc
        ok = (~flat_bad[idx]) & (~oob)
        ww = w * ok
        acc += flat_arr[idx] * ww
        wsum += ww
    good = wsum > 1e-9
    return np.where(good, acc / np.where(good, wsum, 1.0), 0.0), good


# Rasters read on every panorama request, keyed by path and mtime.
#
# These files do not change between requests, and re-reading them was nearly
# half the build: 0.98 s for the DEM, 1.31 s for land cover decimated to the
# DEM grid, 0.38 s for hillshade — 2.67 s of a 5.9 s mesh, repeated on every
# click. Costs a few hundred MB of resident memory for a local server, which is
# a good trade against seconds of latency per panorama.
_RASTER_CACHE: dict = {}


def _cache_key(path: Path, *extra):
    p = Path(path)
    return (str(p), p.stat().st_mtime_ns, *extra)


def _read_full(path: Path):
    """Whole raster plus its georeferencing, cached."""
    key = _cache_key(path)
    hit = _RASTER_CACHE.get(key)
    if hit is None:
        with rasterio.open(path) as s:
            hit = (s.read(1), s.crs, s.transform, s.bounds, s.nodata)
        _RASTER_CACHE[key] = hit
    return hit


def _read_window(path: Path, bounds, shape, resampling):
    """Read a world-bounds window of a raster, decimated to `shape`."""
    key = _cache_key(path, tuple(np.round(np.asarray(bounds, float), 3)),
                     tuple(shape), int(resampling))
    hit = _RASTER_CACHE.get(key)
    if hit is not None:
        return hit
    with rasterio.open(path) as s:
        win = from_bounds(*bounds, transform=s.transform)
        arr = s.read(1, window=win, out_shape=shape, boundless=True,
                     fill_value=(s.nodata if s.nodata is not None else 0),
                     resampling=resampling)
        hit = (arr, s.nodata)
    _RASTER_CACHE[key] = hit
    return hit


def _adaptive_radii(dem, dem_transform, dem_crs, lat: float, lon: float,
                    ground: float, range_m: float, n_az: int) -> np.ndarray:
    """Ring radii placed by how much detail the terrain actually has there.

    A uniform mesh spends the same number of vertices on a lake surface as on
    a cliff band, which is the wrong trade in both directions: too coarse on
    the cliff, wasted on the lake. What the eye sees is the *angular* error of
    the rendered surface, so that is what gets budgeted.

    Between two rings the surface is a straight chord, and the height it misses
    by over a span `s` is the sag, ~ s^2/8 * |d2z/dr2|. Divide by the range to
    get the angular error and invert for the spacing that keeps it under
    SAG_TOL_DEG:

        s(r) = sqrt(8 * tol * r / |d2z/dr2|)

    so smooth ground gets long steps and a sharp ridge crest gets short ones,
    with the error held constant rather than the spacing.

    Two refinements matter as much as the curvature term:

    * **Hidden ground is free.** Terrain below the running-max horizon along
      its own azimuth cannot be seen from the observer, so its curvature is
      excluded. From a summit that is most of the far field.
    * **The percentile, not the maximum.** One nodata cell or one spike would
      otherwise pin the whole ring band to the minimum spacing. The 90th
      percentile across azimuths tracks real structure and ignores single
      cells.

    The probe grid is deliberately coarse (PROBE_AZ azimuths, PROBE_STEP_M
    apart): this is estimating how finely to sample, not doing the sampling.
    """
    if range_m <= NEAR_RING_M:
        return np.array([0.0, range_m])

    to_dem = Transformer.from_crs("EPSG:4326", dem_crs, always_xy=True)
    inv = ~dem_transform
    H, W = dem.shape

    pr = np.arange(NEAR_RING_M, range_m + PROBE_STEP_M, PROBE_STEP_M)
    paz = np.arange(PROBE_AZ) * (360.0 / PROBE_AZ)
    RR, AA = np.meshgrid(pr, paz, indexing="ij")          # (n_pr, n_paz)
    lons, lats, _ = GEOD.fwd(np.full(RR.size, lon), np.full(RR.size, lat),
                             AA.ravel(), RR.ravel())
    xs, ys = to_dem.transform(lons, lats)
    fc, fr = inv * (xs, ys)
    bad = ~np.isfinite(dem) | (dem <= ELEV_FLOOR_M)
    z, ok = _bilinear(dem, np.asarray(fc), np.asarray(fr), bad)
    z = z.reshape(RR.shape)
    ok = ok.reshape(RR.shape) & (fc.reshape(RR.shape) >= 0) & \
        (fc.reshape(RR.shape) < W) & (fr.reshape(RR.shape) >= 0) & \
        (fr.reshape(RR.shape) < H)
    z = np.where(ok, z, ground)

    # elevation angle, with the same curvature drop the renderer applies
    drop = (1.0 - REFRACTION_K) * RR * RR / (2.0 * EARTH_R)
    ang = np.arctan2(z - drop - (ground + EYE_HEIGHT_M), RR)
    visible = ang >= np.maximum.accumulate(ang, axis=0) - 1e-9

    # Measure the sag directly instead of modelling it from curvature.
    #
    # The modelled version — estimate |d2z/dr2|, invert s = sqrt(8*tol*r/|z''|)
    # — reads plausible and does not work: a second difference over a 100 m
    # probe cannot see structure finer than 200 m, so every ridge sharper than
    # that looked smooth and the steps came out far too long. Measured against
    # a 30 m reference it landed at 4.1 px of p95 silhouette error, barely
    # better than no adaptivity at all.
    #
    # So: for a candidate span, take the real probe heights inside it and the
    # chord the renderer would actually draw, and use the largest gap. No model,
    # no assumption about how sharp terrain gets — the answer is only ever as
    # coarse as the probe, which is at DEM resolution.
    weight = (visible & ok).astype(np.float64)
    tol = math.radians(SAG_TOL_DEG)
    iso = RADIAL_ISOTROPY * 2.0 * math.pi / n_az
    P = pr.size

    def sag_ok(i0: int, i1: int) -> bool:
        """Does a chord from ring i0 to i1 stay within tolerance throughout?"""
        if i1 <= i0 + 1:
            return True
        r0, r1 = pr[i0], pr[i1]
        t = ((pr[i0:i1 + 1] - r0) / (r1 - r0))[:, None]
        chord = z[i0][None, :] * (1.0 - t) + z[i1][None, :] * t
        gap = np.abs(z[i0:i1 + 1] - chord) * weight[i0:i1 + 1]
        return bool((gap / pr[i0:i1 + 1][:, None]).max() <= tol)

    n_near = NEAR_RINGS
    out = [0.0] + [NEAR_RING_M * i / n_near for i in range(1, n_near + 1)]
    i = 0
    while pr[i] < NEAR_RING_M and i < P - 1:
        i += 1
    while i < P - 1:
        cap = max(TARGET_ARC_M, MAX_STEP_FACTOR * iso * pr[i])
        lo = 1
        hi = 1
        while (i + 2 * hi < P and pr[i + 2 * hi] - pr[i] <= cap
               and sag_ok(i, i + 2 * hi)):
            lo = 2 * hi
            hi *= 2
        # bisect for the longest acceptable span between lo (good) and 2*hi
        a, b = lo, min(2 * hi, P - 1 - i)
        while a < b:
            mid = (a + b + 1) // 2
            if pr[i + mid] - pr[i] <= cap and sag_ok(i, i + mid):
                a = mid
            else:
                b = mid - 1
        i += max(1, a)
        out.append(float(pr[i]))
    if out[-1] < range_m:
        out.append(range_m)
    return np.asarray(out, dtype=np.float64)


def texture_patch(sources: list[Path], lat: float, lon: float,
                  half_m: float, size: int,
                  quality: int = 86) -> tuple[bytes, list[str]]:
    """A north-up square of imagery centred on the observer, as JPEG.

    The panorama draws this as a **world-space texture** rather than colouring
    each vertex, and that distinction is the whole reason high-resolution
    imagery is worth downloading at all. Per-vertex colour caps the texture at
    the mesh's own density — 15 m of arc between neighbouring vertices — so
    NAIP's 0.6 m would have been thrown away sixteen times over. Sampling in
    the fragment shader decouples the two: geometry stays at whatever the DEM
    can support, detail comes from whatever the imagery can.

    `sources` is in priority order, best first. Each is used to fill whatever
    the previous one left as nodata, so NAIP covers the core and Sentinel-2
    takes over past its edge without a seam in the middle of the view.

    **No hillshade is applied, and there is no option to.** Satellite and aerial
    imagery are photographs taken under real sunlight, so terrain shading is
    already in them, physically correct for the actual sun and the actual
    slopes. Multiplying our own hillshade on top does not add information — it
    applies a second Lambertian term to a surface that already has one.

    Worse, it applies it from the wrong side. The Sentinel-2 scenes here were
    acquired at 19:11 UTC on 2023-08-15 with the sun at azimuth 155.7 deg and
    elevation 54.4 deg (`view:sun_azimuth` / `view:sun_elevation` in the STAC
    metadata — the convention is exact, not a guess). Our cartographic
    hillshade lights from 315 deg, the north-west convention that keeps relief
    from reading inverted. Those are 159 deg apart, very nearly opposite, so
    the shading was darkening genuinely sunlit slopes and brightening
    genuinely shadowed ones.

    The land-cover path still shades, because flat class colours have no
    illumination of their own. If a future source ever needs shading here, it
    must be warped into the same AEQD frame as the imagery below, and lit from
    that scene's recorded sun angle rather than from 315 deg.
    """
    from rasterio.enums import Resampling
    from rasterio.transform import from_origin
    from rasterio.warp import reproject, transform_bounds

    # The texture is rendered in an azimuthal-equidistant projection centred on
    # the observer, NOT cut from the metric CRS.
    #
    # This is forced by how the mesh is built: vertex positions come from
    # GEOD.fwd(lon, lat, azimuth, radius), so the mesh's +y axis is *true*
    # north and its radius is geodesic distance. EPSG:5070 is a conic — its
    # grid north drifts from true north away from the central meridian, and at
    # -121 deg that is 15 deg. Sampling a 5070-aligned square with the mesh's
    # own coordinates therefore rotated the imagery by 15 deg about the
    # observer, putting a feature 60 km out 15.6 km from where it belongs:
    # lakes landed on hillsides and ridges ran through valleys.
    #
    # AEQD centred on the viewpoint preserves azimuth and distance from that
    # centre — exactly the mesh's parameterisation — so the two frames agree
    # by construction and the shader's UV lookup needs no correction term.
    aeqd = (f"+proj=aeqd +lat_0={lat} +lon_0={lon} +x_0=0 +y_0=0 "
            f"+datum=WGS84 +units=m +no_defs")
    px = 2.0 * half_m / size
    dst_transform = from_origin(-half_m, half_m, px, px)
    shape = (size, size)

    rgb = np.zeros((3, size, size), dtype=np.uint8)
    filled = np.zeros(shape, dtype=bool)
    used = []

    # Pick sources by *scale*, not just by sharpness.
    #
    # The outer LOD level covers 120 km in 2048 px — 58 m per texel — and
    # asking a 2 m source for that means decimating 29x. That is only cheap if
    # the file has an overview pyramid; against a raw 45222x41877 raster it is
    # a multi-gigapixel read, and it took the texture fetch from ~1 s to over
    # two minutes. It is also pointless: at 58 m/texel a 10 m source already
    # resolves more than the texture can hold.
    #
    # So skip any source more than MAX_DECIM finer than the target texel, as
    # long as something coarser remains to cover it.
    px_target = 2.0 * half_m / size
    usable = []
    for path in sources:
        if not path or not Path(path).exists():
            continue
        with rasterio.open(path) as s:
            res = float(abs(s.res[0]))
            has_ovr = bool(s.overviews(1))
        if px_target > res * MAX_DECIM and not has_ovr:
            continue
        usable.append(path)
    if not usable:                     # nothing else covers this — use them all
        usable = [p for p in sources if p and Path(p).exists()]

    for path in usable:
        if filled.all():
            break
        with rasterio.open(path) as s:
            # Read only the source window this square needs, decimated to
            # roughly the target scale. Handing reproject the whole band would
            # pull all 24745x23966 pixels of the Sentinel-2 mosaic per request;
            # `out_shape` serves it from the overview pyramid instead. The 1.4x
            # is slack for the rotation between the two frames.
            sb = transform_bounds(aeqd, s.crs, -half_m, -half_m, half_m, half_m,
                                  densify_pts=21)
            win = from_bounds(*sb, transform=s.transform)
            n = int(size * 1.4)
            arr = s.read(indexes=[1, 2, 3], window=win, out_shape=(3, n, n),
                         boundless=True, fill_value=0,
                         resampling=Resampling.bilinear)
            src_tf = rasterio.windows.transform(win, s.transform) * \
                rasterio.Affine.scale(win.width / n, win.height / n)
            out = np.zeros((3, size, size), dtype=np.uint8)
            for b in range(3):
                reproject(
                    source=arr[b], destination=out[b],
                    src_transform=src_tf, src_crs=s.crs, src_nodata=0,
                    dst_transform=dst_transform, dst_crs=aeqd, dst_nodata=0,
                    resampling=Resampling.bilinear, num_threads=WARP_THREADS,
                )
        got = (out.sum(axis=0) > 0) & ~filled
        if not got.any():
            continue
        rgb[:, got] = out[:, got]
        filled |= got
        used.append(Path(path).stem)

    buf = io.BytesIO()
    Image.fromarray(np.moveaxis(rgb, 0, -1), mode="RGB").save(
        buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue(), used


def texture_patch_job(args: tuple):
    """Picklable entry point for rendering one texture in a worker process.

    Lives at module level, and takes/returns only plain values, so that the
    `spawn` start method Windows uses can import and call it without dragging
    the HTTP server's state into the child.
    """
    sources, lat, lon, half_m, size = args
    return texture_patch([Path(s) for s in sources], lat, lon, half_m, size)


def render(dem_path: Path, hillshade_path: Path | None,
           landcover_path: Path | None, peaks_path: Path | None,
           lat: float, lon: float,
           width: int = 2160, fov_deg: float = 60.0,
           el_center: float = -2.0, range_km: float = 60.0,
           haze_km: float = 22.0, max_labels: int = 90) -> dict:
    from rasterio.enums import Resampling

    range_m = range_km * 1000.0
    n_az = width
    az = np.linspace(0.0, 360.0, n_az, endpoint=False)
    dist = _step_distances(range_m)
    n_r = dist.size

    # --- ray endpoints, geodetically then projected --------------------
    with rasterio.open(dem_path) as s:
        dem_crs = s.crs
        dem_transform = s.transform
        dem_bounds = s.bounds
        dem_nodata = s.nodata
        dem = s.read(1)
    to_dem = Transformer.from_crs("EPSG:4326", dem_crs, always_xy=True)

    AZ, D = np.meshgrid(az, dist, indexing="ij")
    lons, lats, _ = GEOD.fwd(
        np.full(AZ.size, lon), np.full(AZ.size, lat),
        AZ.ravel(), D.ravel())
    xs, ys = to_dem.transform(lons, lats)

    inv = ~dem_transform
    cols, rows = inv * (xs, ys)
    # floor, not round: ~transform gives fractional cell coordinates with
    # integers at cell *corners*, so the containing cell is floor(). Rounding
    # offsets every sample by half a cell (15 m at 30 m resolution).
    cols = np.floor(cols).astype(np.int32)
    rows = np.floor(rows).astype(np.int32)
    H, W = dem.shape
    inside = (rows >= 0) & (rows < H) & (cols >= 0) & (cols < W)
    flat = np.where(inside, rows.clip(0, H - 1) * W + cols.clip(0, W - 1), 0)

    z = dem.ravel()[flat].astype(np.float64)
    bad = (~inside) | ~np.isfinite(z) | (z < -500)
    if dem_nodata is not None:
        bad |= z == dem_nodata
    z[bad] = -1e6                     # can never rise above the running max

    z = z.reshape(n_az, n_r)
    flat = flat.reshape(n_az, n_r)

    # observer ground height, then eye height above it
    ox, oy = to_dem.transform(lon, lat)
    oc, orow = inv * (ox, oy)
    oc, orow = int(math.floor(oc)), int(math.floor(orow))
    if not (0 <= orow < H and 0 <= oc < W):
        raise ValueError("observer is outside the DEM")
    ground = float(dem[orow, oc])
    if dem_nodata is not None and ground == dem_nodata:
        raise ValueError("observer is on a DEM void")
    eye = ground + EYE_HEIGHT_M

    # --- elevation angles, with curvature + refraction -------------------
    drop = (1.0 - REFRACTION_K) * dist ** 2 / (2.0 * EARTH_R)
    phi = np.degrees(np.arctan2(z - eye - drop[None, :], dist[None, :]))
    phi[bad.reshape(n_az, n_r)] = -90.0
    skyline = np.maximum.accumulate(phi, axis=1)   # nondecreasing along each ray

    # --- fill the image by looking up where the skyline reaches each row --
    height = int(round(width * fov_deg / 360.0))
    el_top = el_center + fov_deg / 2.0
    el_bot = el_center - fov_deg / 2.0
    el = np.linspace(el_top, el_bot, height)       # row 0 = top

    # searchsorted needs the needle ascending along each row of `skyline`,
    # which np.maximum.accumulate guarantees
    idx = np.empty((n_az, height), dtype=np.int32)
    for i in range(n_az):
        idx[i] = np.searchsorted(skyline[i], el[::-1])
    idx = idx[:, ::-1]                              # back to top-down
    is_sky = idx >= n_r
    idx_c = np.clip(idx, 0, n_r - 1)

    hit_dist = dist[idx_c]
    hit_flat = np.take_along_axis(flat, idx_c, axis=1)

    # --- colour: land cover x hillshade, then haze ------------------------
    rgb = np.zeros((n_az, height, 3), dtype=np.float32)
    shade = np.full((n_az, height), 0.72, dtype=np.float32)

    if landcover_path and Path(landcover_path).exists():
        lc_full, _ = _read_window(
            Path(landcover_path), dem_bounds, dem.shape, Resampling.nearest)
        lc = lc_full.ravel()[hit_flat]
        base = np.full((n_az, height, 3), DEFAULT_RGB, dtype=np.float32)
        for code, col in TERRAIN_RGB.items():
            m = lc == code
            if m.any():
                base[m] = col
    else:
        base = np.full((n_az, height, 3), DEFAULT_RGB, dtype=np.float32)

    if hillshade_path and Path(hillshade_path).exists():
        hs_full, _ = _read_window(
            Path(hillshade_path), dem_bounds, dem.shape, Resampling.bilinear)
        hs = hs_full.ravel()[hit_flat].astype(np.float32)
        shade = np.clip(0.45 + 0.75 * (hs / 255.0), 0.35, 1.35)

    rgb = base * shade[..., None]

    # atmospheric perspective: exponential extinction toward the haze colour
    fog = 1.0 - np.exp(-hit_dist / (haze_km * 1000.0))
    fog = np.clip(fog, 0.0, 0.93)[..., None].astype(np.float32)
    rgb = rgb * (1.0 - fog) + HAZE_RGB * fog

    # sky gradient above the skyline
    t = np.clip((el - el_bot) / max(fov_deg, 1e-6), 0.0, 1.0)[None, :, None]
    sky = SKY_HORIZON * (1.0 - t) + SKY_TOP * t
    rgb = np.where(is_sky[..., None], sky, rgb)

    img = Image.fromarray(
        np.clip(rgb, 0, 255).astype(np.uint8).transpose(1, 0, 2), "RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    data_uri = "data:image/png;base64," + base64.b64encode(
        buf.getvalue()).decode()

    peaks = _visible_peaks(peaks_path, lat, lon, eye, az, dist, skyline,
                           range_m, el_bot, el_top, max_labels)

    vis = hit_dist[~is_sky]
    return {
        "lat": lat, "lon": lon,
        "ground_m": round(ground, 1), "eye_m": round(eye, 1),
        "width": width, "height": height,
        "el_top": round(el_top, 3), "el_bottom": round(el_bot, 3),
        "range_km": range_km,
        "image": data_uri,
        "peaks": peaks,
        "sky_fraction": round(float(is_sky.mean()), 4),
        "max_visible_km": round(float(vis.max() / 1000.0), 1) if vis.size else 0.0,
    }


_SUMMIT_MASKS: dict = {}


def _summit_masks(px: float):
    """Neighbourhood masks for `_is_summit`, cached per cell size."""
    key = round(px, 6)
    m = _SUMMIT_MASKS.get(key)
    if m is None:
        half = max(1, int(math.ceil(SUMMIT_TEST_RADIUS_M / px))) + 1
        rr, cc = np.mgrid[-half:half + 1, -half:half + 1]
        d = np.hypot(rr, cc) * px
        inner = (d <= SUMMIT_TEST_RADIUS_M) & ~((rr == 0) & (cc == 0))
        ring = (d > SUMMIT_TEST_RADIUS_M) & (d <= SUMMIT_TEST_RADIUS_M + 2 * px)
        m = (half, inner, ring)
        _SUMMIT_MASKS[key] = m
    return m


def _is_summit(dem, r: int, c: int, px: float) -> bool:
    """Whether cell (r, c) is a genuine local summit.

    Two conditions at two scales:

    * nothing within SUMMIT_TEST_RADIUS_M is higher — ties allowed, so a summit
      whose cap spans several cells still counts; and
    * it stands MIN_LOCAL_RISE_M above the ring just outside that radius, which
      is what keeps DEM ripple on flat ground from reading as a landform.

    The second condition is also what stops the viewpoint walking up an open
    hillside: on a planar slope there is always higher ground within 90 m, so
    the first condition already fails.
    """
    half, inner, ring = _summit_masks(px)
    H, W = dem.shape
    if not (half <= r < H - half and half <= c < W - half):
        return False
    z = float(dem[r, c])
    w = dem[r - half:r + half + 1, c - half:c + half + 1]
    good = np.isfinite(w) & (w > -500.0)
    if not np.isfinite(z) or z <= -500.0:
        return False
    inner_v = w[inner & good]
    ring_v = w[ring & good]
    if inner_v.size == 0 or ring_v.size == 0:
        return False
    if z < float(inner_v.max()):
        return False
    return (z - float(ring_v.max())) >= MIN_LOCAL_RISE_M


def _local_max_candidates(dem, transform, lat, lon, dem_crs, nodata,
                          radius_m: float):
    """Every strict local maximum within `radius_m` of a lat/lon.

    Yields (z, row, col, distance_from_click). "Strict local maximum" means
    higher than all eight neighbours by at least MIN_LOCAL_RISE_M — see
    `snap_to_local_max` for why merely-highest-in-window is the wrong test.
    Also reports whether the clicked cell is itself such a maximum.
    """
    to_dem = Transformer.from_crs("EPSG:4326", dem_crs, always_xy=True)
    x, y = to_dem.transform(lon, lat)
    inv = ~transform
    fc, fr = inv * (x, y)
    c0, r0 = int(math.floor(fc)), int(math.floor(fr))
    px = abs(transform.a)
    rad = max(1, int(math.floor(radius_m / px)))
    H, W = dem.shape

    def bad(v):
        return (not np.isfinite(v)) or v < -500 or (
            nodata is not None and v == nodata)

    def is_max(r, c):
        return _is_summit(dem, r, c, px)

    out = []
    if not (1 <= r0 < H - 1 and 1 <= c0 < W - 1) or bad(float(dem[r0, c0])):
        return out, 0.0, False
    z0 = float(dem[r0, c0])
    at_max = is_max(r0, c0)

    for dr in range(-rad - 1, rad + 2):
        for dc in range(-rad - 1, rad + 2):
            r, c = r0 + dr, c0 + dc
            d = math.hypot((c + 0.5 - fc) * px, (r + 0.5 - fr) * px)
            if d > radius_m or (dr == 0 and dc == 0):
                continue
            if is_max(r, c):
                out.append((float(dem[r, c]), r, c, d))
    return out, z0, at_max


def nearest_local_max(dem, transform, dem_crs, lat: float, lon: float,
                      nodata, radius_m: float = 500.0) -> dict:
    """The *nearest* strict local maximum within `radius_m`, for the manual
    snap button — as opposed to the automatic snap, which takes the highest
    within a much tighter radius."""
    back = Transformer.from_crs(dem_crs, "EPSG:4326", always_xy=True)
    cands, z0, at_max = _local_max_candidates(
        dem, transform, lat, lon, dem_crs, nodata, radius_m)
    # only offer somewhere higher — the point of the button is a better
    # viewpoint, and a nearer-but-lower summit is never that
    cands = [t for t in cands if t[0] > z0]
    out = {"found": False, "at_max": at_max, "search_radius_m": radius_m}
    # Deliberately not short-circuiting on `at_max`. Standing on a summit is no
    # reason to refuse a higher one 200 m away, and minor ridge bumps are
    # summits too — treating "at a local max" as "nothing better nearby"
    # disabled the button on six points around Dragontail alone, one of them
    # 146 m below a peak within range. `cands` is already filtered to strictly
    # higher ground, which is the condition that actually matters.
    if not cands:
        return out
    z, r, c, d = min(cands, key=lambda t: t[3])
    wx, wy = transform * (c + 0.5, r + 0.5)
    slon, slat = back.transform(wx, wy)
    out.update({"found": True, "lat": slat, "lon": slon,
                "dist_m": round(d, 1), "rise_m": round(z - z0, 1),
                "ele_m": round(z, 1)})
    return out


def snap_to_local_max(dem, transform, dem_crs, lat: float, lon: float,
                      nodata, radius_m: float = 150.0) -> dict:
    """Move a viewpoint to the highest genuine local maximum within `radius_m`.

    "Local maximum" is `_is_summit`: nothing higher within SUMMIT_TEST_RADIUS_M,
    and standing MIN_LOCAL_RISE_M above the ring beyond it. The second half is
    what stops the viewpoint walking up an open hillside — on a planar slope
    there is always higher ground within 90 m, so the first half already fails.

    Getting this test wrong is worse than it sounds: when the summit is
    rejected the viewpoint stays where it was clicked, at the DEM's *bilinear*
    height there, which on a summit cell corner can be metres below the
    surrounding cell centres. The observer then stands in a pit with the
    ground above eye level, and the panorama shows a wall of near terrain
    rising above the horizon.

    Returns a dict describing what happened; `snapped` is False when the
    clicked point is on open slope, in a valley, or already the summit.
    """
    to_dem = Transformer.from_crs("EPSG:4326", dem_crs, always_xy=True)
    back = Transformer.from_crs(dem_crs, "EPSG:4326", always_xy=True)
    x, y = to_dem.transform(lon, lat)
    inv = ~transform
    fc, fr = inv * (x, y)
    c0, r0 = int(math.floor(fc)), int(math.floor(fr))
    px = abs(transform.a)
    rad = max(1, int(math.floor(radius_m / px)))
    H, W = dem.shape

    def bad(v):
        return (not np.isfinite(v)) or v < -500 or (
            nodata is not None and v == nodata)

    out = {"snapped": False, "lat": lat, "lon": lon,
           "search_radius_m": radius_m, "candidates": 0}
    if not (1 <= r0 < H - 1 and 1 <= c0 < W - 1):
        return out
    if bad(float(dem[r0, c0])):
        return out
    # Compare against the *bilinear* height at the click, which is where the
    # observer would actually stand — not against the clicked cell's value.
    # The two differ by metres near a cell corner, and using the cell value
    # meant a click on a summit cell looked like it was already at the top
    # while the viewpoint sat on the cell's flank, below its own summit.
    r1, c1 = max(0, r0 - 1), max(0, c0 - 1)
    win = dem[r1:min(H, r0 + 3), c1:min(W, c0 + 3)]
    inv_win = ~np.isfinite(win) | (win <= -500.0)
    if nodata is not None:
        inv_win |= (win == nodata)
    z_bil, ok_bil = _bilinear(win, np.array([fc - c1]), np.array([fr - r1]),
                              inv_win)
    z0 = float(z_bil[0]) if bool(ok_bil[0]) else float(dem[r0, c0])

    best = None
    # search one cell wider than the radius, then reject on true distance.
    # dr == dc == 0 is included: the clicked cell may itself be the summit, and
    # moving to its centre is exactly what lifts the observer out of the pit.
    for dr in range(-rad - 1, rad + 2):
        for dc in range(-rad - 1, rad + 2):
            r, c = r0 + dr, c0 + dc
            if not (1 <= r < H - 1 and 1 <= c < W - 1):
                continue
            # distance from the *clicked point*, not from its cell centre —
            # the click can sit half a cell from the centre, which otherwise
            # lets a "100 m" search reach ~120 m
            d = math.hypot((c + 0.5 - fc) * px, (r + 0.5 - fr) * px)
            if d > radius_m:
                continue
            z = float(dem[r, c])
            # never snap downhill, and never for a rise that rounds to nothing:
            # once the viewpoint is on a cell centre the bilinear height equals
            # that cell's value to within floating-point noise, which otherwise
            # reports an endless series of 0.0 m "snaps"
            if bad(z) or z <= z0 + 0.01:
                continue
            if not _is_summit(dem, r, c, px):
                continue
            out["candidates"] += 1
            # highest wins; among equal summits take the nearest, so a click on
            # a summit does not jump to an identical one across the radius
            if best is None or (z, -d) > (best[0], -best[3]):
                best = (z, r, c, d)

    if best is None:
        return out

    z, r, c, d = best
    wx, wy = transform * (c + 0.5, r + 0.5)
    slon, slat = back.transform(wx, wy)
    out.update({
        "snapped": True, "lat": slat, "lon": slon,
        "from_lat": lat, "from_lon": lon,
        "rise_m": round(z - z0, 1), "moved_m": round(d, 1),
    })
    return out


def terrain_mesh(dem_path: Path, hillshade_path: Path | None,
                 landcover_path: Path | None, peaks_path: Path | None,
                 lat: float, lon: float, range_km: float = 60.0,
                 n_az: int = 1024, n_r: int = 300,
                 snap_radius_m: float = 150.0) -> tuple[dict, bytes]:
    """Polar terrain mesh around an observer, for client-side 3D rendering.

    A polar grid centred on the viewer is the natural parameterisation here:
    radial spacing grows geometrically, so near ground — which fills most of
    the view — is sampled finely while the far field, where one ring spans a
    fraction of a degree anyway, is not. Ring placement is adaptive (see
    `_adaptive_radii`), giving ~1.7M vertices at 120 km — sub-pixel silhouette
    error, and a browser draws it at ~3 ms a frame.

    It also lets the client dispense with a depth buffer entirely: radius is
    monotonic along every view ray from the centre, so drawing rings from the
    outside in is exact occlusion. See pano3d.js.

    Earth curvature is **baked into the vertex heights** as z - drop(r). The
    client can then use an ordinary flat-earth perspective camera and still get
    elevation angles right, with no curvature maths in the shader.

    Returns (header, payload) where payload is
        float32 z[n_az * n_r]  then  uint8 rgb[n_az * n_r * 3]
    """
    from rasterio.enums import Resampling

    range_m = range_km * 1000.0

    # Round the azimuth cap to a power of two up front, because the ring
    # spacing below is derived from it and the two have to agree.
    n_az = 1 << int(math.floor(math.log2(max(n_az, 8))))

    # radii: a centre point, evenly spaced rings across the first NEAR_RING_M,
    # then geometric growth out to the range limit.
    #
    # The near ramp is not there for detail — the DEM has none at this scale —
    # but for *coverage*. Going straight from the centre vertex to a ring at
    # 25 m means the ground you are standing on is sixteen 25 m triangles
    # radiating from a single point, and on a steep face those cut clean
    # through the camera: the eye is 2 m up, the uphill triangle climbs past
    # 2 m within 3 m of the origin, and everything nearer than the near plane
    # is clipped away, leaving a hole you can see the underside of the mountain
    # through. Small rings near the eye keep the surface hugging its true
    # bilinear shape, so nothing large ever straddles the camera.
    # Ring spacing is derived from a target *spacing*, not from a fixed ring
    # count, and it is matched to the azimuthal spacing so the triangles stay
    # roughly square.
    #
    # Both of those replace earlier mistakes. Fixing `n_r` at 300 made the
    # resolution depend on how far you were looking: the same 300 rings spread
    # over 20 km or over 120 km, so raising the range quietly blurred the near
    # ground. And spacing the rings geometrically at whatever rate that implied
    # left the mesh badly anisotropic — measured 36 screen pixels between rings
    # against 7.5 between azimuths, i.e. 295 m radially at 10 km against 61 m
    # across. Terrain has ridges in every orientation, so there is no reason to
    # sample one axis five times more finely than the other; the coarse axis
    # just sets how blunt the ridges look.
    #
    # Beyond the azimuth cap the arc is 2*pi*r/n_az, so matching it makes the
    # radial step proportional to r — geometric growth again, but with the rate
    # pinned by the azimuth cap instead of chosen independently.
    # ...and that uniform law is now only the ceiling: `_adaptive_radii`
    # stretches the step wherever the terrain is smooth or hidden and tightens
    # it on ridges, holding the *angular* error constant instead of the spacing.
    dem, dem_crs, dem_transform, dem_bounds, dem_nodata = _read_full(dem_path)

    # Snap to a nearby summit before anything else, so the ring placement, the
    # mesh, the peak geometry and the reported ground height all refer to the
    # same point.
    snap = {"snapped": False}
    if snap_radius_m > 0:
        snap = snap_to_local_max(dem, dem_transform, dem_crs, lat, lon,
                                 dem_nodata, snap_radius_m)
        lat, lon = snap["lat"], snap["lon"]

    # offered by the manual "snap to local max" button, measured from wherever
    # the automatic snap actually left us
    nearest = nearest_local_max(dem, dem_transform, dem_crs, lat, lon,
                                dem_nodata, MANUAL_SNAP_RADIUS_M)

    to_dem = Transformer.from_crs("EPSG:4326", dem_crs, always_xy=True)
    ox, oy = to_dem.transform(lon, lat)
    ocol, orow = (~dem_transform) * (ox, oy)
    _gz, _gok = _bilinear(dem, np.array([ocol]), np.array([orow]),
                          ~np.isfinite(dem) | (dem <= ELEV_FLOOR_M))
    ground0 = float(_gz[0]) if bool(_gok[0]) else 0.0

    radii = _adaptive_radii(dem, dem_transform, dem_crs, lat, lon,
                            ground0, range_m, n_az)
    n_r = radii.size

    # Azimuth count per ring, rather than one count for the whole mesh.
    #
    # A constant azimuth count gives constant *angular* resolution, which is
    # the right target for a perspective view — but only in the far field. Near
    # the observer it wildly oversamples the data: 2048 azimuths at r = 25 m is
    # 0.08 m of arc, far finer than a 30 m DEM can support, so those vertices
    # only interpolate between values that were never measured.
    #
    # So: match arc spacing to TARGET_ARC_M near in, and cap at n_az once the
    # ring is big enough to deserve it. Powers of two, so consecutive rings
    # differ by exactly a factor of two and stitch cleanly.
    # The cap must itself be a power of two. Anything else leaves one
    # transition with a fractional ratio — 512 -> 720 is 1.40625 — and the
    # renderer can only stitch rings whose counts are exact multiples. That
    # mismatch showed up as a fan of long spurious triangles at the radius
    # where the transition fell.
    counts = np.empty(n_r, dtype=np.int64)
    for i in range(n_r):
        want = 2.0 * math.pi * max(radii[i], 1.0) / TARGET_ARC_M
        p = 1 << max(3, int(math.ceil(math.log2(max(want, 8.0)))))
        counts[i] = min(n_az, p)
    counts[0] = counts[1]          # centre ring degenerates to a point anyway
    counts = np.maximum.accumulate(counts)   # never decrease outward
    for i in range(1, n_r):
        if counts[i] % counts[i - 1]:
            raise ValueError(
                f"ring {i}: azimuth counts {counts[i-1]} -> {counts[i]} are "
                "not an exact multiple; the mesh cannot be stitched")

    offsets = np.zeros(n_r + 1, dtype=np.int64)
    np.cumsum(counts, out=offsets[1:])
    n_vert = int(offsets[-1])

    ring_idx = np.repeat(np.arange(n_r), counts)
    within = np.arange(n_vert) - offsets[ring_idx]
    az = within / counts[ring_idx] * 360.0
    rad = radii[ring_idx]

    lons, lats, _ = GEOD.fwd(np.full(n_vert, lon), np.full(n_vert, lat),
                             az, np.maximum(rad, 1e-6))
    xs, ys = to_dem.transform(lons, lats)

    inv = ~dem_transform
    fcol, frow = inv * (xs, ys)
    # floor, not round: ~transform gives fractional cell coordinates with
    # integers at cell *corners*, so the containing cell is floor(). Rounding
    # offsets every sample by half a cell (15 m at 30 m resolution).
    cols = np.floor(fcol).astype(np.int32)
    rows = np.floor(frow).astype(np.int32)
    H, W = dem.shape
    inside = (rows >= 0) & (rows < H) & (cols >= 0) & (cols < W)
    flat = np.where(inside, rows.clip(0, H - 1) * W + cols.clip(0, W - 1), 0)

    dem_bad = ~np.isfinite(dem) | (dem < -500)
    if dem_nodata is not None:
        dem_bad |= dem == dem_nodata

    # heights are interpolated, so the surface is continuous rather than a
    # staircase of 30 m cells
    z, zok = _bilinear(dem, fcol, frow, dem_bad)
    bad = (~inside) | (~zok)

    ox, oy = to_dem.transform(lon, lat)
    oc, orow = inv * (ox, oy)
    oci, ori = int(math.floor(oc)), int(math.floor(orow))
    if not (0 <= ori < H and 0 <= oci < W):
        raise ValueError("observer is outside the DEM")
    if dem_bad[ori, oci]:
        raise ValueError("observer is on a DEM void")
    gz, gok = _bilinear(dem, np.array([oc]), np.array([orow]), dem_bad)
    ground = float(gz[0]) if bool(gok[0]) else float(dem[ori, oci])

    # Past the data edge, carry the last valid elevation outward along each ray.
    #
    # Filling with a constant instead — the observer's own ground height is the
    # tempting choice — puts a surface at a fixed *absolute* elevation out
    # there. Curvature then drops it to just below the viewer's horizon, and it
    # renders as a flat-topped mesa hanging at eye level with a hard edge where
    # the data ends. Carrying the last real ridge height forward instead reads
    # as distant ground falling away, and the haze tint below makes it fade out.
    #
    # Rings differ in azimuth count (always by a factor of two), so each outer
    # vertex inherits from its parent on the ring inside it. Going outward in
    # order means the fill propagates however far the gap runs.
    for i in range(1, n_r):
        ratio = int(counts[i]) // int(counts[i - 1])
        co = int(counts[i])
        cur = offsets[i] + np.arange(co)
        parent = offsets[i - 1] + (np.arange(co) // ratio)
        m = bad[cur]
        if m.any():
            z[cur[m]] = z[parent[m]]

    drop = (1.0 - REFRACTION_K) * rad ** 2 / (2.0 * EARTH_R)
    z_render = (z - drop).astype(np.float32)
    valid = (~bad).astype(np.uint8)

    # --- per-vertex colour: land cover x hillshade -------------------------
    rgb = np.full((z.size, 3), DEFAULT_RGB, dtype=np.float32)
    if landcover_path and Path(landcover_path).exists():
        lc_full, _ = _read_window(Path(landcover_path), dem_bounds, dem.shape,
                                  Resampling.nearest)
        lc = lc_full.ravel()[flat]
        for code, col in TERRAIN_RGB.items():
            m = lc == code
            if m.any():
                rgb[m] = col
    if hillshade_path and Path(hillshade_path).exists():
        hs_full, _ = _read_window(Path(hillshade_path), dem_bounds, dem.shape,
                                  Resampling.bilinear)
        # interpolate the shading too, or the smooth surface still carries
        # visible 30 m facets from stepped brightness
        hs, _ok = _bilinear(hs_full.astype(np.float32), fcol, frow,
                            np.zeros(hs_full.shape, dtype=bool))
        rgb *= np.clip(0.45 + 0.75 * (hs.astype(np.float32) / 255.0),
                       0.35, 1.35)[:, None]
    # fade extrapolated ground toward haze so it reads as "no data", not terrain
    if bad.any():
        rgb[bad] = rgb[bad] * 0.35 + HAZE_RGB * 0.65
    rgb_u8 = np.clip(rgb, 0, 255).astype(np.uint8)

    peaks = _peaks_in_range(peaks_path, lat, lon, range_m)

    header = {
        "lat": lat, "lon": lon, "ground_m": round(ground, 1),
        "n_az": n_az, "n_r": n_r, "n_vert": n_vert,
        "counts": [int(c) for c in counts],
        "offsets": [int(o) for o in offsets],
        "radii": [round(float(r), 2) for r in radii],
        "range_km": range_km,
        "earth_r": EARTH_R, "refraction_k": REFRACTION_K,
        "valid_fraction": round(float(valid.mean()), 4),
        "snap": snap,
        "nearest_max": nearest,
        "peaks": peaks,
    }
    return header, z_render.tobytes() + rgb_u8.tobytes() + valid.tobytes()


def _peaks_in_range(peaks_path, lat, lon, range_m) -> list[dict]:
    """Every named summit within range, with geometry but no visibility test.

    Visibility is left to the client because it depends on eye height, which
    the user can change without refetching the mesh.
    """
    if not peaks_path or not Path(peaks_path).exists():
        return []
    try:
        gj = json.loads(Path(peaks_path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    lons, lats, eles, names, volc = [], [], [], [], []
    for f in gj.get("features", []):
        g = f.get("geometry") or {}
        if g.get("type") != "Point":
            continue
        props = f.get("properties", {}) or {}
        name = props.get("name")
        if not name:
            continue
        try:
            ele = float(str(props.get("ele", "")).split()[0])
        except (ValueError, IndexError):
            continue
        lons.append(g["coordinates"][0])
        lats.append(g["coordinates"][1])
        eles.append(ele)
        names.append(name)
        # carried so the panorama can draw the same icon the map does; there
        # are no volcanoes in Alpine Lakes but Rainier and Glacier Peak are in
        # range of plenty of Cascades viewpoints
        volc.append(props.get("natural") == "volcano")
    if not lons:
        return []
    fwd, _, d = GEOD.inv(np.full(len(lons), lon), np.full(len(lons), lat),
                         np.asarray(lons), np.asarray(lats))
    fwd = np.mod(fwd, 360.0)
    keep = (d > 250.0) & (d < range_m)
    # Topographic prominence, joined by scripts/prominence.py. Absent for
    # summits below the source's 100 ft cut and for nodes that failed the join,
    # so the client must treat it as optional rather than assume it.
    prom = {}
    side = Path(peaks_path).parent.parent.parent / "interim" / \
        Path(peaks_path).stem.split("_")[0] / "peak_prominence.json"
    try:
        prom = json.loads(side.read_text(encoding="utf-8"))["prominence_m"]
    except Exception:  # noqa: BLE001
        pass

    out = []
    for i in np.nonzero(keep)[0]:
        e = {"name": names[i], "az": round(float(fwd[i]), 3),
             "dist": round(float(d[i]), 1), "ele": round(float(eles[i]), 1),
             "volcano": bool(volc[i])}
        pv = prom.get(f"{lats[i]:.5f},{lons[i]:.5f}")
        if pv is not None:
            e["prom"] = pv
        out.append(e)
    return out


def _visible_peaks(peaks_path, lat, lon, eye, az, dist, skyline,
                   range_m, el_bot, el_top, max_labels) -> list[dict]:
    if not peaks_path or not Path(peaks_path).exists():
        return []
    try:
        gj = json.loads(Path(peaks_path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []

    cand = []
    for f in gj.get("features", []):
        g = f.get("geometry") or {}
        if g.get("type") != "Point":
            continue
        plon, plat = g["coordinates"][:2]
        props = f.get("properties", {}) or {}
        name = props.get("name")
        if not name:
            continue          # an unnamed summit is not a useful label
        try:
            ele = float(str(props.get("ele", "")).split()[0])
        except (ValueError, IndexError):
            continue
        cand.append((plon, plat, ele, name))
    if not cand:
        return []

    plons = np.array([c[0] for c in cand])
    plats = np.array([c[1] for c in cand])
    eles = np.array([c[2] for c in cand])
    fwd_az, _, d = GEOD.inv(np.full(plons.size, lon), np.full(plons.size, lat),
                            plons, plats)
    fwd_az = np.mod(fwd_az, 360.0)

    keep = (d > 300.0) & (d < range_m)
    out = []
    n_az = az.size
    for i in np.nonzero(keep)[0]:
        di = d[i]
        drop = (1.0 - REFRACTION_K) * di ** 2 / (2.0 * EARTH_R)
        phi = math.degrees(math.atan2(eles[i] - eye - drop, di))
        if not (el_bot < phi < el_top):
            continue
        ia = int(round(fwd_az[i] / 360.0 * n_az)) % n_az
        # Compare against the skyline *well* before the summit. The DEM
        # contains the peak itself, so backing off is necessary at all; but
        # backing off only a couple of hundred metres measures how far the
        # summit clears its own flank, which for a big mountain is almost
        # nothing — Mt Rainier scored 121 m that way, losing to minor spires
        # on its own shoulder. A back-off proportional to distance clears the
        # massif and measures how much it stands above the country in front.
        back = max(400.0, 0.12 * di)
        k = max(0, int(np.searchsorted(dist, max(0.0, di - back))) - 1)
        horizon = float(skyline[ia, k])
        if phi < horizon - 0.05:
            continue
        clear_deg = phi - horizon
        out.append({
            "name": cand[i][3], "az": round(float(fwd_az[i]), 3),
            "el": round(phi, 3), "dist_km": round(float(di) / 1000.0, 2),
            "ele": round(float(eles[i])),
            "clearance": round(clear_deg, 3),
            # metres the summit stands above the ridge in front of it — a
            # viewer-relative prominence, and the scale-stable version of
            # clearance
            "stands_m": round(di * math.tan(math.radians(max(clear_deg, 0.0)))),
        })

    # Rank by how far the summit rises above the ridgeline in front of it,
    # rather than by raw height: a 2400 m peak standing alone on the skyline
    # is a better label than a 2700 m one buried in the flank of something
    # bigger, and elevation cannot tell those apart.
    #
    # Use the metric version (`stands_m`), not the angle. Angular clearance
    # collapses with distance — Mt Rainier at 95 km clears its horizon by
    # 0.07 deg and loses to every foreground bump — whereas in metres it still
    # reads as a 121 m tower.
    #
    # Distance banding on top of that, because the far band holds ~500 of the
    # ~650 visible summits here; without it the near foreground never gets
    # labelled at all.
    bands = [(0, 4_000), (4_000, 12_000), (12_000, 30_000), (30_000, 1e12)]
    grouped = [
        sorted((p for p in out if lo <= p["dist_km"] * 1000 < hi),
               key=lambda p: -p["stands_m"])
        for lo, hi in bands
    ]
    ranked: list[dict] = []
    while len(ranked) < max_labels and any(grouped):
        for g in grouped:
            if g and len(ranked) < max_labels:
                ranked.append(g.pop(0))
    return ranked
