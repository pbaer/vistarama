"""Attach topographic prominence to the AOI's OSM summits.

Source is Kirmse & de Ferranti's global prominence dataset (see
`docs/data-sources.md`), which lists every summit on earth with at least 100 ft
of prominence, derived from SRTM and viewfinder DEMs.

Why prominence rather than the heuristics it replaces:

* Label ranking used `stands`, the height a summit clears the ridge in front of
  it. That is view-dependent, and from Mount Stuart it ranked Liberty Cap above
  Mount Rainier's own summit because the two look over slightly different
  foreground. Prominence has no such quirk.
* Massif suppression used a geometric proxy — same massif if separated by less
  than 15% of the observer distance — with a constant fitted by hand against
  Rainier. "Is this a sub-peak" *is* the definition of low prominence, so the
  proxy and its constant both go away.

The join is nearest-neighbour with an elevation check, because the two sources
disagree by construction: Kirmse's summits are DEM cell centres and OSM's are
hand-placed nodes, so they are tens to a couple of hundred metres apart even
when they mean the same mountain.

Output is a sidecar keyed by the OSM coordinate rather than a patched copy of
the peaks file, so that re-running `download.py` cannot silently drop it.
"""
from __future__ import annotations

import json
import math
import zipfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
FT = 0.3048

# How far an OSM node may sit from a Kirmse summit and still be the same
# mountain, and how much they may disagree on height. Both are generous: the
# sources are independent, and a wrong *miss* (no prominence) is worse here
# than a wrong match, since a missing value falls back to the old behaviour.
JOIN_RADIUS_M = 250.0
JOIN_ELEV_TOL_M = 120.0


def log(msg: str) -> None:
    print(f"[prominence] {msg}", flush=True)


def load_kirmse(zip_path: Path, bbox) -> np.ndarray:
    """Rows of (lat, lon, elev_m, prom_m) inside bbox."""
    w, s, e, n = bbox
    out = []
    with zipfile.ZipFile(zip_path) as z:
        name = z.namelist()[0]
        with z.open(name) as fh:
            for line in fh:
                # lat,lon,elev_ft,saddle_lat,saddle_lon,prominence_ft
                p = line.split(b",")
                if len(p) < 6:
                    continue
                lat = float(p[0]); lon = float(p[1])
                if not (s <= lat <= n and w <= lon <= e):
                    continue
                out.append((lat, lon, float(p[2]) * FT, float(p[5]) * FT))
    return np.asarray(out, dtype=np.float64)


def _peak_ele(props: dict) -> float | None:
    try:
        return float(str(props.get("ele", "")).split()[0])
    except (ValueError, IndexError):
        return None


def main() -> int:
    slug = "alpine-lakes"
    aoi = json.loads((ROOT / "data" / "aoi" / f"{slug}.json").read_text("utf-8"))
    b = aoi["buffered"]
    bbox = (b["west"], b["south"], b["east"], b["north"])

    zp = ROOT / "data" / "raw" / "prominence" / "kirmse_prominence.raw"
    if not zp.exists():
        log(f"missing {zp} — download it first")
        return 1
    k = load_kirmse(zp, bbox)
    log(f"{len(k)} Kirmse summits inside the buffered AOI")

    peaks_path = ROOT / "data" / "raw" / "osm" / f"{slug}_peaks.geojson"
    gj = json.loads(peaks_path.read_text(encoding="utf-8"))
    feats = [f for f in gj.get("features", [])
             if (f.get("geometry") or {}).get("type") == "Point"]
    log(f"{len(feats)} OSM peak nodes")

    # metres per degree at this latitude, good enough for a 250 m search
    lat0 = math.radians((bbox[1] + bbox[3]) / 2)
    m_per_deg_lat = 111132.0
    m_per_deg_lon = 111320.0 * math.cos(lat0)
    kx = k[:, 1] * m_per_deg_lon
    ky = k[:, 0] * m_per_deg_lat

    out: dict[str, float] = {}
    matched = far = 0
    dists = []
    for f in feats:
        lon, lat = f["geometry"]["coordinates"][:2]
        ele = _peak_ele(f.get("properties", {}) or {})
        d = np.hypot(kx - lon * m_per_deg_lon, ky - lat * m_per_deg_lat)
        i = int(np.argmin(d))
        if d[i] > JOIN_RADIUS_M:
            far += 1
            continue
        if ele is not None and abs(k[i, 2] - ele) > JOIN_ELEV_TOL_M:
            far += 1
            continue
        out[f"{lat:.5f},{lon:.5f}"] = round(float(k[i, 3]), 1)
        dists.append(float(d[i]))
        matched += 1

    dst = ROOT / "data" / "interim" / slug / "peak_prominence.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps({
        "source": "Kirmse & de Ferranti global prominence (p100)",
        "citation": ("Kirmse, A. & de Ferranti, J. (2017). Calculating the "
                     "prominence and isolation of every mountain in the world. "
                     "Progress in Physical Geography 41(6)."),
        "join_radius_m": JOIN_RADIUS_M,
        "matched": matched, "unmatched": far,
        "prominence_m": out,
    }, indent=1), encoding="utf-8")
    if dists:
        log(f"matched {matched}/{len(feats)} peaks "
            f"(median join distance {np.median(dists):.0f} m, "
            f"p95 {np.percentile(dists, 95):.0f} m); {far} unmatched")
    log(f"wrote {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
