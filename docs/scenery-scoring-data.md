# Data for "epic scenery" scoring — raster-only, gated approach

Companion to [data-sources.md](data-sources.md), which covers basemap layers.

**Design constraint (decided):** no photography, no visible satellite imagery,
no ML on images. Scoring is a crude geometric/land-cover metric. Urban and
heavily forested points are *excluded from scoring entirely* rather than scored
low, on the grounds that they cannot by definition be epic.

This constraint is doing a lot of work. It collapses the problem from
"predict human aesthetic judgement" to "measure how much interesting terrain
is visible," which is tractable with a handful of small rasters.

---

## What the constraint eliminates

| Dropped | Was for | Saving |
|---|---|---|
| Meta/WRI CHM 1 m canopy | occlusion modelling | 22.65 TB global |
| Building footprints (MS / Overture) for scoring | occlusion modelling | ~10–20 GB |
| Synthetic DSM construction | occlusion modelling | large compute |
| ScenicOrNot / Geograph | ML training labels | — |
| Flickr / YFCC100M / Mapillary | photo-density signals | — |
| Vision models entirely | scenicness regression | — |

**The DSM/DTM tension dissolves.** I previously argued viewshed needs a surface
model because canopy and buildings block sightlines. Under the gate, that
stops mattering: every point you actually score is by construction in open,
unbuilt terrain, so there is nothing at the observer to occlude. A bare-earth
DTM viewshed is now the *correct* model, not a compromise. One elevation
product, used for everything.

---

## What survives, in modified form

### Land cover moves from "occluder" to "quality weight on the view"

You still need land cover, but for a different job. "How much interesting
landscape can I see" requires defining *interesting*, and land cover plus
relief is the crude definition:

- A viewshed filled with uniform green canopy is not epic.
- The same viewshed area filled with rock, snow, glacier, and lake is.

So land cover is sampled **at the visible targets**, not at the observer.
The observer-side use is the gate; the target-side use is the score.

Rough class weights (ESA WorldCover legend):

| Class | Weight |
|---|---|
| Snow and ice | high |
| Bare / sparse vegetation (rock, scree, talus) | high |
| Permanent water bodies | high |
| Moss and lichen (tundra, above-treeline) | medium-high |
| Grassland, Shrubland (alpine meadow, moor) | medium |
| Tree cover | low |
| Cropland | low |
| Built-up | negative |

Glaciers get a boost from **RGI 7.0** rather than relying on the WorldCover
snow class, which conflates seasonal and permanent ice.

### Distance weighting: do NOT use visual magnitude

Worth flagging explicitly because it's the standard metric and it's wrong here.
"Visual magnitude" weights visible area by solid angle, i.e. ~1/d² — which
maximally weights the nearest terrain. That is the exact opposite of the stated
model, where near terrain is uninteresting.

Use a **band-pass distance weight** instead: suppressed below ~1–2 km, peaking
somewhere around 5–30 km, tapering out to the horizon. Tune from a handful of
known-epic and known-dull reference points.

### The exception to "near is boring": look-down

Close terrain at *your own elevation* is boring. Close terrain 800 m *below*
you is a cliff edge, and that is one of the most reliably epic situations
there is. So the near-field suppression should be a function of **depression
angle**, not raw distance — steeply negative angles keep their weight.

This falls out for free from a horizon profile (below), which stores elevation
angle per azimuth. Strongly negative horizon angle nearby = you are standing
on an edge.

---

## The gate

Two masks, both buildable from data already on the basemap list — no new
acquisitions.

**Canopy gate.** *As built, canopy is not a gate but occlusion inside `V`,
per ray; see "Canopy occlusion" in
[visibility-metric.md](visibility-metric.md). The density layer below is
still computed and shown.* ESA WorldCover's `Tree cover` class is binary at 10 m, but you
need *density* to threshold "heavily forested." Compute the tree-pixel fraction
over a neighbourhood (say 300–500 m radius) from the binary layer. That yields
a graded 0–1 canopy density from a free raster, with no separate canopy dataset.
Gate at whatever threshold matches "the trees are the view."

Optional upgrades if the neighbourhood trick proves too crude:
- **Copernicus HRL Tree Cover Density** — 10 m, true 0–100%, yearly, Europe only
- **NLCD Tree Canopy Cover** — 30 m percent canopy, USA only
- Both free; both introduce a US/Europe methodology split, which is why the
  neighbourhood-fraction approach is preferable for consistency.

**Built-up gate.** WorldCover `Built-up` class neighbourhood fraction, plus
**gHM** (Global Human Modification, 0–1, 300 m) as a broader development
signal. Also cheap and already on the list.

### Gate per point, not per region

Important design note: apply the gate at the individual point, not to whole
areas. A trail through dense Cascade forest that emerges onto a bald, or a
lakeshore clearing inside continuous woodland, should score high at those
points even though the surrounding region is heavily forested. Per-point
gating handles treeline traverses, clearings, meadows, and viewpoints
correctly and for free; per-region gating throws them away.

