"""XYZ tile pyramid generation.

Rasters live in an equal-area metric CRS (EPSG:5070 here). For display we warp
them onto the Web Mercator tile grid, one zoom level at a time, in horizontal
bands so peak memory stays bounded regardless of zoom.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import numpy as np
import rasterio
from PIL import Image

from colormaps import TRANSPARENT_INDEX
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import reproject, transform_bounds

ORIGIN = 20037508.342789244
WORLD = 2 * ORIGIN
TILE = 256

# tile rows warped per pass — 8 rows = 2048 px tall, a few hundred MB at most
BAND_ROWS = 8


def tile_span(z: int) -> float:
    return WORLD / (2 ** z)


def merc_to_tile(x: float, y: float, z: int) -> tuple[int, int]:
    span = tile_span(z)
    tx = int((x + ORIGIN) // span)
    ty = int((ORIGIN - y) // span)
    return tx, ty


def tile_range(bounds_3857: tuple[float, float, float, float], z: int):
    """(tx0, ty0, tx1, ty1) inclusive, clamped to the world."""
    w, s, e, n = bounds_3857
    tx0, ty0 = merc_to_tile(w, n, z)
    tx1, ty1 = merc_to_tile(e, s, z)
    lim = 2 ** z - 1
    return (max(0, tx0), max(0, ty0), min(lim, tx1), min(lim, ty1))


def aoi_bounds_3857(lonlat_bbox: tuple[float, float, float, float]):
    w, s, e, n = lonlat_bbox
    return transform_bounds("EPSG:4326", "EPSG:3857", w, s, e, n, densify_pts=21)


def write_pyramid(
    src_path: Path,
    out_dir: Path,
    lonlat_bbox: tuple[float, float, float, float],
    colorize: Callable[[np.ndarray, np.ndarray], np.ndarray],
    minzoom: int,
    maxzoom: int,
    resampling: Resampling = Resampling.bilinear,
    band: int = 1,
    valid_min: float | None = None,
    valid_max: float | None = None,
) -> dict:
    """Warp `src_path` onto the mercator tile grid and write PNG tiles.

    `colorize(values, valid) -> (index array, palette)` does the styling.
    `valid_min` / `valid_max` reject values that resampling can manufacture
    near a nodata boundary. Returns a small stats dict.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    bounds = aoi_bounds_3857(lonlat_bbox)
    written = 0
    skipped = 0

    with rasterio.open(src_path) as src:
        src_nodata = src.nodata
        dtype = src.dtypes[band - 1]
        is_int = np.dtype(dtype).kind in "iu"
        fill = 0 if src_nodata is None else src_nodata

        for z in range(minzoom, maxzoom + 1):
            tx0, ty0, tx1, ty1 = tile_range(bounds, z)
            span = tile_span(z)
            px = span / TILE

            for band_start in range(ty0, ty1 + 1, BAND_ROWS):
                band_end = min(band_start + BAND_ROWS - 1, ty1)
                w_px = (tx1 - tx0 + 1) * TILE
                h_px = (band_end - band_start + 1) * TILE
                transform = from_origin(
                    -ORIGIN + tx0 * span, ORIGIN - band_start * span, px, px
                )

                dest = np.full((h_px, w_px), fill, dtype=dtype)
                reproject(
                    source=rasterio.band(src, band),
                    destination=dest,
                    src_transform=src.transform,
                    src_crs=src.crs,
                    src_nodata=src_nodata,
                    dst_transform=transform,
                    dst_crs="EPSG:3857",
                    dst_nodata=fill,
                    resampling=resampling,
                    num_threads=4,
                )

                if src_nodata is None:
                    valid = np.ones(dest.shape, dtype=bool)
                elif np.isnan(fill):
                    valid = ~np.isnan(dest)
                else:
                    valid = dest != fill
                if not is_int:
                    valid &= np.isfinite(dest)
                if valid_min is not None:
                    valid &= dest >= valid_min
                if valid_max is not None:
                    valid &= dest <= valid_max

                idx, palette = colorize(
                    dest.astype(np.float64, copy=False), valid)

                for ty in range(band_start, band_end + 1):
                    row0 = (ty - band_start) * TILE
                    for tx in range(tx0, tx1 + 1):
                        col0 = (tx - tx0) * TILE
                        chunk = idx[row0:row0 + TILE, col0:col0 + TILE]
                        if bool((chunk == TRANSPARENT_INDEX).all()):
                            skipped += 1
                            continue
                        d = out_dir / str(z) / str(tx)
                        d.mkdir(parents=True, exist_ok=True)
                        img = Image.fromarray(chunk, mode="P")
                        img.putpalette(palette)
                        img.save(d / f"{ty}.png",
                                 transparency=TRANSPARENT_INDEX,
                                 optimize=True)
                        written += 1

    stats = {"tiles": written, "empty": skipped,
             "minzoom": minzoom, "maxzoom": maxzoom}
    # Completion marker. Without it, "does the directory contain any PNG?" says
    # yes for a pyramid whose generation was interrupted half way, and the
    # partial result gets silently reused forever.
    (out_dir / "_complete.json").write_text(json.dumps(stats), encoding="utf-8")
    return stats


