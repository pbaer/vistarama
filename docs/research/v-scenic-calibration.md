# Calibrating V against how people judge scenery

A reference study for western Washington, October 2026. It compares the view
score `V` with how hikers and guidebooks rate the scenery of 80 hikes, climbs
and destinations. It proposes a five-category qualitative scale for `V`,
measures how well `V` ranks the places, and looks into where it disagrees with
people.

The data behind every number here is in
[v-scenic-calibration-data.csv](v-scenic-calibration-data.csv): one row per
place.

**Scores used:** region `wa-west`, score version `v3-79ea95f984-fb6c195` (the
`version` field of each record), read on 2026-10-18. Camp records were being
recomputed at the time, so no camp score is used.

---

## 1. Purpose

`V` is meant to be one fair rule computed from data: the same formula
everywhere, never adjusted by place. Two questions follow.

1. **Does it rank places the way people do?** If not, where and why? The
   reasons we look for are general properties of the algorithm or its inputs,
   never properties of a named place.
2. **What does a number mean?** "V = 0.9" means nothing to a hiker. Can the
   number be mapped onto words like *good* or *epic*?

The reference set is test feedback only. Nothing in `V` should ever depend on
these places. The hypotheses in section 7 are all general changes, each tested
across every place, and none is implemented here.

## 2. What V is, briefly

See [../visibility-metric.md](../visibility-metric.md) and the README's "View
score" section for the full definition. In short:

```
V = Σ ΔΩ · c(class) · w(d) · T · u(φ) · d^0.5      (sr·km^0.5)
```

The sum runs over visible terrain, using 720 rays and a 30 m DEM, from a 2 m
eye. The terms are:

- `ΔΩ`: solid angle.
- `c`: land-cover weight (rock, snow and water 1.0; meadow 0.6; trees 0.3).
- `w(d)`: a 90 m near cutoff and haze `exp(−d/60 km)`.
- `T`: canopy transmittance, with trees treated as 30 m occluders within 600 m.
- `u(φ)`: looming, up to ×3 for terrain above eye level.

Peaks and passes are scored at one point. Trails and lakeshores are sampled
every 50 m, which gives an average `V`, a `V_max` and the `best` point.

## 3. Method

### 3.1 Choosing places

Ninety-five candidates were researched, with the aim of covering:

- **The whole region:** North Cascades, Mt Baker, the Mountain Loop, Stevens
  Pass, Alpine Lakes and the Enchantments, the I-90 corridor and Issaquah Alps,
  Rainier, Goat Rocks, St Helens, Adams, the Columbia Gorge, the east slopes
  and Columbia Basin, the Olympics, the outer coast and Puget Sound.
- **The full range of quality:** famous epics, good local hikes, and forest
  "green tunnels" with no views.

Fifteen were then left out to stay within 80 and to balance the tiers. Most of
the dropped ones duplicated another place, had an ambiguous highlight, or were
closed and poorly mapped. Dropping some places rated 4 and 5 and keeping the
lower-tier forest walks gave more weight to the low end, which famous-hike
sources under-represent. The 80 are:

- 53 compared through a trail record, 14 through a lake, 13 through a summit.
- Human highlight tiers 1/2/3/4/5: 2 / 13 / 17 / 27 / 21.

### 3.2 Sources and ratings

The research was done on the web. The main sources were:

- Washington Trails Association (WTA) hike pages, with their star averages and
  vote counts, and WTA trip reports.
- Statewide and regional best-of lists: Seattle Met, Go Wander Wild's Top 50,
  Explore With Alec's 32 best, The Outbound's North Cascades top 10, Earth
  Trekkers, The Big Outside, Uprooted Traveler, Switchback Travel.
- The Mountaineers route pages, SummitPost, NPS and USFS pages, Hiking Project,
  Oregon Hikers, Hike of the Week, local press and blogs.

The search tools could not reach Reddit or AllTrails, so neither is
represented. Each place has two to five URLs in the CSV.

Each place got two ratings on the scale in section 4:

- **Highlight tier:** the best moment, such as the lake, the summit, or the
  ridge above the pass.
- **Whole-hike tier:** the experience end to end. A long forest walk to a
  stunning lake rates 4 or 5 for its highlight and 2 or 3 for the whole hike.

The tier rates the **visual scenery, chiefly the views**, as the sources
describe it. That is not the same as how much people love a place. The Hoh
rainforest, for example, is one of the most loved hikes in the state but has
almost no open views, so it is tier 2. Where sources disagreed, the CSV
justification says so.

### 3.3 Matching places to scores

Each place was matched to OSM-derived score records by location as well as
name. Many names repeat across the state: there are 27 "Blue Lake" records,
for example. The CSV keeps `all_records` for each place. Where the hike's
destination is a separate feature, its records include the trail(s) and that
feature (a lake shore, a summit node, a pass).

Some matching problems had to be handled:

- **Unnamed OSM features.** The Thornton Lakes and the trail ways in Gothic
  Basin have no name in OSM, so they were found by location.
- **Long trails pieced together by name.** These were *clipped*: only the
  samples within a stated radius of the hike section were used (the `clips`
  column). The trails clipped were the PCT (167 km and 104 km records), the
  Thunder Creek Trail (30 km, through to Park Creek Pass), the Hoh River Trail
  (26 km), the Packwood Lake Trail (which continues to Packwood Saddle), the
  Snow Lakes Trail (which runs through the Enchantments), and the Wonderland
  Trail.
- **Summit nodes that are not where hikers stand.** These were excluded. The
  Hidden Lake Peaks high point is 0.5 km from the lookout. The OSM "Sourdough
  Mountain" node is the true summit, 1.7 km from the lookout.

### 3.4 The comparator

- **Highlight comparator, `V_hl`:** the maximum, over the place's records, of
  `V_max` for trails and lakes and `V` for summits and passes. It is always
  taken at a point on a mapped feature, never at a hand-placed coordinate.
  Using a hand-placed point would let the comparator be tuned by hand.
- **Whole-hike comparator:** the average `V` along the place's main trail
  record, clipped where noted. 77 places have one.
- **Diagnostic only, `V_at_highlight_coord`:** `V` at the coordinate the
  sources give for the highlight. Many of these coordinates are estimates; the
  `highlight_coord` column says which.

Every comparator point was then scored again through `/api/vscore` to get its
full breakdown: canopy factor and V without canopy, haze, looming, distance
bands, land-cover shares and ground elevation. These come back within 0.002 of
the stored score.

## 4. The five-category scale

