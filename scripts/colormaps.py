"""Colour ramps and categorical palettes. No matplotlib dependency."""
from __future__ import annotations

import numpy as np

# (stop 0-1, (r, g, b))
RAMPS: dict[str, list[tuple[float, tuple[int, int, int]]]] = {
    "gray": [(0.0, (0, 0, 0)), (1.0, (255, 255, 255))],
    "elevation": [
        (0.00, (56, 104, 66)),
        (0.18, (122, 156, 88)),
        (0.36, (196, 194, 124)),
        (0.54, (188, 152, 106)),
        (0.70, (150, 122, 104)),
        (0.85, (206, 206, 212)),
        (1.00, (255, 255, 255)),
    ],
    "slope": [
        (0.00, (255, 255, 229)),
        (0.30, (254, 217, 142)),
        (0.55, (254, 153, 41)),
        (0.78, (217, 72, 26)),
        (1.00, (127, 22, 18)),
    ],
    "ruggedness": [
        (0.00, (68, 1, 84)),
        (0.25, (59, 82, 139)),
        (0.50, (33, 145, 140)),
        (0.75, (94, 201, 98)),
        (1.00, (253, 231, 37)),
    ],
    # deliberately a different family from "ruggedness" — the two layers answer
    # different questions and get compared against each other constantly, so
    # they must not look alike
    "relief": [
        (0.00, (12, 7, 35)),
        (0.25, (85, 15, 109)),
        (0.50, (170, 51, 117)),
        (0.75, (243, 113, 92)),
        (1.00, (252, 253, 191)),
    ],
    # Diverging, for openness: 90 deg is the neutral value a plane takes, so
    # the ramp must be centred there — blue = enclosed/concave, orange =
    # dominant/convex, pale at exactly planar.
    "openness": [
        (0.00, (38, 70, 122)),
        (0.28, (94, 142, 190)),
        (0.50, (238, 238, 232)),
        (0.72, (226, 150, 78)),
        (1.00, (140, 60, 26)),
    ],
    "defect": [
        (0.00, (18, 22, 30)),
        (0.30, (60, 84, 122)),
        (0.60, (146, 152, 152)),
        (1.00, (250, 244, 214)),
    ],
    "canopy": [
        (0.00, (247, 246, 238)),
        (0.35, (176, 205, 158)),
        (0.70, (79, 143, 79)),
        (1.00, (16, 74, 32)),
    ],
    "builtup": [
        (0.00, (250, 250, 250)),
        (0.30, (245, 205, 190)),
        (0.65, (226, 116, 90)),
        (1.00, (176, 20, 20)),
    ],
}

# ESA WorldCover v200 official palette
WORLDCOVER: dict[int, tuple[int, int, int]] = {
    10: (0, 100, 0),      # Tree cover
    20: (255, 187, 34),   # Shrubland
    30: (255, 255, 76),   # Grassland
    40: (240, 150, 255),  # Cropland
    50: (250, 0, 0),      # Built-up
    60: (180, 180, 180),  # Bare / sparse vegetation
    70: (240, 240, 240),  # Snow and ice
    80: (0, 100, 200),    # Permanent water bodies
    90: (0, 150, 160),    # Herbaceous wetland
    95: (0, 207, 117),    # Mangroves
    100: (250, 230, 160),  # Moss and lichen
}

WORLDCOVER_LABELS: dict[int, str] = {
    10: "Tree cover", 20: "Shrubland", 30: "Grassland", 40: "Cropland",
    50: "Built-up", 60: "Bare / sparse", 70: "Snow and ice",
    80: "Permanent water", 90: "Herbaceous wetland", 95: "Mangroves",
    100: "Moss and lichen",
}


def _lut(name: str, n: int = 256) -> np.ndarray:
    """Build an n x 3 uint8 lookup table from a ramp definition."""
    stops = RAMPS[name]
    xs = np.array([s[0] for s in stops])
    cols = np.array([s[1] for s in stops], dtype=float)
    t = np.linspace(0.0, 1.0, n)
    out = np.empty((n, 3), dtype=np.uint8)
    for c in range(3):
        out[:, c] = np.interp(t, xs, cols[:, c]).round().astype(np.uint8)
    return out