def write_rgb_pyramid(
    src_path: Path,
    out_dir: Path,
    lonlat_bbox: tuple[float, float, float, float],
    minzoom: int,
    maxzoom: int,
    resampling: Resampling = Resampling.bilinear,
    quality: int = 82,
) -> dict:
    """Same warp, but for a 3-band true-colour source.

    Kept separate from `write_pyramid` rather than folded into it, because
    almost everything below the warp differs: no colorize callback, no palette,
    and JPEG output instead of PNG. Imagery is photographic, so a 256-colour
    palette would posterise it badly and a truecolour PNG costs several times
    the bytes for no visible gain.

    Nodata is written as black rather than transparent: JPEG has no alpha, and
    imagery only goes missing outside the AOI where nothing is drawn anyway.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    bounds = aoi_bounds_3857(lonlat_bbox)
    written = 0
    skipped = 0

    with rasterio.open(src_path) as src:
        if src.count < 3:
            raise ValueError(f"{src_path} has {src.count} bands, need 3 (RGB)")
        nodata = src.nodata

        for z in range(minzoom, maxzoom + 1):
            tx0, ty0, tx1, ty1 = tile_range(bounds, z)
            span = tile_span(z)
            px = span / TILE

            for band_start in range(ty0, ty1 + 1, BAND_ROWS):
                band_end = min(band_start + BAND_ROWS - 1, ty1)
                w_px = (tx1 - tx0 + 1) * TILE
                h_px = (band_end - band_start + 1) * TILE
                transform = from_origin(
                    -ORIGIN + tx0 * span, ORIGIN - band_start * span, px, px
                )

                dest = np.zeros((3, h_px, w_px), dtype=np.uint8)
                for bi in range(3):
                    reproject(
                        source=rasterio.band(src, bi + 1),
                        destination=dest[bi],
                        src_transform=src.transform,
                        src_crs=src.crs,
                        src_nodata=nodata,
                        dst_transform=transform,
                        dst_crs="EPSG:3857",
                        dst_nodata=0,
                        resampling=resampling,
                        num_threads=4,
                    )

                rgb = np.moveaxis(dest, 0, -1)
                for ty in range(band_start, band_end + 1):
                    row0 = (ty - band_start) * TILE
                    for tx in range(tx0, tx1 + 1):
                        col0 = (tx - tx0) * TILE
                        chunk = rgb[row0:row0 + TILE, col0:col0 + TILE]
                        if not chunk.any():
                            skipped += 1          # entirely outside the imagery
                            continue
                        d = out_dir / str(z) / str(tx)
                        d.mkdir(parents=True, exist_ok=True)
                        Image.fromarray(chunk, mode="RGB").save(
                            d / f"{ty}.jpg", quality=quality, optimize=True)
                        written += 1

    stats = {"tiles": written, "empty": skipped,
             "minzoom": minzoom, "maxzoom": maxzoom}
    (out_dir / "_complete.json").write_text(json.dumps(stats), encoding="utf-8")
    return stats