| Tier | Name | What people say | V at the highlight (`V_max`, or point V) | V averaged along a trail (whole hike) |
|---|---|---|---|---|
| 1 | **Enclosed** | No open view: forest, a creek, perhaps a waterfall. Reviews say "no views" or don't mention any. | **< 0.07** | **< 0.07** |
| 2 | **Modest** | One or two partial or framed views, or a forest lake or waterfall. Pleasant, but not a reason to go. | **0.07 – 0.2** | **0.07 – 0.2** |
| 3 | **Good** | A clear open view or an attractive lake or meadow, recommended locally, but the view is of foothills or lowland, or partly screened. Rarely on statewide best-of lists. | **0.2 – 0.6** | **0.2 – 0.5** |
| 4 | **Great** | Wide alpine views or a dramatic lake basin. Widely recommended and on regional best-of lists. | **0.6 – 1.4** | **0.5 – 1.0** |
| 5 | **Epic** | Among the most scenic in the state: close glaciated peaks, a sweeping alpine panorama, or a spectacular basin. Tops statewide lists. | **≥ 1.4** | **≥ 1.0** |

### How the cut points were derived

For the highlight scale, all sets of four cut points on a log grid from 0.03
to 3.5 were searched for the lowest mean absolute tier error. The best set,
with ties broken by exact agreement, was 0.064 / 0.21 / 0.58 / 1.34. These were
rounded to 0.07 / 0.2 / 0.6 / 1.4. Rounding costs almost nothing: mean absolute
error goes from 0.44 to 0.45.

How reliable each cut point is varies:

- **1 | 2 (0.07)** is the least certain. Only two reference places are tier 1.
- **2 | 3 (0.2)** and **3 | 4 (0.6)** are well supported.
- **4 | 5 (1.4)** sits where the two tiers overlap most.

The same search gave the whole-hike cuts: 0.074 / 0.19 / 0.48 / 0.99, rounded
to 0.07 / 0.2 / 0.5 / 1.0. The tier-5 whole-hike class has only three places,
so its cut point is a rough guide.

### V by human tier (highlight)

| Human tier | n | min | 25% | median | 75% | max |
|---|---|---|---|---|---|---|
| 1 Enclosed | 2 | 0.041 | – | 0.053 | – | 0.064 |
| 2 Modest | 13 | 0.065 | 0.15 | 0.20 | 0.40 | 0.56 |
| 3 Good | 17 | 0.16 | 0.37 | 0.57 | 0.87 | 1.74 |
| 4 Great | 27 | 0.21 | 0.59 | 0.77 | 1.22 | 2.32 |
| 5 Epic | 21 | 0.62 | 1.41 | 1.66 | 2.06 | 3.47 |

The medians rise steadily, about ×2.5 to ×3 per tier at the low end and ×1.4
to ×2 at the top. Tiers 3 and 4 overlap a lot; tier 5 is clearly separated
from tiers 1 to 3.

### What share of the region falls in each category

This uses all named features in `wa-west` with the highlight cut points.

| Records | Enclosed | Modest | Good | Great | Epic |
|---|---|---|---|---|---|
| Trails (17,308; `V_max`) | 59% | 20% | 14% | 5.4% | 1.2% |
| Lakes (2,616; shore `V_max`) | 36% | 25% | 23% | 12% | 3.9% |
| Peaks (3,069; summit `V`) | 22% | 16% | 21% | 20% | 21% |
| Passes (296) | 28% | 20% | 27% | 23% | 1.4% |

For whole-hike average `V` on trails: 75% Enclosed, 16% Modest, 6.4% Good,
2.1% Great, 0.5% Epic.

A fifth of named peaks land in Epic. Most of them are steep, glaciated North
Cascades and volcano-flank summits that few people visit. That is plausible,
but this study cannot check it (see section 8). It also means a summit's point
`V` and a trail's `V_max` are not quite the same quantity (see section 8).

## 5. Results

### 5.1 Rank correlation

| Comparison | n | Spearman ρ (95% bootstrap CI) | Kendall τ-b |
|---|---|---|---|
| Highlight tier vs `V_hl` | 80 | **0.755** (0.64 – 0.84) | **0.62** |
| Whole-hike tier vs trail-average V | 77 | **0.715** (0.58 – 0.82) | **0.57** |
| Highlight tier vs trail-average V | 77 | 0.666 | – |
| Whole-hike tier vs `V_hl` | 80 | 0.730 | – |

By comparator type, Spearman ρ is 0.81 for trails (n = 53), 0.64 for summits
(n = 13) and 0.55 for lakes (n = 14). The last two are small samples.

### 5.2 How well the scale separates the tiers

Highlight scale (0.07 / 0.2 / 0.6 / 1.4). Rows are the human tier; columns
are the tier `V` predicts.

| human \ V | Enclosed | Modest | Good | Great | Epic |
|---|---|---|---|---|---|
| 1 Enclosed | **2** | 0 | 0 | 0 | 0 |
| 2 Modest | 1 | **6** | 6 | 0 | 0 |
| 3 Good | 0 | 1 | **8** | 7 | 1 |
| 4 Great | 0 | 0 | 8 | **13** | 6 |
| 5 Epic | 0 | 0 | 0 | 5 | **16** |

- **On the reference set:** exact agreement 56%, within one tier 99%, mean
  absolute error 0.45 tiers.
- **Leave-one-out:** with the cut points refitted without each place in turn,
  exact agreement is 49%, within one tier 96%, mean absolute error 0.55.
- **Errors stay within one tier.** Only one place (Rainy Lake, Good to Epic)
  is off by two, and no place is off by three or more.

Whole-hike scale (0.07 / 0.2 / 0.5 / 1.0):

| human \ V avg | Enclosed | Modest | Good | Great | Epic |
|---|---|---|---|---|---|
| 1 | **7** | 8 | 1 | 0 | 0 |
| 2 | 4 | **13** | 2 | 0 | 0 |
| 3 | 0 | 10 | **10** | 4 | 0 |
| 4 | 0 | 3 | 4 | **7** | 1 |
| 5 | 0 | 0 | 0 | 2 | **1** |

Exact agreement 49%, within one tier 95%, mean absolute error 0.56.

### 5.3 What V captures and what it doesn't

Several single properties of the comparator point were checked against the
human tier: ground elevation, the share of `V` from rock and snow, and canopy
factor.

