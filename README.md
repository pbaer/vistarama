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
| Scoring every trail, peak, pass, lake and camp, ranked and filterable in the viewer | ✅ working (one AOI, GPU) |
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
Expect the first build to keep ~15 GB and take a couple of hours, most of it
NAIP aerial imagery (about 50 GB streamed for a 7.7 GB mosaic).

The data goes in `data/` unless you point it elsewhere. Put it on a fast
disk with room: the tiler and the panorama read it hard. Either set
`VISTARAMA_DATA`, or create an untracked `vistarama.local.json` at the repo
root such as `{"data_root": "C:/data/vistarama"}`. The AOI definitions in
`data/aoi/` stay in the repo either way; `serve.py` maps `/data/` onto the
data root.

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
(`python -m pip install "cupy-cuda12x[ctk]"`, NVIDIA only): about 5 minutes
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
V = Σ  ΔΩ · c(class) · w(d) · T · u(φ) · d^γ
```

computed by marching the DEM outward along 720 azimuths, tracking the running
maximum elevation angle. `γ` blends "how much of my visual field is filled"
against "how much of the world can I see" (0.5 as built), `T` lets trees
block the view, and `u(φ)` weights terrain rising above eye level.

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

Press **V** with the cursor anywhere over the map, or click a trail, peak,
pass, lake or camp. A click on empty map only clears the ranked
list's selection, and Alt-click opens a feature's tags. A pane opens at the
bottom with a WebGL rendering of the surrounding terrain, with the view score
for that spot in its lower-left corner: collapsed to the score, expanded for
the breakdown.

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
hovering it also marks its best point, the highest-V point found on the path
(see "Trails, peaks, passes, lakes and camps"), with a white dot labelled with just that V. A multi-segment trail gets one such dot
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
  the view settles, up to twelve **focus patches** of 2048² are fetched where
  they gain the most sharpness per screen pixel, from 0.6 m (the NAIP mosaic's
  own resolution) up. Twelve is what the sixteen texture units allow beside
  the four ladder levels; they are mipmapped, so a patch keeps serving the
  ground beyond the distance it was cut for.
- **Textures are tied to the point they were cut for.** The panorama records
  that point, and a mesh built for any other point never draws with them: it
  shows land-cover colours until its own imagery arrives, and fetches it
  itself if nothing else did. Without this, a load that skipped the imagery
  left the previous viewpoint's textures in place, 2 km out of register, and
  since the sharp near levels then covered the wrong ground the whole view
  looked smeared. A level that fails to load is retried, then dropped.
- **Every texture is anisotropically filtered** (16×). Trilinear filtering
  picks its mip level from the longer axis of a pixel's footprint, and terrain
  seen obliquely is stretched along the line of sight many times over, so it
  fell to a far blurrier level than the texture held.
- **How sharp is sharp enough** is measured, not guessed: `textureAudit()` in
  `pano3d.js` emulates, for a grid of screen pixels, the texel the shader
  actually samples (ladder level, mip level, patch) and compares it with what
  the screen can show there, the pixel's footprint across the line of sight
  but never finer than the best source (0.6 m NAIP in the core, 10 m
  Sentinel-2 outside). Over 48 views on a 2293×1175 canvas, two summits, a
  lakeshore and a valley at fov 55 to 8: with four unmipmapped patches and no
  anisotropic filtering 26% of pixels were more than 1.25× softer than they
  could be; anisotropic filtering alone brought that to 18%; twelve mipmapped
  patches to 3.8% (90th percentile 1.10×), by that audit. But the audit then
  assumed a patch was drawn only where it helped, and the shader drew a
  patch everywhere inside its square, so a coarse patch planned for the far
  field (56 m texels over 114 km) smeared the whole foreground in views that
  had one, unmeasured. Patches now fade out wherever they are no finer than
  the ladder beneath, and the audit models exactly that: over 24 views at the
  panorama's default size (1030×406), Stuart's summit and Colchuck's shore at
  fov 55 to 8, 1.9% of pixels are more than 1.25× soft, at worst 9% in one
  zoomed summit view, where twelve patches cannot cover every distance at
  full detail.
  Frame time stays ~3 ms; the patches cost ~270 MB of texture and a few
  seconds of streaming after a view settles.
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
V = Σ ΔΩ · c(class) · w(d) · T · u(φ) · d^γ    (d in km, so V is in sr·km^γ)
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
inside the canopy wherever it runs less than 30 m above forest, within 600 m
of the observer, and is attenuated as `exp(−metres inside / 40 m)`. How much
forest a 30 m cell holds is the share of its nine 10 m WorldCover pixels in
the tree class, and the path through it counts in that proportion. (Reading
one pixel per cell, as resampling to the DEM grid does, gave the same forest
on average but flipped cells between forest and open every 10 m; on
lakeshores with trees at their back the worst tenth of points swung 20%
over a 15 m step, 17% with the share.) Looking up a wall from the forest floor leaves
the canopy after some tens of metres, so you get glimpses; looking across the
forest, you see nothing; across a lake or meadow the sightline is clear. It is
per ray: from Colchuck Lake's east shore, V keeps 80% of its no-tree value,
and 100 m back into the trees at the north end only 11%. The panel shows `T`
as the canopy factor, with V as it would be without trees.

**Near terrain and terrain above you count for more.** The first versions
used the doc's `γ = 1` and a 300 m near cutoff, and every lake basin scored a
few percent of a summit. Two reasons, both general. At `γ = 1` a visible
wall is worth roughly its height times the angle it spans *whatever its
distance*, so a 300 m wall rising 45° over a lakeshore counted the same as a
300 m hill 10 km away rising under 2°. And the walls of a small cirque lake
rise from the shore, well inside 300 m, where V counted nothing. Now:

- **`γ = 0.5`**, so how large terrain looks counts, while distant ranges
  still matter. The doc's case against low `γ` was enclosure: a forest
  clearing ringed by trees would win. With a bare-earth DEM and trees as
  occluders that cannot happen.
- **Near cutoff 90 m**, three DEM cells: enough to skip the ground underfoot,
  not enough to throw away a cirque.
- **Looming, `u(φ)`**: terrain seen above eye level counts up to 3×, rising
  linearly from the horizontal to full weight at 20° (the band's mid angle).
  Ground below the horizontal keeps weight 1, so a summit is not penalised.
  The doc proposed this as a refinement.

These were checked against a reference set: points 12 m outside the
shorelines of 22 named lakes (each lake's best), six summits, and valley,
forest and canyon points, plus 240 random points nobody picked. The median
lake went from 5% of the median summit to 39%, valleys and forest stay under
10%, and the random points' top scorers are open alpine terrain (mean canopy
22% against 81% overall), the same kind of place as before. No place is
treated specially: the reference set only says whether a general rule works.

| parameter | value | source |
|---|---|---|
| `γ` | 0.5 | see above; the doc started at 1.0 |
| `w(d)` near cutoff | 90 m | three DEM cells; the doc had 300 m |
| `u(φ)` looming | ×(1 + 2·clip(φ / 20°, 0, 1)) | the doc's proposed refinement; gain and angle are judgement |
| `w(d)` haze taper | `exp(−d / 60 km)` | the doc; independent of the panorama's haze slider |
| eye height | 2 m | fixed, so scores compare between points; the panel adds V at the slider's height as a comparison. A raised "survey" eye for terrain occlusion was tested and not adopted, see below |
| `c` snow and ice, bare / sparse, water | 1.0 | the doc's "high" group |
| `c` moss and lichen | 0.8 | "medium-high" |
| `c` grassland, shrubland, wetland | 0.6 | "medium" |
| `c` tree cover, cropland | 0.3 | "low" |
| `c` built-up | −0.5 | "negative" |
| `T` canopy height, mean free path, reach | 30 m, 40 m, 600 m | judgement: a Cascade conifer stand, and how far you see into one at eye level |
| `T` forest per cell | share of the 3 × 3 10 m tree pixels | all of WorldCover's data, not one pixel per 30 m cell |
| rays × step | 720 × 30 m | by convergence, below |

**Eye height was tested, and 2 m stays.** The worry was that the 30 m DEM,
interpolated into large flat facets, hides more from a 2 m eye than real
ground would, making V jumpy. Terrain occlusion was computed from 2 to 8 m
(the canopy test kept at the standing eye, so raising it could not see over
trees) for 240 random points and 264 lakeshore points, each with six
neighbours 15 m away. The swing of V across those 15 m barely moved: median
7.1% at 2 m, 6.6% at 6 m (lakeshores 9.8% to 9.1%), while V rose 6-12%.
With trees made transparent the swing is only 2.7% at 2 m: most of it is the
canopy, not coarse terrain. Part of that was sampling, one 10 m pixel per
30 m cell, which the forest share above fixed (median 7.1% to 7.0%, the
worst tenth of lakeshores 20% to 17%); the rest is real, since 15 m at a
forest edge does change how much forest stands between you and the view.
The lift stays in the code (`vscore.TERRAIN_LIFT_M`) at 0.

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
shore, for example, 205 m of height more than doubles V (1.50 → 3.67); from
Stuart's summit, where nothing nearby blocks the view, 200 m adds 58%.

The panel shows:

- **The terms, summarised:** visible solid angle, its median distance, `dᵞ`
  there, the mean `c`, the haze, canopy and looming factors (each with V as it
  would be without it), and the same sum at `γ = 0`
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

### Trails, peaks, passes, lakes and camps

Hover a trail, peak, pass, lake or camp on the map and press **V** to score it. The
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
- **A trail's best point is searched for, not just sampled.** V can change a
  lot within 50 m (one step onto a shoulder opens a view), so around each
  way's two best samples the path itself is scored every 5 m out to 25 m
  either side, then every metre out to 4 m around the winner. The search
  stays on the trail's geometry. It found a better point than the best sample
  on 90% of trails: median +5%, a tenth of them +29% or more. The average
  still comes from the 50 m samples.
- **Peaks** are scored on their own summit: from the OSM node, climb the DEM
  cell by cell, always to the highest of the eight neighbours, until no
  neighbour is higher, and stand at that cell's centre, the top of the
  surface the score samples. Mount Stuart's node sits 24 m down its flank.
  This replaced "the highest summit within 150 m" (the panorama's click
  snap), which put 27 peaks on a different, higher summit nearby: Guye Peak
  North and Middle both landed on one point. If the climb runs past 150 m
  (44 peaks, mostly pinnacles the 30 m DEM cannot resolve, on slopes rising
  into another mountain) the node is scored where it is: slightly low rather
  than the wrong mountain.
- **Passes** are scored at the node. Snapping would climb out of the saddle.
- **Camps** (campsites, huts and trail shelters) are scored at their point.
  Every camp is a point: a campsite mapped as an area is collapsed to its
  geometric centre when the data is downloaded (`download.camp_points`). Bus,
  picnic and gazebo shelters are left out (`featurescore.poi_kind`). The POI
  download no longer keeps springs, drinking water or OSM viewpoints:
  finding the viewpoints is what V is for.
- **Lakes** are scored along their shoreline, the way a trail is along its
  path: a point every 50 m around each outer ring (islands are left out),
  averaged, and the same along-the-path search for the best point, wrapping
  around the closed ring. Standing on the shoreline itself, the view over the
  water is open and the forest behind is occluded as usual. Only closed,
  standing fresh water of at least 0.5 ha counts: OSM `natural=water` with
  `water` lake, pond, reservoir or untagged, and not salt or tidal. That
  leaves out the sea (OSM's `natural=coastline`), bays, rivers, stormwater
  basins and the like, and ponds; 0.5 ha (about 80 m across) still keeps the
  small named alpine lakes (Divide Lake, Little Caroline Lake, Upper Florence
  Lake are all just under 1 ha). The OSM download returns whole features, so a
  lake crossing its edge is still complete (Lake Chelan reaches Stehekin).
  `process.py` marks qualifying lakes in the tiles (`lake`, `area`), so the
  viewer offers V only on those. The card shows the lake's area and its
  average V along the shore; its best point is marked on hover like a
  trail's.

Each point costs ~1.5 s on one CPU core. A pool of 8 worker processes brings
that to ~240 ms (more workers did not help: the march is limited by memory
bandwidth, not cores). On the GPU (`scripts/vscore_gpu.py`) it is ~1.4 ms:
one CUDA thread per ray walks the same march, and a second kernel computes
the ray geometry itself (Vincenty's direct geodesic, then the DEM's Albers
projection), because preparing it with pyproj on the CPU cost 25-30 ms a
point against 0.5 ms for the march. Checked against the CPU: ray positions
agree to 1.5 cm and V to 0.19% at worst (0.03% on average; the canopy test
is a threshold, and float32 tips the odd sample across it). The V key uses the GPU when there
is one, so even a long trail scores in about a second.

### Scoring everything, and the ranked list

`scripts/score_all.py` scores every peak, pass and camp inside the core box,
every lake touching it and every trail with a way inside it: 7,226 items (496
lakes, 196 camps), 120,886 samples at 50 m plus the best-point search, in
about 4 minutes on the GPU. It skips anything already stored under the
current version, so it resumes after an interruption, and writes the store
and a status file after each chunk.

Cheaper strategies were considered and dropped: sampling trails coarsely
where V looks flat, or screening them first with a cheap estimate (or the
ruggedness and canopy layers) and scoring only the likely winners. On the
GPU the exhaustive run costs a couple of minutes, so they would save seconds
while risking exactly the scenic stretches a ranking exists to find. Without
a GPU (hours of CPU) they would be worth revisiting.

The **ranked list** on the right shows every scored trail, peak, pass, lake
or camp, and sorts by any column: V (the average along a trail or shoreline),
best V (its highest point), length (a lake's shoreline), minimum and maximum
elevation, and elevation gain; the Lakes tab shows each lake's area instead
of gain, and its surface elevation as Max.
Elevation is sampled from the DEM every 10 m; gain counts a rise only once
the profile has climbed 5 m from its last turning point, so DEM ripple adds
nothing, and takes each segment in its more uphill direction, since a
branching trail has no single direction. Peaks and passes show their OSM
elevation (the DEM at a summit is typically ~10 m low, up to ~25 m, as a
30 m grid rounds off sharp tops).

What the list covers is either the **visible area** of the map (the default)
or a **box** dragged out on the map, and a **search** box filters by name as
you type, across the whole region. The server answers one page at a time
(`scripts/scoreindex.py`), since a region has far more items than a browser
should download. A selection lasts only while the item passes the list's
filters: redrawing the box without it, switching type, a search that misses
it, or panning it out of the visible area drops it, as does clicking its row
again or clicking empty map. Selecting a row switches the item's layer on if
needed and marks it (a lake gets an outline); the map moves to it only when
that cannot change the list (in Box mode, or with a search), and never closer
than zoom 13 unless a long trail needs it further out. It also opens the panorama
at the item's best point (a trail's or lake's highest-V point, a peak's
scored summit, a pass's node), unsnapped, so the panel's V is the stored
best, and turns the camera to face the most V: the heading whose horizontal
field of view holds the largest share of the point's V by direction, pitched
half way up to the skyline there, so a wall over a lake is framed and a
summit's panorama sits on the horizon. The heading is found when the item is
scored (`vscore.best_heading`, from the per-ray V and skyline of the GPU
march, over an 80° window) and stored with it, so the camera and the map's
view wedge face it the moment the row is selected rather than turning once
the panorama has loaded. While a bulk job runs, the
list shows its progress and fills in as chunks land.

The lake ranking is led by the high cirque lakes under the glaciated peaks
around Mount Daniel and Mount Hinman: Venus Lake (1.92 along the shore, best
2.63), Hinman Lake, Chain Lakes, Jack Lake, Iceberg Lake, Chair Peak Lake.
Colchuck Lake averages 1.14 but is seventh by best point (1.94, its south end
under Dragontail). Forested lakes such as Mig and Hope Lake near Stevens Pass
come last.

**Filter by view score** (left pane) hides every scorable item below a
threshold, on the map and in the ranked list, while the slider is dragged. A
toggle picks what is compared: the **average** V of a trail or shoreline, or
its **best** point; a peak, pass or camp has one V either way. The slider is
quadratic, so most of its travel covers the low values where most trails sit.
A scorable item without a stored score (a lake outside the core box nobody
has pressed V on) hides too once the threshold is above zero, since it cannot
be shown to pass; features that are never scored (roads, rivers, ponds, water
sources) are left alone. Hidden features stay on the map unstroked and stop
taking the mouse, so each drag step only restyles the features whose state
flips: 3-11 ms with ~9,000 on screen.

Trails default to 1 km and up. Averaged V favours the very short: a 10 m spur
on a summit or a 100 m bootpath averages higher than any real trail, and
without a floor those fill the top of the list. From 1 km up, the top of the
list mixes summit routes and lake basins: Aasgard Pass Trail (Colchuck Lake
up into the Upper Enchantments), Venus Lake, Dragontail, Little Annapurna,
Circle Lake, the Mount Daniel climbers' trail, Chain Lakes. Sorting by best V
instead surfaces trails with one standout point and an ordinary average:
Colchuck Lake Trail is 30th by average (0.41) but fourth by best V (1.91, at
the lake), and Baring Mountain Trail goes from 59th to tenth (best 1.64).

Two rule changes moved this list a lot. Canopy occlusion took Tumwater
Mountain Trail, a forested ridge above Leavenworth, from first by average to
105th, and moved Snow Lakes Trail's best point from under 89% canopy at the
mouth of Snow Creek's canyon into the Upper Enchantments. Then `γ = 0.5`, the
90 m cutoff and looming lifted basins against summits: Snow Lakes Trail went
from 114th to 24th by average, its Enchantments section scoring 1.0–1.3
against 0.1–0.8 in the canyon, while summits still lead (Mount Stuart 3.20).

**Stored scores are versioned.** Each carries `vscore.version_tag()`, a
hand-bumped `ALGORITHM_VERSION` plus a hash of every parameter that affects V
(γ, haze, cutoff, range, eye height, rays, step, class weights, canopy,
looming, refraction),
and `featurescore.version()` adds the sampling parameters. The server ignores
any stored score whose tag differs, so changing a constant invalidates old
scores automatically; bump `ALGORITHM_VERSION` for changes to the method that
move no constant.

First readings, all at the snapped viewpoint:

| viewpoint | V | dominance | extent | median distance | V from ≤ 3 km | top class | canopy T | looming u |
|---|---|---|---|---|---|---|---|---|
| Mount Stuart summit | **3.20** | 1.90 sr | 132 km² | 3.2 km | 33% | bare / sparse 39% | 0.90 | 1.00 |
| Dragontail Peak summit | **2.05** | 1.32 sr | 90 km² | 2.0 km | 43% | bare / sparse 42% | 1.00 | 1.00 |
| Colchuck Lake east shore | **1.50** | 1.78 sr | 2 km² | 0.8 km | 98% | bare / sparse 65% | 0.80 | 2.19 |
| Icicle Creek valley floor | **0.05** | 0.05 sr | 3 km² | 0.1 km | 42% | tree cover 72% | 0.12 | 1.10 |
| Snoqualmie Pass | **0.15** | 0.14 sr | 1 km² | 0.5 km | 85% | tree cover 60% | 0.20 | 1.64 |

The order is the intuitive one. Two things to note before reading the numbers
as calibrated:

- **Forest seen from above still earns something.** At `c = 0.3`, the
  forested valleys 3–10 km below a summit fill a lot of solid angle. That is
  the class weights working as specified (canopy occlusion is about trees
  between you and the view, not trees *in* it).
- **V is raw, in sr·km^γ.** The doc normalises it to its median over a
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
data/       AOI definitions (tracked); the built data lives here too unless
            vistarama.local.json moves it (see data/README.md)
```

