"""Download raw datasets for an AOI.

Idempotent: existing files of plausible size are skipped, so re-running after
an interruption resumes rather than restarts.

    python scripts/download.py [aoi-slug]
"""
from __future__ import annotations

import json
import sys
import time
import zipfile
from pathlib import Path

import requests

from aoi import RAW, ensure_dirs, load_aoi, bbox

TIMEOUT = 120
CHUNK = 1 << 20

# Overpass (and some Apache front ends) reject the default python-requests
# user agent with 406. A descriptive UA is also just good citizenship.
HEADERS = {
    "User-Agent": "vistarama/0.1 "
                  "(hiking scenery research; contact via repo)"
}

DEP3_URL = (
    "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1/TIFF/"
    "current/{tile}/USGS_1_{tile}.tif"
)
WORLDCOVER_URL = (
    "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
    "ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
)
RGI_URL = (
    "https://daacdata.apps.nsidc.org/pub/DATASETS/nsidc0770_rgi_v7/"
    "regional_files/RGI2000-v7.0-G/RGI2000-v7.0-G-02_western_canada_usa.zip"
)
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]


def log(msg: str) -> None:
    print(f"[download] {msg}", flush=True)


def fetch(url: str, dest: Path, min_bytes: int = 4096,
          magic: bytes | None = None) -> bool:
    """Stream a URL to dest. Returns True on success (or already present).

    `magic` guards against servers that answer 200 with an HTML login or error
    page instead of the file (NSIDC does this when Earthdata auth is missing).
    """
    if dest.exists() and dest.stat().st_size >= min_bytes:
        log(f"skip {dest.name} ({dest.stat().st_size / 1e6:.1f} MB, present)")
        return True
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, 4):
        try:
            with requests.get(url, stream=True, timeout=TIMEOUT,
                              headers=HEADERS) as r:
                if r.status_code != 200:
                    log(f"HTTP {r.status_code} for {url}")
                    if r.status_code in (401, 403, 404):
                        return False
                    raise requests.HTTPError(str(r.status_code))
                total = int(r.headers.get("content-length", 0))
                got = 0
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_content(CHUNK):
                        fh.write(chunk)
                        got += len(chunk)
            if magic:
                with open(tmp, "rb") as fh:
                    head = fh.read(len(magic))
                if head != magic:
                    log(f"{dest.name}: server returned a non-file response "
                        f"(got {head!r}, expected {magic!r})")
                    tmp.unlink(missing_ok=True)
                    return False
            tmp.replace(dest)
            log(f"got  {dest.name} ({got / 1e6:.1f} MB"
                + (f" of {total / 1e6:.1f})" if total else ")"))
            return True
        except Exception as exc:  # noqa: BLE001
            log(f"attempt {attempt} failed for {dest.name}: {exc}")
            time.sleep(3 * attempt)
    tmp.unlink(missing_ok=True)
    return False


# --------------------------------------------------------------------------
# OSM / Overpass
# --------------------------------------------------------------------------

OSM_THEMES: dict[str, str] = {
    # sidewalks and crossings are urban furniture, not trails
    "trails": (
        'way["highway"~"^(path|footway|bridleway|track|steps)$"]'
        '["footway"!~"^(sidewalk|crossing|traffic_island)$"]({bbox});'
    ),
    "roads": (
        'way["highway"~"^(motorway|trunk|primary|secondary|tertiary|'
        'unclassified|residential|service)$"]({bbox});'
    ),
    "water_lines": 'way["waterway"~"^(river|stream|canal)$"]({bbox});',
    # Areas must query relations as well as ways. Anything with islands or a
    # complex shoreline is mapped as a type=multipolygon relation — Colchuck
    # Lake is relation 17094764 — so a way-only query silently returns some
    # lakes and not others.
    "water_areas": (
        'way["natural"="water"]({bbox});'
        'relation["natural"="water"]({bbox});'
        'way["landuse"="reservoir"]({bbox});'
        'relation["landuse"="reservoir"]({bbox});'
    ),
    "glaciers": (
        'way["natural"="glacier"]({bbox});'
        'relation["natural"="glacier"]({bbox});'
    ),
    "peaks": (
        'node["natural"="peak"]({bbox});'
        'node["natural"="volcano"]({bbox});'
    ),
    "passes": (
        'node["mountain_pass"="yes"]({bbox});'
        'node["natural"="saddle"]({bbox});'
    ),
    "poi": (
        'node["tourism"~"^(camp_site|wilderness_hut|alpine_hut|viewpoint)$"]({bbox});'
        'way["tourism"~"^(camp_site|wilderness_hut|alpine_hut|viewpoint)$"]({bbox});'
        'node["amenity"~"^(shelter|drinking_water)$"]({bbox});'
        'node["natural"="spring"]({bbox});'
    ),
    "protected": (
        'way["boundary"="protected_area"]({bbox});'
        'relation["boundary"="protected_area"]({bbox});'
        'way["leisure"="nature_reserve"]({bbox});'
        'relation["leisure"="nature_reserve"]({bbox});'
        'relation["boundary"="national_park"]({bbox});'
    ),
}