| Property at the comparator point | Spearman ρ with highlight tier | median by tier 1…5 |
|---|---|---|
| **V (as built)** | **0.755** | 0.05, 0.20, 0.57, 0.77, 1.66 |
| Ground elevation | 0.813 | 443, 471, 607, 1614, 1994 m |
| Snow + bare share of V | 0.806 | 0.01, 0.00, 0.01, 0.13, 0.60 |
| Canopy factor T | 0.759 | 0.12, 0.28, 0.65, 0.74, 0.96 |
| Tree share of V | −0.627 | 0.97, 0.93, 0.53, 0.38, 0.16 |
| Looming factor | **−0.436** | 1.63, 2.18, 1.12, 1.03, 1.13 |
| Haze factor | −0.334 | 0.93, 0.98, 0.93, 0.90, 0.92 |

**Elevation alone ranks this set at least as well as V.** That is partly a
property of the set: famous scenery in Washington is alpine. Still, it shows
where V is weak.

| Elevation band | n | ρ(tier, V) | ρ(tier, elevation) |
|---|---|---|---|
| < 1000 m | 32 | **0.66** | −0.11 |
| 1000 – 1600 m | 13 | 0.19 | 0.10 |
| > 1600 m | 35 | 0.49 | **0.62** |

- **V carries real information beyond elevation.** The partial rank
  correlation of tier with V, controlling for elevation, is 0.45.
- **In the lowlands and foothills V does the work.** Elevation tells you
  nothing there.
- **Among alpine places V separates Great from Epic less well than
  elevation.** Most of the hypotheses in section 7 concern errors that hit high
  places.

None of this argues for adding elevation to V. It is a confounded diagnostic,
not a view property.

### 5.4 Counterfactuals at the comparator points

The breakdown already gives V without canopy, without haze and without
looming. Class weights enter V linearly, so changing them at a fixed point is
exact. This is not a full rescore: the best point along a trail could move.
But it shows which way each term pushes.

| Variant (at the same points) | Spearman ρ | Δρ vs as built (95% CI) |
|---|---|---|
| As built | 0.755 | – |
| No canopy (T = 1) | 0.585 | −0.17 (−0.28 to −0.08) |
| No haze | 0.787 | +0.031 (+0.001 to +0.073) |
| No looming | 0.794 | +0.038 (−0.021 to +0.10) |
| No haze and no looming (approximate) | 0.807 | – |
| Snow and bare weight 1.5 | 0.781 | – |
| Snow weight 1.5 only | 0.769 | – |
| Tree weight 0.1 or 0.15 | 0.756 | – |
| Water weight 0.5 | 0.751 | – |
| Grass and shrub weight 0.8 | 0.742 | – |
| Best 90° window instead of the full panorama | 0.638 | – |
| Best 60° / 120° window | 0.631 / 0.655 | – |

What the table shows:

- **Canopy occlusion is essential.** Removing it costs more than any other
  change. Section 7 argues its data is wrong in one specific setting.
- **Haze and looming are the two terms that most look miscalibrated.**
- **A best-direction view scores worse than the full panorama.** People do
  value the whole panorama, so the hypothesis that hikers judge by the single
  best direction (the photo) is not supported.

## 6. Inversions, and what the data shows about each

Places are listed by how far `V_hl` sits from the median `V` of their human
tier. The figures are from the comparator point's breakdown (`/api/vscore`):
T is the canopy factor and V(noT) is V without trees. The full breakdowns are
in the CSV.

### V lower than people judge

1. **Heliotrope Ridge** (Great; V 0.21, Modest)
   - **What V does:** the OSM "Heliotrope Ridge Trail" ends at 1,510 m in
     forest, at 48.7955, −121.8685. Its best point has T 0.17 and V(noT) 1.23.
   - **Where the view is:** hikers continue a few hundred metres to a moraine
     facing the Coleman Glacier icefall. V at the estimated overlook (48.788,
     −121.870) is 1.33, Great. The adjoining "Coleman-Deming Low Route" record
     averages 1.41.
   - **Cause:** the trail ends where the OSM segment ends, not where hikers
     stop. This is a mapping and segmentation issue, not a flaw in V.
2. **Rialto Beach / Hole-in-the-Wall** (Great; 0.21) and **Cape Flattery**
   (Great; 0.36)
   - **What V sees:** 90–94% of V is sea surface, seen almost edge-on. The
     ground is 10–12 m and 80% of V lies within 1 km.
   - **What people see:** the appeal is sea stacks, an arch, surf, sea caves
     and 30–40 m cliffs, mostly below what a 30 m DEM resolves, plus things
     that are not terrain at all.
   - **Cause:** DEM resolution and flat water at grazing angles. Everything
     else is out of scope for V by design.
3. **Mount Adams summit** (Epic; 0.62) and **Mount Rainier, Columbia Crest**
   (Epic; 0.96)
   - **Rainier:** from a 2 m eye on a broad summit dome, the convex summit
     hides the mountain's own flanks. 23% of V comes from summit snow within
     300 m, nothing from 3–10 km, and the haze factor is 0.54.
   - **Adams:** no V at all from 1–10 km. The median distance of visible
     terrain is 30 km and the haze factor is 0.46.
   - **Moving off the summit point:** stepping 250 m toward the edge raises
     Adams to 1.43 but leaves Rainier at 0.95. Stuart, a sharp summit, scores
     lower anywhere off its top: its best point within 250 m is 2.01, against
     3.32 at the top.
   - **Cause:** the 60 km haze applied at 4 km altitude, and a convex summit
     seen from a single point.
4. **Spray Park** (Epic; 0.68)
   - **What V does:** canopy is not the issue (T 0.95). The scene is open
     meadow with Rainier's NW face 8–9 km away, rising about 15°. Visible
     terrain at 3–10 km supplies only 34% of V.
   - **Why it falls short:** a single huge glaciated mountain filling roughly
     0.1–0.2 sr is worth about 0.4 in V. A summit with 360° of terrain below
     it collects several times that.
   - **Cause:** how V weighs one dominant mid-distance mountain against
     all-round exposure. Wildflowers, the other half of the appeal, are out of
     scope.
5. **High Divide / Seven Lakes Basin** (Epic; 0.90)
   - **What V does:** Bogachiel Peak has T 0.64 and V(noT) 1.41; 43% of its V
     is tree-covered terrain. Olympus is about 12 km away across the Hoh valley.
   - **Cause:** the same distant-landmark issue as Spray Park, plus canopy
     classification on an open ridge.
6. **Rachel Lake and Rampart Lakes** (Great; 0.38)
   - **What V does:** the Rampart Lakes shore scores 0.21. 95% of Rachel
     Lake's V lies within 300 m.
   - **Why:** the Rampart plateau's charm is many small tarns, larches and
     granite knobs on a bench. These are small features with no big walls,
     largely below what a 30 m DEM and a terrain integral can express.
   - **Cause:** this is a limitation of what V measures more than an error.
