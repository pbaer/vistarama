"""Build interim, derived and web-ready layers for an AOI.

    python scripts/process.py [aoi-slug] [--skip-tiles]

Pipeline
--------
raw (EPSG:4326)  ->  interim (EPSG:5070, equal-area)  ->  derived  ->  web tiles

Everything metric happens in EPSG:5070 so that the ruggedness kernel radius and
slope are honest in metres. Reprojection to Web Mercator is display-only and
happens last, straight onto the XYZ tile grid.
"""
from __future__ import annotations

import json
import math
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.merge import merge
from rasterio.warp import calculate_default_transform, reproject
from scipy import ndimage, signal

import colormaps as cm
import tiles as tl
from aoi import DERIVED, INTERIM, RAW, WEB, bbox, ensure_dirs, load_aoi

METRIC_CRS = "EPSG:5070"
DEM_RES = 30.0          # metres
LC_RES = 10.0           # metres
S2_RES = 10.0           # Sentinel-2 true-colour, native
RUGGED_RES = 120.0      # metres — the outer average needs no more
RUGGED_RADIUS_M = 5_000.0

# Relief is a different question from ruggedness — "how much vertical range is
# around me" rather than "how broken is the ground" — so it gets its own,
# wider radius. The two are complementary, not competing.
RELIEF_RADIUS_M = 10_000.0

# Ruggedness is measured as variety in the *slope vector*, at three scales:
#   GRAD_BASELINE_M   distance over which the local slope vector is measured.
#                     Also what keeps vertical scale meaningful — a badlands
#                     with 50 m gullies cannot produce a large gradient over a
#                     240 m baseline, an alpine cirque easily can.
#   ROUGH_INNER_M     window over which that slope vector's variance is taken,
#                     i.e. "how much does the slope change around here".
#   RUGGED_RADIUS_M   outer average, i.e. "how consistently broken is this area".
ROUGH_RES = 60.0
GRAD_BASELINE_M = 240.0
ROUGH_INNER_M = 600.0

# Second, near-field ruggedness band. Schirpke et al. (2013) fitted scenic
# distance weights of 0.64 / 0.22 / 0.14 for 0-1.5 / 1.5-10 / 10-50 km, and
# NatureScot's wildness mapping — the only terrain metric validated against
# perceived scenic quality — uses a 250 m radius. Our 5 km kernel is far wider
# than either. This runs the same roughness field through a 1.5 km kernel so
# the two can be compared against reference points.
RUGGED_NEAR_RADIUS_M = 1_500.0

# Topographic openness (Yokoyama, Shirasawa & Pike 2002). Exactly 90 deg on a
# plane of any slope, so it is slope-invariant by construction. Negative
# openness is the depression-angle / cliff-edge term docs/scenery-scoring-data.md
# already specifies.
OPENNESS_RADIUS_M = 1_200.0
OPENNESS_DIRS = 8

# Canopy/built-up gate. Kept at the land cover's native 10 m and a small
# radius: the gate has to resolve clearings, treeline and forest edges, and a
# wide neighbourhood smears exactly the boundaries it exists to find.
DENSITY_RES = LC_RES
DENSITY_RADIUS_M = 100.0

# Nothing on land sits below this; used to reject nodata that survives warping.
ELEV_FLOOR = -500.0

SUN_AZIMUTH = 315.0     # compass degrees, north-west
SUN_ALTITUDE = 45.0

T0 = time.time()


def log(msg: str) -> None:
    print(f"[{time.time() - T0:7.1f}s] {msg}", flush=True)


def compress_opts(dtype) -> dict:
    """DEFLATE with the appropriate predictor.

    Predictor 3 is the floating-point predictor; on smooth elevation rasters
    it is worth roughly 2x over plain LZW, which barely compresses float32 at
    all. Predictor 2 is the horizontal differencing one for integer data.
    """
    kind = np.dtype(dtype).kind
    opts = {"compress": "deflate", "zlevel": 6, "tiled": True,
            "blockxsize": 512, "blockysize": 512, "BIGTIFF": "IF_SAFER"}
    if kind == "f":
        opts["predictor"] = 3
    elif kind in "iu":
        opts["predictor"] = 2
    return opts


# --------------------------------------------------------------------------
# interim: mosaic + reproject to the metric CRS
# --------------------------------------------------------------------------

def mosaic_crop(src_paths: list[Path], bounds_ll, dst: Path, nodata=None) -> Path:
    """Merge tiles, cropped to the AOI, still in the source CRS."""
    if dst.exists():
        log(f"skip mosaic {dst.name} (present)")
        return dst
    srcs = [rasterio.open(p) for p in src_paths]
    try:
        arr, transform = merge(srcs, bounds=bounds_ll, nodata=nodata)
        profile = srcs[0].profile.copy()
        profile.update(
            height=arr.shape[1], width=arr.shape[2], transform=transform,
            count=arr.shape[0], **compress_opts(arr.dtype),
        )
        if nodata is not None:
            profile["nodata"] = nodata
        with rasterio.open(dst, "w", **profile) as out:
            out.write(arr)
    finally:
        for s in srcs:
            s.close()
    log(f"mosaic {dst.name} {arr.shape[2]}x{arr.shape[1]}")
    return dst


def warp_to_metric(src: Path, dst: Path, res: float,
                   resampling: Resampling, dtype=None, nodata=None) -> Path:
    """Reproject a file to EPSG:5070 at a fixed resolution, streaming to disk."""
    if dst.exists():
        log(f"skip warp {dst.name} (present)")
        return dst
    with rasterio.open(src) as s:
        transform, width, height = calculate_default_transform(
            s.crs, METRIC_CRS, s.width, s.height, *s.bounds, resolution=res
        )
        nd = nodata if nodata is not None else s.nodata
        profile = s.profile.copy()
        profile.update(
            crs=METRIC_CRS, transform=transform, width=width, height=height,
            **compress_opts(dtype or s.dtypes[0]),
        )
        if dtype:
            profile["dtype"] = dtype
        if nd is not None:
            profile["nodata"] = nd
        with rasterio.open(dst, "w", **profile) as d:
            for b in range(1, s.count + 1):     # 1 for the metric rasters, 3 for imagery
                reproject(
                    source=rasterio.band(s, b),
                    destination=rasterio.band(d, b),
                    src_transform=s.transform, src_crs=s.crs,
                    src_nodata=s.nodata,
                    dst_transform=transform, dst_crs=METRIC_CRS, dst_nodata=nd,
                    resampling=resampling, num_threads=4,
                )
    log(f"warp   {dst.name} {width}x{height}x{s.count} @{res:g}m {METRIC_CRS}")
    return dst


def add_overviews(path: Path, factors=(2, 4, 8, 16, 32, 64)) -> None:
    """Build internal overviews, if the file has none.

    The panorama's outermost texture level asks for a 120 km square decimated
    into 2048 px. Without overviews that is a 144-megapixel three-band read per
    request; with them GDAL picks a pyramid level near the target scale and the
    read is trivial. Nothing else in the pipeline needs them, so this is only
    applied to imagery.
    """
    with rasterio.open(path, "r+") as ds:
        if ds.overviews(1):
            return
        log(f"overviews {path.name} {list(factors)}")
        ds.build_overviews(list(factors), Resampling.average)
        ds.update_tags(ns="rio_overview", resampling="average")


# --------------------------------------------------------------------------
# derived layers
# --------------------------------------------------------------------------

def _write_like(path: Path, data: np.ndarray, ref_profile: dict,
                dtype: str, nodata) -> None:
    p = ref_profile.copy()
    p.update(dtype=dtype, nodata=nodata, count=1, **compress_opts(dtype))
    with rasterio.open(path, "w", **p) as d:
        d.write(data.astype(dtype), 1)


