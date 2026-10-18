# Plan: one western Washington region

Status: **plan only, apart from moving the data (done).** It replaces the
per-AOI design for Washington described in [aoi-manifest.md](aoi-manifest.md).
Alpine Lakes stays as it is until this region is built and checked, and then
the region replaces it completely.

## Decisions

- **One region, `wa-west`**: western Washington plus the east slopes of the
  Cascades (bounds below). It has no AOIs or sections inside it.
- **The right pane filters by place**, either by the visible map area (the
  default) or by a box the user drags out on the map.
- **The right pane has a search box** that filters by name as you type.
- **0.6 m NAIP aerial imagery in five rectangles:** the Olympics, Bellingham
  and the San Juans, and the North, Central and South Cascades (below). Elsewhere, Sentinel-2 satellite
  imagery at 10 m.
- **Sentinel-2 is a summer median composite**, chosen for the fewest seams.
- **NAIP picks the best quad**, even at the cost of seams. The rule stays
  "finest ground sample, then summer, then newest".
- **Every map tile is pre-built**, stored as one MBTiles file per layer.
- **Closed land is included**: the Cedar River and Tolt watersheds, the
  Yakama Reservation, military land. Features there are scored and listed
  like any other.
- **No coast scoring for now.** Lakes stay closed lakes only, and sea shores
  are not a feature type.
- **V's algorithm is unchanged.** It stays one rule computed from the data,
  the same everywhere; no place is ever weighted up or down by name or
  location. Calibrating V against new terrain comes later.
  - Scores near Alpine Lakes' old edges will still shift a little, because
    rays no longer stop at a DEM edge 60 km out. That is a change in the
    data, not in the algorithm.
- **`wa-west` replaces `alpine-lakes`** once it is built and checked.
- **The data lives in `C:/data/vistarama`** (done 2026-10-17). C: is a PCIe
  4.0 NVMe: 7.1 GB/s sequential reads against 3.4 GB/s on D:, with ~640 GB
  free.

## Why one region, not tiled AOIs

- **Rays go far.** V marches every ray to 150 km (`vscore.RANGE_M`), and the
  volcanoes really are visible from that far. An AOI about 100 km across
  would need a box about 400 km across to hold its far field, so tiled AOIs
  would each carry most of the state.
- **Today's buffer is too small.** Rays stop at the DEM's edge, and the
  Alpine Lakes buffer is only 60 km, so points near its edge lose up to 90 km
  of view on that side. A single region with a 150 km buffer fixes this.
- **No seams.** The map is continuous, the panorama can walk anywhere, and
  no feature straddles a boundary.

## Region bounds

**The core** is where features are scored:

- **West:** the Pacific coast.
- **North:** the Canadian border.
- **South:** the Columbia River.
- **East:** a line along the foot of the east slopes:
  1. the Okanogan River from Oroville to its mouth at Brewster;
  2. the Columbia River down to Wenatchee;
  3. US 97 over Blewett Pass to Ellensburg;
  4. I-82/US 97 to Yakima;
  5. US 97 to Goldendale and the Columbia at Maryhill.

The core takes in the Methow, Chelan, Entiat, Teanaway, Manastash, the west
half of the Yakama Reservation and west Klickitat, and stops where the
Columbia Plateau begins. It is about 87,000 km² of land, about 17 times the
Alpine Lakes core.

The polygon is drawn once from OSM (coastline, border, rivers, highways),
simplified, and stored in `regions/wa-west.json`.

**The far field** is the core plus 150 km: about 44.2–50.4°N and
126.8–117.7°W. It reaches Hood, Jefferson and the Three Sisters to the
south and the BC Coast Mountains to the north. The 30 m layers cover all of
it, so rays never stop at a DEM edge.

## NAIP rectangles

These are longitude/latitude boxes, clipped to the core and stored in
`regions/wa-west.json` next to the polygon.