7. **Mailbox Peak** (Great; 0.48), **Mount Ellinor** (Great; 0.59), **Mount
   Pilchuck** (0.66), **Tolmie Peak lookout** (0.37 at the lookout, 0.77 on its
   trail) and **Mount Storm King** (0.66)
   - **What V does:** these are open rocky summits and lookouts with 360°
     views, but T is only 0.28–0.40 at the summit. V(noT) is 0.84–1.84, Great
     or Epic. Mount Si (Good; 0.55) has the same pattern (T 0.40, V(noT) 1.38)
     but stays inside its tier.
   - **Why:** ESA WorldCover marks these summits as trees (table below). It
     classifies 10 m pixels, so a few scattered subalpine firs or the shadow of
     a rock face can make a pixel "tree". The canopy model then treats every
     tree pixel as a 30 m stand.
   - **Cause:** canopy input data on summits and ridgetops.

   | Point | Tree share of WorldCover within 30 m | within 100 m | within 300 m | T |
   |---|---|---|---|---|
   | Mailbox summit | 0.95 | 0.77 | 0.90 | 0.28 |
   | Mount Ellinor summit | 0.88 | 0.64 | 0.38 | 0.35 |
   | Tolmie lookout | 1.00 | 0.73 | 0.51 | 0.29 |
   | Mount Si summit | 0.88 | 0.83 | 0.96 | 0.40 |
   | Mount Pilchuck lookout | 0.72 | 0.62 | 0.44 | 0.36 |
   | *Controls:* Granite Mtn lookout | 0.00 | 0.29 | 0.32 | 0.76 |
   | Hurricane Hill | 0.07 | 0.13 | 0.23 | 0.75 |
   | Sahale Arm | 0.00 | 0.02 | 0.05 | 0.97 |

8. **Naches Peak Loop** (Great; 0.51)
   - **What V does:** the best point has T 0.71 and 41% tree share, and Rainier
     is about 25 km away.
   - **Mapping:** OSM maps the loop's eastern half as the PCT, so the "Naches
     Peak Loop Trail" record is only half the loop. The PCT samples on that
     half reach 0.62.
   - **Cause:** distant-landmark weighting, canopy, and trail segmentation
     together.

### V higher than people judge

9. **Rainy Lake** (Good; 1.74, Epic)
   - **What V does:** a cirque lake whose walls (Lyall Glacier, the Frisco
     cliffs) rise steeply over open water, so looming is 2.27. The land cover
     is mostly meadow and avalanche slope (49% grassland). It scores in the
     same range as Blue Lake (2.32) and Lake Ann (1.63).
   - **Why people rate it lower:** they call it a paved leg-stretcher, and the
     big views are up on Maple Pass.
   - **Cause:** partly a human-side bias. Easy, roadside and "lesser sibling"
     places get marked down. Partly the looming term (H2).
10. **Forest valley trails:** Middle Fork Snoqualmie (Modest; 0.56), Thunder
    Creek (Modest; 0.52), Iron Goat (Modest; 0.56)
    - **What V does:** each best point sits in a valley bottom or a small
      clearing, looking up at forested walls 1–3 km away. Looming is
      1.7–2.4, T is about 0.5, and 49–82% of V is tree cover.
    - **Cause:** looming triples terrain above eye level whatever covers it.
11. **Lakes and rivers in forest:** Lake Twenty-Two (Good; 1.06), Green Lake
    (Modest; 0.40), Talapus (Modest; 0.35), Lake Valhalla (Good; 0.64), and the
    river trail at Staircase Rapids (Modest; 0.21)
    - **What V does:** open water removes the canopy in front (T 0.4–0.9), and
      the basin walls rise above eye level (looming 2.1–2.6). 64–79% of V is
      tree-covered walls (Lake 22, Green Lake).
    - **Cause:** the same looming mechanism as the valley trails. A forested
      bowl scores like a rock cirque.
12. **High over big water:** Beacon Rock (Good; 1.29), Ebey's Landing (Good;
    0.87), Rattlesnake Ledge (Good; 0.88), Chelan Lakeshore (Great; 2.09)
    - **What V does:** these are cliff edges or bluffs above a river, lake or
      sea, with lookdown down to −66° and water making up 23–92% of V.
    - **Why people rate them lower:** they call these "lowland" views.
    - **Cause:** this looks like the alpine-character gap (H5), not water as
      such. Halving the water weight makes the overall ranking slightly worse
      (0.751).

## 7. Hypotheses about systematic causes

Each hypothesis is about a general property of V or its data. Each test is
meant to be run on every point the same way. The reference set would then
judge the result, held out (for example by cross-validating the cut points)
so the set does not become a tuning target. None of these has been
implemented.

### H1: Canopy is over-applied on open summits, ridges and lookouts
- **Evidence.**
  - Five Great summits and lookouts (Mailbox, Ellinor, Pilchuck, Tolmie
    lookout, Storm King) have T 0.28–0.40 at the summit or lookout itself,
    and Mount Si (Good) has 0.40.
  - WorldCover marks 72–100% of pixels within 30 m of those rocky summits as
    trees (table in section 6, item 7).
  - Without canopy those places would be Great to Epic.
  - Canopy is still essential overall: removing it drops ρ by 0.17.
- **Mechanism.** A 10 m tree pixel stands for a uniform 30 m stand. At
  subalpine elevations the trees are sparse, 5–15 m tall, and the classifier
  is easily fooled by mixed or shadowed pixels. From a summit, the sightlines
  that matter run down the slope and clear 30 m within about 50 m. So any tree
  pixels in that first 50 m cut T to around 0.3.
- **Test.** Replace the constant canopy height and binary class with a measured
  canopy height. Options:
  - a lidar canopy height model (WA DNR's lidar portal covers most of the
    region);
  - a global canopy height product (e.g. the 1 m Meta/WRI map or the 10 m
    ETH map);
  - a tree mask from the project's own 0.6 m NAIP imagery (it has NIR).

  Then rescore and check two things: the Great and Epic summits with T < 0.5
  should move up, and the Enclosed and Modest forest trails should stay put.

### H2: Looming credits forested walls as if they were dramatic
- **Evidence.**
  - The looming factor at the comparator point runs opposite to human tier:
    ρ = −0.44, with a median of 1.6–2.2 for Enclosed and Modest places against
    1.0–1.1 for Great and Epic.
  - The over-scored Modest and Good places all have looming 1.7–2.6 and mostly
    tree-covered views (items 10 and 11).
  - Removing looming entirely raises ρ to 0.794, with a 90% bootstrap
    probability of improvement. But looming exists for a good reason: it
    rescues real basins (Blue Lake 2.46, Colchuck 2.53, Royal Basin 2.07).
