"""XYZ tile pyramid generation.

Rasters live in an equal-area metric CRS (EPSG:5070 here). For display we warp
them onto the Web Mercator tile grid, one zoom level at a time, in horizontal
bands so peak memory stays bounded regardless of zoom.

A pyramid goes either to a directory of {z}/{x}/{y} files or, when the output
path ends in .mbtiles, into one MBTiles (SQLite) file. A region is millions of
tiles, and as files they are slow to copy, count or delete and to back up;
as one file per layer they are not, and serve.py answers the same URLs from
it.
"""
from __future__ import annotations

import io
import json
import sqlite3
import threading
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


def is_mbtiles(out: Path) -> bool:
    return Path(out).suffix == ".mbtiles"


def complete_path(out: Path) -> Path:
    """The completion marker of a pyramid. Without it, "does the output hold
    any tiles?" says yes for a pyramid whose generation was interrupted half
    way, and the partial result gets silently reused forever."""
    out = Path(out)
    return out.with_name(out.name + ".complete.json") if is_mbtiles(out) else out / "_complete.json"


class _DirSink:
    def __init__(self, out: Path, ext: str):
        self.out, self.ext = Path(out), ext
        self.out.mkdir(parents=True, exist_ok=True)

    def put(self, z: int, x: int, y: int, data: bytes) -> None:
        d = self.out / str(z) / str(x)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{y}.{self.ext}").write_bytes(data)

    def close(self, stats: dict) -> None:
        complete_path(self.out).write_text(json.dumps(stats), encoding="utf-8")


class _MBTilesSink:
    """Tiles into an MBTiles file, built as <name>.part and renamed when done.
    Workers call put() from many threads; rows are batched under a lock and
    written in one transaction per batch. MBTiles rows are TMS (y up)."""
    BATCH = 2000

    def __init__(self, out: Path, ext: str, name: str):
        self.out, self.ext = Path(out), ext
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.part = self.out.with_name(self.out.name + ".part")
        self.part.unlink(missing_ok=True)
        self.db = sqlite3.connect(self.part, check_same_thread=False, isolation_level=None)
        # a .part file is rebuilt from scratch if anything goes wrong, so it
        # needs no journal and no syncing
        self.db.execute("PRAGMA journal_mode=OFF")
        self.db.execute("PRAGMA synchronous=OFF")
        self.db.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
        self.db.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
                        "tile_row INTEGER, tile_data BLOB)")
        self.name = name
        self.lock = threading.Lock()
        self.buf: list = []

    def _flush(self) -> None:
        if self.buf:
            self.db.execute("BEGIN")
            self.db.executemany("INSERT INTO tiles VALUES (?, ?, ?, ?)", self.buf)
            self.db.execute("COMMIT")
            self.buf = []

    def put(self, z: int, x: int, y: int, data: bytes) -> None:
        with self.lock:
            self.buf.append((z, x, (1 << z) - 1 - y, data))
            if len(self.buf) >= self.BATCH:
                self._flush()

    def close(self, stats: dict, bounds_ll=None) -> None:
        with self.lock:
            self._flush()
            self.db.execute("CREATE UNIQUE INDEX tile_index ON tiles "
                            "(zoom_level, tile_column, tile_row)")
            meta = {"name": self.name, "format": "jpg" if self.ext == "jpg" else "png",
                    "type": "overlay", "version": "1.1",
                    "minzoom": str(stats["minzoom"]), "maxzoom": str(stats["maxzoom"])}
            if bounds_ll:
                meta["bounds"] = ",".join(f"{v:.6f}" for v in bounds_ll)
            self.db.executemany("INSERT INTO metadata VALUES (?, ?)", list(meta.items()))
            self.db.close()
        self.part.replace(self.out)
        complete_path(self.out).write_text(json.dumps(stats), encoding="utf-8")


def _sink(out: Path, ext: str, bounds_ll=None):
    return (_MBTilesSink(out, ext, Path(out).stem) if is_mbtiles(out)
            else _DirSink(out, ext))