def derive_terrain(dem_path: Path, out_dir: Path) -> dict[str, Path]:
    """Hillshade and slope, computed in the equal-area CRS."""
    hs_p = out_dir / "hillshade.tif"
    sl_p = out_dir / "slope.tif"
    if hs_p.exists() and sl_p.exists():
        log("skip hillshade/slope (present)")
        return {"hillshade": hs_p, "slope": sl_p}

    with rasterio.open(dem_path) as s:
        z = s.read(1).astype(np.float32)
        profile = s.profile.copy()
        nodata = s.nodata
        px = abs(s.transform.a)

    valid = np.isfinite(z)
    if nodata is not None:
        valid &= z != nodata
    valid &= z > ELEV_FLOOR
    zf = np.where(valid, z, np.nan)
    # fill holes so gradients don't blow up at the edges of voids
    if (~valid).any():
        idx = ndimage.distance_transform_edt(
            ~valid, return_distances=False, return_indices=True
        )
        zf = zf[tuple(idx)]
    zf = np.nan_to_num(zf, nan=0.0)

    # np.gradient returns (d/drow, d/dcol). Rows increase *southward* in a
    # north-up raster, so dz/dnorth is the negative of the row derivative.
    d_row, d_col = np.gradient(zf, px, px)
    dz_de = d_col      # d z / d east
    dz_dn = -d_row     # d z / d north

    slope = np.arctan(np.hypot(dz_de, dz_dn))

    # Illumination as an explicit dot product of the surface normal with the
    # sun direction, both in (east, north, up). Doing it this way avoids the
    # compass-azimuth vs maths-azimuth conversion that the classic
    # aspect-based formulation needs — getting that wrong silently puts the
    # sun in the opposite quadrant, which is what happened here first time.
    nx, ny = -dz_de, -dz_dn
    inv_norm = 1.0 / np.sqrt(nx * nx + ny * ny + 1.0)
    a = math.radians(SUN_AZIMUTH)
    alt = math.radians(SUN_ALTITUDE)
    sx = math.sin(a) * math.cos(alt)   # east component
    sy = math.cos(a) * math.cos(alt)   # north component
    sz = math.sin(alt)
    hs = np.clip((nx * sx + ny * sy + sz) * inv_norm, 0.0, 1.0)
    hs_u8 = np.where(valid, (hs * 254 + 1), 0).astype(np.uint8)

    _write_like(hs_p, hs_u8, profile, "uint8", 0)
    _write_like(sl_p, np.where(valid, np.degrees(slope), -9999.0),
                profile, "float32", -9999.0)
    log(f"derive hillshade.tif, slope.tif ({z.shape[1]}x{z.shape[0]})")
    return {"hillshade": hs_p, "slope": sl_p}


def biweight_kernel(radius_cells: int) -> np.ndarray:
    """K(r) ~ (1 - (r/R)^2)^2, compactly supported, normalised to sum 1.

    Falls faster than 1/r so the near field genuinely dominates once the
    r*dr area Jacobian is accounted for (see docs/visibility-metric.md).
    """
    n = 2 * radius_cells + 1
    y, x = np.mgrid[-radius_cells:radius_cells + 1,
                    -radius_cells:radius_cells + 1]
    r = np.hypot(x, y) / float(radius_cells)
    k = np.where(r <= 1.0, (1.0 - r ** 2) ** 2, 0.0)
    assert k.shape == (n, n)
    return (k / k.sum()).astype(np.float32)


def _sep_box(a: np.ndarray, r: int) -> np.ndarray:
    """Separable box mean over a (2r+1) square window."""
    box = np.full(2 * r + 1, 1.0 / (2 * r + 1))
    t = ndimage.correlate1d(a, box, axis=1, mode="nearest")
    return ndimage.correlate1d(t, box, axis=0, mode="nearest")


def plane_gradient(z: np.ndarray, px: float, radius_m: float):
    """Local slope vector from a least-squares plane fit over `radius_m`.

    Fitting rather than differencing gives a gradient representative of the
    whole baseline instead of two adjacent cells, which is what makes the
    result insensitive to DEM noise and to sub-baseline detail.

    {1, x, y} is orthogonal under a symmetric window, so no matrix solve is
    needed: a = E[xz]/E[x^2], b = E[yz]/E[y^2].
    """
    r = max(1, int(round(radius_m / px)))
    n = 2 * r + 1
    box = np.full(n, 1.0 / n)
    lin = ((np.arange(n) - r) * px) / n       # yields E[x * z]

    def sep(a, wx, wy):
        t = ndimage.correlate1d(a, wx, axis=1, mode="nearest")
        return ndimage.correlate1d(t, wy, axis=0, mode="nearest")

    exx = px * px * r * (r + 1) / 3.0         # E[x^2] over the window
    return sep(z, lin, box) / exx, sep(z, box, lin) / exx


def local_roughness(z: np.ndarray, px: float) -> np.ndarray:
    """Variety in the local slope vector — the thing "rugged" actually means.

    Earlier attempts measured elevation instead, and both failed the same way:

      * sigma of elevation measures total relief spread, so a smooth planar
        valley wall beats a jumbled cirque.
      * RMS departure from a fitted plane looked like the fix, but the residual
        amplitude itself scales with steepness (steeper ground has
        proportionally deeper gullies), so it came out 96% correlated with mean
        slope — *worse* than the sigma it replaced.

    The discriminator is not how steep the ground is, or even how far it
    departs from a plane, but how often the slope vector *changes*. A hillside
    has a nearly constant gradient however steep; a cirque swings from flat
    slabs to vertical faces and back within a few hundred metres. So:

        sigma_s = degrees( sqrt( -2 * ln( ||mean unit normal|| ) ) )

    This is the **spherical standard deviation of surface normals** of Lindsay,
    Newman & Francioni 2019 (doi:10.3390/geosciences9070322), which ships as
    WhiteboxTools `SphericalStdDevOfNormals`. They describe it as "zero for
    flat *and inclined* planes and increases infinitely with greater surface
    complexity" — exactly the property wanted here.

    Two deviations from the paper, both deliberate:

    * They pre-blur the DEM with a Gaussian of sigma = radius/3 to suppress
      sub-scale detail; we fit a least-squares plane over GRAD_BASELINE_M
      instead, which serves the same purpose and is what gives the measure its
      vertical-scale sensitivity (a badlands with 50 m gullies cannot produce a
      large gradient over a 240 m baseline; an alpine cirque can).
    * The dispersion is then taken over a separate, wider window.

    An earlier version used the variance of the raw gradient. That is very
    nearly the same thing (r = 0.998) but inflates by sec^2(theta), because
    gradient space is a gnomonic projection — 4x at 60 degrees — which left a
    residual slope coupling on steep ground. Normalising to unit normals first
    removes it.
    """
    gx, gy = plane_gradient(z, px, GRAD_BASELINE_M)
    inv = 1.0 / np.sqrt(gx * gx + gy * gy + 1.0)
    nx, ny, nz = -gx * inv, -gy * inv, inv        # unit surface normals

    r = max(1, int(round(ROUGH_INNER_M / px)))
    mx, my, mz = _sep_box(nx, r), _sep_box(ny, r), _sep_box(nz, r)
    # ||mean unit normal||; 1 means perfectly aligned (a plane), 0 fully random
    rbar = np.sqrt(mx * mx + my * my + mz * mz)
    rbar = np.clip(rbar, 1e-12, 1.0)
    return np.degrees(np.sqrt(-2.0 * np.log(rbar)))


