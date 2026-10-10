# AOI manifest — target regions and what to actually download

Scope narrowed to four regions. This is the concrete acquisition list.

**Status:** only a subset of the WA region has been built so far, the Alpine
Lakes Wilderness (`data/aoi/alpine-lakes.json`), with its own 60 km far-field
buffer. Everything else below is the plan, not downloaded data.

| AOI | Core bbox | With 60 km far-field buffer |
|---|---|---|
| **WA — Cascades + Olympics** | 45.5–49.0°N, 124.8–120.0°W | 45.0–49.6°N, 125.5–119.3°W |
| **Alps** | 43.8–48.0°N, 5.0–16.0°E | 43.3–48.6°N, 4.2–16.8°E |
| **Pyrenees** | 42.2–43.3°N, 1.8°W–3.2°E | 41.6–43.9°N, 2.6°W–4.0°E |
| **Scandinavia** | 58.0–71.2°N, 4.5–31.0°E | as core (sea to the west) |

Combined land area ≈ 2.1M km².

---

## The counter-recommendation: don't upgrade resolution for scoring

Higher-resolution data is available for every one of these regions — WA DNR
lidar, swissALTI3D at 0.5 m, IGN LiDAR HD, Kartverket 1 m, PNOA. **Use none of
it for the score.** Two reasons:

**1. Cross-region comparability is the whole product.** The output is a ranking
— "the most epic places." If Switzerland is scored from a 10 m DEM and France
from 30 m, the ruggedness σ and the view integral differ systematically between
them, and Switzerland wins for a reason that has nothing to do with terrain.
Mixed resolution introduces a regional bias that looks exactly like a result.
A single resolution everywhere is worth more than local fidelity.

**2. The metric barely notices.** Both terms integrate over large angular and
spatial scales:
- Ruggedness σ at a 10 km kernel is dominated by relief at kilometre scale.
  Sub-100 m detail contributes almost nothing to the variance.
- The view integral sums solid angle in bands of several degrees. A 200 m cliff
  at 2 km subtends 5.7°; whether its edge is resolved to 30 m or 1 m changes
  the total solid angle by a fraction of a percent.

So: **1 arc-second (~30 m) everywhere, for every AOI.** The scoring DEM for all
four regions comes to roughly 6.5 GB.

Where higher resolution *does* pay is **rendering** — contours, hillshade,
relief shading. That pipeline has no comparability requirement, so use the best
local source per region freely. Keep the two pipelines separate; this is the
main reason to do so.

**Contingency:** if reference-point testing shows the near-field "looming wall"
term is under-weighted at 30 m, the fix is to test 10 m *uniformly across all
four AOIs*, not to upgrade one region. Cost of that upgrade is ~25 GB, so it
stays available.

---

## Europe: Sonny covers 100% of the targets

Checked against Sonny's country list:

| AOI | Countries needed | Sonny coverage |
|---|---|---|
| Alps | AT, CH, FR, IT, DE, SI | all six ✓ |
| Scandinavia | NO, SE, FI, DK, IS | all five ✓ |
| Pyrenees | FR, ES | both ✓ (see Andorra note) |

**Nothing gets dropped.** The countries Sonny lacks — Greece, Bulgaria, the
western Balkans, Ukraine, Turkey — are all outside the stated scope. The
tiering compromise offered isn't needed.

Practical note: **don't bother subsetting.** All of Europe at 1″ is 4.8 GB as a
single set of downloads. Cherry-picking countries costs more effort than the
disk it saves, and it leaves you re-downloading when the AOI shifts.

*Andorra spot-check:* Andorra isn't named in Sonny's country list but sits
inside tile N42E001, which both the Spain and France sets cover. Verify there's
no void there before trusting Pyrenees results — it's 468 km² in the middle of
the range.

---

## Washington: the far field matters more than usual

The Cascade volcanoes are visible at extreme range — Rainier, Baker, Glacier
Peak, Adams, St. Helens, and Hood are the dominant scenic features and are
routinely seen 100+ km away. Two consequences:

- **Hood is in Oregon** and is visible from much of southern WA. The far-field
  DEM must include OR.
- **The North Cascades continue into British Columbia**, within 60 km of Baker,
  Ross Lake, and the Pasayten. Needs Canadian coverage.

Sources:
- **3DEP 1 arc-second** for WA + OR + ID — public domain, seamless, ~1 GB
  compressed for the three states. Or just take CONUS (~15 GB) and stop
  thinking about it.
- **Copernicus GLO-30** for southern BC — ~0.5 GB for the border strip and
  Coast Mountains.

The DTM/DSM seam at the Canadian border falls in heavily forested terrain, so
the step is ~20–30 m. At 40 km that's 0.04° of angular error. Immaterial.

