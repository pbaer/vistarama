"""Shared AOI definitions and path helpers."""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The AOI definitions are tracked inputs and stay in the repo. Everything
# built from them lives under the data root, which can be anywhere: it is
# ~15 GB for one AOI and read hard by the tiler and the panorama, so it
# belongs on the fastest disk with room. VISTARAMA_DATA wins, then
# "data_root" in the untracked vistarama.local.json at the repo root, then
# <repo>/data.
LOCAL_CONFIG = ROOT / "vistarama.local.json"


def _data_root() -> Path:
    env = os.environ.get("VISTARAMA_DATA")
    if env:
        return Path(env)
    if LOCAL_CONFIG.exists():
        root = json.loads(LOCAL_CONFIG.read_text(encoding="utf-8")).get("data_root")
        if root:
            return Path(root)
    return ROOT / "data"


DATA = _data_root()
AOI_DIR = ROOT / "data" / "aoi"
RAW = DATA / "raw"
INTERIM = DATA / "interim"
DERIVED = DATA / "derived"
WEB = DATA / "web"


def load_aoi(slug: str = "alpine-lakes") -> dict:
    with open(AOI_DIR / f"{slug}.json", encoding="utf-8") as fh:
        return json.load(fh)


def bbox(aoi: dict, which: str = "buffered") -> tuple[float, float, float, float]:
    """Return (west, south, east, north)."""
    b = aoi[which]
    return (b["west"], b["south"], b["east"], b["north"])


def paths(slug: str) -> dict[str, Path]:
    return {
        "interim": INTERIM / slug,
        "derived": DERIVED / slug,
        "web": WEB / slug,
    }


def ensure_dirs(slug: str) -> None:
    for p in paths(slug).values():
        p.mkdir(parents=True, exist_ok=True)
    (WEB / slug / "layers").mkdir(parents=True, exist_ok=True)
    (WEB / slug / "vectors").mkdir(parents=True, exist_ok=True)
    for sub in ("3dep-1as", "worldcover-v200", "rgi7", "osm"):
        (RAW / sub).mkdir(parents=True, exist_ok=True)