def derive_openness(dem_path: Path, out_dir: Path) -> dict[str, Path]:
    """Yokoyama, Shirasawa & Pike (2002) topographic openness.

    Along each of 8 azimuths, find the extreme elevation angle to any point
    within OPENNESS_RADIUS_M, then

        positive  Phi = mean over azimuths of (90 - max angle)   sky-ness
        negative  Psi = mean over azimuths of (90 + min angle)   ground-ness

    Both in degrees, both 0-180. Phi > 90 is convex/dominant ground (ridges,
    summits, spurs); Phi < 90 is concave/enclosed (gullies, basins). Psi is the
    mirror, and is the **depression-angle / cliff-edge** measure that
    docs/scenery-scoring-data.md specifies: standing at the lip of a drop gives
    a strongly negative minimum angle and so a large Psi.

    The property that makes it worth having: on a uniform plane the horizon
    angle along azimuth phi is arctan(tan(theta)*cos(phi)), so antipodal pairs
    cancel exactly and Phi = Psi = 90.000 deg at *any* slope. It is therefore
    slope-invariant by construction, unlike sky-view factor, which clips at the
    mathematical horizon and so carries a first-order slope term (1 - theta/pi).

    Also emitted is the **defect**, 180 - (Phi + Psi). Per-azimuth,
    Phi + Psi = 180 - (angular relief range), so the defect is exactly zero iff
    every radial profile is straight and strictly positive otherwise — a single
    slope-invariant complexity number.
    """
    paths = {
        "openness_pos": out_dir / "openness_pos.tif",
        "openness_neg": out_dir / "openness_neg.tif",
        "openness_defect": out_dir / "openness_defect.tif",
    }
    if all(p.exists() for p in paths.values()):
        log("skip openness (present)")
        return paths

    with rasterio.open(dem_path) as s:
        fine = int(round(ROUGH_RES / abs(s.transform.a)))
        h, w = s.height // fine, s.width // fine
        z = s.read(1, out_shape=(h, w),
                   resampling=Resampling.average).astype(np.float32)
        nodata = s.nodata
        profile = s.profile.copy()
        transform = s.transform * s.transform.scale(fine, fine)

    valid = np.isfinite(z) & (z > ELEV_FLOOR)
    if nodata is not None:
        valid &= z != nodata
    if (~valid).any():
        idx = ndimage.distance_transform_edt(
            ~valid, return_distances=False, return_indices=True)
        z = z[tuple(idx)]
    z = np.nan_to_num(z, nan=0.0)

    steps = max(1, int(round(OPENNESS_RADIUS_M / ROUGH_RES)))
    dirs = [(0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1), (-1, 0), (-1, 1)]
    pad = steps + 1
    zp = np.pad(z, pad, mode="edge")

    phi_sum = np.zeros((h, w), dtype=np.float32)
    psi_sum = np.zeros((h, w), dtype=np.float32)
    log(f"openness: {w}x{h} @{ROUGH_RES:g}m, {OPENNESS_DIRS} azimuths, "
        f"{steps} steps to {OPENNESS_RADIUS_M:g} m")

    for dr, dc in dirs:
        # Track the extreme *tangent* and take arctan once at the end — the
        # comparison is monotonic in the angle, and this turns ~160 arctan
        # passes over 15M cells into 16.
        tmax = np.full((h, w), -np.inf, dtype=np.float32)
        tmin = np.full((h, w), np.inf, dtype=np.float32)
        span = math.hypot(dr, dc) * ROUGH_RES
        for k in range(1, steps + 1):
            r0, c0 = pad + k * dr, pad + k * dc
            t = (zp[r0:r0 + h, c0:c0 + w] - z) / (k * span)
            np.maximum(tmax, t, out=tmax)
            np.minimum(tmin, t, out=tmin)
        phi_sum += 90.0 - np.degrees(np.arctan(tmax))
        psi_sum += 90.0 + np.degrees(np.arctan(tmin))

    phi = phi_sum / OPENNESS_DIRS
    psi = psi_sum / OPENNESS_DIRS
    defect = 180.0 - (phi + psi)

    profile.update(height=h, width=w, transform=transform)
    for key, arr in (("openness_pos", phi), ("openness_neg", psi),
                     ("openness_defect", defect)):
        _write_like(paths[key], np.where(valid, arr, -9999.0),
                    profile, "float32", -9999.0)
    v = defect[valid]
    log(f"derive openness_pos/neg/defect.tif  Phi median {np.median(phi[valid]):.1f} deg, "
        f"Psi median {np.median(psi[valid]):.1f} deg, defect median {np.median(v):.1f} deg")
    return paths


def water_mask(lc_path: Path, shape, transform, crs) -> np.ndarray:
    """Permanent water (WorldCover class 80) resampled onto an arbitrary grid."""
    out = np.zeros(shape, dtype=np.uint8)
    with rasterio.open(lc_path) as s:
        reproject(
            source=rasterio.band(s, 1), destination=out,
            src_transform=s.transform, src_crs=s.crs, src_nodata=s.nodata,
            dst_transform=transform, dst_crs=crs, dst_nodata=0,
            resampling=Resampling.mode,
        )
    return out == 80


def derive_ruggedness(dem_path: Path, out_path: Path,
                      sigma_path: Path | None = None,
                      lc_path: Path | None = None,
                      near_path: Path | None = None) -> Path:
    """Local terrain roughness, averaged over the neighbourhood.

    Two stages:

      1. `local_roughness` measures how much the slope *vector* varies within
         a few hundred metres — near zero on a smooth hillside of any
         steepness, large in a cirque of slabs, tarns and spires.
      2. That field is averaged over the RUGGED_RADIUS_M biweight kernel, so
         the result says "how consistently broken is the terrain around here"
         rather than "how much total relief is nearby". Averaging a *local*
         quantity is what separates one big valley (locally uniform almost
         everywhere) from genuinely complex ground.

    Output is in **degrees** (see `local_roughness`). Vertical-scale
    sensitivity — a cirque with 300 m walls outranking a badlands with 20 m
    gullies — comes from GRAD_BASELINE_M, *not* from the choice of unit.
    Cell-level VRM fails that test; VRM computed on the same 240 m baseline
    passes it just as well as this does. The baseline is the load-bearing
    ingredient.

    Equivalent to the spherical standard deviation of surface normals of
    Lindsay, Newman & Francioni 2019 (doi:10.3390/geosciences9070322), which
    ships as WhiteboxTools `SphericalStdDevOfNormals`. They pre-blur the DEM
    with a Gaussian of sigma = r/3 where this fits a least-squares plane; the
    two agree to r = 0.998 at the 5 km output scale. Known difference: because
    gradient space is a gnomonic projection, this inflates by sec^2(theta) for
    a fixed *angular* dispersion, giving a small residual slope coupling on
    very steep ground that the true spherical form does not have.

    `sigma_path` additionally writes *relief* — plain elevation sigma on its
    own wider RELIEF_RADIUS_M kernel. That answers a different question ("how
    much vertical range is around me") and is kept as a companion layer, not a
    superseded one: a broad glacial trough scores high on relief and low on
    ruggedness, an alpine cirque scores high on both, and the pair carries more
    than either alone.

    `lc_path` supplies a permanent-water mask. Lake surfaces are excluded from
    both kernels: a large dead-flat area pinned at the window's low end makes
    the elevation distribution bimodal, and sigma rewards bimodality, so Lake
    Chelan scored ~14% higher than its actual relief warrants while lake-free
    valleys of comparable depth were unaffected. Water still participates in
    the local gradient fit — shoreline slopes are real — it just does not
    contribute its own flatness to the averages.
    """
    # every output has to be present to skip — guarding on out_path alone
    # silently skips regenerating the others when only their parameters changed
    if (out_path.exists()
            and (sigma_path is None or sigma_path.exists())
            and (near_path is None or near_path.exists())):
        log("skip ruggedness / ruggedness_near / relief (present)")
        return out_path

    # --- inner scale: local departure from a plane, at ROUGH_RES -----------
    with rasterio.open(dem_path) as s:
        fine = int(round(ROUGH_RES / abs(s.transform.a)))
        fh, fw = s.height // fine, s.width // fine
        zf = s.read(1, out_shape=(fh, fw),
                    resampling=Resampling.average).astype(np.float32)
        nodata = s.nodata
        profile = s.profile.copy()
        base_transform = s.transform

    fine_valid = np.isfinite(zf) & (zf > ELEV_FLOOR)
    if nodata is not None:
        fine_valid &= zf != nodata
    zf = np.where(fine_valid, zf, np.nan)
    # fill voids from the nearest valid cell so the plane fit isn't skewed
    if (~fine_valid).any():
        idx = ndimage.distance_transform_edt(
            ~fine_valid, return_distances=False, return_indices=True)
        zf = zf[tuple(idx)]
    zf = np.nan_to_num(zf, nan=0.0).astype(np.float64)

    rough = local_roughness(zf, ROUGH_RES)
    log(f"local roughness: {fw}x{fh} @{ROUGH_RES:g}m, slope vector over "
        f"{GRAD_BASELINE_M:g} m, variance over {ROUGH_INNER_M:g} m")

    # --- outer scale: average that field over the biweight kernel ----------
    coarse = int(round(RUGGED_RES / ROUGH_RES))
    h, w = fh // coarse, fw // coarse
    rough_c = rough[:h * coarse, :w * coarse].reshape(
        h, coarse, w, coarse).mean(axis=(1, 3))
    z_c = zf[:h * coarse, :w * coarse].reshape(
        h, coarse, w, coarse).mean(axis=(1, 3))
    valid_c = fine_valid[:h * coarse, :w * coarse].reshape(
        h, coarse, w, coarse).any(axis=(1, 3))
    transform = base_transform * base_transform.scale(fine * coarse,
                                                      fine * coarse)

    # Lake surfaces contribute no terrain information and, being flat and low,
    # skew both kernels. Weight by land only — but still evaluate *at* water
    # cells from the surrounding land, so lakes don't become holes in the map.
    if lc_path is not None and Path(lc_path).exists():
        wet = water_mask(lc_path, (h, w), transform, profile["crs"])
        log(f"water mask: {100 * wet.mean():.1f}% of the coarse grid")
    else:
        wet = np.zeros((h, w), dtype=bool)
    land_c = valid_c & ~wet

    radius = int(round(RUGGED_RADIUS_M / RUGGED_RES))
    k = biweight_kernel(radius).astype(np.float64)
    log(f"ruggedness: {w}x{h} @{RUGGED_RES:g}m, kernel r={radius} cells "
        f"({RUGGED_RADIUS_M / 1000:g} km)")

    v = land_c.astype(np.float64)
    kv = signal.fftconvolve(v, k, mode="same")
    kr = signal.fftconvolve(np.where(land_c, rough_c, 0.0), k, mode="same")
    with np.errstate(invalid="ignore", divide="ignore"):
        rugged = kr / kv
    support = (kv > 0.35) & valid_c
    rugged = np.where(support, rugged, -9999.0)

    profile.update(height=h, width=w, transform=transform)
    _write_like(out_path, rugged, profile, "float32", -9999.0)
    fin = rugged[rugged > -9998]
    log(f"derive ruggedness.tif  range {fin.min():.1f}-{fin.max():.1f} deg, "
        f"median {np.median(fin):.1f} deg  (spherical SD of normals)")

    # --- the same roughness field through a near-field kernel --------------
    if near_path is not None and not near_path.exists():
        rn = int(round(RUGGED_NEAR_RADIUS_M / RUGGED_RES))
        kn = biweight_kernel(rn).astype(np.float64)
        kvn = signal.fftconvolve(v, kn, mode="same")
        krn = signal.fftconvolve(np.where(land_c, rough_c, 0.0), kn, mode="same")
        with np.errstate(invalid="ignore", divide="ignore"):
            near = krn / kvn
        near = np.where((kvn > 0.35) & valid_c, near, -9999.0)
        _write_like(near_path, near, profile, "float32", -9999.0)
        fn = near[near > -9998]
        log(f"derive ruggedness_near.tif  kernel r={rn} cells "
            f"({RUGGED_NEAR_RADIUS_M / 1000:g} km), range {fn.min():.1f}-"
            f"{fn.max():.1f} deg, median {np.median(fn):.1f} deg")

    # --- relief: elevation sigma, on its own wider kernel -----------------
    if sigma_path is not None and not sigma_path.exists():
        r2 = int(round(RELIEF_RADIUS_M / RUGGED_RES))
        k2 = biweight_kernel(r2).astype(np.float64)
        zz = np.where(land_c, z_c, 0.0)
        kv2 = signal.fftconvolve(v, k2, mode="same")
        kz = signal.fftconvolve(zz, k2, mode="same")
        kz2 = signal.fftconvolve(zz * zz, k2, mode="same")
        with np.errstate(invalid="ignore", divide="ignore"):
            mean = kz / kv2
            var = kz2 / kv2 - mean ** 2
        sigma = np.where((kv2 > 0.35) & valid_c,
                         np.sqrt(np.clip(var, 0.0, None)), -9999.0)
        _write_like(sigma_path, sigma, profile, "float32", -9999.0)
        fs = sigma[sigma > -9998]
        log(f"derive relief_sigma.tif  kernel r={r2} cells "
            f"({RELIEF_RADIUS_M / 1000:g} km), range {fs.min():.0f}-"
            f"{fs.max():.0f} m, median {np.median(fs):.0f} m")
    return out_path