---

## Suggested computation: ray casting, not full viewsheds

Given a crude metric, a full viewshed per point is overkill. Cast N rays
(72 at 5° azimuth spacing is plenty) from each candidate point, march outward
sampling the DEM, and in one pass collect:

- **Horizon elevation angle** per azimuth — running max of the angle to each
  sampled cell. Gives sky openness, panorama width, and look-down detection.
- **Visible cells** — a cell is visible if its angle exceeds the running max.
- **Content at each visible cell** — land cover class, glacier flag, water
  flag, elevation, all sampled at the same time.

Cost is O(rays × range) per point instead of O(area), it's trivially
parallel, and it produces the score inputs and the visibility test in a single
traversal. Apply the earth-curvature and refraction correction (k≈0.13) while
marching — at 100 km, curvature alone drops the horizon ~650 m.

Store the horizon profile (72 × int16 = 144 bytes/point) rather than collapsing
to a scalar immediately. It preserves directionality, which matters because
the view is behind you on the way up an out-and-back.

Reference implementations if you'd rather not write the marcher: GRASS
`r.horizon` does exactly the horizon-profile computation; GRASS `r.viewshed`
and WhiteboxTools `VisibilityIndex` cover the full-viewshed path if you
decide you want it.

---

## Revised data requirements

Everything the scoring pipeline needs, and nothing more:

| Layer | Source | Res | License | Size |
|---|---|---|---|---|
| **Terrain, Europe** | Sonny's LiDAR DTM 1″ | ~20–30 m | CC BY 4.0 | **4.8 GB** |
| **Terrain, USA** | USGS 3DEP 1 arc-second | 30 m | Public domain | **~15 GB** |
| **Terrain, gap-fill** | Copernicus GLO-30 | 30 m | Free/open | ~2–5 GB |
| **Land cover** | ESA WorldCover v200 | 10 m | CC BY 4.0 | ~8–15 GB (1–2 GB if resampled to 30 m) |
| **Glaciers** | RGI 7.0 | vector | CC BY 4.0 | <1 GB |
| **Peaks** | Kirmse prominence (p100) | points | ⚠️ unstated — used with attribution | 112 MB |
| **Water** | OSM + HydroLAKES | vector | ODbL / CC BY 4.0 | ~1 GB |
| **Development** | gHM v3 | 300 m | Open (Zenodo) | ~2 GB |
| **Trails / POIs** | OSM (filtered) | vector | ODbL | ~2–5 GB |

**Total acquired: ~35–45 GB.** Derived horizon profiles add little — 10M
sampled points × 144 bytes ≈ 1.4 GB.

### 30 m is enough for scoring

The expensive elevation products in the basemap doc — 3DEP 1/3 arc-second at
~100–160 GB, 1 m at ~32 TB — are for **rendering contours and hillshade**, not
for scoring. Ruggedness, relief, horizon angles, and visible-area measures are
all stable at 30 m. Keep the two pipelines separate and the scoring stack stays
small enough to iterate on quickly.

Corollary: the far-field DEM problem mostly evaporates. Sonny covers all of
Western/Central/Northern Europe seamlessly at 1″, and 3DEP 1 arc-second covers
CONUS + Alaska — both are already continental, so they serve as their own far
field. GLO-30 is needed only for the Balkans, Greece, Turkey, and Eastern
Europe, where Sonny has no coverage.

---

## Coverage verification

Checked against what the model in [visibility-metric.md](visibility-metric.md)
actually consumes. Verdict: complete, with three gaps, all cheap to close.

### What the model consumes, exhaustively

| Model input | Source | Notes |
|---|---|---|
| `z` for ray march | DEM | needs ~60 km beyond every scored point |
| `z` for ruggedness | DEM | needs ~10 km beyond every scored point |
| `c(class)` at hit cell | WorldCover + RGI | same 60 km footprint as the march |
| canopy gate | WorldCover tree class | candidate points only |
| built-up gate | WorldCover built-up + gHM | candidate points only |
| candidate points | OSM | trails, summits, cols, campsites, lakeshores |
| validation | OSM `tourism=viewpoint`, US scenic designations | |

Everything else in the model — curvature, `γ`, `w(d)`, kernel shape — is a
constant, not data. **The entire scoring pipeline runs on DEM + WorldCover +
RGI + OSM.**

### Gap 1: DEM must extend past the study borders

This is the one that would actually break things. The march needs elevation
60 km out *in every direction*, which means across national borders.

- **USA**: Glacier NP, North Cascades, Boundary Waters, and Katahdin are all
  within 60 km of Canada; Big Bend and the Sky Islands within 60 km of Mexico.
  3DEP stops at the border. → **Copernicus GLO-30 for southern Canada and
  northern Mexico.**