## Pipeline

```
raw (as published)       3DEP DEM, ESA WorldCover, OSM via Overpass,
                         Sentinel-2 (UTM), Kirmse prominence
   ↓ mosaic + warp       (NAIP is warped straight into interim/ at 0.6 m, JPEG)
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

Vectors are cut into zoom tiers (`process.VECTOR_LOD`): low up to z9, mid to
z11, high to z13, and full beyond for roads and trails. Each coarser tier keeps
only what still reads at its scale, since performance matters more than detail
when zoomed out:

- **Roads:** not drawn at all below z9, and thinner and half transparent at
  z9–11, so they orient without hiding the terrain. Low keeps motorway, trunk
  and primary; mid adds secondary, tertiary and the motorway links; high adds
  unclassified (most forest roads); only full draws residential streets.
- **Rivers:** at low only actual rivers, at mid rivers and canals.
- **Trails:** at high, named trails plus unnamed paths over ~120 m.
- **Merging:** at low and mid, road ways sharing class, route number and name
  are joined into continuous lines (OSM splits a highway at every bridge).
- **Tags:** cut to the ones the viewer reads, through high for roads and
  rivers.

A large tier is split into a grid sized per tier in degrees, so a view fetches
a few nearby cells. It stays one file when gridding would not shrink what a
view fetches, as with national-forest polygons, which land in every cell they
touch.

Over western Washington this took the zoomed-out map from 41k road ways to
9.4k features in all (3.1 MB), and a view of Seattle at z13 from 53 MB to
5.6 MB.

The sea is a raster layer (`derived/sea.tif`, tiled to z14), since OSM maps it
only as coastline ways: WorldCover's water class wherever the DEM is within
2 m of sea level. Lakes sit higher (Lake Washington ~6 m) and keep their own
polygons; both are drawn in the same blue.

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
- **NAIP at 0.6 m costs disk only if stored uncompressed.** The quads are
  4-band (RGB + near-infrared) DEFLATE, which barely compresses photographs: a
  2023 quad is 458 MB against 442 MB raw, and the near-infrared comes along
  whether or not it is wanted. The mosaic is where the size is won: JPEG in
  YCbCr at quality 88 is ~8× smaller with no visible artefacts: 7.7 GB for
  the core at 0.6 m with overviews, the same as the old uncompressed 2 m
  mosaic and against ~63 GB uncompressed. A compressed TIFF must have each block written once, though, so
  `imagery.py` builds in 2048-row strips top to bottom (every quad crossing a
  strip read for just that band, merged, written), fetches only inside the
  core's lon/lat box (the mosaic's slanted corners would be a third of the
  transfer), and resumes from the last strip written. About 50 GB streamed.
- **Throughput is connections in flight.** One connection to the NAIP blobs
  gives 1-2 MB/s. Fetching each quad's share of a strip as 2048-column
  pieces, 48 at once, built the core in about an hour (67 minutes with
  the overviews).
- **Trust a SAS token's stated expiry, and refresh it once.** The tokens say
  they last 45 minutes; the code assumed 50, so for five minutes in every
  fifty every read failed with HTTP 403. Each of the 48 failing workers then
  forced a refresh, and the token service answered the flood with HTTP 429
  for minutes. Now the stated expiry (less five minutes) is used, a worker
  reports which token failed and only that one is replaced, and a 429 is
  waited out with the lock held so nobody else asks meanwhile.
- **A window that misses the image is not a failed read.** Footprints from a
  quad's lon/lat bbox are a little larger than its image, so some pieces
  miss it; rasterio raises on an empty intersection, and the retry loop took
  that for a network error and retried it with fresh tokens.
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
- **`V` is only loosely calibrated.** `γ`, the cutoff and the looming weight
  were checked against a small reference set (lakeshores, summits, valleys,
  random points; see View score), not fitted to it, and there is no median
  to normalise by.
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
- **NAIP has mixed-year seams.** Each quad takes its finest flight, then a
  summer one, then the newest: 149 of the 187 are 2023, 23 are August 2019
  and 15 July 2021 (where 2023 was flown in October, with fresh snow), all at
  0.6 m. The tonal step between years follows quad boundaries; histogram
  matching would hide it.
- **Panorama load is ~5.4 s warm**, dominated by the four imagery textures
  (~5.1 s). Profiling put ~55% of that in GDAL's `reproject` and ~40% in the
  windowed read. Two hypotheses were tested and both refuted: a process pool
  (GIL) gained almost nothing on 32 cores, and DEFLATE compression made it
  ~1.6× *slower*. Raising `WARP_THREADS` to 16 is the one thing that helped.