def derive_densities(lc_path: Path, out_dir: Path) -> dict[str, Path]:
    """Neighbourhood fraction of tree cover and of built-up.

    WorldCover's tree class is binary at 10 m; the local fraction over a small
    neighbourhood turns it into the graded canopy density the gate needs, with
    no extra dataset.

    Output stays at the source's native 10 m and uses a 100 m radius. Both
    matter: the gate must resolve clearings, treeline and forest edges, and a
    wide kernel smooths away exactly the boundaries it exists to detect.

    Stored as uint8 percent (0-100, 255 = nodata) — a float32 at 10 m over this
    AOI would be 2.2 GB for data whose useful precision is ~1%.
    """
    canopy_p = out_dir / "canopy_density.tif"
    built_p = out_dir / "builtup_density.tif"
    if canopy_p.exists() and built_p.exists():
        log("skip canopy/builtup density (present)")
        return {"canopy_density": canopy_p, "builtup_density": built_p}

    rad = int(round(DENSITY_RADIUS_M / DENSITY_RES))
    size = 2 * rad + 1

    with rasterio.open(lc_path) as s:
        h, w = s.height, s.width
        prof = s.profile.copy()
        prof.update(dtype="uint8", nodata=255, count=1,
                    **compress_opts("uint8"))

        with rasterio.open(canopy_p, "w", **prof) as dc, \
                rasterio.open(built_p, "w", **prof) as db:
            step = 2048
            for r0 in range(0, h, step):
                r1 = min(h, r0 + step)
                # read with a halo so the filter sees across chunk seams
                rr0, rr1 = max(0, r0 - rad), min(h, r1 + rad)
                block = s.read(1, window=rasterio.windows.Window(
                    0, rr0, w, rr1 - rr0))
                nodata_mask = block == 0
                lo, hi = r0 - rr0, (r0 - rr0) + (r1 - r0)

                for cls, dst in ((10, dc), (50, db)):
                    frac = ndimage.uniform_filter(
                        (block == cls).astype(np.float32),
                        size=size, mode="nearest")
                    out = np.clip(frac[lo:hi] * 100.0 + 0.5, 0, 100).astype(
                        np.uint8)
                    out[nodata_mask[lo:hi]] = 255
                    dst.write(out, 1, window=rasterio.windows.Window(
                        0, r0, w, r1 - r0))
                    del frac, out

    log(f"derive canopy_density.tif, builtup_density.tif "
        f"({w}x{h} @{DENSITY_RES:g}m, {DENSITY_RADIUS_M:g}m radius, uint8 %)")
    return {"canopy_density": canopy_p, "builtup_density": built_p}


# --------------------------------------------------------------------------
# sea
# --------------------------------------------------------------------------

# Water at sea level: WorldCover's water class where the DEM is within this
# of 0 m. Lakes sit higher (Lake Washington ~6 m) and stay land here, drawn
# by their own polygons; the sea, which OSM maps only as coastline ways, gets
# this mask instead. Over Puget Sound 3DEP holds ~-0.4 m, and region.py set
# the open sea it has no tile for to exactly 0.
SEA_MAX_ELEV_M = 2.0


def derive_sea(lc_path: Path, dem_path: Path, dst: Path, block: int = 4096) -> Path | None:
    """derived/sea.tif on the land-cover grid: 1 where the sea is, 0 elsewhere."""
    if dst.exists():
        log(f"skip sea (present)")
        return dst
    from rasterio.windows import from_bounds
    with rasterio.open(lc_path) as lc, rasterio.open(dem_path) as dem:
        profile = {"driver": "GTiff", "dtype": "uint8", "count": 1, "nodata": 0,
                   "width": lc.width, "height": lc.height, "crs": lc.crs,
                   "transform": lc.transform, **compress_opts("uint8")}
        part = dst.with_name(dst.stem + ".part.tif")
        n = 0
        with rasterio.open(part, "w", **profile) as out:
            for r0 in range(0, lc.height, block):
                for c0 in range(0, lc.width, block):
                    win = rasterio.windows.Window(c0, r0, min(block, lc.width - c0), min(block, lc.height - r0))
                    cls = lc.read(1, window=win)
                    water = cls == 80
                    if not water.any():
                        continue
                    b = rasterio.windows.bounds(win, lc.transform)
                    z = dem.read(1, window=from_bounds(*b, transform=dem.transform),
                                 out_shape=cls.shape, boundless=True,
                                 fill_value=dem.nodata if dem.nodata is not None else -1e9,
                                 resampling=Resampling.nearest)
                    ok = np.isfinite(z) & (z > -1e5)
                    if dem.nodata is not None:
                        ok &= z != dem.nodata
                    sea = water & ok & (z <= SEA_MAX_ELEV_M)
                    if sea.any():
                        out.write(sea.astype(np.uint8), 1, window=win)
                        n += int(sea.sum())
    part.replace(dst)
    log(f"derive sea.tif  {n * abs(profile['transform'].a) ** 2 / 1e6:,.0f} km² of sea")
    return dst


# --------------------------------------------------------------------------
# vectors: zoom-tiered simplification
# --------------------------------------------------------------------------