- **Test.** Make looming depend on what is looming. For example:
  - scale the looming gain by the class weight (no gain on trees);
  - or base it on the relief of the band above its own base rather than on
    elevation angle alone.

  Then rescore the trails and lakes. The test passes if basin lakes keep their
  tier while forest lakes and valley-bottom trails drop.

### H3: A constant 60 km haze punishes high, far-seeing viewpoints
- **Evidence.**
  - The places with the lowest haze factors are all high summits rated Great
    or Epic: Adams (haze 0.46), Rainier (0.54), Townsend (0.65), Ellinor
    (0.71), Pilchuck (0.72).
  - Removing haze raises ρ by 0.031 (CI +0.001 to +0.073), the only
    counterfactual whose interval excludes zero.
  - The viewer's panorama already uses a "clear day for the viewpoint's
    altitude"; V does not.
- **Test.** Make `d_haze` depend on altitude: on the observer's height and the
  mean height of the sightline, for example with an aerosol scale height of
  1–2 km. Then rescore the peaks and check whether the volcano summits rise
  without lifting lowland points.

### H4: A broad, convex summit seen from one point hides its own drop
- **Evidence.**
  - Adams and Columbia Crest get no V at all from 1–10 km (Adams) or 3–10 km
    (Columbia Crest).
  - Adams rises from 0.56 to 1.43 within 250 m of its summit point.
  - Sharp summits behave the other way: Stuart's centre (3.32) beats anything
    within 250 m. Rainier's dome does not improve within 250 m (0.95).
- **Test.** Score every peak as the maximum V over its summit area, defined
  the same way for every peak: for example, the area within N metres of
  elevation of the top, or a fixed walkable radius of about 100–250 m. Compare
  the volcano summits and check that pointed summits are unchanged. Rainier
  suggests the summit area alone will not be enough; H3 likely matters more
  there.

### H5: V under-weights the alpine character of what is seen, especially one big glaciated mountain at mid-distance
- **Evidence.**
  - The snow-and-bare share of V alone ranks the set at ρ = 0.806, against
    0.755 for V.
  - Raising the snow and bare weights to 1.5 lifts ρ to 0.781. Lowering the
    tree weight does nothing (0.756), and nor does lowering the water weight
    (0.751).
  - Spray Park, Naches Peak, Tolmie, High Divide and Skyline Divide all frame
    one large glaciated peak 8–25 km away. Their V is 0.5–1.4 against human
    tiers of 4–5.
  - The cliff-over-water viewpoints (item 12) are the mirror image: high V,
    low alpine content, rated lower.
  - There is no glacier class yet. The doc lists it, and `raw/rgi7` has the
    outlines, but no raster is built.
- **Test.** Rasterise RGI 7 glaciers as their own class with a weight above
  1.0, and try a steeper class contrast: for example ice 1.5, rock and snow
  1.2, meadow 0.6, trees 0.2. Optionally also let distant terrain count in
  proportion to its visible relief, which rewards a high massif over a low
  ridge at the same distance. Check the change with cut points cross-validated
  on held-out places.

  A test of the opposite idea, that people judge by the best single direction
  rather than the whole panorama, made the ranking worse (ρ 0.63–0.66). It
  should not be pursued.

### H6: A 30 m DEM cannot see coastal and small-scale relief
- **Evidence.** The coast places (Rialto 0.21, Cape Flattery 0.36) and small
  granite benches (Rampart Lakes 0.21) are the largest under-scores that canopy
  and haze do not explain. Their V comes almost entirely from within 1 km.
  The doc's resolution note assumes 30 m features are noise, which holds for a
  mountain panorama but not for sea stacks and 30 m cliffs.
- **Test.** For a sample of coastal and lowland points, compute V on a 1–3 m
  lidar DEM resampled to 5 or 10 m and compare it with the 30 m score. If the
  coast moves by a tier, the region needs a finer DEM near the shore.

### H7: OSM trail segmentation decides where a trail ends and what counts as one trail
- **Evidence.**
  - Heliotrope Ridge's named trail stops short of the viewpoint: 0.21 on the
    trail against about 1.3 at the overlook.
  - Half of the Naches loop is a different named way, the PCT.
  - The Thornton Lakes and the Gothic Basin ways have no name.
  - Long named trails merge distinct hikes: Thunder Creek runs 30 km to Park
    Creek Pass, with V_max 2.05 far beyond the day-hike section, and the Snow
    Lakes Trail runs through the Enchantments core. A trail's `V_max` can come
    from somewhere a day hiker never reaches, and its average mixes hikes.
- **Test.** Group trails by OSM route relations where they exist. Attach
  connected unnamed ways, up to a named destination or a dead end, to the
  named trail. Report a `V_max` per segment between junctions or trailheads.
  Then rerun this reference set against the segment scores.

### H8: Some disagreement is on the human side, not V's
- Ratings fold in effort, access, crowds, novelty and fame. Rainy Lake, an easy
  paved walk under the same cliffs that make Maple Pass famous, is rated Good;
  V calls it Epic.
- Waterfalls, old growth, wildflowers, wildlife and history drive the
  Enclosed/Modest split and much of the love for the Hoh, Spray Park and the
  Iron Goat. V cannot see any of these, by design.
- These cases should be read as limits of the reference set, not targets for
  V.

## 8. Limitations

- **Subjectivity and source bias.**
  - The tiers are a synthesis of WTA star averages, best-of lists and
    trip-report language. They are not a survey.
  - Famous alpine places are over-represented among sources, and the "views"
    criterion is not applied uniformly by reviewers.
  - Reddit and AllTrails, which reflect a broader audience, could not be
    reached.
  - Several places had half-tier disagreements; these are noted in the CSV
    justifications.
- **Small low tiers.** Only two places are Enclosed and 13 Modest, so the
  1 | 2 cut point is weak. The whole-hike Epic class has only three places.
- **The elevation confound.** Because the set leans alpine, any feature
  correlated with altitude looks good on it. Improvements suggested by this set
  should also be checked against random and lowland points, as the README's
  earlier reference set did.
- **Comparators are not like for like.** A summit's `V` is one point. A trail's
  `V_max` is the best of many points (up to hundreds), refined by a local
  search. A trail therefore gets more chances at a high score than a summit.
  The point and line distributions in section 4 differ partly for that reason.
