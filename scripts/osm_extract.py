"""OSM themes for a region from a Geofabrik extract instead of Overpass.

    python scripts/osm_extract.py <slug>

A region the size of western Washington is too much for Overpass: the Seattle
metro alone has hundreds of thousands of road ways, and the public servers
time out or rate-limit long before. So the state extract is downloaded once
(`raw/osm/washington-latest.osm.pbf`, ~300 MB) and read with pyosmium, and
every theme is written exactly as `download.py` writes it from Overpass: the
same tag rules, the same GeoJSON (`way/<id>` and `relation/<id>` ids, tags as
properties, closed area-like ways as polygons), kept where it meets the
region's vector box.

Peaks are the exception. They label the panorama's far field, which reaches
Oregon and British Columbia, so they still come from Overpass over the full
raster extent; nodes only, which Overpass answers quickly.
"""
from __future__ import annotations

import json
import re
import sys
import time

import osmium

import download
from aoi import RAW, bbox, load_aoi

GEOFABRIK_URL = "https://download.geofabrik.de/north-america/us/washington-latest.osm.pbf"


def log(msg: str) -> None:
    print(f"[osm] {msg}", flush=True)


def _rx(s: str):
    return re.compile(s)


# Each theme: which object kinds it takes, and a predicate on the tags.
# These mirror download.OSM_THEMES; keep the two in step.
TRAIL_HW = _rx(r"^(path|footway|bridleway|track|steps)$")
NOT_SIDEWALK = _rx(r"^(sidewalk|crossing|traffic_island)$")
# 'service' (driveways, parking aisles, alleys) is left out at region scale:
# it is most of the road ways in a metro area, and the map's imagery shows
# them anyway. Nothing is scored on roads.
ROAD_HW = _rx(r"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential)$")
WATERWAY = _rx(r"^(river|stream|canal)$")
POI_TOURISM = _rx(r"^(camp_site|wilderness_hut|alpine_hut|viewpoint)$")
POI_AMENITY = _rx(r"^(shelter|drinking_water)$")


def themes_for(kind: str, t: dict) -> list[str]:
    """The themes an object of `kind` ('node', 'way', 'relation') belongs to."""
    out = []
    g = t.get
    if kind == "way":
        if TRAIL_HW.match(g("highway", "")) and not NOT_SIDEWALK.match(g("footway", "")):
            out.append("trails")
        if ROAD_HW.match(g("highway", "")):
            out.append("roads")
        if WATERWAY.match(g("waterway", "")):
            out.append("water_lines")
    if kind in ("way", "relation"):
        if g("natural") == "water" or g("landuse") == "reservoir":
            out.append("water_areas")
        if g("natural") == "glacier":
            out.append("glaciers")
        if (g("boundary") == "protected_area" or g("leisure") == "nature_reserve"
                or (kind == "relation" and g("boundary") == "national_park")):
            out.append("protected")
    if kind == "node":
        if g("mountain_pass") == "yes" or g("natural") == "saddle":
            out.append("passes")
        if (POI_TOURISM.match(g("tourism", "")) or POI_AMENITY.match(g("amenity", ""))
                or g("natural") == "spring"):
            out.append("poi")
    if kind == "way" and POI_TOURISM.match(g("tourism", "")):
        out.append("poi")
    return out


LINEAR_THEMES = {"trails", "roads", "water_lines"}
THEMES = ["trails", "roads", "water_lines", "water_areas", "glaciers",
          "passes", "poi", "protected"]