# theme -> list of (tier, max simplify tolerance in degrees, filter fn)
def _peak_rank(props: dict) -> float:
    try:
        return float(str(props.get("ele", "0")).split()[0])
    except (ValueError, IndexError):
        return 0.0


# theme -> [(tier, simplify tolerance in degrees), ...]
# ~0.002 deg is roughly 150 m, 0.0004 is ~30 m, 0.00005 is ~4 m.
VECTOR_LOD = {
    # Trails start at mid: zoomed out they are a dense red haze that obscures
    # the terrain without being individually readable.
    # ... and get a "full" tier like roads: every short park path only from z14
    "trails":      [("mid", 0.0004), ("high", 0.0001), ("full", 0.00005)],
    # Roads get a fourth tier: residential streets only from z14 ("full"),
    # see TIER_CLASS_KEEP. Other themes have no "full" tier, and the viewer
    # falls back to their finest one.
    "roads":       [("low", 0.002), ("mid", 0.0004), ("high", 0.0001), ("full", 0.00005)],
    "water_lines": [("low", 0.002), ("mid", 0.0004), ("high", 0.00005)],
    "water_areas": [("low", 0.001), ("mid", 0.0002), ("high", 0.00005)],
    "glaciers":    [("low", 0.001), ("high", 0.00005)],
    "peaks":       [("low", 0.0), ("mid", 0.0), ("high", 0.0)],
    "passes":      [("high", 0.0)],
    # from the POI download, camps only (featurescore.poi_kind)
    "camps":       [("high", 0.0)],
    "protected":   [("low", 0.001), ("high", 0.0001)],
}

# Coarse tiers drop features that are too small to see at that zoom, rather
# than taking a global top-N. Top-N throws away the entire tail wherever it
# happens to be, which silently deletes whole areas of the map; an extent
# threshold only removes things that would render as a sub-pixel speck. Named
# features are kept regardless of size.
#
# Thresholds are bbox diagonals in degrees: ~0.0035 deg is roughly 300 m,
# which is around one screen pixel at z9.
TIER_MIN_EXTENT: dict[str, float] = {"low": 0.0035, "mid": 0.0007, "high": 0.0}

# Classes a theme may keep at a given tier, on top of the extent test. Roads
# stay visible at every zoom because they are how you orient, but each tier out
# keeps only the roads that still mean something at that scale, and the map
# draws far less: across western Washington the old "low" tier (through
# routes down to secondary) was 41k ways and 20 MB, and the Seattle cell of
# the "high" tier alone was 35 MB.
#   low  (<= z9)   motorway, trunk, primary: the highways you navigate by
#   mid  (z10-11)  + secondary, tertiary and the motorway/trunk links
#   high (z12-13)  + the remaining links and unclassified (forest roads)
#   full (>= z14)  everything, residential streets included
# Rivers likewise: zoomed out, only waterway=river, not every named creek.
_ROADS_LOW = {"motorway", "trunk", "primary"}
_ROADS_MID = _ROADS_LOW | {"secondary", "tertiary", "motorway_link", "trunk_link"}
_ROADS_HIGH = _ROADS_MID | {"primary_link", "secondary_link", "tertiary_link", "unclassified"}
TIER_CLASS_KEEP: dict[str, dict[str, set[str]]] = {
    "roads": {"low": _ROADS_LOW, "mid": _ROADS_MID, "high": _ROADS_HIGH},
    "water_lines": {"low": {"river"}, "mid": {"river", "canal"}},
}

# Per-theme extent floors that override TIER_MIN_EXTENT. Trails at "mid"
# (z10-11) drop the unnamed short paths of every city park, which at that
# zoom are specks: ~0.004 deg is ~350 m.
TIER_MIN_EXTENT_THEME: dict[str, dict[str, float]] = {"trails": {"mid": 0.004, "high": 0.0015}}

# At the coarse tiers a feature keeps only the tags the viewer reads (styling,
# the hover card); OSM ways carry a dozen more (tiger:*, lanes, sources...)
# that made up much of each file. "high" and "full" keep every tag, for the
# Alt-click tag table.
TIER_SLIM = {"low", "mid"}
# Roads and rivers are drawn rather than read, so they slim through "high"
# too: Seattle's z12-13 road cell was 6 MB, most of it tags.
TIER_SLIM_THEME = {"roads": {"low", "mid", "high"}, "water_lines": {"low", "mid", "high"}}
VIEWER_TAGS = {
    "name", "ref", "highway", "waterway", "natural", "water", "landuse", "leisure",
    "boundary", "protect_class", "protection_title", "operator", "tourism", "amenity",
    "ele", "prominence", "mountain_pass", "intermittent", "sac_scale", "surface",
    "trail_visibility", "access", "tracktype", "lanes", "maxspeed", "tunnel", "bridge",
    "fee", "backcountry", "reservation", "drinking_water", "toilets", "volcano:status",
    "reservoir_type",
}

# Roads at the coarse tiers are drawn, not inspected, and OSM splits every
# highway into hundreds of short ways (at each bridge, speed change, lane
# change). There, ways sharing class, ref and name are merged into
# continuous lines first, which is most of what made the low tier 19k
# features.
TIER_MERGE = {"roads": {"low", "mid"}}


def _merge_lines(feats: list[dict]) -> list[dict]:
    """Ways with the same highway class, ref and name, joined end to end."""
    from shapely.geometry import MultiLineString, mapping, shape
    from shapely.ops import linemerge
    groups: dict[tuple, list] = {}
    for f in feats:
        g = f.get("geometry") or {}
        if g.get("type") not in ("LineString", "MultiLineString"):
            continue
        p = f.get("properties") or {}
        key = (p.get("highway"), p.get("ref"), p.get("name"))
        groups.setdefault(key, []).append(shape(g))
    out = []
    for (hw, ref, name), geoms in groups.items():
        lines = []
        for gg in geoms:
            lines.extend(gg.geoms if gg.geom_type == "MultiLineString" else [gg])
        merged = linemerge(MultiLineString(lines)) if len(lines) > 1 else lines[0]
        parts = merged.geoms if merged.geom_type == "MultiLineString" else [merged]
        props = {k: v for k, v in (("highway", hw), ("ref", ref), ("name", name)) if v}
        for part in parts:
            out.append({"type": "Feature", "properties": props, "geometry": mapping(part)})
    return out
# the tag each theme's classes come from
TIER_CLASS_KEY = {"roads": "highway", "water_lines": "waterway"}


def _tier_class_ok(theme: str, tier: str, props: dict) -> bool:
    keep = TIER_CLASS_KEEP.get(theme, {}).get(tier)
    if not keep:
        return True
    return (props or {}).get(TIER_CLASS_KEY.get(theme, "highway")) in keep

# Points have no extent, so they still need ranking to stay legible when
# zoomed out. Peaks rank by elevation; everything else is small enough to ship
# whole at every tier.
POINT_TIER_LIMIT: dict[str, dict[str, int]] = {
    "peaks": {"low": 250, "mid": 1000},
}


def _bbox_diag(geom: dict) -> float:
    w, s, e, n = _feature_bbox(geom)
    return math.hypot(e - w, n - s)


# Tiers larger than this get split into a spatial grid, so that zooming in
# fetches only the cells on screen instead of the whole AOI. LOD alone fixes
# the zoomed-out case; gridding fixes the zoomed-in one.
#
# The grid is sized in degrees per tier, not as a fixed count: a fixed 6x4
# over western Washington made cells ~100 km across, and the one over Seattle
# held 35 MB of roads and 16 MB of trails, all fetched to show a few blocks.
# Each tier's cell is a fraction of the area a screen shows at that tier's
# zooms, so a view fetches a handful of cells and little beyond them.
GRID_SPLIT_BYTES = 2_000_000
GRID_CELL_DEG = {"low": 1.0, "mid": 0.5, "high": 0.25, "full": 0.125}


def _feature_bbox(geom: dict) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []

    def walk(c):
        if c and isinstance(c[0], (int, float)):
            xs.append(c[0])
            ys.append(c[1])
        else:
            for sub in c:
                walk(sub)

    walk(geom.get("coordinates", []))
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))


def _grid_shape(tier: str, bounds) -> tuple[int, int]:
    """(cols, rows) for a tier's grid over bounds: cells of about
    GRID_CELL_DEG[tier] degrees."""
    w, s, e, n = bounds
    cell = GRID_CELL_DEG.get(tier, 0.25)
    return max(1, math.ceil((e - w) / cell - 1e-9)), max(1, math.ceil((n - s) / cell - 1e-9))


