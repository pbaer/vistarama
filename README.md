# Vistarama

Scores how epic the view is from a given point (a trail, a summit, a campsite,
a lakeshore) using open elevation, land-cover and imagery data. The data choices
are made for the USA and Europe; one region, the Alpine Lakes Wilderness in
Washington, is built so far.

## What it does

Most of what makes scenery epic is geometry. From any given spot you can
measure how much of the earth's surface falls in your visual field, how much of
that field it fills, how far off it sits, and how broken the surrounding land
is. A DEM gives you all four, so the score is computed rather than rated. No
photographs or image models are involved.

| | state |
|---|---|
| Open-data pipeline (elevation, vectors, hydrography, land cover) | ✅ working |
| Satellite + aerial imagery acquisition (Sentinel-2, NAIP) | ✅ working |
| Terrain metrics: slope, ruggedness, relief, openness | ✅ working |
| Interactive map viewer with LOD vector + raster tiles | ✅ working |
| 3D "view from here" panorama, with peak identification | ✅ working |
| Topographic prominence, joined from the Kirmse dataset | ✅ working |
| **View score `V`** (the solid-angle integral), at the selected viewpoint | ✅ working |
| Combining `V` with ruggedness `R` into one score | 🔜 designed, not built |
| Canopy occlusion in `V` (trees block sightlines) | ✅ working |
| Built-up gating of candidate points | 🔜 layer exists, gate not applied |
| Scoring every trail, peak and pass, ranked in the viewer | ✅ working (one AOI, GPU) |
| More AOIs: Alps, Scandinavia, Pyrenees | 🔜 sources chosen, nothing pulled |

The viewer came first because terrain data is hard to trust sight-unseen:
nearly every bug found during the build showed up when someone looked at a place
they knew and saw it come out wrong. Aggregate statistics hid all of them.