| Rectangle | Lat | Lon | Area | Covers |
|---|---|---|---|---|
| Olympics | 47.246–48.17 | −124.251 to −122.799 | 11,200 km² | the Olympic Mountains, Hurricane Ridge to the Skokomish |
| Bellingham & San Juans | 48.38–49.00 | −123.25 to −122.10 | 5,800 km² (mostly water) | the San Juan Islands, Fidalgo, Lummi, Chuckanut and the hills around Bellingham, Point Roberts |
| North Cascades | 47.75–49.00 | −122.10 to −119.90 | 22,700 km² | Baker, Glacier Peak, the Pasayten, the Methow, Lake Chelan |
| Central Cascades | 47.15–47.75 | −122.20 to −120.35 | 9,300 km² | the Issaquah Alps, Alpine Lakes, Snoqualmie and Stevens passes, out to Wenatchee |
| South Cascades | 45.55–47.15 | −122.605 to −120.745 | 25,500 km² | Rainier, Goat Rocks, St Helens, Adams, the Gorge |
| **Total** | | | **~74,500 km²** | |

- **The boxes abut.** Between them they contain the whole existing Alpine
  Lakes mosaic (47.25–47.85°N, 121.6–120.6°W), so that mosaic goes in as is
  and its area is never fetched again.
- **The Bellingham box is mostly salt water and some of Canada's Gulf
  Islands.** NAIP has nothing outside the US and is clipped to the core, so
  the real fetch there is well under its box area. The estimates below
  count full box areas, so they are upper bounds.
- **They only choose an imagery source**, so they have no bearing on V.

## Layers