def _split_grid(feats: list[dict], bounds: tuple[float, float, float, float],
                cols: int, rows: int):
    """Bucket features into a cols x rows grid by bbox overlap.

    A feature spanning several cells is written into each of them. That costs a
    little duplication but avoids clipping geometry, which would complicate
    both the writer and the popup data.
    """
    w, s, e, n = bounds
    dx = (e - w) / cols
    dy = (n - s) / rows
    cells: dict[tuple[int, int], list[dict]] = {}
    for f in feats:
        fw, fs, fe, fn = _feature_bbox(f["geometry"])
        c0 = max(0, min(cols - 1, int((fw - w) / dx))) if dx else 0
        c1 = max(0, min(cols - 1, int((fe - w) / dx))) if dx else 0
        r0 = max(0, min(rows - 1, int((fs - s) / dy))) if dy else 0
        r1 = max(0, min(rows - 1, int((fn - s) / dy))) if dy else 0
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                cells.setdefault((r, c), []).append(f)
    return cells


def _round_coords(geom, nd=5):
    def rnd(c):
        if isinstance(c[0], (int, float)):
            return [round(c[0], nd), round(c[1], nd)]
        return [rnd(x) for x in c]
    geom["coordinates"] = rnd(geom["coordinates"])
    return geom


def build_vector_lod(slug: str, out_dir: Path,
                     bounds: tuple[float, float, float, float]) -> dict:
    from shapely.geometry import mapping, shape

    out_dir.mkdir(parents=True, exist_ok=True)
    # a rebuild can change tiers and grids; files from the old layout would
    # otherwise sit there unused
    for old in out_dir.glob("*.geojson"):
        old.unlink()
    manifest = {}
    import featurescore
    for theme, tiers in VECTOR_LOD.items():
        src = RAW / "osm" / f"{slug}_{featurescore.RAW_THEME.get(theme, theme)}.geojson"
        if not src.exists():
            continue
        gj = json.loads(src.read_text(encoding="utf-8"))
        feats = gj.get("features", [])
        if theme == "camps":
            # huts, campsites and trail shelters; bus and picnic shelters are
            # not somewhere to stay
            feats = [f for f in feats
                     if featurescore.poi_kind(f.get("properties") or {}) == "camp"]
        if not feats:
            log(f"vectors {theme}: empty, skipping")
            continue

        # Trails carry their length and, when OSM splits them into several
        # ways, which trail they belong to. The viewer highlights the whole
        # trail on hover and shows segment and trail lengths in the card. The
        # grouping is featurescore's own, so the highlight is exactly what V
        # scores. Lengths are geodesic, from the raw geometry, so the
        # simplified tiers report the same numbers as the full one.
        extra_info = {}
        if theme == "camps":
            for f in feats:
                if f.get("id"):
                    extra_info[f["id"]] = {"camp": 1}
        if theme == "water_areas":
            # lakes big enough to score carry their area, so the viewer only
            # offers V on what featurescore will accept
            import featurescore
            for f in feats:
                if f.get("id"):
                    a = featurescore.lake_area(f)
                    if a is not None:
                        extra_info[f["id"]] = {"lake": 1, "area": round(a)}
        if theme == "trails":
            import featurescore
            from pyproj import Geod
            geod = Geod(ellps="WGS84")
            seg = {f["id"]: geod.geometry_length(shape(f["geometry"]))
                   for f in feats if f.get("id")}
            groups = featurescore.trail_groups(slug)
            for wid, m in seg.items():
                info = {"len": round(m)}
                members = groups.get(wid)
                if members and len(members) > 1:
                    info["trail"] = featurescore.group_key(members)
                    info["trail_len"] = round(sum(seg.get(x, 0.0) for x in members))
                    info["trail_n"] = len(members)
                extra_info[wid] = info

        entries = []
        for tier, tol in tiers:
            point_limit = POINT_TIER_LIMIT.get(theme, {}).get(tier)
            threshold = TIER_MIN_EXTENT_THEME.get(theme, {}).get(
                tier, TIER_MIN_EXTENT.get(tier, 0.0))
            if point_limit and len(feats) > point_limit:
                sel = sorted(
                    feats, key=lambda f: -_peak_rank(f["properties"] or {})
                )[:point_limit]
            elif threshold > 0:
                sel = [f for f in feats
                       if (f["properties"] or {}).get("name")
                       or _bbox_diag(f["geometry"]) >= threshold]
            else:
                sel = feats
            # Class filter, applied after the extent test and *not* exempting
            # named features: a named residential street is still clutter at
            # z9, and the point of this tier is to leave only the through
            # routes you navigate by.
            if TIER_CLASS_KEEP.get(theme, {}).get(tier):
                sel = [f for f in sel
                       if _tier_class_ok(theme, tier, f["properties"] or {})]
            if tier in TIER_MERGE.get(theme, ()):
                sel = _merge_lines(sel)
            slim = tier in TIER_SLIM_THEME.get(theme, TIER_SLIM)

            out_feats = []
            for f in sel:
                g = f["geometry"]
                if tol > 0:
                    try:
                        s = shape(g).simplify(tol, preserve_topology=True)
                        if s.is_empty:
                            continue
                        g = mapping(s)
                    except Exception:  # noqa: BLE001
                        pass
                props = f["properties"] or {}
                if slim:
                    props = {k: v for k, v in props.items() if k in VIEWER_TAGS}
                feat = {
                    "type": "Feature",
                    "properties": props,
                    "geometry": _round_coords(dict(g)),
                }
                # the OSM id ("way/123", "node/456"): the viewer keys stored
                # view scores by it, and it is what ties a simplified tile
                # feature back to its full-resolution raw geometry
                if f.get("id"):
                    feat["id"] = f["id"]
                    # GeoJSON foreign members, so they stay out of the tag
                    # table the Alt-click popup shows
                    feat.update(extra_info.get(f["id"], {}))
                out_feats.append(feat)

            def dump(features):
                return json.dumps(
                    {"type": "FeatureCollection", "features": features},
                    separators=(",", ":"))

            payload = dump(out_feats)
            cells = None
            if len(payload) > GRID_SPLIT_BYTES:
                cols, rows = _grid_shape(tier, bounds)
                cells = _split_grid(out_feats, bounds, cols, rows)
                # Big polygons (a national forest) land in every cell they
                # touch, so a grid can leave every cell nearly as big as the
                # whole: protected areas went from one 4 MB file to 49 MB of
                # cells. What matters is what a view fetches, a few cells, so
                # keep one file when the largest cell is over a quarter of it,
                # or the copies add up to more than eight times it.
                sizes = [len(dump(cf)) for cf in cells.values()]
                if 4 * max(sizes) > len(payload) or sum(sizes) > 8 * len(payload):
                    cells = None
            if cells:
                total = 0
                names = []
                for (r, c), cf in sorted(cells.items()):
                    name = f"r{r}c{c}"
                    dst = out_dir / f"{theme}.{tier}.{name}.geojson"
                    dst.write_text(dump(cf), encoding="utf-8")
                    total += dst.stat().st_size
                    names.append(name)
                entries.append({
                    "tier": tier,
                    "pattern": f"vectors/{theme}.{tier}.{{cell}}.geojson",
                    "grid": {"cols": cols, "rows": rows,
                             "bounds": list(bounds), "cells": names},
                    "features": len(out_feats),
                    "bytes": total,
                })
            else:
                dst = out_dir / f"{theme}.{tier}.geojson"
                dst.write_text(payload, encoding="utf-8")
                entries.append({
                    "tier": tier,
                    "file": f"vectors/{dst.name}",
                    "features": len(out_feats),
                    "bytes": dst.stat().st_size,
                })
        manifest[theme] = entries
        log(f"vectors {theme}: " + ", ".join(
            f"{e['tier']}={e['features']}f/{e['bytes'] / 1e6:.1f}MB"
            + (f" in {len(e['grid']['cells'])} cells" if "grid" in e else "")
            for e in entries))
    return manifest


# --------------------------------------------------------------------------