def _encode(img: Image.Image, fmt: str, **kw) -> bytes:
    b = io.BytesIO()
    img.save(b, format=fmt, **kw)
    return b.getvalue()


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
    sink = _sink(out_dir, "png")
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
                        img = Image.fromarray(chunk, mode="P")
                        img.putpalette(palette)
                        sink.put(z, tx, ty, _encode(img, "PNG",
                                                    transparency=TRANSPARENT_INDEX,
                                                    optimize=True))
                        written += 1

    stats = {"tiles": written, "empty": skipped,
             "minzoom": minzoom, "maxzoom": maxzoom}
    sink.close(stats, *([lonlat_bbox] if is_mbtiles(out_dir) else []))
    return stats


def write_rgb_pyramid(
    src_path: Path,
    out_dir: Path,
    lonlat_bbox: tuple[float, float, float, float],
    minzoom: int,
    maxzoom: int,
    resampling: Resampling = Resampling.bilinear,
    quality: int = 82,
    workers: int = 16,
    under: Path | None = None,
) -> dict:
    """Same warp, but for a 3-band true-colour source.

    With `under` (another 3-band source), a tile is written only where the
    main source has data, and its empty pixels are filled from `under`. That
    is the region's NAIP layer: NAIP covers five boxes, Sentinel-2 the rest.
    Tiles beyond NAIP are not written at all, so the map shows the
    Sentinel-2 layer beneath them, and tiles on a box edge carry Sentinel-2
    where NAIP stops instead of black (JPEG has no transparency).

    Kept separate from `write_pyramid` rather than folded into it, because
    almost everything below the warp differs: no colorize callback, no palette,
    and JPEG output instead of PNG. Imagery is photographic, so a 256-colour
    palette would posterise it badly and a truecolour PNG costs several times
    the bytes for no visible gain.

    Each block of tiles reads its share of the source once, all three channels
    together and decimated to about twice the tile resolution, which GDAL
    serves from the source's overviews; the small array is then warped in
    memory. Warping straight from the dataset instead read the source at full
    resolution for every zoom and every channel, which for the 0.6 m NAIP
    mosaic (21 gigapixels) meant decoding it twenty times over. Blocks of
    BAND_ROWS x BAND_ROWS tiles run in parallel, each worker with its own
    dataset handle.

    Nodata is written as black rather than transparent: JPEG has no alpha, and
    imagery only goes missing outside the AOI where nothing is drawn anyway.
    """
    import concurrent.futures as cf

    sink = _sink(out_dir, "jpg")
    bounds = aoi_bounds_3857(lonlat_bbox)
    local = threading.local()

    with rasterio.open(src_path) as src:
        if src.count < 3:
            raise ValueError(f"{src_path} has {src.count} bands, need 3 (RGB)")

    def dataset(path=None):
        key = "ds" if path is None else "under"
        if getattr(local, key, None) is None:
            setattr(local, key, rasterio.open(src_path if path is None else path))
        return getattr(local, key)

    def warp_block(ds, west, north, w_px, h_px, px, transform):
        """The source under a block of tiles, warped onto it; None if the
        source has nothing there."""
        sw, ss, se, sn = transform_bounds("EPSG:3857", ds.crs, west, north - h_px * px,
                                          west + w_px * px, north, densify_pts=21)
        m = 4 * px
        win = rasterio.windows.from_bounds(sw - m, ss - m, se + m, sn + m, ds.transform)
        win = win.round_offsets().round_lengths()
        full = rasterio.windows.Window(0, 0, ds.width, ds.height)
        if not rasterio.windows.intersect(win, full):
            return None
        win = win.intersection(full)
        res = ds.res[0]
        # about two source pixels per tile pixel (ground metres), never finer
        # than the source itself
        target = max(res, px * 0.5 * 0.67)       # 3857 m are ~1.5x ground m here
        dec = max(1.0, target / res)
        shape = (3, max(1, int(win.height / dec)), max(1, int(win.width / dec)))
        arr = ds.read(indexes=[1, 2, 3], window=win, out_shape=shape,
                      resampling=Resampling.average)
        if not arr.any():
            return None
        src_tf = ds.window_transform(win) * rasterio.Affine.scale(
            win.width / shape[2], win.height / shape[1])
        dest = np.zeros((3, h_px, w_px), dtype=np.uint8)
        reproject(source=arr, destination=dest, src_transform=src_tf, src_crs=ds.crs,
                  src_nodata=ds.nodata, dst_transform=transform, dst_crs="EPSG:3857",
                  dst_nodata=0, resampling=resampling, num_threads=2)
        return dest

    def band(z, band_start, band_end, tx0, tx1):
        """Warp one band of tiles and write its JPEGs: (written, skipped)."""
        ds = dataset()
        span = tile_span(z)
        px = span / TILE
        w_px = (tx1 - tx0 + 1) * TILE
        h_px = (band_end - band_start + 1) * TILE
        west, north = -ORIGIN + tx0 * span, ORIGIN - band_start * span
        transform = from_origin(west, north, px, px)
        dest = warp_block(ds, west, north, w_px, h_px, px, transform)
        if dest is None:
            return 0, (band_end - band_start + 1) * (tx1 - tx0 + 1)
        has = dest.any(axis=0)
        if under is not None and not has.all():
            low = warp_block(dataset(under), west, north, w_px, h_px, px, transform)
            if low is not None:
                # The decimated read averages the main source's edge pixels
                # with the empty ones beyond, which darkens them without
                # making them empty: a dark line along every NAIP edge. Three
                # pixels in from the edge, `under` takes over.
                from scipy.ndimage import binary_erosion
                keep = binary_erosion(has, iterations=3, border_value=1)
                dest = np.where(keep[None], dest, low)
        rgb = np.moveaxis(dest, 0, -1)
        written = skipped = 0
        for ty in range(band_start, band_end + 1):
            row0 = (ty - band_start) * TILE
            for tx in range(tx0, tx1 + 1):
                col0 = (tx - tx0) * TILE
                chunk = rgb[row0:row0 + TILE, col0:col0 + TILE]
                # with `under`, only tiles the main source reaches
                if not (has[row0:row0 + TILE, col0:col0 + TILE].any() if under is not None
                        else chunk.any()):
                    skipped += 1          # entirely outside the imagery
                    continue
                sink.put(z, tx, ty, _encode(
                    Image.fromarray(np.ascontiguousarray(chunk), mode="RGB"), "JPEG",
                    quality=quality, optimize=True))
                written += 1
        return written, skipped

    written = skipped = 0
    with cf.ThreadPoolExecutor(workers) as ex:
        futs = []
        for z in range(minzoom, maxzoom + 1):
            tx0, ty0, tx1, ty1 = tile_range(bounds, z)
            # Square blocks, not full-width bands: Web Mercator sits rotated
            # ~18 deg to an Albers source this far from its central meridian,
            # so the source rectangle under a thin 75 km band is ~24 km tall,
            # and at 0.6 m one z17 band asked for a 12 GB array.
            for band_start in range(ty0, ty1 + 1, BAND_ROWS):
                band_end = min(band_start + BAND_ROWS - 1, ty1)
                for c0 in range(tx0, tx1 + 1, BAND_ROWS):
                    c1 = min(c0 + BAND_ROWS - 1, tx1)
                    futs.append(ex.submit(band, z, band_start, band_end, c0, c1))
        for f in cf.as_completed(futs):
            w, sk = f.result()
            written += w
            skipped += sk

    stats = {"tiles": written, "empty": skipped,
             "minzoom": minzoom, "maxzoom": maxzoom}
    sink.close(stats, *([lonlat_bbox] if is_mbtiles(out_dir) else []))
    return stats
