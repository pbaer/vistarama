"""Interim rasters for a region too large to mosaic in memory.

    python scripts/region.py <slug>

`process.py` merges the source tiles into one in-memory array and then
reprojects it. For western Washington with its 150 km far field that array
would be 3 GB of DEM and 8 GB of land cover, before the reprojection doubles
it. Here the tiles are joined in a GDAL VRT instead (an XML file that only
points at them), and the VRT is warped straight into the EPSG:5070 rasters
the scorer and the panorama read, a chunk at a time.

* **DEM:** 3DEP 1 arc-second where it exists, Copernicus GLO-30 elsewhere
  (Canada). In the VRT the later source wins, so GLO-30 is listed first.
  Then every cell no source covers is set to 0: those are the open sea west
  of the coast, and a sea surface at height 0 is what a ray from the
  Olympics should meet, not the end of the data.
* **Land cover:** ESA WorldCover at 10 m, as for Alpine Lakes.

Both land in interim/<slug>/ under the names process.py uses, so the scorer,
the server and the tiler need no change.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import calculate_default_transform, reproject
from rasterio.windows import Window, from_bounds

from aoi import INTERIM, RAW, bbox, load_aoi

METRIC_CRS = "EPSG:5070"
DEM_RES = 30.0
LC_RES = 10.0
DEM_NODATA = -999999.0


def log(msg: str) -> None:
    print(f"[region] {msg}", flush=True)


def build_vrt(paths: list[Path], dst: Path, bounds, nodata, like: Path | None = None) -> Path:
    """A mosaic VRT of single-band tiles, clipped to `bounds` (west, south,
    east, north), on the grid of `like` (default: the first tile). Later
    tiles are drawn over earlier ones.

    Geographic tiles in different datums are allowed together: 3DEP is NAD83
    and GLO-30 WGS84, about a metre apart here, which a 30 m DEM cannot see."""
    with rasterio.open(like or paths[0]) as s0:
        crs, res, dtype = s0.crs, s0.res, s0.dtypes[0]
    w, s, e, n = bounds
    xres, yres = res
    width, height = int(round((e - w) / xres)), int(round((n - s) / yres))
    vrt_dtype = {"float32": "Float32", "int16": "Int16", "uint8": "Byte",
                 "float64": "Float64"}[dtype]
    sources = []
    for p in paths:
        with rasterio.open(p) as src:
            if src.crs != crs and not (src.crs.is_geographic and crs.is_geographic):
                raise ValueError(f"{p.name}: CRS {src.crs} differs from {crs}")
            sw, ss, se, sn = src.bounds
            if se <= w or sw >= e or sn <= s or ss >= n:
                continue
            nd = src.nodata if src.nodata is not None else nodata
            dx0, dy0 = (sw - w) / xres, (n - sn) / yres
            dxs, dys = (se - sw) / xres, (sn - ss) / yres
            sources.append(
                f'    <ComplexSource>\n'
                f'      <SourceFilename relativeToVRT="0">{escape(str(p))}</SourceFilename>\n'
                f'      <SourceBand>1</SourceBand>\n'
                f'      <SrcRect xOff="0" yOff="0" xSize="{src.width}" ySize="{src.height}"/>\n'
                f'      <DstRect xOff="{dx0:.6f}" yOff="{dy0:.6f}" xSize="{dxs:.6f}" ySize="{dys:.6f}"/>\n'
                + (f'      <NODATA>{nd}</NODATA>\n' if nd is not None else '')
                + '    </ComplexSource>\n')
    xml = (f'<VRTDataset rasterXSize="{width}" rasterYSize="{height}">\n'
           f'  <SRS>{escape(crs.to_wkt())}</SRS>\n'
           f'  <GeoTransform>{w:.12f}, {xres:.15f}, 0, {n:.12f}, 0, {-yres:.15f}</GeoTransform>\n'
           f'  <VRTRasterBand dataType="{vrt_dtype}" band="1">\n'
           + (f'    <NoDataValue>{nodata}</NoDataValue>\n' if nodata is not None else '')
           + ''.join(sources)
           + '  </VRTRasterBand>\n</VRTDataset>\n')
    dst.write_text(xml, encoding="utf-8")
    log(f"vrt    {dst.name}: {len(sources)} of {len(paths)} tiles, {width}x{height}")
    return dst


def warp(src: Path, dst: Path, res: float, resampling, dtype: str, nodata) -> Path:
    """Reproject to EPSG:5070 at `res`, streamed by GDAL in chunks."""
    if dst.exists():
        log(f"skip warp {dst.name} (present)")
        return dst
    t0 = time.time()
    with rasterio.open(src) as s:
        transform, width, height = calculate_default_transform(
            s.crs, METRIC_CRS, s.width, s.height, *s.bounds, resolution=res)
        profile = {"driver": "GTiff", "dtype": dtype, "count": 1, "nodata": nodata,
                   "width": width, "height": height, "crs": METRIC_CRS,
                   "transform": transform, "tiled": True, "blockxsize": 512,
                   "blockysize": 512, "compress": "deflate", "zlevel": 6,
                   "predictor": 3 if dtype.startswith("float") else 2, "BIGTIFF": "YES"}
        part = dst.with_name(dst.stem + ".part.tif")
        with rasterio.open(part, "w", **profile) as d:
            reproject(source=rasterio.band(s, 1), destination=rasterio.band(d, 1),
                      src_transform=s.transform, src_crs=s.crs, src_nodata=s.nodata,
                      dst_transform=transform, dst_crs=METRIC_CRS, dst_nodata=nodata,
                      resampling=resampling, num_threads=8, warp_mem_limit=4096)
    part.replace(dst)
    log(f"warp   {dst.name} {width}x{height} @{res:g}m, {(time.time() - t0) / 60:.1f} min")
    return dst


def fill_sea(path: Path, nodata: float, box_ll, value: float = 0.0, block: int = 4096) -> int:
    """Inside the lon/lat box, set every nodata cell to `value`; outside it,
    make every cell nodata. In place, block by block. Returns cells filled.

    The box is a slanted quadrilateral in EPSG:5070, so the raster's corners
    lie beyond it, over Idaho or interior BC, where no source tile was asked
    for. Those are not sea; filling them made flat sea-level ground there."""
    from rasterio.features import geometry_mask
    from shapely.geometry import box as sbox, mapping
    from shapely.ops import transform as stransform
    from pyproj import Transformer
    n = 0
    with rasterio.open(path, "r+") as ds:
        to = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True).transform
        inside_geom = mapping(stransform(to, sbox(*box_ll).segmentize(0.01)))
        for r0 in range(0, ds.height, block):
            for c0 in range(0, ds.width, block):
                win = Window(c0, r0, min(block, ds.width - c0), min(block, ds.height - r0))
                a = ds.read(1, window=win)
                inside = geometry_mask([inside_geom], out_shape=a.shape, invert=True,
                                       transform=ds.window_transform(win))
                m = a == nodata
                if a.dtype.kind == "f":
                    m |= ~np.isfinite(a)
                fill = m & inside
                clear = ~inside & ~m
                if fill.any() or clear.any():
                    a[fill] = value
                    a[clear] = nodata
                    ds.write(a, 1, window=win)
                    n += int(fill.sum())
    return n


def main() -> int:
    slug = sys.argv[1] if len(sys.argv) > 1 else "wa-west"
    aoi = load_aoi(slug)
    interim = INTERIM / slug
    interim.mkdir(parents=True, exist_ok=True)
    bb = bbox(aoi, "buffered")

    # ---- DEM: GLO-30 first, 3DEP over it -----------------------------------
    names = aoi["sources"]["dem"]["tiles"]
    dep3 = [RAW / "3dep-1as" / f"USGS_1_{t}.tif" for t in names]
    dep3 = [p for p in dep3 if p.exists()]
    glo = sorted((RAW / "glo30").glob("Copernicus_DSM_COG_10_*_DEM.tif"))
    log(f"DEM: {len(dep3)} 3DEP tiles, {len(glo)} GLO-30 tiles")
    dem = interim / "dem_5070.tif"
    if not dem.exists():
        # On 3DEP's grid (GLO-30 steps 1.5" in longitude north of 50°), with
        # GLO-30 listed first so 3DEP is drawn over it wherever both exist.
        vrt = build_vrt(glo + dep3, interim / "dem_4326.vrt", bb, DEM_NODATA, like=dep3[0])
        warp(vrt, dem, DEM_RES, Resampling.bilinear, "float32", DEM_NODATA)
        n = fill_sea(dem, DEM_NODATA, bb)
        log(f"sea    {n:,} cells with no DEM set to 0 m")

    # ---- land cover -----------------------------------------------------------
    lc_tiles = [RAW / "worldcover-v200" / f"ESA_WorldCover_10m_2021_v200_{t}_Map.tif"
                for t in aoi["sources"]["landcover"]["tiles"]]
    lc_tiles = [p for p in lc_tiles if p.exists()]
    log(f"land cover: {len(lc_tiles)} WorldCover tiles")
    lc = interim / "landcover_5070.tif"
    if not lc.exists():
        vrt = build_vrt(lc_tiles, interim / "landcover_4326.vrt", bb, 0)
        warp(vrt, lc, LC_RES, Resampling.nearest, "uint8", 0)
        # WorldCover covers all land, and its tiles carry the sea next to it as
        # class 80; only open ocean has no tile. That is water too.
        n = fill_sea(lc, 0, bb, value=80)
        log(f"sea    {n:,} land-cover cells with no tile set to water (80)")
    if "--refill" in sys.argv:
        # redo the sea fill on rasters built before it was limited to the box
        # (cells outside it go back to nodata)
        log(f"refill {dem.name}: {fill_sea(dem, DEM_NODATA, bb):,} cells")
        log(f"refill {lc.name}: {fill_sea(lc, 0, bb, value=80):,} cells")
    log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