# keyed by (ramp name, size) — the indexed path uses 255 entries so that index
# 255 can mean transparent, while the RGBA path uses a full 256
_LUT_CACHE: dict[tuple[str, int], np.ndarray] = {}


def get_lut(name: str, n: int) -> np.ndarray:
    key = (name, n)
    if key not in _LUT_CACHE:
        _LUT_CACHE[key] = _lut(name, n)
    return _LUT_CACHE[key]


def ramp_rgba(
    values: np.ndarray,
    valid: np.ndarray,
    ramp: str,
    vmin: float,
    vmax: float,
    alpha: int = 255,
) -> np.ndarray:
    """Map a float array through a ramp to an (H, W, 4) uint8 RGBA array."""
    lut = get_lut(ramp, 256)

    span = max(vmax - vmin, 1e-9)
    idx = np.clip((values - vmin) / span, 0.0, 1.0)
    idx = (idx * 255.0).round().astype(np.uint8)

    h, w = values.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    rgba[..., :3] = lut[idx]
    rgba[..., 3] = np.where(valid, alpha, 0).astype(np.uint8)
    return rgba


# --------------------------------------------------------------------------
# Indexed (palette) output.
#
# Every ramp here is a 256-entry LUT and the categorical palettes have 11
# classes, so an 8-bit paletted PNG is lossless *and* about 3x smaller than
# RGBA — noisy layers like hillshade and slope compress badly in RGBA because
# every pixel is a distinct 4-byte tuple.
#
# Index 255 is reserved for transparent, leaving 0..254 for colour.
# --------------------------------------------------------------------------

TRANSPARENT_INDEX = 255

# open water, as the viewer draws lakes and rivers (viewer/app.js WATER_BLUE)
WATER_BLUE = (0x2f, 0x6f, 0x9f)


def _palette_bytes(colors: list[tuple[int, int, int]]) -> bytes:
    flat = bytearray(768)
    for i, (r, g, b) in enumerate(colors[:255]):
        flat[i * 3:i * 3 + 3] = bytes((r, g, b))
    return bytes(flat)


def ramp_indexed(
    values: np.ndarray,
    valid: np.ndarray,
    ramp: str,
    vmin: float,
    vmax: float,
) -> tuple[np.ndarray, bytes]:
    """Map a float array to (index array, PNG palette). 255 == transparent."""
    lut = get_lut(ramp, 255)

    span = max(vmax - vmin, 1e-9)
    idx = np.clip((values - vmin) / span, 0.0, 1.0)
    idx = (idx * 254.0).round().astype(np.uint8)
    idx = np.where(valid, idx, TRANSPARENT_INDEX).astype(np.uint8)
    return idx, _palette_bytes([tuple(c) for c in lut])


def categorical_indexed(
    values: np.ndarray,
    palette: dict[int, tuple[int, int, int]],
) -> tuple[np.ndarray, bytes]:
    """Map class codes to (index array, PNG palette). Unlisted -> transparent."""
    codes = sorted(palette)
    idx = np.full(values.shape, TRANSPARENT_INDEX, dtype=np.uint8)
    for i, code in enumerate(codes):
        idx[values == code] = i
    return idx, _palette_bytes([palette[c] for c in codes])


def categorical_rgba(
    values: np.ndarray,
    palette: dict[int, tuple[int, int, int]],
    alpha: int = 255,
) -> np.ndarray:
    """Map integer class codes to RGBA. Unlisted codes become transparent."""
    h, w = values.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    for code, (r, g, b) in palette.items():
        m = values == code
        if not m.any():
            continue
        rgba[m, 0] = r
        rgba[m, 1] = g
        rgba[m, 2] = b
        rgba[m, 3] = alpha
    return rgba