- **Europe**: Sonny covers ~28 countries but **not** Greece, Bulgaria, Serbia,
  Bosnia, Albania, North Macedonia, Montenegro, Kosovo, Ukraine, Belarus,
  Moldova, or Turkey. That excludes genuinely epic hiking terrain — Pindus,
  Olympus, Rila, Pirin, Durmitor, the Accursed Mountains, the Ukrainian
  Carpathians. → **GLO-30 for the Balkans, Greece, Bulgaria, Ukraine, Turkey.**

Cost: ~7 GB. Verified that GLO-30 Public withholds only **two countries**, and
neither is in scope.

### Gap 2: mixing DTM and DSM across the seam

Sonny and 3DEP are bare-earth; GLO-30 is a surface model. At the coverage
boundary, forested terrain steps by roughly canopy height (~20–30 m).

Magnitude check — this is fine:
- **Ruggedness**: a 20 m step against a signal with σ in the hundreds of metres
  is sub-percent, and it only occurs at coverage boundaries.
- **View march**: 20 m at 20 km is 0.06° of angular error. The metric integrates
  bands of several degrees.

Worth logging where the seam falls, not worth correcting.

### Gap 3: ocean handling

Confirmed: **GLO-30 has no ocean tiles and the documented convention is to
assume z = 0**. That matches the sea-as-zero requirement for the ruggedness
convolution exactly. GLO-30 also ships a Water Body Mask auxiliary layer, which
is the cleanest way to distinguish ocean from inland water for class weighting.

### Not a data gap, but will bite: projection

Both metrics are defined in **metres**, and a lat/lon grid is not.

- Across Europe (36°N–71°N) a 1″ cell varies from ~26 m to ~10 m in width — a
  **2.5× swing**. A fixed-cell-count ruggedness kernel would mean a 10 km radius
  in Sicily and a 4 km radius in Tromsø.
- The ray march must step by **geodesic distance**, not by degrees.

Fix: reproject to equal-area before convolving. Standard choices —
**EPSG:3035** (ETRS89-LAEA) for Europe, **EPSG:5070** (CONUS Albers) for the
USA. Alaska needs its own (EPSG:3338).

Vertical datums also differ — Sonny is SRTM-convention geoid, GLO-30 is EGM2008,
3DEP is NAVD88 — but the offsets are 1–2 m, which is noise at this scale.

### Also verified

- **ESA WorldCover** is delivered as 2,651 tiles of 3°×3°, consistent with full
  global land coverage; Sentinel-2's acquisition envelope reaches ~84°N, which
  covers northern Scandinavia (71°N) and northern Alaska (71°N) comfortably.
  Spot-check Svalbard if it's in scope.
- **RGI 7.0** is global, so Alps, Pyrenees, Scandinavia, Iceland, Alaska,
  Rockies, Cascades, and Sierra glaciers are all present.

## Score components, concretely

All computable from the table above:

**Observer-side (gate + intrinsic)**
- Canopy density, built-up density, gHM — the gate
- Prominence of the point itself (is it a summit / ridge / col)
- Local relief and ruggedness (TRI/VRM at 500 m / 2 km / 10 km radii)
- Sky view factor, from the horizon profile

**View-side (the actual score)**
- Visible area, band-pass weighted by distance, depression-angle corrected
- Panorama width — azimuthal span with distant open horizon
- Look-down — minimum horizon angle and the drop it implies
- Visible relief — elevation spread across visible cells
- Visible prominent peaks — count and angular size, weighted by prominence
- Visible glacier / snow / rock / water area, class-weighted
- Visible tree-cover and built-up fraction, as penalties
- Maximum sightline distance

---

## Validation without imagery

You've dropped the ML-label sources, but two non-imagery label sets survive and
cost nothing, worth keeping as sanity checks even if you're hand-tuning weights:

- **OSM `tourism=viewpoint`** — hundreds of thousands of human-placed "there is
  a view here" markers, global, already in your OSM extract. Your score should
  be high at these and it's a free regression test.
- **US official scenic designations** — National Scenic Trails, Wild & Scenic
  Rivers, National Scenic Byways, National Natural Landmarks. Public domain,
  and literally a government body asserting "this is scenic."

Neither is training data under your constraint; both are cheap checks that
catch a badly-tuned weight before you notice it by eye.

---

## Remaining open questions

1. **Band-pass distance curve shape** — the main tuning knob. Pick reference
   points (a known-epic summit, a known-dull forest track) and fit to them.
2. **Directional vs scalar** — keep horizon profiles, collapse late.
3. **Sampling density along trails** — every 100 m? 250 m? Drives total compute
   linearly.
4. **Seasonality** — snow cover changes both the gate and the class weights.
   One score, or one per month? Deferred, but the data (MODIS snow cover,
   public domain) is cheap if you want it later.
5. **Kirmse's peak dataset has no stated license** — used with attribution for
   panorama peak selection. If that ever matters, recompute from GLO-30 with
   the open-source `akirmse/mountains` generator.