| Layer | Source | Extent | Resolution |
|---|---|---|---|
| DEM | 3DEP 1 arc-second (US); Copernicus GLO-30 (BC) | far field | 30 m |
| Land cover, tree counts | ESA WorldCover v200 | far field | 10 m (counted per 30 m cell) |
| Sentinel-2 imagery | sentinel-2-l2a: a July–September median of clear pixels, one year | core + 30 km | 10 m |
| Sentinel-2, far field | the same, decimated | far field | ~60 m (the panorama's farthest level only needs ~150 m/texel) |
| NAIP imagery | Planetary Computer `naip` | the five rectangles | 0.6 m, JPEG q88 |
| OSM features | Geofabrik `washington-latest.osm.pbf` | core | — |
| OSM peaks for panorama labels | Overpass, `natural=peak` only | far field (OR, BC) | — |
| Prominence | Kirmse & de Ferranti (already local) | far field | — |

Notes:

- **The Sentinel-2 composite:**
  - It uses the scene classification band (SCL) to drop cloud, thin cirrus
    and cloud shadow, then takes the per-pixel median of what remains over
    July–September. Snow is kept, because summer snowfields and glaciers are
    real.
  - It is the year NAIP mostly comes from (2023), so the two match as well
    as they can.
  - Today's Alpine Lakes layer is one clear day (2023-08-15). No such day
    covers the whole region.
- **GLO-30 is a surface model**: it includes tree height, while 3DEP is bare
  earth. Across the border, V's canopy model would count trees twice. This
  only affects terrain north of the border, so it matters most for
  viewpoints near Baker and the Pasayten. Measure the step along the border
  before deciding whether it matters.
- **Sea and Puget Sound** must be height 0 and class "water" in every layer.
  - Check what 3DEP holds over salt water before trusting it.
  - The lake rule (natural=water, not salt or tidal, ≥ 0.5 ha) must still
    exclude lagoons, estuaries and bays now that the region has a real
    coastline.
- **Sentinel-2 crosses UTM zones 10 and 11**, at 120°W. Everything is
  reprojected to EPSG:5070 anyway.

## Data layout

- **Data root (done 2026-10-17).** `aoi.DATA` comes from `VISTARAMA_DATA`,
  else `data_root` in the untracked `vistarama.local.json`, else
  `<repo>/data`.
  - Every script and `serve.py` goes through it, and the server maps
    `/data/` onto it.
  - Alpine Lakes (14.6 GB, 201k files) was copied to `C:/data/vistarama`,
    checked file by file (sizes, plus hashes of a sample), and removed
    from D:.
- **Still to do: move the tracked definitions out of `data/`.**
  `data/aoi/*.json` becomes `regions/*.json` at the repo root, and
  `data/README.md` becomes `docs/data-layout.md`. Then the repo's `data/`
  folder can go entirely.
- **NAIP is stored in work tiles of 0.25° × 0.25°** (about 19 × 28 km), each
  its own JPEG GeoTIFF with overviews, joined by one GDAL VRT.
  - A failure loses one tile, not a 90 GB file, and resuming is per tile.
  - Only tiles that meet a rectangle are fetched.
- **Map tiles are in MBTiles**, one SQLite file per raster layer.
  - `tiles.py` writes rows from its workers through a single writer.
  - `serve.py` answers the same `/data/web/<region>/layers/<layer>/{z}/{x}/{y}`
    URLs from the file, so the viewer doesn't change.
  - That is ~2 M tiles as a handful of files instead of ~2 M files on NTFS,
    which also makes the data easy to move, back up and later host.
  - The vector chunks stay as files; there are only a few hundred.

## Viewer

### Right pane

- **Place filter:** two buttons at the top of the pane, **Visible area | Box**.
  - **Visible area (default).** The list shows the features whose best-V
    point is inside the current map view, refreshed on pan and zoom
    (debounced). Selecting an item pans the map and opens the panorama; the
    selected row stays pinned at the top until another selection, so the
    list does not jump out from under the click.
  - **Box.** Press *Draw box*, then drag a rectangle on the map. Dragging
    again replaces it, and *Clear* returns to Visible area. The box stays
    drawn and is remembered in the browser, and the map can be panned freely
    without changing the list.
- **Search box:** filters by name as you type, character by character.
  - It matches anywhere in the name, ignoring case and accents.
  - While it holds text, it searches the whole region rather than the place
    filter, because what people search for is usually not on screen.
    Clearing it returns to the place filter.
- **Everything combines** with the existing type buttons (trails, peaks,
  passes, lakes, camps) and the left pane's V slider.

### Scaling to ~100k+ scored features

Alpine Lakes has 7,227 records in a 7.9 MB file. The region should have
between 80k and 150k, so roughly 100–150 MB.

- **The server holds the scores** in memory, with a grid index and a name
  index.
  - `/api/feature_scores` takes a bbox or a search string, the types and a
    V threshold.
  - It returns the sorted top rows plus a total count.
  - A substring scan of 150k names takes a few milliseconds, fast enough to
    answer every keystroke.
- **The list is virtualized**, rendering only the rows on screen.
- **Hover cards, best-V dots and the map's V filter** fetch scores for what
  the map has loaded, through the same call.
- **The V slider's per-drag cost** (3–11 ms over 7k features today) has to be
  re-measured at region density. It already runs only over features in
  loaded tiles, which bounds it.

### Imagery layers

- **Aerial**: one combined JPEG layer. NAIP is used where it exists, with
  Sentinel-2 composited underneath per tile, so JPEG needs no transparency at
  the rectangle edges. It goes to z17 inside the rectangles and z14
  elsewhere, overzoomed above that.
- **Sentinel-2** is also kept as its own layer, to z14.

## Pipeline changes

1. **Data root (done) and region files.**
2. **`download.py`**:
   - OSM from the Geofabrik extract, filtered with osmium (or pyosmium) by
     the same tag rules as the Overpass queries.
   - DEM and WorldCover tiles for the far field.
   - GLO-30 for BC.
   - Overpass only for far-field peaks.
3. **`process.py` at region scale.** The far-field DEM is about 23k × 23k
   cells (2.1 GB as float32).
   - Steps that hold several full arrays need windowed processing; 64 GB of
     RAM is enough for a few arrays, not a dozen.
   - Derived layers that V doesn't use (openness, ruggedness, relief) are
     built for the core only, or dropped.
4. **V scorer.** The GPU holds the far-field DEM plus tree counts and
   classes, about 3 GB of the 4080 Super's 16 GB. `score_all.py` already
   checkpoints, and it runs over region chunks.
5. **`imagery.py`**:
   - the Sentinel-2 summer median composite;
   - NAIP per work tile inside the rectangles, then the VRT.
6. **`tiles.py`**:
   - writes MBTiles;
   - builds the combined Aerial layer from the VRTs (square blocks already
     keep memory bounded).
7. **`serve.py`**:
   - tiles from MBTiles;
   - the bbox and search scores API;
   - DEM, texture and panorama reads from the region files;
   - textures fall back to Sentinel-2 per pixel outside the rectangles.
8. **Retire Alpine Lakes** once `wa-west` is checked:
   - move its NAIP mosaic into the region's NAIP folder (it is part of the
     VRT);
   - delete the rest of its data and `data/aoi/alpine-lakes.json`;
   - point the viewer at `wa-west`.