def overpass_query(body: str, bb: tuple[float, float, float, float]) -> dict | None:
    w, s, e, n = bb
    bbox_str = f"{s},{w},{n},{e}"
    q = f"[out:json][timeout:300];({body.format(bbox=bbox_str)});out geom;"
    # Overpass rate-limits hard (429) and times out under load (504); both
    # want a patient backoff rather than a fast retry.
    for round_no in range(1, 4):
        for endpoint in OVERPASS_ENDPOINTS:
            try:
                r = requests.post(endpoint, data={"data": q},
                                  headers=HEADERS, timeout=420)
                if r.status_code == 200:
                    return r.json()
                log(f"overpass HTTP {r.status_code} at {endpoint} "
                    f"(round {round_no})")
                if r.status_code == 429:
                    time.sleep(45 * round_no)
                else:
                    time.sleep(10 * round_no)
            except Exception as exc:  # noqa: BLE001
                log(f"overpass round {round_no} at {endpoint} failed: {exc}")
                time.sleep(10 * round_no)
    return None


def _point_in_ring(pt, ring) -> bool:
    x, y = pt
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _stitch_rings(members: list, role: str) -> list[list]:
    """Join a multipolygon's member ways into closed rings.

    Members arrive as unordered fragments that share endpoints; a ring is
    complete when its first and last coordinate match. Fragments may need
    reversing, and may attach to either end of the ring being built.
    """
    ways = [
        [(p["lon"], p["lat"]) for p in (m.get("geometry") or [])]
        for m in members
        if (m.get("role") or "outer") == role and m.get("geometry")
    ]
    ways = [w for w in ways if len(w) >= 2]

    rings = []
    while ways:
        ring = ways.pop(0)
        extended = True
        while ring[0] != ring[-1] and extended:
            extended = False
            for i, w in enumerate(ways):
                if w[0] == ring[-1]:
                    ring = ring + w[1:]
                elif w[-1] == ring[-1]:
                    ring = ring + w[-2::-1]
                elif w[-1] == ring[0]:
                    ring = w[:-1] + ring
                elif w[0] == ring[0]:
                    ring = w[:0:-1] + ring
                else:
                    continue
                ways.pop(i)
                extended = True
                break
        if len(ring) >= 4:
            if ring[0] != ring[-1]:
                ring = ring + [ring[0]]   # force-close a slightly broken ring
            rings.append(ring)
    return rings


def _relation_polygon(el: dict) -> dict | None:
    outers = _stitch_rings(el.get("members") or [], "outer")
    if not outers:
        return None
    inners = _stitch_rings(el.get("members") or [], "inner")
    polys = []
    for o in outers:
        holes = [h for h in inners if _point_in_ring(h[0], o)]
        polys.append([o] + holes)
    if len(polys) == 1:
        return {"type": "Polygon", "coordinates": polys[0]}
    return {"type": "MultiPolygon", "coordinates": polys}