- **OSM coverage and name matching.**
  - Some destinations are unnamed or split across records, which section 3.3
    handled by hand.
  - The clipping radii are judgement calls.
  - Some highlight coordinates are estimates (the CSV `highlight_coord`
    column). They are used only as diagnostics, never as the comparator.
- **V's design choices.** V is scored at a fixed 2 m eye, so lookout towers and
  boulders a hiker would stand on are ignored. Its 30 m DEM and fixed land-cover
  weights are judgement calls, and it has no glacier class yet.
- **Counterfactuals were taken at fixed points.** The rho values in section 5.4
  are evidence of direction, not full rescores. A rescore could move a trail's
  best point.
- **Closures change nothing here.** Several places were closed in 2026 (fires,
  the Fairfax Bridge, SR 504, the Grove of the Patriarchs bridge). The tiers
  describe scenery, not access.

## 9. How to rerun or extend

Everything needed is in the CSV.

1. **Scores.** For each row, look up `all_records` in the store. Read-only
   options:
   - `featurescore.summaries('wa-west')['items'][key]` gives `V`, `V_max`,
     `best` and `bbox`;
   - `featurescore.stored('wa-west', key)['samples']` gives the per-sample
     `[lat, lon, V]` along trails and shores;
   - or `GET /api/feature_query?slug=wa-west&q=<name>&type=all&named=0`.
2. **Clips.** Where the `clips` column has `key@lat,lon,Rkm`, keep only that
   record's samples within R km of (lat, lon) before taking the max and the
   mean.
3. **Comparators.** The highlight comparator is the maximum of `V_max` (lines
   and shores) or `V` (points) over the records. The whole-hike comparator is
   the clipped mean of the longest trail record.
4. **Breakdown.** `GET /api/vscore?lat=..&lon=..&slug=wa-west` at `best_lat`,
   `best_lon` gives the per-point breakdown: `axes.v_no_canopy`,
   `axes.v_no_haze`, `axes.v_no_loom`, `visible.*`, `by_distance` and
   `by_class`. Class reweighting is exact from `by_class[].v`.
5. **Statistics.** Spearman and Kendall τ-b between `human_tier` and `V_max`.
   For cut points, search four thresholds on a log grid for the minimum mean
   absolute tier error, and report leave-one-out results.

```python
import csv, math, featurescore                     # run from scripts/
items = featurescore.summaries('wa-west')['items']
def dkm(a, b, c, d):
    return 6371 * math.hypot(math.radians(d - b) * math.cos(math.radians((a + c) / 2)), math.radians(c - a))
for row in csv.DictReader(open('../docs/research/v-scenic-calibration-data.csv', encoding='utf-8')):
    clips = {c.split('@')[0]: tuple(map(float, c.split('@')[1].rstrip('km').split(',')))
             for c in row['clips'].split(';') if c}
    best = 0
    for key in row['all_records'].split(';'):
        r = items[key]
        if key in clips:
            la, lo, km = clips[key]
            s = [x for x in featurescore.stored('wa-west', key)['samples'] if dkm(la, lo, x[0], x[1]) <= km]
            best = max(best, max(x[2] for x in s))
        else:
            best = max(best, r.get('V_max') or r['V'])
    print(row['human_tier'], f"{best:.3f}", row['name'])
```

After a change to V, rerun this and compare ρ, the confusion table and the
inversion list. If the V distribution shifts, refit the cut points rather than
keeping the old ones. Add places in bands that are thin (Enclosed, Modest, and
lowland Good) and in areas not yet covered: the North Cascades east of Hwy 20
(the Pasayten), the Kettle River area and the Blue Mountains are outside
`wa-west`. Rate any new place on the section 4 scale *before* looking up its V.

## Appendix: all 80 places

Places are sorted by human highlight tier, then by `V_max`. "V tier" applies
the highlight cut points; (higher) or (lower) marks where it differs from the
human tier.