9. **Docs**: README, data layout and data sources; retire the WA section of
   `aoi-manifest.md`.

## Build order

1. **Move the data and add the data-root setting.** Done; the viewer, scores
   and panorama were checked from `C:/data/vistarama`.
2. **Region polygon, rectangles, 30 m layers.** Check the coastline, sea
   handling and the BC seam.
3. **Score V for every feature in the region.** V doesn't need NAIP, so the
   whole region's rankings exist within the first few hours.
4. **Sanity check only.** Confirm nothing is broken: sea at height 0, no
   rays stopping at an edge, scores of the same order as Alpine Lakes. No
   tuning; calibration comes later.
5. **Viewer:** place filter, search, bbox scores API, virtualized list,
   MBTiles serving.
6. **Sentinel-2 composite and tiles.**
7. **NAIP work tiles** (the long pole, unattended), **then Aerial tiles.**
8. **Retire Alpine Lakes.**

## Estimates

Scaled from Alpine Lakes:

- a 5,030 km² core;
- 50 GB of NAIP streamed for a 7.7 GB mosaic in 67 minutes;
- 158k aerial tiles (2.7 GB) in 24 minutes;
- V for 7.2k features in about 4 minutes on the GPU.

| Item | Disk | Download | Time |
|---|---|---|---|
| 30 m DEM + land cover, reprojected and derived | ~10 GB | ~5 GB | ~1 h |
| Sentinel-2 summer composite (10 m near, 60 m far) | ~6 GB | ~30–60 GB (every clear date, not one) | 2–4 h |
| NAIP, five rectangles (≤ 74,500 km²; ~5,000 km² already built) | ≤ 115 GB | ≤ 690 GB | ≤ 16 h |
| Map tiles (Aerial to z17 in the rectangles, everything else) | ≤ 40 GB in MBTiles | — | ~6 h |
| OSM | ~2 GB | 0.3 GB | minutes |
| V scores (80k–150k features) | ~150 MB | — | 1–1.5 h |
| **Total** | **≤ 175 GB** on C:, plus ~7 GB of Alpine Lakes until retired | ≤ 760 GB | **about a day**, mostly unattended |

## Risks

- **The NAIP stream** depends on Planetary Computer's tokens and rate limits.
  The builder now survives both, and work tiles keep any loss small.
- **Colour seams between NAIP years.** These are accepted for the sake of
  quality; they are cosmetic.
- **The Sentinel-2 median** can carry faint cloud edges or late snow where
  few clear dates exist (the outer coast, high north-facing slopes). Check
  it on the map before tiling.
- **The lowlands have dense trail networks** (park paths), which will
  outnumber mountain trails. They score low, as they should, but they
  dominate the count and the scoring time.

## Open questions

None for now. Later:

- **Calibrating V against new terrain:** sea views, volcanoes 100+ km away,
  lowland hills.
- **Coast as a feature type.**
