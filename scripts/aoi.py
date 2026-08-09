"""Shared AOI definitions and path helpers."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
AOI_DIR = DATA / "aoi"
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