def main() -> int:
    slug = sys.argv[1] if len(sys.argv) > 1 else "alpine-lakes"
    skip_tiles = "--skip-tiles" in sys.argv
    aoi = load_aoi(slug)
    ensure_dirs(slug)
    interim, derived, web = INTERIM / slug, DERIVED / slug, WEB / slug
    bb = bbox(aoi, "buffered")

    # ---- interim -------------------------------------------------------
    dem_tiles = sorted((RAW / "3dep-1as").glob("USGS_1_*.tif"))
    lc_tiles = sorted((RAW / "worldcover-v200").glob("*_Map.tif"))
    if not dem_tiles:
        log("ERROR: no DEM tiles in data/raw/3dep-1as")
        return 1
    log(f"{len(dem_tiles)} DEM tiles, {len(lc_tiles)} land cover tiles")

    # A region's metric rasters come from scripts/region.py, which warps
    # straight from a VRT; mosaicking here would hold the whole far field in
    # memory first. So only build what is not there yet.
    dem = interim / "dem_5070.tif"
    if not dem.exists():
        dem_ll = mosaic_crop(dem_tiles, bb, interim / "dem_4326.tif")
        dem = warp_to_metric(dem_ll, dem, DEM_RES, Resampling.bilinear)

    lc = interim / "landcover_5070.tif"
    if not lc.exists():
        lc = None
        if lc_tiles:
            lc_ll = mosaic_crop(lc_tiles, bb, interim / "landcover_4326.tif",
                                nodata=0)
            lc = warp_to_metric(lc_ll, interim / "landcover_5070.tif", LC_RES,
                                Resampling.nearest, nodata=0)

    # Imagery is optional and acquired by a separate script (scripts/imagery.py)
    # because it is the only source here that needs a STAC search rather than a
    # fixed URL per tile. Sentinel-2 arrives in its own UTM zone and gets warped
    # like everything else; NAIP is already written in the metric CRS.
    s2_raw = RAW / slug / "s2_visual_utm.tif"
    imagery_s2 = None
    if s2_raw.exists():
        imagery_s2 = warp_to_metric(s2_raw, interim / "imagery_s2_5070.tif",
                                    S2_RES, Resampling.bilinear, nodata=0)
        add_overviews(imagery_s2)
    elif (interim / "imagery_s2_5070.tif").exists():
        # a region's composite, written in the metric CRS with overviews
        imagery_s2 = interim / "imagery_s2_5070.tif"
    # a region's NAIP is a VRT over its work tiles (imagery.write_naip_vrt)
    imagery_naip = interim / "imagery_naip_5070.vrt"
    if "--skip-naip" in sys.argv:
        # the NAIP build is still running: its interim VRT must not be tiled
        # (and marked complete) half done
        imagery_naip = None
    elif not imagery_naip.exists():
        imagery_naip = interim / "imagery_naip_5070.tif"
        if imagery_naip.exists():
            add_overviews(imagery_naip)
        else:
            imagery_naip = None
    if not imagery_s2 and not imagery_naip:
        log("no satellite imagery present — run scripts/imagery.py to add it")

    # ---- derived -------------------------------------------------------
    # A region can leave out derived layers V does not use (aoi
    # "skip_derived"): ruggedness, openness and the densities are map layers
    # only, and over a region's far field they are hours of compute.
    skip = set(aoi.get("skip_derived", []))
    layers: dict[str, Path] = {"dem": dem}
    layers.update(derive_terrain(dem, derived))
    if "ruggedness" not in skip:
        layers["ruggedness"] = derive_ruggedness(
            dem, derived / "ruggedness.tif", derived / "relief_sigma.tif", lc,
            derived / "ruggedness_near.tif")
        for key in ("relief_sigma", "ruggedness_near"):
            if (derived / f"{key}.tif").exists():
                layers[key] = derived / f"{key}.tif"
    if "openness" not in skip:
        layers.update(derive_openness(dem, derived))
    if lc is not None:
        layers["landcover"] = lc
        if "densities" not in skip:
            layers.update(derive_densities(lc, derived))
        layers["sea"] = derive_sea(Path(lc), Path(dem), derived / "sea.tif")

    # ---- web tiles -----------------------------------------------------
    manifest = {
        # Cache-busting token. Tiles and vector chunks are served with a long
        # max-age, so regenerating them has to change their URLs or the
        # browser keeps serving yesterday's pyramid.
        "built": int(time.time()),
        "aoi": {"slug": slug, "name": aoi["name"],
                "core": aoi["core"], "buffered": aoi["buffered"],
                "vector": aoi.get("vector"), "buffer_km": aoi.get("buffer_km"),
                "core_polygon": aoi.get("core_polygon")},
        "raster_layers": [], "vector_layers": {},
    }

    # Nearest, not average: averaging blends the -999999 nodata sentinel into
    # neighbouring real cells. And the mask has to test the declared nodata
    # value explicitly — -999999 is comfortably greater than -1e30, so a
    # generic "very negative" guard lets it straight through and drags the
    # colour ramp minimum down to -999999.
    with rasterio.open(dem) as s:
        zz = s.read(1, out_shape=(1024, 1024),
                    resampling=Resampling.nearest).astype("float64")
        dem_nodata = s.nodata
    m = np.isfinite(zz) & (zz > ELEV_FLOOR)
    if dem_nodata is not None:
        m &= zz != dem_nodata
    zz = zz[m]
    emin, emax = float(np.percentile(zz, 0.5)), float(np.percentile(zz, 99.9))
    log(f"elevation range for ramp: {emin:.0f}-{emax:.0f} m "
        f"({m.sum()} valid of {m.size} sampled)")

    def p995(key: str, fallback: float) -> float:
        p = layers.get(key)
        if not p or not Path(p).exists():
            return fallback
        with rasterio.open(p) as s:
            v = s.read(1)
        v = v[v > -9998]
        return float(np.percentile(v, 99.5)) if v.size else fallback

    rug_max = p995("ruggedness", 30.0)
    near_max = p995("ruggedness_near", 30.0)
    sig_max = p995("relief_sigma", 700.0)
    def_max = p995("openness_defect", 40.0)

    # `units` and `vmin`/`vmax` travel in the manifest so the viewer never has
    # to guess a unit or scrape a range out of prose.
    specs = [
        dict(id="hillshade", title="Hillshade", layer="hillshade",
             ramp="gray", vmin=1, vmax=255, zmin=6, zmax=12,
             resampling=Resampling.bilinear, units="",
             lo="shaded", hi="lit",
             description="Shaded relief, sun from the north-west at 45°"),
        dict(id="dem", title="Elevation", layer="dem",
             ramp="elevation", vmin=emin, vmax=emax, zmin=6, zmax=12,
             resampling=Resampling.bilinear, units="m",
             description="Height above sea level"),
        dict(id="slope", title="Slope", layer="slope",
             ramp="slope", vmin=0, vmax=60, zmin=6, zmax=12,
             resampling=Resampling.bilinear, units="°",
             description="Steepness of the ground, measured at 30 m"),
        dict(id="ruggedness", title="Ruggedness", layer="ruggedness",
             ramp="ruggedness", vmin=0, vmax=rug_max, zmin=6, zmax=12,
             resampling=Resampling.bilinear, units="°",
             description=(
                 f"How much the slope keeps changing. RMS deviation of the "
                 f"local slope vector, measured over a {GRAD_BASELINE_M:g} m "
                 f"baseline within a {ROUGH_INNER_M:g} m window, then averaged "
                 f"across {RUGGED_RADIUS_M / 1000:g} km. An angle, not a "
                 f"height: 0° is a uniform slope of any steepness.")),
        dict(id="ruggedness_near", title="Ruggedness (1.5 km)",
             layer="ruggedness_near",
             ramp="ruggedness", vmin=0, vmax=near_max, zmin=6, zmax=12,
             resampling=Resampling.bilinear, units="°",
             description=(
                 f"The same roughness field on a {RUGGED_NEAR_RADIUS_M / 1000:g} km "
                 f"kernel instead of {RUGGED_RADIUS_M / 1000:g} km. Fitted scenic "
                 f"distance weights put ~2/3 of perceived beauty inside 1.5 km, so "
                 f"this is the near-field comparison band.")),
        dict(id="openness_pos", title="Openness (positive)", layer="openness_pos",
             ramp="openness", vmin=60, vmax=120, zmin=6, zmax=12,
             resampling=Resampling.bilinear, units="°",
             lo="60° enclosed", hi="120° dominant",
             description=(
                 f"Yokoyama openness over {OPENNESS_RADIUS_M:g} m. Exactly 90° on a "
                 f"plane of any slope, so it is slope-invariant by construction. "
                 f"Above 90° = convex and dominant (ridges, spurs, summits); "
                 f"below = concave and enclosed (gullies, basins).")),
        dict(id="openness_neg", title="Openness (negative)", layer="openness_neg",
             ramp="openness", vmin=60, vmax=120, zmin=6, zmax=12,
             resampling=Resampling.bilinear, units="°",
             lo="60°", hi="120° on an edge",
             description=(
                 "The mirror measure, looking down rather than up. This is the "
                 "depression-angle term: standing at the lip of a drop gives a "
                 "large value. High here + high positive openness = a cliff edge.")),
        dict(id="openness_defect", title="Openness defect",
             layer="openness_defect",
             ramp="defect", vmin=0, vmax=def_max, zmin=6, zmax=12,
             resampling=Resampling.bilinear, units="°",
             description=(
                 "180° − (positive + negative). Zero if and only if every radial "
                 "profile is straight; strictly positive otherwise. A single "
                 "slope-invariant complexity number.")),
        dict(id="relief_sigma", title="Relief", layer="relief_sigma",
             ramp="relief", vmin=0, vmax=sig_max, zmin=6, zmax=12,
             resampling=Resampling.bilinear, units="m",
             description=(
                 f"How much vertical range is nearby. Standard deviation of "
                 f"elevation within {RELIEF_RADIUS_M / 1000:g} km, over land "
                 f"only. Metres of spread — not per anything.")),
        # mode (majority), not nearest: with 11 discrete classes, nearest
        # picks one arbitrary source pixel per output pixel and produces
        # speckle when zoomed out. Majority is the correct reduction for
        # categorical data and degrades to nearest at native resolution.
        dict(id="landcover", title="Land cover", layer="landcover",
             categorical=True, zmin=6, zmax=13, resampling=Resampling.mode,
             units="", description="ESA WorldCover 10 m v200, majority-resampled",
             legend=[{"label": cm.WORLDCOVER_LABELS[k], "color": cm.WORLDCOVER[k]}
                     for k in sorted(cm.WORLDCOVER)]),
        dict(id="canopy_density", title="Canopy density", layer="canopy_density",
             ramp="canopy", vmin=0, vmax=100, zmin=6, zmax=13,
             resampling=Resampling.average, units="%",
             description="Fraction of ground under tree cover, 100 m radius at 10 m"),
        dict(id="builtup_density", title="Built-up density", layer="builtup_density",
             ramp="builtup", vmin=0, vmax=50, zmin=6, zmax=13,
             resampling=Resampling.average, units="%",
             description="Fraction of ground built on, 100 m radius at 10 m"),
    ]

    # The sea, above the terrain layers and below the imagery (whose own
    # photographs of the water are better than a flat colour when it is on).
    specs.append(dict(
        id="sea", title="Sea", layer="sea", categorical=True,
        palette={1: cm.WATER_BLUE}, zmin=6, zmax=14, resampling=Resampling.mode,
        units="", description="Water at sea level: WorldCover water within 2 m of 0 m",
        legend=[{"label": "Sea", "color": cm.WATER_BLUE}]))

    # Imagery layers are appended rather than declared inline above, because
    # they are the only ones that may be absent and the only ones whose extent
    # is not the buffered box — NAIP is US-only and downloaded over the core.
    if imagery_s2 is not None:
        layers["imagery_s2"] = imagery_s2
        specs.append(dict(
            id="imagery_s2", title="Satellite imagery", layer="imagery_s2",
            rgb=True, zmin=6, zmax=14, extent="buffered",
            resampling=Resampling.bilinear, units="",
            attribution="Contains modified Copernicus Sentinel data",
            description=(
                "Sentinel-2 L2A true colour at 10 m, single cloud-free "
                "acquisition. Contains modified Copernicus Sentinel data.")))
    if imagery_naip is not None:
        layers["imagery_naip"] = imagery_naip
        specs.append(dict(
            id="imagery_naip", title="Aerial imagery (NAIP)",
            layer="imagery_naip",
            # starts at z11: below that the Sentinel-2 layer is already finer
            # than the screen and these tiles would only duplicate it
            # to z17 (0.8 m/px at this latitude): the mosaic is NAIP's native
            # 0.6 m, and z18 would only quadruple the tiles to interpolate
            rgb=True, zmin=11, zmax=17, extent="core",
            # a region's NAIP covers boxes inside the core; its tiles carry
            # Sentinel-2 where NAIP stops, and none are written beyond it
            under=imagery_s2 if aoi.get("naip_boxes") else None,
            resampling=Resampling.bilinear, units="",
            attribution="USDA National Agriculture Imagery Program",
            description=(
                "USDA NAIP aerial photography at its native 0.6 m (mostly "
                "2023, some 2019 and 2021). Public domain. Covers the core "
                "AOI only.")))

    def make_colorize(spec):
        if spec.get("categorical"):
            pal = spec.get("palette", cm.WORLDCOVER)
            return lambda v, m: cm.categorical_indexed(v.astype(np.int32), pal)
        ramp, lo, hi = spec["ramp"], spec["vmin"], spec["vmax"]
        return lambda v, m: cm.ramp_indexed(v, m, ramp, lo, hi)

    if skip_tiles:
        # Carry the previous raster entries forward. Rebuilding the manifest
        # without them silently removes every raster layer from the viewer —
        # the same trap --skip-vectors already guards against below.
        prev = web / "manifest.json"
        if prev.exists():
            try:
                manifest["raster_layers"] = json.loads(
                    prev.read_text(encoding="utf-8")).get("raster_layers", [])
                log(f"kept {len(manifest['raster_layers'])} raster layers "
                    f"from the previous manifest (--skip-tiles)")
            except Exception:  # noqa: BLE001
                log("WARNING: could not read previous manifest; raster layers lost")
    else:
        for spec in specs:
            key = spec["id"]
            path = layers.get(spec["layer"])
            if not path or not Path(path).exists():
                continue
            # a region keeps each layer in one MBTiles file (aoi "tile_store");
            # serve.py answers the same {z}/{x}/{y} URLs from either
            out_dir = web / "layers" / (f"{key}.mbtiles" if aoi.get("tile_store") == "mbtiles"
                                        else key)
            marker = tl.complete_path(out_dir)
            if marker.exists():
                stats = json.loads(marker.read_text(encoding="utf-8"))
                log(f"skip tiles {key} ({stats['tiles']} tiles cached)")
            else:
                log(f"tiling {key} z{spec['zmin']}-{spec['zmax']} ...")
                extent = bbox(aoi, spec["extent"]) if spec.get("extent") else bb
                if spec.get("rgb"):
                    stats = tl.write_rgb_pyramid(
                        Path(path), out_dir, extent,
                        spec["zmin"], spec["zmax"],
                        resampling=spec["resampling"], under=spec.get("under"))
                else:
                    stats = tl.write_pyramid(
                        Path(path), out_dir, extent, make_colorize(spec),
                        spec["zmin"], spec["zmax"],
                        resampling=spec["resampling"],
                        valid_min=(ELEV_FLOOR if key == "dem" else None),
                        valid_max=(100.0 if key.endswith("_density") else None),
                    )
                log(f"  {key}: {stats['tiles']} tiles written")
            ext = "jpg" if spec.get("rgb") else "png"
            entry = {
                "id": key, "title": spec["title"],
                "description": spec["description"],
                "units": spec["units"],
                "rgb": bool(spec.get("rgb")),
                "attribution": spec.get("attribution"),
                "url": f"layers/{key}/{{z}}/{{x}}/{{y}}.{ext}",
                "minzoom": stats["minzoom"], "maxzoom": stats["maxzoom"],
                "tiles": stats["tiles"], "legend": spec.get("legend"),
            }
            if not spec.get("categorical") and not spec.get("rgb"):
                entry["vmin"] = round(float(spec["vmin"]), 2)
                entry["vmax"] = round(float(spec["vmax"]), 2)
                entry["lo_label"] = spec.get("lo")
                entry["hi_label"] = spec.get("hi")
            manifest["raster_layers"].append(entry)

    # ---- vectors -------------------------------------------------------
    if "--skip-vectors" not in sys.argv:
        manifest["vector_layers"] = build_vector_lod(
            slug, web / "vectors", bbox(aoi, aoi.get("vector_extent", "buffered")))
    else:
        log("skipping vector LOD build (--skip-vectors)")
        prev = web / "manifest.json"
        if prev.exists():
            try:
                manifest["vector_layers"] = json.loads(
                    prev.read_text(encoding="utf-8")).get("vector_layers", {})
            except Exception:  # noqa: BLE001
                pass

    (web / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    log(f"wrote manifest.json ({len(manifest['raster_layers'])} raster, "
        f"{len(manifest['vector_layers'])} vector layers)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