---

## Scandinavia: the projection issue is acute here

Norway spans 58°N to 71°N. At 71°N a 1″ cell is ~10 m wide and ~30 m tall — a
**3:1 anisotropy inside a single AOI**, and a 3× variation in cell width across
the AOI itself.

Reprojection to equal-area is not optional for this region: a fixed-cell-count
ruggedness kernel would mean a 10 km radius in Denmark and a 3.5 km radius in
Finnmark, which would fabricate a north-south ruggedness gradient out of
nothing.

Use **EPSG:3035** (ETRS89-LAEA) for all European AOIs and **EPSG:5070** (CONUS
Albers) for WA. Both are equal-area, which is what keeps the kernel honest and
the two regions comparable.

Also region-specific:
- **Fjords make the sea-as-zero handling load-bearing.** Half the ruggedness
  kernel at a fjord-side viewpoint is water. Use GLO-30's Water Body Mask and
  set ocean to z = 0 explicitly; nodata would poison the convolution along
  every fjord.
- Most of the Norwegian and Swedish fjäll is **above treeline**, so the canopy
  gate will pass nearly everything. Expect a much higher fraction of candidate
  points surviving to the view march here than in the Cascades.

---

## Alps and Pyrenees

Straightforward — Sonny 1″ covers both completely.

- **Glaciers matter most here.** RGI 7.0 for the Alps is dense and is probably
  the single highest-value class weight in the whole model for this region.
  The Pyrenees have only remnant glaciers (Aneto, Monte Perdido) — correct, and
  the data reflects it.
- **OSM trail data in the Alps is exceptional.** SAC/CAI/DAV networks are
  thoroughly mapped, `sac_scale` is widely applied, and hut coverage
  (`tourism=alpine_hut`) is near-complete. Best candidate-point source of any
  AOI.
- Sonny offers **0.5″ and 10 m products for Austria and Switzerland only**.
  Tempting, but taking them would break comparability with France and Italy
  inside the same AOI. Reserve them for rendering.

---

## Download manifest

| Item | Source | Size |
|---|---|---|
| Sonny 1″ DTM, all Europe | sonny.4lima.de (Google Drive) | **4.8 GB** |
| 3DEP 1 arc-second, WA/OR/ID | `s3://prd-tnm/StagedProducts/Elevation/` | ~1 GB |
| Copernicus GLO-30, southern BC | `s3://copernicus-dem-30m/` | ~0.5 GB |
| ESA WorldCover v200, AOI tiles | `s3://esa-worldcover/` | ~3 GB |
| RGI 7.0 glacier outlines | NSIDC | <1 GB |
| OSM extracts — WA, Alps, Nordics, FR/ES | download.geofabrik.de | ~8 GB raw, ~1 GB filtered |
| HydroLAKES | hydrosheds.org | 0.8 GB |

**Total ≈ 15 GB**, of which the scoring DEM is ~6.5 GB.

Imagery for the panorama comes on top of that (see
[data-sources.md](data-sources.md#e-true-colour-imagery)). For Alpine Lakes
alone it was ~0.8 GB of Sentinel-2 at 10 m and ~7.7 GB of NAIP at 2 m. NAIP is
US-only; Sentinel-2 covers every target.

Optional / later:
- gHM for the built-up gate — WorldCover's built-up class likely suffices, skip
  initially
- Kirmse prominence — **used directly** for panorama peak labels (see
  [data-sources.md](data-sources.md#topographic-prominence)). If its unstated
  licence ever matters, recompute from the same GLO-30/Sonny mosaic with
  `akirmse/mountains` instead
- WA DNR lidar, swissALTI3D, IGN LiDAR HD, Kartverket 1 m — **rendering only**

---

## Reference points for tuning

The four AOIs conveniently span every failure mode in the model:

| Failure mode | Candidate |
|---|---|
| Classic col — near walls + long sightlines | Col du Galibier; Besseggen |
| Cirque floor — high dominance, low extent | Cirque de Gavarnie |
| Summit above deep valley | Mt. Rainier's Camp Muir; Galdhøpiggen |
| Summit above flat ground | any Puget Sound lowland hill |
| Forest clearing — should be gated out | Olympic rainforest |
| Lakeshore | Ross Lake; Lac de Gaube |
| Ridge walk | Cascade Pass–Sahale Arm |
| Valley trail, occluded | Hoh River trail |

Gavarnie vs. Galibier is the sharpest test of `γ`: both are spectacular, but
one is enclosed and one is a pass. If `γ ≈ 1` ranks Galibier above Gavarnie,
the exponent is doing its job.