def _bounds(coords_iter):
    xs, ys = [], []
    for x, y in coords_iter:
        xs.append(x)
        ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def extract(pbf, slug: str, bb) -> dict:
    w, s, e, n = bb

    def meets(b):
        return b is not None and b[0] <= e and b[2] >= w and b[1] <= n and b[3] >= s

    outs = {th: (RAW / "osm" / f"{slug}_{th}.geojson.part").open("w", encoding="utf-8")
            for th in THEMES}
    counts = {th: 0 for th in THEMES}
    for th, fh in outs.items():
        fh.write('{"type": "FeatureCollection", "features": [\n')

    def emit(theme, feat):
        fh = outs[theme]
        if counts[theme]:
            fh.write(",\n")
        fh.write(json.dumps(feat, separators=(",", ":")))
        counts[theme] += 1

    t0 = time.time()
    seen = 0
    fp = osmium.FileProcessor(str(pbf)).with_locations().with_areas()
    for obj in fp:
        seen += 1
        if seen % 5_000_000 == 0:
            log(f"  {seen / 1e6:.0f}M objects, {time.time() - t0:.0f} s")
        if obj.is_area():
            # multipolygons assembled from relations; closed ways are handled
            # as ways below, as download.py does
            if obj.from_way():
                continue
            tags = dict(obj.tags)
            ths = themes_for("relation", tags)
            if not ths:
                continue
            polys = []
            for outer in obj.outer_rings():
                ring = [[nd.lon, nd.lat] for nd in outer]
                holes = [[[nd.lon, nd.lat] for nd in inner] for inner in obj.inner_rings(outer)]
                polys.append([ring] + holes)
            if not polys:
                continue
            b = _bounds((x, y) for p in polys for x, y in p[0])
            if not meets(b):
                continue
            geom = ({"type": "Polygon", "coordinates": polys[0]} if len(polys) == 1
                    else {"type": "MultiPolygon", "coordinates": polys})
            feat = {"type": "Feature", "id": f"relation/{obj.orig_id()}",
                    "properties": tags, "geometry": geom}
            for th in ths:
                emit(th, feat)
        elif obj.is_way():
            tags = dict(obj.tags)
            ths = themes_for("way", tags)
            if not ths:
                continue
            try:
                coords = [[nd.lon, nd.lat] for nd in obj.nodes]
            except osmium.InvalidLocationError:
                continue
            if len(coords) < 2 or not meets(_bounds(coords)):
                continue
            closed = coords[0] == coords[-1] and len(coords) >= 4
            area_ish = closed and any(k in tags for k in
                                      ("natural", "landuse", "leisure", "boundary", "tourism"))
            line = {"type": "LineString", "coordinates": coords}
            poly = {"type": "Polygon", "coordinates": [coords]} if area_ish else line
            for th in ths:
                # trails, roads and waterways are lines whatever else a way is
                # tagged with: a closed path around a park (leisure=park) is
                # still a path, and the scorer walks it as one
                emit(th, {"type": "Feature", "id": f"way/{obj.id}", "properties": tags,
                          "geometry": line if th in LINEAR_THEMES else poly})
        elif obj.is_node():
            if not obj.tags:
                continue
            tags = dict(obj.tags)
            ths = themes_for("node", tags)
            if not ths:
                continue
            x, y = obj.location.lon, obj.location.lat
            if not (w <= x <= e and s <= y <= n):
                continue
            feat = {"type": "Feature", "id": f"node/{obj.id}", "properties": tags,
                    "geometry": {"type": "Point", "coordinates": [x, y]}}
            for th in ths:
                emit(th, feat)

    for th, fh in outs.items():
        fh.write("\n]}\n")
        fh.close()
        part = RAW / "osm" / f"{slug}_{th}.geojson.part"
        part.replace(part.with_suffix(""))
        log(f"{slug}_{th}.geojson: {counts[th]:,} features")
    log(f"read {seen / 1e6:.1f}M objects in {(time.time() - t0) / 60:.1f} min")
    return counts


def main() -> int:
    slug = sys.argv[1] if len(sys.argv) > 1 else "wa-west"
    aoi = load_aoi(slug)
    (RAW / "osm").mkdir(parents=True, exist_ok=True)
    pbf = RAW / "osm" / "washington-latest.osm.pbf"
    if not download.fetch(GEOFABRIK_URL, pbf, min_bytes=50 << 20):
        log("could not download the Geofabrik extract")
        return 1
    extract(pbf, slug, bbox(aoi, aoi.get("vector_extent", "buffered")))

    # far-field peaks from Overpass, nodes only
    dest = RAW / "osm" / f"{slug}_peaks.geojson"
    if dest.exists() and dest.stat().st_size > 200:
        log(f"skip {dest.name} (present)")
    else:
        payload = download.overpass_query(download.OSM_THEMES["peaks"], bbox(aoi, "buffered"))
        if payload is None:
            log("FAILED peaks from Overpass; rerun to retry")
            return 1
        gj = download.osm_to_geojson(payload)
        dest.write_text(json.dumps(gj), encoding="utf-8")
        log(f"{dest.name}: {len(gj['features']):,} features")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