`V` is computed at any viewpoint, with a panel showing every term that went
into it, and for every trail, peak and pass in the AOI, ranked in a sortable
list beside the map (see [View score](#view-score)). It is uncalibrated: the
next step is a reference set of known-epic and known-dull places to tune it
against.

Everything runs on your own machine. There is no server and no cloud storage;
the data is rebuilt from public sources locally.

## Quick start

Only one AOI is set up so far: **Alpine Lakes Wilderness**, central WA Cascades.
Expect the first build to pull ~11 GB and take a couple of hours, most of it
NAIP aerial imagery.

```bash
python -m pip install rasterio numpy scipy pyproj shapely requests pillow
python scripts/download.py alpine-lakes
python scripts/imagery.py --slug alpine-lakes    # satellite + aerial (optional)
python scripts/prominence.py                     # peak prominence (optional)
python scripts/process.py alpine-lakes
python scripts/score_all.py alpine-lakes         # V for every trail/peak/pass (optional)
python scripts/serve.py
```

`score_all.py` runs on the GPU if CuPy is installed
(`python -m pip install "cupy-cuda12x[ctk]"`, NVIDIA only): about 1.5 minutes
for this AOI on an RTX 4080 SUPER. Without it, it falls back to every CPU
core, which takes hours.

Then open <http://127.0.0.1:8765/>.

`prominence.py` needs one file fetched by hand first, because it sits behind a
Google Drive confirm page: `AllPeaksProminenceP100Sorted.zip` (112 MB) from
<http://www.andrewkirmse.com/prominence>, saved unchanged as
`data/raw/prominence/kirmse_prominence.raw`. Without it the panorama still
works, but every summit falls back to the dataset's 100 ft floor and far fewer
get labels.

## Documentation

| Doc | What's in it |
|---|---|
| [docs/data-sources.md](docs/data-sources.md) | Survey of free/permissive data for elevation, vectors, hydrography, land cover across USA + Europe, with licences and disk costs |
| [docs/scenery-scoring-data.md](docs/scenery-scoring-data.md) | What the scoring model consumes, the canopy/built-up gate, coverage verification |
| [docs/visibility-metric.md](docs/visibility-metric.md) | The scoring model: view score `V`, ruggedness `R`, and how they combine |
| [docs/aoi-manifest.md](docs/aoi-manifest.md) | Target regions and the concrete download list |
| [docs/terrain-metric-review.md](docs/terrain-metric-review.md) | How our terrain metrics compare to the published literature, with the standard measures tested on our own DEM |
| [data/README.md](data/README.md) | Storage layout and CRS policy |

## The model, in brief

A point's score combines two terms:

**View score `V`**, a solid-angle integral over the visible panorama:

```
V = Σ  ΔΩ · c(class) · w(d) · d^γ
```

computed by marching the DEM outward along 720 azimuths, tracking the running
maximum elevation angle. `γ ≈ 1` blends "how much of my visual field is filled"
against "how much of the world can I see"; it is the main tuning knob.

**Ruggedness `R`**, variety in the local slope vector, in degrees: the RMS
deviation of a plane-fitted gradient over a 240 m baseline, taken within a
600 m window and averaged over a 5 km biweight kernel. Zero on a uniform slope
of any steepness. It is smooth, cheap and position-insensitive, which stabilises
the fragile view term.

This is equivalent to the **spherical standard deviation of surface normals**
of [Lindsay, Newman & Francioni 2019](https://doi.org/10.3390/geosciences9070322)
(WhiteboxTools `SphericalStdDevOfNormals`), computed from a least-squares plane
gradient rather than their Gaussian pre-blur. Measured correlation is 0.998 at
our output scale.

Combined with a power mean, `S = (a·V^p + (1−a)·R^p)^(1/p)`, starting
conjunctive (`p → 0`).

Points in built-up areas are to be **gated out** rather than scored low, on
the grounds that they cannot be epic by construction. Canopy, which the doc
first planned as a gate too, is instead modelled inside `V` as occlusion (see
[View score](#view-score)): it is per ray, so a lakeshore keeps its view over
the water with the forest at its back, which a per-point gate cannot do.

## "View from here" — interactive 3D

Click anywhere on the map (Alt-click opens a feature's tags instead). A pane
opens at the bottom with a WebGL rendering of the surrounding terrain.

- **Drag** to look around, **scroll** to zoom (fov 8° to 100°).
- **Eye** slider: 2 m to 2 km above the ground. **Range**: 10 to 517 km,
  default 120; 517 km is the farthest one 15,000 ft summit can see another
  over the curve of the earth. **Haze**: from "nothing past 1 km" to none at
  all, defaulting to a clear day for the viewpoint's altitude.
- **Satellite / land cover** switches the surface between imagery and
  WorldCover classes.
- **Arrow keys or WASD** nudge the camera sideways without refetching
  anything, keeping eye height above the ground (Shift for 100 m/s instead of
  25). Past 300 m a warning says the foreground is no longer finely meshed;
  `0` recentres. To the map, the camera's position is the viewpoint, as if
  you had clicked there: the marker and view wedge follow you as you walk,
  the cursor card measures distance and bearing from where you stand, and a
  range change rebuilds the panorama there. A click's leader line to the
  summit it snapped to shows only while you are back on that summit. Peak
  labels are re-tested for occlusion from where you stand once you stop
  (a quarter-second after the last step), and terrain occludes correctly at
  any distance from the centre (see the logarithmic depth note under traps).
- **Snap up** jumps to the nearest higher summit (see below).
- Hovering the panorama puts a red dot on the map, hovering the map puts one
  on the panorama, and the map's cursor card gives distance and bearing from
  the viewpoint. Drag the pane's top edge to resize it.

On the map itself, hovering a trail, road, lake, peak or other feature
highlights it and shows a card with its name, type and the handful of tags
worth knowing (difficulty and surface for trails, elevation for peaks, and so
on). The card does not follow the cursor. For a line it is placed once, beside
the middle of the hovered segment's visible part, at the nearest spot that
keeps a 14 px margin from the segment, stays off the best-V dots and the
panels floating over the map, and fits inside the map. It tries to clear the
rest of the trail too, but not at the cost of sitting much further away. A
peak or pass gets the same search around its marker; lakes and other areas,
or a line with no free spot, fall back to following the cursor.

A trail that OSM splits into several ways is highlighted whole, all of it
drawn the same, and the card gives both lengths in one row: this segment of
the whole trail, with its segment count. Once a trail has a view score,
hovering it also marks its best point, the sample with the highest V, with a
white dot labelled with just that V. A multi-segment trail gets one such dot
at every segment's best point, the trail's best among them; where two labels
would collide the lower one moves to another side of its dot, or is left off. The card gives the trail's average V and,
on a multi-segment trail, the hovered segment's. Per-segment
stats are stored with each trail's score; for scores computed before they
existed, `featurescore.add_way_stats()` derives them from the stored samples
without rescoring, since the sampling is deterministic and says exactly which
samples came from which way.
The grouping and lengths are written into the tiles by `process.py`, using
the same same-name-connected rule the trail score uses. The "At cursor" card in the corner sticks to the raster
values under the pointer.

This shares the geometry the view score `V` needs, so it doubles as a visual
check on that machinery before the metric is wired up.

- The server sends a **polar heightfield** centred on the viewer, with a
  **per-ring azimuth count** rather than one count for the whole mesh. A
  constant count gives constant *angular* resolution, which suits a perspective
  view in the far field; near the observer it invents detail the DEM does not
  have. Counts are powers of two (required, so consecutive rings stitch
  cleanly) holding ~15 m of arc near in and capping at 2048.
- **Ring placement is adaptive**, driven by measured silhouette error rather
  than a fixed count or a fixed law. For a candidate span, the real DEM heights
  inside it are compared against the chord the renderer will draw;
  rings are inserted until the largest gap subtends less than `SAG_TOL_DEG`
  (0.04°, sub-pixel). Ground that is smooth or hidden behind nearer terrain
  gets long steps; ridge crests get short ones. ~1.7M vertices at 120 km, with
  0.22 px mean / 0.77 px p95 error against a 30 m reference.
- Heights and shading are **bilinearly interpolated** between DEM cell centres.
  Nearest-cell sampling is what makes a rendered heightfield look like
  Minecraft: dozens of near-field vertices land in one 30 m cell, take
  identical heights, and produce flat-topped blocks. Duplicate-height fraction
  on the inner mesh dropped from ~90% to 0.06%.
- Ray endpoints are computed **geodetically on WGS84** and only then projected.
  Stepping in EPSG:5070 directly would bend azimuths by the grid convergence.
- **Curvature and refraction are baked into the vertex heights** as
  `z − (1−k)d²/2R`, k = 0.13. The client then uses an ordinary flat-earth
  perspective camera and still gets elevation angles right, with no curvature
  maths in the shader.
- Past the data edge, heights **carry the last valid elevation outward** along
  each ray. Filling with a constant instead puts a surface at fixed *absolute*
  elevation out there; curvature then drops it to just below the viewer's
  horizon and it renders as a flat-topped mesa hanging at eye level. Those
  vertices are also flagged so they never occlude, and tinted toward haze.
- A dashed **0° line** marks true horizontal. The visible horizon sits below it
  by the curvature drop, so the gap between the two is itself informative.
- The mesh is **viewer-independent**, so changing eye height only moves the
  camera, with no refetch. Peak visibility is recomputed in the browser against
  the same height grid, so labels appear as you rise above obstructions.
- The surface is imagery by default, sampled per fragment from four
  observer-centred LOD squares (half-widths 2.5, 10, 30 km and the full range)
  rendered in an azimuthal-equidistant frame around the viewpoint. NAIP wins
  where it exists, Sentinel-2 fills the rest. In land-cover mode it is ESA
  WorldCover × hillshade per vertex. **Haze** is exponential extinction toward
  a pale horizon colour in the fragment shader.
- When you zoom in, the screen resolves more than those squares hold (at
  fov 8°, Cashmere from 9 km needs ~1.4 m/texel and the ladder had 14.6). Once
  the view settles, up to four **focus patches** of 2048² are fetched where
  they gain the most sharpness per screen pixel, from 2 m (the NAIP mosaic's
  own resolution) up. They cost the same VRAM the ladder used to spend on a
  zoomed-in 4096² near level, and fade out wherever they would be minified,
  since they have no mipmaps.
- Raw WebGL, no framework: `pano3d.js` is ~1,200 lines including the mat4
  helpers.

### Peak labels

Which summits deserve a label is a question about the mountains, not about the
view, so it is answered with **topographic prominence** (elevation minus the
highest col on the lowest path to any higher ground) from Kirmse &
de Ferranti's global dataset, joined to our OSM summits by `scripts/prominence.py`.

This replaced two heuristics that both failed on the same case. Ranking by
*standing height* (metres above the ridge in front) is view-dependent, and from
Mount Stuart it put Liberty Cap above Mount Rainier's own summit because the two
look over slightly different foreground. Suppressing sub-peaks by *geometry*
(anything within 15% of the observer distance of a higher summit) needed a
constant fitted by hand against Rainier and had no principle behind it
elsewhere. Prominence answers both: Rainier 4113 m, Liberty Cap 149 m.

Filters, in order:

1. **Angular clearance, scaled with range.** A summit must clear the ridge in
   front of it by a minimum *angle*, and because the number of summits in a
   distance band grows with its area, the requirement itself grows with range,
   doubling by 60 km.
2. **Prominence, as an angle.** A peak's prominence must subtend at least
   `MIN_PROM_DEG` (0.35°) from the eye: ~580 m at 95 km, ~61 m at 10 km. One
   test, no fitted radius.
3. **Same-name dedupe within 1.5 km**, for massifs carrying one name on several
   OSM nodes (Mount Daniel has two, 728 m apart). 1500 m sits in a clear gap:
   every same-name pair closer than 1.3 km is one massif, the next distinct
   pair is 2.4 km away.
4. **Coincident-summit merge.** Two *different* names can be one mountain: OSM
   has both "Mount Rainier" and "Columbia Crest" on the same summit, so they
   share a key saddle and report identical prominence. Equal prominence within
   600 m collapses to the nearer node.
5. **At most 16 standing labels**, in prominence order, for summits standing
   at least 40 m above the ridge in front of them. They sit in a band under
   the compass with a width-aware overlap test and a stem down to the summit.
   Everything surviving the filters gets a marker that names itself on hover.

### Performance

Measured at a 1958×861 canvas, warm server:

| | cost |
|---|---|
| pan / zoom | **~3 ms/frame**: ~1.5 ms GL, ~1.4 ms label overlay |
| mesh | ~1.7M vertices, ~3.7M triangles |
| mesh build (server) | ~1.8 s |
| imagery textures (4 LOD levels, fetched alongside the mesh) | ~5.1 s |
| click to finished panorama | ~5.4 s |
| view score `V` (server, alongside the imagery) | ~1.5 s |
| focus patches (4 × 2048², after the view settles) | ~1.8 s |

Geometry is cheap: a modern GPU draws 3.7M triangles in a couple of
milliseconds, so there is **no occlusion or frustum culling**. Adding it would
optimise the wrong thing.

The occlusion *test for peak labels* is the costly part: up to ~1000 summits,
each walking the ring table with `atan2`. It depends only on eye height, not
on view direction, so it is **cached and invalidated only when the eye height
changes**.

### Summit snapping

The viewpoint snaps to the highest **summit** within 150 m of the click, and
never downhill. A summit (`_is_summit` in `panorama.py`) is a cell with:

- **nothing higher within 90 m** (ties allowed, since a summit is often flat
  across a few 30 m cells), and
- a **≥ 1 m rise over the ring just outside that radius**, so sub-metre DEM
  ripple on flat ground does not read as a landform. Before this guard, a
  click on the Puget lowland snapped 70 m sideways for a gain of 0.3 m.

The highest cell in a search window is the wrong test. On a smooth planar
hillside that cell is just the most uphill one, so snapping to it would walk
the viewpoint up the slope for as long as the slope continues. Under the test
above, open slope always has higher ground within 90 m, so the click stays
where it is. The Traps section below has the earlier, adjacent-cell version
of this test, which rejected almost every real summit.

Distance is measured from the **clicked point**, not from its cell centre. A
click can sit half a cell from the centre, which let a "100 m" search reach
114 m.

On 600 random points in the core AOI: 3.7% snap, the largest move is 141 m,
the smallest rise 1.5 m, and every landing passes the summit test.

A **"Snap up" button** covers the rest: it searches out to 500 m and offers the
nearest summit that is **higher than where you are**, since a nearer but lower
one is never a better viewpoint. It greys out when there is no such summit in
range. The jump runs with the automatic snap disabled, so the viewpoint lands
where the button said rather than being re-snapped to some other nearby
summit.

The distance lives in its own fixed-width slot with tabular numerals, so the
button is the same width whether it reads "267 m" or "—" and the toolbar never
reflows as you move around.

The map also shows **where the panorama camera is looking**: a wedge from the
viewpoint spanning the horizontal field of view, with a dashed centre-bearing
line. It follows the camera 100 ms after the view stops changing rather than
on every frame, since redrawing the map's canvas is what made panning choppy
on a large map. The horizontal FOV is derived from the camera's vertical FOV
and the canvas aspect, so at a 50° vertical FOV on a wide pane the wedge spans
123°.

### Why snapping matters

Take the OSM node for Mount Stuart (`node/348974307`). The DEM cell under it
is 2842.5 m, and the cell 30 m to its west is 11.2 m higher: a 30 m DEM smooths
a sharp summit and shifts its apex, so a hand-placed summit node can land on
the flank. Standing there at 2 m, that bump hides much of the view. Snapping
moves 23.6 m to the apex and the number of marked peaks goes from **78 to 148**.

The eye-height slider is the other lever on the same problem. Peaks marked
in all directions from the unsnapped node, after the label filters, at 120 km
range:

| eye height (unsnapped) | peaks marked |
|---|---|
| 2 m | 78 |
| 10 m | 141 |
| 25 m | 148 |
| 200 m | 162 |

Worth carrying into the `V` metric: sampling trail vertices at 30 m will show
this same sensitivity, which is more evidence for keeping `R` as a stabiliser.

Checked from the snapped Stuart summit at 2 m. "Predicted" is computed with
`pyproj` from published USGS summit coordinates and elevations; "rendered" is
what the mesh and peak table report:

| target | predicted | rendered |
|---|---|---|
| Glacier Peak | az 347.5°, el **−0.00°**, 72.6 km | az 347.45°, el −0.001°, 72.55 km |
| Mount Daniel | az 295.7°, el −1.15°, 23.1 km | az 295.58°, el −1.147°, 23.25 km |
| Mount Rainier | az 223.6°, el +0.56°, 95.0 km | az 223.43°, el +0.557°, 94.87 km |

Glacier Peak sits on the horizon to within a thousandth of a degree, and only
does so with the curvature term in place.

## View score

`scripts/vscore.py` implements Part 1 of
[docs/visibility-metric.md](docs/visibility-metric.md) for a single point:

```
V = Σ ΔΩ · c(class) · w(d) · T · d^γ    (d in km, so V is in sr·km)
```

One march per azimuth, 720 of them, in 30 m steps out to 150 km or the DEM's
edge. Along each ray the running maximum elevation angle is the horizon so far.
A sample that rises above it uncovers the band between the old and new angle,
which has solid angle `Δα (sin φ_new − sin φ_old)` exactly. That band is
credited at the sample's distance, with the land-cover class found there.
Hidden ground never raises the maximum, so occlusion is free, and terrain
seen edge-on sweeps almost no angle, so foreshortening is too.

It uses the panorama's geometry throughout: the same DEM and bilinear
sampling, the same curvature and refraction term, rays laid out in the same
observer-centred azimuthal frame, and the snapped viewpoint the 3D view is
drawn from. Ground nearer than the cutoff still blocks the view; it just
earns nothing.

**Trees block the view.** The DEM is bare earth, so without help a viewpoint
in forest "sees" the walls above it straight through the trees: the first
version of V did exactly that, and put the best point of Snow Lakes Trail
under 89% canopy at the mouth of Snow Creek's canyon instead of in the
Enchantments. `T` puts the trees back as occluders. A sightline counts as
inside the canopy wherever it runs less than 30 m above ground of WorldCover's
tree class, within 600 m of the observer, and is attenuated as
`exp(−metres inside / 40 m)`. Looking up a wall from the forest floor leaves
the canopy after some tens of metres, so you get glimpses; looking across the
forest, you see nothing; across a lake or meadow the sightline is clear. It is
per ray: from Colchuck Lake's east shore, V keeps 81% of its no-tree value,
and 100 m back into the trees at the north end only 11%. The panel shows `T`
as the canopy factor, with V as it would be without trees.

| parameter | value | source |
|---|---|---|
| `γ` | 1.0 | the doc's starting value; tune within 0.8–1.3 |
| `w(d)` near cutoff | 300 m | the doc |
| `w(d)` haze taper | `exp(−d / 60 km)` | the doc; independent of the panorama's haze slider |
| eye height | 2 m | fixed, so scores compare between points; the panel adds V at the slider's height as a comparison |
| `c` snow and ice, bare / sparse, water | 1.0 | the doc's "high" group |
| `c` moss and lichen | 0.8 | "medium-high" |
| `c` grassland, shrubland, wetland | 0.6 | "medium" |
| `c` tree cover, cropland | 0.3 | "low" |
| `c` built-up | −0.5 | "negative" |
| `T` canopy height, mean free path, reach | 30 m, 40 m, 600 m | judgement: a Cascade conifer stand, and how far you see into one at eye level |
| rays × step | 720 × 30 m | by convergence, below |

Ray count barely matters. Against a 2880-ray, 15 m-step reference at four
test points, 360 rays land within 1.2% and 720 within 0.6%. Halving the step
moves V by a steady 0.4%; doubling it to 60 m costs up to 3.5%.

The panel opens beside the map whenever a panorama loads. Walking the camera
with WASD or the arrow keys rescores wherever it ends up: the panel dims as
soon as you move and recomputes 0.5 s after the last movement key is
released, with the distance moved in its header. Recent points are cached, so
`0` brings the original score straight back.

The score is always taken at a standing 2 m. When the eye slider is set
anywhere else, a second line under it gives V for the same spot at the slider's
height and the change against 2 m, to show how much a viewpoint gains from
height. It is fetched after the 2 m score lands, recomputed 0.5 s after the
slider stops, and never replaces the real score. From Colchuck Lake's east
shore, for example, 205 m of height more than triples V (0.62 → 2.06); from
Stuart's summit, where nothing nearby blocks the view, 200 m adds 35%.

The panel shows:

- **The terms, summarised:** visible solid angle, its median distance, `dᵞ`
  there, the mean `c`, the haze factor, and the same sum at `γ = 0`
  (dominance, sr) and `γ = 2` (extent, km²), the doc's two axes, and the
  canopy factor `T` with V as it would be without trees.
- **V and solid angle by distance band and by land-cover class.**
- **V by direction** in 10° columns, with the panorama camera's field of view
  shaded.
- **By-products** of the same march: sky fraction, horizon range, farthest
  visible terrain, steepest look-down, the visible elevation range, and where
  the DEM edge cuts the rays.
- **All the parameters.**

Bands the DEM edge cuts short are starred; their V is a lower bound. From the
middle of this AOI that only affects the 60–150 km bands, which carry ~6% of a
summit's score.

### Trails, peaks and passes

Hover a trail, peak or pass on the map and press **V** to score it. The
result goes into the feature's hover card and is stored, so it is shown again
on later hovers without recomputing, across reloads
(`scripts/featurescore.py`, `data/derived/<aoi>/feature_scores.json`).

- **Trails are scored as whole trails.** OSM splits a named trail into many
  ways (median way length here is 216 m; Snow Lakes Trail is 15 ways and
  19.7 km), so the hovered way alone is usually a fragment. The trail is
  every way with the same name connected to it through shared nodes, which
  keeps apart different trails that share a name. V is averaged over points
  every 50 m along it, at the middle of each 50 m piece, using the full raw
  geometry. The card shows the average, the range along the trail, and how
  many points and OSM ways it covers. Unnamed ways are scored on their own.
- **Peaks** are scored at their summit: the OSM node snapped to the highest
  summit within 150 m, as the panorama does. Mount Stuart's node sits 24 m
  down its flank; at the summit it scores 8.44.
- **Passes** are scored at the node. Snapping would climb out of the saddle.

Each point costs ~1.5 s on one CPU core. A pool of 8 worker processes brings
that to ~240 ms (more workers did not help: the march is limited by memory
bandwidth, not cores). On the GPU (`scripts/vscore_gpu.py`) it is ~1.4 ms:
one CUDA thread per ray walks the same march, and a second kernel computes
the ray geometry itself (Vincenty's direct geodesic, then the DEM's Albers
projection), because preparing it with pyproj on the CPU cost 25-30 ms a
point against 0.5 ms for the march. Checked against the CPU: ray positions
agree to 1.5 cm and V to 0.27% at worst (0.02% on average; the canopy test
is a threshold, and float32 tips the odd sample across it). The V key uses the GPU when there
is one, so even a long trail scores in about a second.

### Scoring everything, and the ranked list

`scripts/score_all.py` scores every peak and pass inside the core box and
every trail with a way inside it: 6,534 items, 107,624 trail samples at 50 m,
in about 2.5 minutes on the GPU. It skips anything already stored under the
current version, so it resumes after an interruption, and writes the store
and a status file after each chunk.

Cheaper strategies were considered and dropped: sampling trails coarsely
where V looks flat, or screening them first with a cheap estimate (or the
ruggedness and canopy layers) and scoring only the likely winners. On the
GPU the exhaustive run costs a couple of minutes, so they would save seconds
while risking exactly the scenic stretches a ranking exists to find. Without
a GPU (hours of CPU) they would be worth revisiting.

The **ranked list** on the right shows every scored trail, peak or pass, and
sorts by any column: V (the average along a trail), best V (its highest
sample), length, minimum and maximum elevation, and elevation gain.
Elevation is sampled from the DEM every 10 m; gain counts a rise only once
the profile has climbed 5 m from its last turning point, so DEM ripple adds
nothing, and takes each segment in its more uphill direction, since a
branching trail has no single direction. Peaks and passes show their OSM
elevation (the DEM at a summit is typically ~10 m low, up to ~25 m, as a
30 m grid rounds off sharp tops). Selecting a row zooms the map to the item, switches its layer on
if needed, and marks it. While a bulk job runs, the list shows its progress
and fills in as chunks land.

Trails default to 1 km and up. Averaged V favours the very short: a 10 m spur
on a summit or a 100 m bootpath averages higher than any real trail, and
without a floor those fill the top of the list. From 1 km up, the top of the
list is open summit and ridge routes: the Mount Daniel climbers' trail, Iron
Peak, Mount Hinman's west ridge, Little Annapurna. Sorting by best V instead
surfaces trails with one standout point and an ordinary average: Kaleetan
Peak Climbers Trail is 27th by average (0.47) but seventh by best V (2.80),
and Baring Mountain Trail goes from 34th to third (best 3.90).

Canopy occlusion moved this list a lot. Before it, Tumwater Mountain Trail, a
forested ridge above Leavenworth, was first by average (2.33); it is now 26th
(0.48). Snow Lakes Trail's best point moved from the canyon mouth (1.17 then,
0.35 now) to the Upper Enchantments at 2,276 m (0.92), and the forested
middle of the trail, km 3 to 13, scores close to nothing. What it did not
change: an enclosed basin still scores well below a summit. The Enchantments
and Colchuck Lake's shore reach 0.4–0.9, against 5–8 on the summits above
them, because at `γ = 1` a wall is worth roughly its visible height times
the angle it spans, so a summit looking down 2 km on every side outscores a
basin ringed by 400 m walls. Whether that should change (a lower `γ`, or a
term for the alpine setting you stand in) is for the reference set to decide.

**Stored scores are versioned.** Each carries `vscore.version_tag()`, a
hand-bumped `ALGORITHM_VERSION` plus a hash of every parameter that affects V
(γ, haze, cutoff, range, eye height, rays, step, class weights, canopy,
refraction),
and `featurescore.version()` adds the sampling parameters. The server ignores
any stored score whose tag differs, so changing a constant invalidates old
scores automatically; bump `ALGORITHM_VERSION` for changes to the method that
move no constant.

First readings, all at the snapped viewpoint:

| viewpoint | V | dominance | extent | median distance | V from ≤ 3 km | top class | canopy T |
|---|---|---|---|---|---|---|---|
| Mount Stuart summit | **7.97** | 1.59 sr | 133 km² | 3.6 km | 15% | tree cover 35% | 0.94 |
| Dragontail Peak summit | **4.81** | 1.26 sr | 89 km² | 2.5 km | 19% | bare / sparse 29% | 1.00 |
| Colchuck Lake east shore | **0.62** | 0.57 sr | 0.9 km² | 1.0 km | 95% | bare / sparse 67% | 0.81 |
| Icicle Creek valley floor | **0.14** | 0.02 sr | 3 km² | 1.0 km | 6% | tree cover 60% | 0.16 |
| Snoqualmie Pass | **0.15** | 0.09 sr | 1 km² | 1.5 km | 63% | tree cover 74% | 0.27 |

The last column is the canopy factor `T`. The order is the intuitive one. Two
things to note before reading the numbers as calibrated:

- **Forest seen from above still earns a lot.** At `c = 0.3`, tree cover is
  the largest single contributor from Stuart, because the valleys 3–10 km
  below it are forested and fill a lot of solid angle. That is the class
  weights working as specified (canopy occlusion is about trees between you
  and the view, not trees *in* it), but it is the first thing the reference
  set should test.
- **V is raw, in sr·km.** The doc normalises it to its median over a
  reference region before combining with `R`; there is no reference region
  yet.

## Layout

```
docs/       design notes and data research
scripts/
  download.py     DEM, land cover, glaciers, OSM themes
  imagery.py      Sentinel-2 and NAIP mosaics
  prominence.py   joins Kirmse prominence onto OSM summits
  process.py      mosaic, reproject, derive metrics, build tiles + manifest
  tiles.py        XYZ pyramid writers (PNG and JPEG)
  panorama.py     polar mesh, summit snapping, panorama textures
  vscore.py       the view score V at one point
  vscore_gpu.py   the same V on the GPU, for many points at once
  featurescore.py V for trails, peaks and passes, stored and versioned
  score_all.py    scores every trail, peak and pass in an AOI
  serve.py        local HTTP server and the /api endpoints
  aoi.py, colormaps.py
viewer/     Leaflet map + WebGL panorama (Leaflet vendored, works offline)
data/       not in git; see data/README.md
```

## Pipeline

```
raw (as published)       3DEP DEM, ESA WorldCover, OSM via Overpass,
                         Sentinel-2 (UTM), Kirmse prominence
   ↓ mosaic + warp       (NAIP is warped straight into interim/ at 2 m)
interim (EPSG:5070)      equal-area, so metres mean metres
   ↓ derive
derived (EPSG:5070)      hillshade, slope, ruggedness, relief, openness,
                         canopy/built-up density
   ↓ tile + simplify
web (EPSG:3857)          XYZ pyramids (PNG, JPEG for imagery) + zoom-tiered
                         GeoJSON + manifest
```

The panorama does not use `web/`: `serve.py` builds its mesh and textures per
request from `interim/` and `derived/`.

The equal-area intermediate matters: computing the ruggedness kernel in
lat/lon would vary its radius with latitude and fabricate a north–south
gradient. See [data/README.md](data/README.md).

## Validation baseline

Spot-checks of the derived layers against known ground truth in the AOI. These
double as a regression test: the pipeline should reproduce them exactly.

Two complementary terrain layers, answering different questions and kept side
by side:

- **Ruggedness**: slope-vector variety (240 m baseline, 600 m window, averaged
  over 5 km). *How broken is the ground.*
- **Relief**: elevation σ over a 10 km biweight kernel. *How much vertical
  range is nearby.*

**Units.** Ruggedness is an **angle**: the RMS deviation of the local slope
vector, i.e. "the slope here typically differs from the neighbourhood slope by
N degrees". A uniform slope of any steepness reads 0°. Relief is a **length**:
metres of elevation spread, not metres per anything. Point slope is included
below to show neither is a restatement of steepness.

| Location | rugged 5 km | rugged 1.5 km | relief | open + | open − | defect |
|---|---|---|---|---|---|---|---|
| Colchuck / Dragontail cirque | **21.4°** | 22.6° | 323 m | 72.1° | 77.3° | **30.7°** |
| Stuart Range | 20.6° | 22.1° | 320 m | 67.2° | 90.3° | 22.5° |
| Enchantments core | 19.4° | 16.6° | 430 m | 74.3° | 78.0° | 27.6° |
| Hillside N of Enchantments | 18.5° | **22.5°** | 518 m | 65.3° | 84.3° | 30.4° |
| Icicle Ck valley | 14.5° | 18.1° | 400 m | 72.7° | 91.7° | 15.7° |
| Lake Chelan wall | 14.1° | 13.1° | **493 m** | 69.9° | 82.5° | 27.7° |
| Entiat Mtns (no lake) | 13.1° | 10.3° | 333 m | 83.6° | 86.6° | 9.8° |
| Cle Elum valley | 2.8° | 2.9° | 156 m | 87.6° | 89.9° | 2.5° |
| Puget lowland (flat) | 2.1° | 2.0° | 58 m | **90.1°** | 88.1° | 1.8° |

Openness reads **90° on a plane of any slope**, which is why the Puget lowland
sits there and everything mountainous falls below it. The 1.5 km band diverges
most where the terrain is organised at a scale the 5 km kernel blurs over: the
hillside north of the Enchantments jumps 18.5° → 22.5°, while the Enchantments
basin drops 19.4° → 16.6°.

The Lake Chelan wall is the clearest demonstration that ruggedness is not
steepness: **39° of mean slope but only 15.0° of ruggedness**, because that
slope barely changes. The Stuart Range is the mirror image, 21° of slope but
22.0° of ruggedness, because the ground is constantly turning.

The pair separates cases neither can alone. The Lake Chelan trough is the
highest-relief place in the AOI and among the lowest-ruggedness, being a huge
smooth glacial groove. The Colchuck/Dragontail cirques are the reverse: the
most broken ground, on moderate relief because the whole complex is compact.

They are **not** orthogonal, though: measured `r = 0.815` across the AOI, so
they share ~67% of their variance. Complementary at the extremes, largely
redundant in the middle. Lindsay et al. 2019 make the same distinction and
argue the two *concepts* are orthogonal, elevation variability ("ruggedness")
versus surface texture ("complexity"); by their vocabulary our "Ruggedness" is
a complexity measure and our "Relief" is a ruggedness one.

**Permanent water is excluded from both kernels.** A lake surface is a large
dead-flat area pinned at the window's low end, which makes the elevation
distribution bimodal, and σ rewards bimodality independently of relief. Lake
Chelan was reading ~14% above what its relief warranted while lake-free valleys
of comparable depth were untouched. Water still participates in the local
gradient fit, since shoreline slopes are real; it just no longer contributes
its own flatness to the averages.

The check that this worked is that **relief now tracks true elevation range**
uniformly. The relief-to-range ratio across all the sites above falls in
0.221–0.277, with Lake Chelan at 0.276, inside the pack rather than the outlier
at 0.305 it was before. Its remaining lead is earned: 1959 m of range against
1289–1492 m for the valleys it was being compared with.

Reads that confirm the chain end to end:

- **Snoqualmie Pass elevation 923 m** against a surveyed 921 m, which validates
  the mosaic → reproject → sample path including the 4326→5070 transform.
- **Stuart and Daniel at canopy 6% / 0%**, both above treeline. That is what
  the gate needs to detect, and it falls out of the neighbourhood-fraction
  trick with no canopy dataset.
- **Cle Elum built-up 96%, wilderness 0%.** The 100 m radius at 10 m keeps the
  full dynamic range: Snoqualmie Pass reads 98% canopy right in the trees, a
  few hundred metres from 1% built-up on the I-90 corridor. An earlier 400 m
  neighbourhood smeared those to 74% and 17%. The gate has to resolve
  clearings and forest edges, so a wide kernel destroys the boundaries it
  exists to find.
- **Ruggedness ordering** puts the alpine cirques on top (Colchuck/Dragontail
  102, Colchuck basin 95, Enchantments 91, Stuart 91), then a smooth hillside
  88, Snoqualmie Pass 74, the Lake Chelan trough walls 60–61, and valleys and
  plains in single or low double digits.
- **The plain σ got this backwards.** Its top-ranked point in the whole
  Enchantments window was the *smooth hillside north of the basin* (514), with
  the Enchantments core near the bottom (294). It also put the Chelan trough
  walls at #3 and #4. σ rewards total relief spread, and a glacial trough wall
  is close to a tilted plane: the most relief spread with the least variety.
- **Two wrong turns before this landed:**
  1. σ of elevation → measures relief spread, favours smooth big slopes.
  2. RMS departure from a fitted plane → looked like the fix, but residual
     amplitude itself scales with steepness, so it came out **96% correlated
     with mean slope**, worse than what it replaced.

  What works is measuring variety in the *slope vector*: a hillside has a
  near-constant gradient however steep, a cirque swings from slab to face and
  back within a few hundred metres.
- **Judge it with the correlation conditioned on slope, not the raw one.**
  Overall r vs mean slope is 0.975, but that is dominated by the legitimate
  plains-vs-mountains contrast. Restricted to real terrain it falls to 0.769
  above 25° and **0.515 above 30°**, i.e. three quarters of the variance among
  steep ground is something other than steepness. The raw correlation is
  nearly useless as a diagnostic here.
- Every one of these failures was invisible in aggregate statistics. Each
  produced a plausible histogram and a plausible-looking map. They only
  surfaced when specific known places were ranked against one another.

## Notes on sources

- **RGI 7.0 glaciers need an Earthdata login**, so the pipeline falls back to
  OSM `natural=glacier`. The download script detects NSIDC's login page by
  checking the zip signature rather than trusting HTTP 200.
- **Overpass rejects the default `python-requests` user agent** with HTTP 406,
  and rate-limits with 429/504 under load. Both are handled in
  `scripts/download.py`.
- Vectors use a tighter bbox than rasters. The 60 km raster buffer exists for
  far-field visibility; pulling OSM over it drags in the whole Seattle metro.

## Traps

Each of these produced a plausible-looking but wrong result:

- **3DEP's nodata is `-999999`, which is greater than `-1e30`.** A generic
  "reject very negative values" guard passes it straight through, and the
  elevation colour ramp then spans −999999 to 2627 m. Test the declared nodata
  value explicitly, and never use `Resampling.average` to compute statistics
  over a raster with a nodata sentinel; it blends the sentinel into real cells.
- **Compass azimuth runs the other way from maths azimuth.** The classic
  aspect-based hillshade formula needs `360 − az + 90`; skipping that puts the
  sun in the opposite quadrant. Computing illumination as an explicit dot
  product of the surface normal with the sun vector, both in (east, north, up),
  sidesteps the conversion entirely. Note also that `np.gradient` returns the
  *row* derivative first, and rows increase southward.
- **`L.geoJSON`'s `style` option is applied to circleMarkers too.** It runs on
  any layer exposing `setStyle()`, including the ones `pointToLayer` just
  created, so a line style with `fill: false` silently makes every point layer
  invisible. Only pass `style` for line and polygon themes.
- **Categorical rasters need `Resampling.mode`, not `nearest`, when zooming
  out.** Nearest picks one arbitrary source pixel per output pixel and turns 11
  land-cover classes into speckle.
- **Thinning vectors by global top-N deletes whole regions.** Sorting all
  features and keeping the best N clusters the survivors wherever the ranking
  happens to favour; filtering by on-screen extent instead only removes what
  would render sub-pixel.
- **Not every OSM area is a way.** Anything with islands or a complex shoreline
  is a `type=multipolygon` *relation*, so a `way[...]`-only Overpass query
  returns some lakes and drops others: Colchuck Lake is `relation/17094764`.
  The Alpine Lakes Wilderness boundary was missing for the same reason. Area
  themes must query `relation` too, and the converter has to stitch member ways
  into closed rings (they arrive unordered, may need reversing, and attach to
  either end). Recovering these took water areas from 3023 → 3635 features and
  protected areas from 46 → 165.
- **Leaflet's default hit tolerance is 0**, so a 1 px line is hittable only
  within half its own width. The hover highlight and card depend on it; set
  `tolerance` on the renderer (`L.canvas({tolerance: 12})`).
- **A memo key has to cover every input, including the ones that rarely
  change.** The panorama caches its camera matrices by yaw, pitch, fov, eye
  height, camera offset and canvas size. The eye's altitude is the mesh's
  ground height plus eye height, and the mesh was not in the key, so picking
  a new viewpoint without touching the view reused the old matrices: after
  Stuart's summit the camera stayed at 2,972 m for every later point, 1.2 km
  in the air over the Icicle valley. The key now carries a counter bumped on
  every mesh load, which also catches the far plane after a range change.
- **A second Leaflet canvas renderer blocks the first one's hover.** Each
  canvas renderer is its own `<canvas>` element, and the browser sends the
  mouse to whichever is on top. The panorama's view wedge has its own canvas
  (so redrawing it does not repaint 100k roads), and in the shared overlay
  pane it sat above the feature canvas: from the moment a panorama opened,
  feature hover silently stopped. It now lives in its own pane with
  `pointer-events: none`, which is safe because nothing on it is interactive.
- **Don't `bringToFront` a hovered feature on a shared canvas.** It moves the
  layer to the end of the renderer's draw list for good, not just while
  hovered: a lake hovered once then covers every trail crossing it. The hover
  highlight changes stroke width and symbol size in place and restores them
  on mouseout.
- **`~transform * (x, y)` returns fractional cell coordinates with integers at
  cell *corners*, so the containing cell is `floor`, not `round`.** Using
  `round`/`np.rint` offsets every sample by half a cell (15 m at 30 m
  resolution) and does so inconsistently, since it lands on cell `c` or `c+1`
  depending on which half of the cell the point falls in. This was silently
  wrong in every ray-sampling and observer-lookup site until a randomised test
  found that round-tripping a snapped cell centre didn't return the same cell.
  `rasterio.DatasetReader.index()` uses floor; match it.
- **`bindPopup` calls `stopPropagation()`**, so `map.on('click')` never fires
  when the click lands on a feature. Any map-wide click tool built on the map's
  own click event will do nothing wherever a peak or trail happens to be under
  the cursor, and will look like it works because it works on empty ground. The
  viewpoint picker intercepts on `document` in the **capture** phase instead,
  which runs before every Leaflet handler.
- **The polar mesh only stitches if every ring's azimuth count divides the next
  one exactly, which forces the cap to be a power of two.** Rings are refined by
  doubling, so a cap of 720 produced a 512 → 720 step with ratio 1.40625; the
  client's stitcher assumed the ratio was 1 or 2, took the 2 branch, and emitted
  triangles joining unrelated azimuths, producing the fan of sheets radiating
  from the observer seen near Colchuck Peak. Fixed in three places:
  `panorama.py` rounds `n_az` down to a power of two, raises on any
  non-multiple transition, and `pano3d.js` fans `ratio + 1` triangles per inner
  edge for arbitrary integer ratios. Note that `serve.py`'s query default is a
  separate constant from `panorama.py`'s signature default: it was still 720
  after the fix, so requests rounded down to a 512 cap and the far field was
  half the intended angular resolution while looking entirely correct.
  Verify meshes by auditing the built index buffer: no triangle may span more
  than one ring, and none may span more azimuth than its own ring's step.
- **A panorama spanning centimetres to 84 km cannot use a depth buffer.** 24
  bits quantises depth as `z²/(znear·2²⁴)`, so a near plane close enough to
  avoid clipping the ground at your feet (5 cm) leaves ~4 km of quantisation at
  60 km and distant ridges z-fight. Pushing the near plane out to 2 m instead
  clipped away the near ground entirely: on a steep face you saw straight
  through the slope you were standing on and out the underside of the mountain.
  The first way out was to need no depth buffer at all. The mesh is a
  heightfield in polar coordinates centred on the camera, so horizontal radius
  is monotonic along every view ray, and emitting the index bands
  **outermost-first** is exact occlusion in a single draw call (GL rasterises
  primitives in buffer order). Measured 0% missing-terrain pixels across 15
  yaw/pitch cases versus 23–100% before, at 2.2 ms/frame.
  That only holds while the camera stays at the centre. Once WASD could walk
  it off, the ground around the centre was drawn last whatever its distance:
  118 m off Cashmere's summit, the summit painted itself over the slopes in
  front of the camera as folded sheets with sawtooth edges. The depth is now
  **logarithmic**, written per fragment as `log2(1 + w)` scaled to the far
  plane (`EXT_frag_depth`). Its precision is relative, ~1e-6 of the distance,
  so ~10 cm at 100 km with the near plane still at 5 cm, and it is exact
  wherever the camera stands. Buffer order stays as the fallback.
- **The near ground needs rings for coverage, not detail.** Going from the
  centre vertex straight to a ring at 25 m makes the ground underfoot sixteen
  25 m triangles radiating from one point; on a steep face the uphill one
  climbs past eye height within 3 m of the origin and straddles the camera. Now
  16 evenly spaced rings cover the first 25 m (1.6 m apart, the scale of the
  eye height rather than of the 30 m DEM) before geometric growth takes over.
  Cost is ~120 vertices and 0.14%/ring of far-field spacing.
- **A 1 m rise over the *adjacent cell* rejects most real summits.** At 30 m
  posting a summit is usually flat on top: Dragontail's summit cell is 2681.5 m
  with a 2681.2 m cell beside it. All eight neighbours were lower, but by
  0.24 m, so `MIN_LOCAL_RISE_M = 1.0` threw the peak out, and with it Stuart,
  Colchuck, Cashmere, Prusik, every summit tested. Noise rejection has to be
  measured at a scale where landform and noise diverge: `_is_summit`
  now asks for nothing higher within 90 m plus a 1 m rise over the ring beyond
  that. The anti-walk-uphill property survives (open slope has higher ground
  within 90 m), flat water still yields nothing, and summit density is a
  plausible 1 per 1.4 km².
- **A rejected summit leaves the viewpoint in a pit.** The observer's ground
  height is sampled *bilinearly* at the exact click, while the mesh around it
  interpolates toward the cell centres. Click a summit cell near its corner and
  you stand 4.7 m below your own peak, with the ground 20 m away 2.6 m above
  your eye: a 7° wall of near terrain right around the observer, above the
  horizon in every direction, occluding everything. From Dragontail that cut
  visible peaks from 86 to 18. The snap now compares against the bilinear
  height and includes the clicked cell as a candidate, so landing on a summit
  cell still moves to its centre.
- **Snapping must be idempotent, and floating point will not give you that for
  free.** Once on a cell centre the bilinear height equals that cell's value to
  within an ULP, which read as an endless series of 0.0 m "snaps"; the
  downhill guard needs a real epsilon (1 cm), not `<=`.
- **`at_max` should not disable "snap up".** Minor ridge bumps are
  summits too, so treating "standing on a local max" as "nothing better nearby"
  greyed the button out on points up to 146 m below a peak within range. The
  condition that matters is whether any candidate is *higher*.
- **Standing height elects the wrong summit of a massif.** From Mount Stuart,
  Liberty Cap stands 2779 m above the ridge in front of it and Mount Rainier's
  own 4392 m summit only 2629 m (they look over slightly different ground 95 km
  away), so ranking peaks by standing height labels the mountain "Liberty Cap".
- **A clustering radius is wrong somewhere, however you scale it.** The same
  3 km between two summits is a separate mountain at 10 km and an internal
  wrinkle at 95 km. Scaling the radius to 15% of the observer distance
  absorbed 18 named summits on Rainier's silhouette into one while leaving
  Dragontail's close neighbours alone, but the 0.15 was fitted to Rainier and
  meant nothing anywhere else. Both this and the standing-height ranking are
  gone: "is this its own mountain" is what prominence measures (see Peak
  labels).
- **Check that a sort key reaches the sort.** The labels were meant to go out
  in prominence order, but `visiblePeaks()` built its output objects without
  the `prom` field, so the comparator saw the same fallback value for every
  peak and the order stayed standing height. Nothing looked broken; 127 m
  sub-summits simply held label slots while Mount Index (921 m) went
  unlabelled.
- **Cloud cover says nothing about data coverage.** A Sentinel-2 granule
  clipped by the edge of the swath reports 92% nodata and 0.001% cloud at the
  same time, because cloud percentage is computed over the pixels that exist.
  Picking the lowest-cloud scene per MGRS tile therefore chose the *emptiest*
  granules and produced a mosaic only 67% covered. Compounding it, one tile can
  have several scenes on one date from adjacent orbits, each holding a
  different slice: keeping one per tile throws away the half the other was
  carrying. Take every scene on the date, order fullest-first, and let the
  merge composite them.
- **Judge coverage by unioning the STAC footprints, not by any percentage.**
  Those footprints are data outlines whose areas track
  `s2:nodata_pixel_percentage` to the decimal, so a shapely union answers "is
  any of the AOI missing" exactly, which no combination of per-scene numbers
  can once scenes overlap. 2023-08-15 came out 100.00% covered at 0.00% cloud.
- **`reproject` on a `rasterio.band` reads the source at full resolution**, so
  it never touches a COG's overviews no matter how coarse the destination
  grid. That made each 0.6 m NAIP quad take 150 s, about 15 hours for this
  AOI. Read with `out_shape` first (which *does* use the overview pyramid),
  then warp the small array: ~18 s per quad.
- **A blob host's per-connection rate says nothing about its bandwidth.** NAIP
  from `naipeuwest` measured 0.78 MB/s on one connection, 2.95 on four and 6.66
  on twelve. Fetch quads concurrently and serialise only the writes; a GDAL
  dataset must not be written from two threads at once.
- **`rasterio.open(path, "w")` produces a dataset you cannot read back.**
  Compositing overlapping quads needs read-modify-write, so it must be `"w+"`.
  With a per-item `try/except` this failed identically on every quad and still
  exited 0 with an empty mosaic. That handler now raises when *nothing*
  succeeded, and warns when more than 10% did not.
- **A fixed ring count makes mesh resolution depend on how far you are
  looking.** `n_r = 300` spread the same rings over 20 km or 120 km, so raising
  the range quietly blurred the near ground. Ring placement is now derived from
  a target *spacing*, and the ring count falls out of it.
- **Check both axes of a polar mesh before assuming it is the DEM's fault.**
  Measured, the mesh was 7.5 screen pixels between azimuths and 36 between
  rings (61 m against 295 m at 10 km). Terrain has ridges in every orientation,
  so an anisotropic mesh just blunts whichever ones run the wrong way; the
  radial axis was doing all the damage. Matching radial spacing to azimuthal
  fixed it, and the azimuthal cap (`n_az`) then pins both.
- **Do not estimate terrain curvature from a coarse probe; measure the sag.**
  The tidy version of adaptive refinement (estimate `|d2z/dr2|`, invert
  `s = sqrt(8*tol*r/|z''|)`) fails because a second difference over a 100 m
  probe cannot see structure finer than 200 m: every sharp ridge reads as
  smooth and the steps come out far too long. Measured against a 30 m
  reference it scored 4.1 px of p95 silhouette error, barely better than no
  adaptivity. Taking the real probe heights against the chord the renderer
  will draw, and refining until the largest gap is under tolerance,
  scores 0.77 px for a third fewer vertices than the uniform mesh.
- **Validate a mesh by its silhouette error against a dense reference, and
  compare over the same radial domain.** The first attempt at this scored all
  three candidate meshes identically to four decimals, which was the giveaway:
  the near-ramp rings at 1.5-25 m subtend enormous elevation angles that a
  reference starting at 25 m never sees, so the metric measured only that
  common artefact. Clipping both to r >= 200 m made the differences appear.
- **The panorama's texture must be cut in an observer-centred azimuthal
  projection, not in the metric CRS.** Mesh vertices come from
  `GEOD.fwd(lon, lat, azimuth, radius)`, so the mesh's +y is *true* north and
  its radius is geodesic distance. EPSG:5070 is a conic: its grid north drifts
  from true north away from the central meridian, and at -121 deg that is
  **15 deg**. Sampling a 5070-aligned square with the mesh's own coordinates
  therefore rotated the imagery about the observer, putting a feature 60 km out
  **15.6 km** from where it belonged: lakes on hillsides, ridges through
  valleys. `+proj=aeqd +lat_0 +lon_0` on the viewpoint preserves azimuth and
  distance from that centre, which is exactly the mesh's parameterisation;
  measured agreement with `GEOD.fwd` is **0.0 m** at every azimuth out to
  120 km, since PROJ's aeqd is ellipsoidal too. The same 15 deg is why
  `panorama.py` marches rays geodetically and projects afterwards. The bug was
  importing a flat-CRS assumption into the one place that had avoided it.
- **To test raster-on-mesh registration, correlate against a layer you already
  trust and then deliberately misalign it.** Land cover says where water is;
  satellite imagery says water is dark. Contrast between the two was **-19.5**
  before the fix (water pixels *brighter* than land, because the rotation put
  lakes on sunlit rock) and **+52.2** after. In the renderer, shifting the
  water mask 20 px drops contrast from 46.2 to 38.6 and 60 px drops it to
  15.4; a peak at zero offset is the proof, not the raw score. Beware the
  sky: it is blue, so a naive "blue means water" test flagged 17% of the frame.
  The sky shader is identical in both surface modes, so pixels that are
  bit-identical between them are exactly the non-terrain ones.
- **Do not hillshade satellite imagery; it already contains shading, from
  almost the opposite direction.** The Sentinel-2 scenes here were acquired
  with the sun at azimuth 155.7° and elevation 54.4° (`view:sun_azimuth` /
  `view:sun_elevation` in the STAC metadata, so these are measured values). The
  cartographic hillshade lights from 315°, the north-west convention that stops
  relief reading inverted. Those are **159° apart**, so multiplying one onto
  the other darkened sunlit slopes and brightened shadowed ones. Imagery is a
  photograph: the diffuse shading is already physically correct, and adding a
  second Lambertian term contributes no information even when the angles agree.
  Land cover is the opposite case, since flat class colours have no shading of
  their own and need it.
- **Test rendering holes by repainting the sky, not by colour-matching it.**
  The first version of the hole test counted blue-dominant pixels below the
  horizon and flagged Colchuck Lake as a 13% failure. Forcing the sky shader to
  magenta makes "no terrain was drawn here" unambiguous.
- **Don't place a detail texture by aiming at the screen centre.** The first
  focus patch went wherever the centre ray hit the ground. Looking at Cashmere
  Mountain from 9 km at fov 8°, that ray slipped past the summit and landed on
  a ridge 67 km away, so the patch went there and Cashmere stayed at
  14.6 m/texel. Sampling a grid across the screen and placing patches by total
  gain fixed it, and planning around the patches already loaded keeps a 1° pan
  from refetching them.

## What's not done

The viewer and its inputs are in good shape. The scoring itself is still
missing.

- **Only trails, peaks and passes are scored in bulk**, not arbitrary points
  across the map, and only inside the core box. A full raster of V (a score
  for every 30 m cell) would be ~170 million points: about two days on this
  GPU, so it would want the doc's `R` pre-filter and built-up gate.
- **`V` is uncalibrated.** There is no reference set of known-epic and
  known-dull points to fit `γ` and `c` against, and no median to normalise by.
  The doc ranks glaciers above snow and ice, but there is no glacier raster
  (RGI needs a login; OSM glaciers are vectors), so WorldCover's snow-and-ice
  class stands in for both.
- **`R` is computed but never combined.** Ruggedness ships as a raster layer;
  the power-mean combination `S = (a·V^p + (1−a)·R^p)^(1/p)` is designed only.
- **The built-up gate is not applied.** The density layer exists and renders,
  but nothing excludes urban candidates. (Canopy is handled inside `V` as
  occlusion instead of as a gate.)
- **Canopy is a class, not a height.** WorldCover says where trees are, not
  how tall or dense; every tree pixel gets the same 30 m canopy and 40 m mean
  free path. Sparse subalpine parkland and old-growth valley forest occlude
  alike, per pixel. A canopy height model (Meta/WRI CHM, 1 m) would do better
  and is listed in docs/scenery-scoring-data.md.
- **Only one AOI.** Alps, Scandinavia and Pyrenees are specified in
  [docs/aoi-manifest.md](docs/aoi-manifest.md) with sources chosen; nothing is
  downloaded. The imagery path carries over, since Sentinel-2 is global and its
  selection logic is AOI-agnostic. NAIP is US-only and simply absent, which the
  priority-ordered source list already handles.
- **NAIP has visible mixed-year seams.** 126 quads are from 2023 and 38 from
  2015–2019, and the tonal step between them follows quad boundaries. Whether
  2023 coverage exists for those quads in Planetary Computer's catalogue is
  unverified; if it does, it is a dedupe-key bug, and if not the options are
  histogram matching or falling back to Sentinel-2 there.
- **Panorama load is ~5.4 s warm**, dominated by the four imagery textures
  (~5.1 s). Profiling put ~55% of that in GDAL's `reproject` and ~40% in the
  windowed read. Two hypotheses were tested and both refuted: a process pool
  (GIL) gained almost nothing on 32 cores, and DEFLATE compression made it
  ~1.6× *slower*. Raising `WARP_THREADS` to 16 is the one thing that helped.
- **NAIP is stored at 2 m, not its native 0.6 m.** At the tightest zoom
  (fov 8°) the screen resolves ~1.4 m on terrain 9 km away, so close-up
  imagery is still a little soft. Keeping 0.6 m for this AOI would be ~63 GB
  of uncompressed RGB against 7.7 GB now; the alternative is fetching 0.6 m on
  demand for the focus patches only.