| Place | Area | Human tier: highlight / whole | V_max | V avg | V tier | Comparator record |
|---|---|---|---|---|---|---|
| Mount Stuart | Alpine Lakes / Stuart Range | 5 Epic / 4 Great | 3.47 | - | 5 Epic | `node/348974307` Mount Stuart |
| Burroughs Mountain | Mount Rainier | 5 Epic / 5 Epic | 2.69 | 1.03 | 5 Epic | `trail:3d64e7ac0d21` Burroughs Mountain Trail |
| Skyline Trail / Panorama Point (Paradise) | Mount Rainier | 5 Epic / 5 Epic | 2.37 | 0.51 | 5 Epic | `trail:186d6593391b` Skyline Trail |
| Mount St Helens summit (Monitor Ridge) | Mount St Helens | 5 Epic / 4 Great | 2.15 | 0.89 | 5 Epic | `node/356546327` Mount Saint Helens |
| Enchantments core (Upper Enchantment Lakes / Aasgard Pass) | Alpine Lakes / Enchantments | 5 Epic / 5 Epic | 2.10 | 0.70 | 5 Epic | `trail:35ca8dcbd43f` Aasgard Pass Trail |
| Cascade Pass and Sahale Arm | North Cascades NP | 5 Epic / 4 Great | 2.06 | 0.98 | 5 Epic | `way/128570152` Sahale Arm Trail |
| Colchuck Lake | Alpine Lakes / Enchantments | 5 Epic / 3 Good | 2.00 | 0.41 | 5 Epic | `relation/17094764` Colchuck Lake |
| Goat Rocks (Snowgrass Flat, Old Snowy, Knife Edge) | Goat Rocks | 5 Epic / 4 Great | 1.96 | 0.83 | 5 Epic | `node/348190251` Old Snowy Mountain |
| Royal Basin | Olympics (rain shadow) | 5 Epic / 3 Good | 1.92 | 0.13 | 5 Epic | `way/240072844` Upper Royal Basin Trail |
| Maple Pass Loop | North Cascades (Hwy 20) | 5 Epic / 4 Great | 1.80 | 0.58 | 5 Epic | `trail:0027b9784daa` Maple Pass Trail |
| Summerland and Panhandle Gap | Mount Rainier | 5 Epic / 4 Great | 1.66 | 0.13 | 5 Epic | `trail:480a85f29393` Wonderland Trail |
| Yellow Aster Butte | Mount Baker | 5 Epic / 4 Great | 1.65 | 0.52 | 5 Epic | `node/356547007` Yellow Aster Butte |
| Lake Ingalls | Alpine Lakes / Teanaway | 5 Epic / 4 Great | 1.61 | 0.61 | 5 Epic | `trail:0e5d49015b63` Ingalls Way Trail |
| Ptarmigan Ridge | Mount Baker | 5 Epic / 4 Great | 1.58 | 0.92 | 5 Epic | `way/237014363` Ptarmigan Ridge Trail |
| Hidden Lake Lookout | North Cascades (Cascade River) | 5 Epic / 4 Great | 1.46 | 0.49 | 5 Epic | `way/128830643` Hidden Lake Trail |
| Skyline Divide | Mount Baker | 5 Epic / 3 Good | 1.41 | 0.41 | 5 Epic | `way/182280797` Skyline Divide Trail |
| Tuck and Robin Lakes | Alpine Lakes (Cle Elum) | 5 Epic / 3 Good | 1.02 | 0.40 | 4 Great (lower) | `trail:0756d69cd3a9` Robin Lakes Trail |
| Mount Rainier summit | Mount Rainier | 5 Epic / 5 Epic | 0.96 | - | 4 Great (lower) | `node/7677147230` Columbia Crest |
| High Divide / Seven Lakes Basin | Olympics (Sol Duc) | 5 Epic / 4 Great | 0.90 | 0.24 | 4 Great (lower) | `way/36741068` Bogachiel Peak Trail |
| Spray Park | Mount Rainier | 5 Epic / 4 Great | 0.68 | 0.20 | 4 Great (lower) | `trail:7053161ad018` Spray Park Trail |
| Mount Adams summit (South Climb) | Mount Adams | 5 Epic / 4 Great | 0.62 | - | 4 Great (lower) | `node/356544063` Mount Adams |
| Blue Lake (Washington Pass) | North Cascades (Hwy 20) | 4 Great / 3 Good | 2.32 | 0.22 | 5 Epic (higher) | `way/301687140` Blue Lake |
| Chelan Lakeshore Trail | East slopes / Lake Chelan | 4 Great / 3 Good | 2.09 | 0.75 | 5 Epic (higher) | `trail:33a808726143` Lakeshore (Chelan) Trail |
| Spider Meadow and Spider Gap | Glacier Peak Wilderness (east) | 4 Great / 3 Good | 1.98 | 0.47 | 5 Epic (higher) | `trail:69351eaf611f` Phelps Creek Trail |
| Lake Ann (Mount Baker) | Mount Baker | 4 Great / 3 Good | 1.63 | 0.76 | 5 Epic (higher) | `trail:60ae35cdc0b6` Lake Ann Trail |
| Lake Serene | Stevens Pass (Hwy 2) | 4 Great / 2 Modest | 1.56 | 0.16 | 5 Epic (higher) | `relation/16680239` Lake Serene |
| Harry's Ridge | Mount St Helens | 4 Great / 4 Great | 1.51 | 1.12 | 5 Epic (higher) | `way/43141072` Harry's Ridge Trail |
| Thornton Lakes and Trappers Peak | North Cascades NP | 4 Great / 2 Modest | 1.28 | 0.16 | 4 Great | `node/349028372` Trappers Peak |
| Gothic Basin and Foggy Lake | Mountain Loop | 4 Great / 3 Good | 1.16 | 0.21 | 4 Great | `way/840058699` None |
| Mount Townsend | Olympics (Buckhorn Wilderness) | 4 Great / 3 Good | 1.11 | 0.17 | 4 Great | `node/2481886239` Mount Townsend |
| Mount Dickerman | Mountain Loop | 4 Great / 3 Good | 1.02 | 0.27 | 4 Great | `trail:0e18d95bdfe0` Dickerman Mountain Trail |
| Snow Lake (Snoqualmie Pass) | I-90 corridor | 4 Great / 3 Good | 1.02 | 0.21 | 4 Great | `relation/6446962` Snow Lake |
| Hurricane Hill | Olympics (Hurricane Ridge) | 4 Great / 4 Great | 0.93 | 0.35 | 4 Great | `node/348997349` Hurricane Hill |
| Granite Mountain lookout | I-90 corridor | 4 Great / 3 Good | 0.81 | 0.25 | 4 Great | `node/349018654` Granite Mountain |
| Sourdough Mountain lookout | North Cascades NP | 4 Great / 2 Modest | 0.77 | 0.25 | 4 Great | `trail:d827b35bb5db` Sourdough Mountain Trail |
| Tolmie Peak lookout / Eunice Lake | Mount Rainier | 4 Great / 3 Good | 0.77 | 0.17 | 4 Great | `way/129536770` Tolmie Peak Trail |
| Dog Mountain | Columbia River Gorge | 4 Great / 3 Good | 0.74 | 0.15 | 4 Great | `trail:8666463d0123` Dog Mountain Trail |
| Lake Angeles / Klahhane Ridge | Olympics (Port Angeles) | 4 Great / 3 Good | 0.67 | 0.07 | 4 Great | `way/918404820` Klahhane Ridge Trail |
| Mount Storm King | Olympics (Lake Crescent) | 4 Great / 2 Modest | 0.66 | 0.28 | 4 Great | `trail:f1f0e7b2ec38` Mt. Storm King Trail |
| Mount Pilchuck lookout | Mountain Loop | 4 Great / 3 Good | 0.66 | 0.13 | 4 Great | `node/348999742` Mount Pilchuck |
| Upper Lena Lake | Olympics (Hood Canal) | 4 Great / 2 Modest | 0.59 | 0.15 | 3 Good (lower) | `relation/15260233` Upper Lena Lake |
| Mount Ellinor | Olympics (southeast) | 4 Great / 3 Good | 0.59 | 0.13 | 3 Good (lower) | `node/4109442269` Mount Ellinor |
| Naches Peak Loop | Mount Rainier (Chinook Pass) | 4 Great / 4 Great | 0.51 | 0.19 | 3 Good (lower) | `way/232482239` Naches Peak Loop Trail |
| Mailbox Peak | I-90 corridor | 4 Great / 2 Modest | 0.48 | 0.14 | 3 Good (lower) | `trail:a97ef2726ae2` Mailbox Peak Trail |
| Rachel Lake and Rampart Lakes | I-90 corridor (Alpine Lakes) | 4 Great / 3 Good | 0.38 | 0.11 | 3 Good (lower) | `relation/16215472` Rachel Lake |
| Cape Flattery | Olympic coast | 4 Great / 2 Modest | 0.36 | 0.04 | 3 Good (lower) | `trail:ab826a86776b` Cape Flattery Trail |
| Rialto Beach / Hole-in-the-Wall | Olympic coast | 4 Great / 4 Great | 0.21 | 0.14 | 3 Good (lower) | `trail:20ff91ef718a` North Coast Beach Travelway |
| Heliotrope Ridge (Coleman Glacier overlook) | Mount Baker | 4 Great / 2 Modest | 0.21 | 0.09 | 3 Good (lower) | `trail:285e1b1acfb8` Heliotrope Ridge Trail |
| Rainy Lake | North Cascades (Hwy 20) | 3 Good / 2 Modest | 1.74 | 0.19 | 5 Epic (higher) | `way/339423897` Rainy Lake |
| Beacon Rock | Columbia River Gorge | 3 Good / 3 Good | 1.29 | 0.63 | 4 Great (higher) | `way/276151688` Beacon Rock Trail |
| Lake Twenty-Two | Mountain Loop | 3 Good / 2 Modest | 1.05 | 0.08 | 4 Great (higher) | `relation/14862342` Lake Twentytwo |
| Rattlesnake Ledge | I-90 corridor | 3 Good / 2 Modest | 0.88 | 0.12 | 4 Great (higher) | `trail:30790237f265` Rattlesnake Ledge Trail |
| Ebey's Landing bluff | Puget Sound (Whidbey) | 3 Good / 3 Good | 0.87 | 0.63 | 4 Great (higher) | `trail:a1625e787295` Bluff Trail |
| Ancient Lakes | East slopes / Columbia Basin | 3 Good / 3 Good | 0.69 | 0.21 | 4 Great (higher) | `relation/18902924` Ancient Lake |
| Lake Valhalla | Stevens Pass | 3 Good / 3 Good | 0.64 | 0.10 | 4 Great (higher) | `relation/17032390` Lake Valhalla |
| Hamilton Mountain | Columbia River Gorge | 3 Good / 3 Good | 0.62 | 0.17 | 4 Great (higher) | `trail:a3b9bccf331f` Hamilton Mountain Trail |
| Oyster Dome | Chuckanut / Puget Sound | 3 Good / 2 Modest | 0.57 | 0.19 | 3 Good | `way/222723732` Oyster Dome Trail |
| Mount Si | I-90 corridor | 3 Good / 2 Modest | 0.55 | 0.11 | 3 Good | `node/356546426` Mount Si / q̓əlbc̓ |
| Packwood Lake | Goat Rocks (west) | 3 Good / 2 Modest | 0.53 | 0.09 | 3 Good | `relation/5712324` Packwood Lake |
| Annette Lake | I-90 corridor | 3 Good / 2 Modest | 0.37 | 0.19 | 3 Good | `relation/16737457` Annette Lake |
| Baker Lake Trail | Mount Baker (Baker Lake) | 3 Good / 2 Modest | 0.37 | 0.13 | 3 Good | `trail:92aabd52ca06` (PNT) Baker Lake Trail |
| Discovery Park Loop | Puget Sound (Seattle) | 3 Good / 2 Modest | 0.30 | 0.06 | 3 Good | `trail:0f3ce3ba294a` South Beach Trail |
| Wallace Falls | Skykomish | 3 Good / 2 Modest | 0.28 | 0.02 | 3 Good | `trail:3a67331c4259` Woody Trail |
| Mount Walker | Olympics (Quilcene) | 3 Good / 1 Enclosed | 0.20 | 0.10 | 3 Good | `way/243330579` Mt. Walker Trail #894 |
| Poo Poo Point (Chirico Trail) | Issaquah Alps | 3 Good / 2 Modest | 0.16 | 0.07 | 2 Modest (lower) | `trail:59ee5015e58b` Chirico Trail |
| Iron Goat Trail | Stevens Pass | 2 Modest / 1 Enclosed | 0.56 | 0.17 | 3 Good (higher) | `trail:3245b62a5453` Iron Goat Trail |
| Middle Fork Snoqualmie River Trail | I-90 corridor | 2 Modest / 1 Enclosed | 0.56 | 0.17 | 3 Good (higher) | `trail:29bcaf243eaf` Middle Fork Trail |
| Thunder Creek (day hike section) | North Cascades NP | 2 Modest / 1 Enclosed | 0.52 | 0.23 | 3 Good (higher) | `trail:e95aa02672bb` Thunder Creek Trail |
| Green Lake (Carbon River) | Mount Rainier | 2 Modest / 1 Enclosed | 0.40 | 0.12 | 3 Good (higher) | `way/563567651` Green Lake |
| Talapus and Olallie Lakes | I-90 corridor | 2 Modest / 1 Enclosed | 0.35 | 0.07 | 3 Good (higher) | `relation/11777506` Talapus Lake |
| Staircase Rapids | Olympics (southeast) | 2 Modest / 1 Enclosed | 0.21 | 0.13 | 3 Good (higher) | `way/36743306` Staircase Rapids Nature Trail |
| Marymere Falls | Olympics (Lake Crescent) | 2 Modest / 1 Enclosed | 0.20 | 0.05 | 2 Modest | `trail:37ae0c365264` Marymere Falls Nature Trail |
| Lewis River Falls | Mount St Helens (Lewis River) | 2 Modest / 1 Enclosed | 0.18 | 0.03 | 2 Modest | `trail:ed30c4732219` Lewis River Trail |
| Twin Falls (Olallie SP) | I-90 corridor | 2 Modest / 1 Enclosed | 0.16 | 0.06 | 2 Modest | `trail:d16c8b396f0d` Twin Falls Trail |
| Boulder River | Mountain Loop (Darrington) | 2 Modest / 1 Enclosed | 0.15 | 0.07 | 2 Modest | `trail:11b61ecff7c2` Boulder River Trail |
| Sol Duc Falls | Olympics (Sol Duc) | 2 Modest / 1 Enclosed | 0.13 | 0.08 | 2 Modest | `trail:2a84ba426ba0` Sol Duc Falls Nature Trail |
| Hoh River Trail / Hall of Mosses | Olympics (west) | 2 Modest / 1 Enclosed | 0.10 | 0.06 | 2 Modest | `trail:100f9c699228` Hoh River Trail |
| Franklin Falls | I-90 corridor | 2 Modest / 1 Enclosed | 0.06 | 0.05 | 1 Enclosed (lower) | `way/232089623` Franklin Falls Trail |
| Grove of the Patriarchs | Mount Rainier | 1 Enclosed / 1 Enclosed | 0.06 | 0.03 | 1 Enclosed | `trail:1439a9978938` Grove of the Patriarchs |
| Cougar Mountain (Wilderness Peak loop) | Issaquah Alps | 1 Enclosed / 1 Enclosed | 0.04 | 0.01 | 1 Enclosed | `trail:56a88b64ae8b` Whittaker Wilderness Peak Trail |