def osm_to_geojson(payload: dict) -> dict:
    """Convert an Overpass 'out geom' response to GeoJSON."""
    feats = []
    for el in payload.get("elements", []):
        tags = el.get("tags", {}) or {}
        etype = el.get("type")
        if etype == "relation":
            geom = _relation_polygon(el)
            if geom is None:
                continue
            feats.append({
                "type": "Feature",
                "id": f"relation/{el.get('id')}",
                "properties": tags,
                "geometry": geom,
            })
            continue
        if etype == "node":
            if "lat" not in el:
                continue
            geom = {"type": "Point", "coordinates": [el["lon"], el["lat"]]}
        elif etype == "way":
            coords = [[p["lon"], p["lat"]] for p in el.get("geometry", []) or []]
            if len(coords) < 2:
                continue
            closed = coords[0] == coords[-1] and len(coords) >= 4
            area_ish = closed and (
                "natural" in tags or "landuse" in tags or "leisure" in tags
                or "boundary" in tags or "tourism" in tags
            )
            geom = (
                {"type": "Polygon", "coordinates": [coords]}
                if area_ish
                else {"type": "LineString", "coordinates": coords}
            )
        else:
            continue
        feats.append({
            "type": "Feature",
            "id": f"{etype}/{el.get('id')}",
            "properties": tags,
            "geometry": geom,
        })
    return {"type": "FeatureCollection", "features": feats}


# Peaks are sparse and are the labels in the far-field panorama, so they use
# the full raster extent rather than the tighter vector one — otherwise a
# landmark like Mt Rainier is rendered in the view but has no name attached.
THEME_EXTENT = {"peaks": "buffered"}


def download_osm(slug: str, bb: tuple[float, float, float, float],
                 aoi: dict | None = None) -> None:
    out_dir = RAW / "osm"
    for theme, body in OSM_THEMES.items():
        theme_bb = bb
        if aoi and theme in THEME_EXTENT:
            b = aoi[THEME_EXTENT[theme]]
            theme_bb = (b["west"], b["south"], b["east"], b["north"])
        dest = out_dir / f"{slug}_{theme}.geojson"
        if dest.exists() and dest.stat().st_size > 200:
            log(f"skip {dest.name} (present)")
            continue
        log(f"overpass: {theme} ...")
        payload = overpass_query(body, theme_bb)
        if payload is None:
            log(f"FAILED {theme} — leaving empty placeholder")
            gj = {"type": "FeatureCollection", "features": []}
        else:
            gj = osm_to_geojson(payload)
        dest.write_text(json.dumps(gj), encoding="utf-8")
        log(f"got  {dest.name} ({len(gj['features'])} features, "
            f"{dest.stat().st_size / 1e6:.1f} MB)")
        time.sleep(2)


# --------------------------------------------------------------------------

def main() -> int:
    slug = sys.argv[1] if len(sys.argv) > 1 else "alpine-lakes"
    aoi = load_aoi(slug)
    ensure_dirs(slug)
    bb = bbox(aoi, aoi.get("vector_extent", "buffered"))
    ok = True

    log("=== 3DEP 1 arc-second DEM ===")
    for tile in aoi["sources"]["dem"]["tiles"]:
        dest = RAW / "3dep-1as" / f"USGS_1_{tile}.tif"
        if not fetch(DEP3_URL.format(tile=tile), dest, min_bytes=1 << 20):
            log(f"WARNING: DEM tile {tile} unavailable")
            ok = False

    log("=== ESA WorldCover 10 m ===")
    for tile in aoi["sources"]["landcover"]["tiles"]:
        dest = RAW / "worldcover-v200" / f"ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
        if not fetch(WORLDCOVER_URL.format(tile=tile), dest, min_bytes=1 << 20):
            log(f"WARNING: WorldCover tile {tile} unavailable")

    log("=== RGI 7.0 glaciers (region 02) ===")
    rgi_zip = RAW / "rgi7" / "RGI2000-v7.0-G-02_western_canada_usa.zip"
    # NSIDC answers 200 with an Earthdata login page when unauthenticated,
    # so require a real zip signature.
    if fetch(RGI_URL, rgi_zip, min_bytes=1 << 18, magic=b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(rgi_zip) as zf:
                zf.extractall(RAW / "rgi7")
            log("extracted RGI archive")
        except Exception as exc:  # noqa: BLE001
            log(f"could not extract RGI zip: {exc}")
    else:
        log("RGI unavailable (likely needs Earthdata login) "
            "— OSM natural=glacier will be used instead")

    log("=== OpenStreetMap (Overpass) ===")
    download_osm(slug, bb, aoi)

    log("done" if ok else "done, with warnings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
