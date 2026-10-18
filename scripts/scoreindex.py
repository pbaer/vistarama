"""Server-side queries over the stored scores, for a viewer that cannot hold
them all.

Alpine Lakes has ~7k scored items and the viewer simply downloaded every
summary (4.6 MB). Western Washington has ~190k (~110 MB), so the ranked list
asks the server for one page at a time, and the map asks only for the
features it has loaded:

* `query(slug, ...)`: the ranked list. Items whose best point lies in a
  bounding box, or whose name contains a search string (anywhere, ignoring
  case and accents), filtered by type, named-only, minimum trail length and
  the V threshold, sorted, one page of rows plus the total. Also the counts
  and the top V the left pane's filter needs.
* `lookup(slug, ids)`: the full summaries behind a set of OSM way or node ids.

Built from featurescore.summaries() into numpy arrays, and rebuilt whenever
that changes (it is cached on the store file's mtime), so a query is a few
vectorised comparisons and one argsort, milliseconds even at 190k items.
"""
from __future__ import annotations

import math
import threading
import unicodedata

import numpy as np

import featurescore

_lock = threading.Lock()
_cache: dict = {}            # slug -> (summaries object, index)

THEMES = ["trails", "peaks", "passes", "water_areas", "camps"]
# sortable columns -> record field (or a getter)
SORT_FIELDS = {"V": "V", "V_max": "V_max", "area_m2": "area_m2", "length_m": "length_m",
               "ele_min_m": "ele_min_m", "ele_max_m": "ele_max_m", "gain_m": "gain_m"}
# fields left out of what the list and the map receive
DROP = ("way_stats", "version", "computed_at", "elapsed_s", "eye_m", "spacing_m")


def norm(s: str) -> str:
    """Lower case, accents dropped: what the search matches against."""
    return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                   if not unicodedata.combining(c)).lower()


def _path_record(r: dict) -> bool:
    return r.get("kind") in ("line", "shore")


class _Index:
    def __init__(self, summ: dict):
        items = list(summ["items"].values())
        self.items = items
        self.ids = summ["ids"]
        n = len(items)
        self.theme = np.array([THEMES.index(r["theme"]) if r.get("theme") in THEMES else -1
                               for r in items], dtype=np.int8)
        self.named = np.array([bool(r.get("name")) for r in items])
        self.kind_line = np.array([r.get("kind") == "line" for r in items])
        self.is_path = np.array([_path_record(r) for r in items])

        def col(field):
            return np.array([(r.get(field) if r.get(field) is not None else np.nan)
                             for r in items], dtype=np.float64)

        self.cols = {k: col(f) for k, f in SORT_FIELDS.items()}
        # V_max only means something along a path; elsewhere V stands in
        self.v_best = np.where(self.is_path, self.cols["V_max"], self.cols["V"])
        lat = np.full(n, np.nan)
        lon = np.full(n, np.nan)
        for i, r in enumerate(items):
            p = r.get("best") if _path_record(r) else None
            if p:
                lat[i], lon[i] = p["lat"], p["lon"]
            elif r.get("lat") is not None:
                lat[i], lon[i] = r["lat"], r["lon"]
        self.lat, self.lon = lat, lon
        self.pos = {r.get("id"): i for i, r in enumerate(items)}
        self.names = [norm(r.get("name") or "") for r in items]
        self.name_order = np.argsort(np.array(self.names, dtype=object), kind="stable")
        self.name_rank = np.empty(n, dtype=np.int64)
        self.name_rank[self.name_order] = np.arange(n)

    def value(self, mode: str) -> np.ndarray:
        return self.v_best if mode == "best" else self.cols["V"]


def _index(slug: str) -> _Index:
    summ = featurescore.summaries(slug)
    with _lock:
        hit = _cache.get(slug)
        if hit and hit[0] is summ:
            return hit[1]
        idx = _Index(summ)
        _cache[slug] = (summ, idx)
        return idx


def _public(r: dict, keep_ways: bool = False) -> dict:
    return {k: v for k, v in r.items()
            if k not in DROP or (keep_ways and k == "way_stats")}


def query(slug: str, *, bbox=None, q: str = "", type_: str = "trails", named: bool = True,
          min_len: float = 0.0, vmode: str = "avg", vmin: float = 0.0, sort: str = "V",
          direction: int = -1, offset: int = 0, limit: int = 200, pin: str | None = None,
          excl=None) -> dict:
    idx = _index(slug)
    n = len(idx.items)
    val = idx.value(vmode)
    # the V filter, over every scored item: what the left pane reports
    vpass = np.ones(n, dtype=bool) if vmin <= 0 else (np.nan_to_num(val, nan=-1.0) >= vmin)
    m = vpass.copy()
    if type_ != "all":
        m &= idx.theme == (THEMES.index(type_) if type_ in THEMES else -2)
    if named:
        m &= idx.named
    if min_len > 0:
        m &= ~idx.kind_line | (np.nan_to_num(idx.cols["length_m"]) >= min_len)
    qn = norm(q.strip())
    if qn:
        # a search looks everywhere: what people search for is rarely on screen
        hits = np.zeros(n, dtype=bool)
        hits[[i for i, s in enumerate(idx.names) if qn in s]] = True
        m &= hits
    elif bbox:
        w, s, e, nn = bbox
        with np.errstate(invalid="ignore"):
            m &= (idx.lon >= w) & (idx.lon <= e) & (idx.lat >= s) & (idx.lat <= nn)
        # parts of the area the viewer covers with a card (the cursor card):
        # on the map but not visible, so not "in view"
        for xw, xs, xe, xn in excl or []:
            with np.errstate(invalid="ignore"):
                m &= ~((idx.lon >= xw) & (idx.lon <= xe) & (idx.lat >= xs) & (idx.lat <= xn))
    sel = np.flatnonzero(m)
    if sort == "name":
        key = idx.name_rank[sel].astype(np.float64)
        order = np.argsort(key if direction > 0 else -key, kind="stable")
    else:
        key = idx.cols.get(sort, idx.cols["V"])[sel]
        missing = np.isnan(key)
        k = np.where(missing, 0.0, key) * (1 if direction > 0 else -1)
        # missing values sink whichever way the column is sorted
        order = np.lexsort((k, missing))
    sel = sel[order]
    page = sel[offset:offset + limit] if limit > 0 else sel[:0]
    out = {
        "total": int(sel.size),
        "offset": offset,
        "rows": [_public(idx.items[i]) for i in page],
        "scored": n,
        "vf_pass": int(vpass.sum()),
        "vmax": {"avg": float(np.nanmax(idx.cols["V"])) if n else 0.0,
                 "best": float(np.nanmax(idx.v_best)) if n else 0.0},
    }
    if pin:
        key = idx.ids.get(pin, pin)
        rec = featurescore.summaries(slug)["items"].get(key)
        if rec:
            out["pinned"] = _public(rec)
            # whether it passes everything this list is filtered by; the
            # viewer drops the selection when it no longer does
            i = idx.pos.get(rec.get("id"))
            out["pinned_match"] = bool(i is not None and m[i])
    return out


def lookup(slug: str, ids: list[str]) -> dict:
    """{id: summary} for every id that has a score (aliases resolved), with
    the per-way bests the hover card marks."""
    summ = featurescore.summaries(slug)
    items, alias = summ["items"], summ["ids"]
    out = {}
    for fid in ids:
        key = alias.get(fid)
        if key and key in items:
            out[fid] = _public(items[key], keep_ways=True)
    return out


def parse_bbox(s: str | None):
    if not s:
        return None
    try:
        w, so, e, n = (float(v) for v in s.split(","))
    except ValueError:
        return None
    if not all(math.isfinite(v) for v in (w, so, e, n)):
        return None
    return (w, so, e, n)
