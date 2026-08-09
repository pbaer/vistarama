# The scoring model

Turning "how much interesting landscape can I see" and "how dramatic is the
country I'm standing in" into a number.

Two terms:

- **View score `V`** — solid-angle integral over the visible panorama.
  Position-sensitive, expensive, computed per point.
- **Ruggedness `R`** — distance-weighted vertical relief in the surrounding
  area, independent of visibility. Smooth, cheap, computed as a raster.

Combined at the end. Companion to
[scenery-scoring-data.md](scenery-scoring-data.md).

---

# Part 1 — The view score `V`

---

## Two axes, not one

There are two genuinely different things a viewpoint can have, and epic
requires both:

- **Dominance** — how much of your visual field is filled by terrain.
  A cirque maximises this. Measured in steradians.
- **Extent** — how much of the world you can actually see.
  A summit above a wide basin maximises this. Measured in m².

They are computed from the same data but they rank places differently, because
distance varies by orders of magnitude across a single panorama:

| | Dominance | Extent |
|---|---|---|
| Cirque, walls at 2 km | high | low |
| Distant range at 50 km | low | high |
| Slot canyon | very high | ~zero |
| **Col / pass — walls close, sightlines long both ways** | **high** | **high** |

A col is the only common landform that maximises both, which is exactly the
intuition that it should score highest. Any metric using only one axis will
fail to distinguish a col from whichever landform maxes out that single axis.

### What solid angle does and doesn't already give you

The solid angle subtended by a surface patch of true area `A` at distance `d`,
whose normal makes angle `θ` with the line of sight, is:

```
dΩ = A · cos(θ) / d²
```

This means **you never compute surface area or surface normals explicitly** —
the cliff-area intuition is already encoded. A cliff face viewed head-on has
large `A` and `cos θ ≈ 1` and contributes fully; a plain viewed at grazing
incidence has `cos θ ≈ 0` and contributes almost nothing, which is why standing
on a plain doesn't feel scenic despite the ground technically occupying half
your visual field.

But integrating `dΩ` alone collapses the two axes into one and throws away
extent. The `1/d²` is doing that — it makes 500 km² at 40 km worth less than
5 km² at 2 km.

---

## The formulation: one integral, one exponent

Rather than computing two quantities and multiplying them (awkward to
normalise, hard to tune, and they're correlated), put a distance exponent
inside the single integral:

```
S = Σ  ΔΩ · c(class) · w(d) · d^γ
   rays
```

The exponent `γ` interpolates continuously between the two axes:

| `γ` | Effective falloff | What it measures |
|---|---|---|
| 0 | `1/d²` | pure dominance — solid angle |
| 1 | `1/d` | balanced |
| 2 | none | pure extent — foreshortened surface area |

At `γ = 2`, `Σ ΔΩ·d²` is exactly `Σ A·cos θ` — the **foreshortened** visible
surface area. That's the right quantity rather than raw area: a vast plain seen
edge-on gets discounted by `cos θ` automatically, which is the behaviour you
want and it costs nothing. Raw area would need `1/cos θ` and would reward
looking across flat ground at a grazing angle.

So `γ` is simultaneously the extent/dominance blend *and* the distance
attenuation knob. One interpretable parameter instead of two quantities plus a
combination rule.

`w(d)` then only has two small jobs: a near cutoff (~200–300 m, so you're not
scoring the DEM cell you're standing on) and a haze taper,
`exp(−d/d_haze)` with `d_haze` ≈ 50–80 km, which is a real atmospheric
perspective effect rather than a fudge.

### Worked comparison

Solid angles from geometry; `d` in km; `Σ ΔΩ·d^γ`, then with the haze taper.

| Scenario | Ω, d | γ=0 | γ=1 | γ=1, hazed | γ=1.5, hazed |
|---|---|---|---|---|---|
| **Col** — 0.5 sr walls at 1 km + 0.15 sr through gaps at 30 km | mixed | 0.65 | **5.00** | **3.22** | **15.4** |
| Summit, 2000 m drop, valley 5 km out | 0.81 sr @ 5 km | 0.81 | 4.05 | 3.73 | 8.3 |
| Distant range, 2000 m up at 50 km | 0.08 sr @ 50 km | 0.08 | 4.00 | 1.74 | 12.3 |
| Cirque, 1000 m walls at 2 km | 0.94 sr @ 2 km | **0.94** | 1.88 | 1.82 | 2.6 |
| Forest clearing, trees at 50 m | 3.0 sr @ 50 m | **3.00** | 0.15 | 0.15 | 0.03 |
| Flat plain beyond 500 m | 0.02 sr @ ~2 km | 0.02 | 0.04 | 0.04 | 0.06 |

Reading the columns:

- **γ=0 is clearly wrong.** A forest clearing wins, and the cirque beats
  everything else. Pure solid angle rewards enclosure, full stop.
- **γ=1 ranks the way the intuition says**: col > summit ≈ distant range >
  cirque >> clearing > plain.
- **γ=1 with haze** is the best-behaved: col and summit lead, distant-range-only
  drops to cirque level. That matches "just seeing some large mountain very far
  away doesn't count as epic."
- **γ=1.5 overshoots** — distant-range-only climbs back above the summit, which
  contradicts the same intuition.

**Start at γ ≈ 1.0, tune within 0.8–1.3.** The col leads at every γ ≥ 1 because
it's the only scenario with two terms.

Note also that `γ > 0` makes the near cutoff much less load-bearing: at γ=1 the
ground within 100 m of your feet contributes ~6% of a cirque's score rather
than dominating. Keep a modest cutoff anyway, since the DEM cell you're
standing on has no meaningful local geometry.

---

## The degenerate cases still die on their own

For an observer at eye height `h` on flat ground, the ground **beyond**
distance `D` subtends only `2π · sin(arctan(h/D)) ≈ 2π·h/D`:

| Ground beyond | Solid angle | Fraction of sphere |
|---|---|---|
| 100 m | 0.107 sr | 0.9% |
| 500 m | 0.021 sr | 0.2% |
| 1 km | 0.011 sr | 0.1% |

Nearly the entire 2π sr of "ground" on a plain lies within ~100 m of your feet.
Combined with `d^γ`, flat terrain contributes essentially nothing at any γ ≥ 0.5.

---

## Do you need a vertical band limit?

Probably not, and a hard cutoff would be actively harmful.

- The **zenith is sky**, which contributes nothing — rays escape.
- The **nadir is suppressed** by `cos(φ)` in the solid angle element, plus the
  near cutoff, plus `d^γ`.
- A **hard cutoff at ±30° would break cliff-edge viewpoints**, which legitimately
  place terrain at −40° to −60° and are among the most epic situations there are.

If you want one, use a soft taper — full weight to ±35°, rolling to ~0.3 by
±70°. Start without it.

---

## Algorithm: one DEM march per azimuth gives the exact integral

You do not need a 2D grid of rays. A single outward march per azimuth yields
the complete vertical column of the panorama, exactly.

March outward along azimuth `α`, sampling the DEM. At step `i`:

```
φ_i = atan( (z_i − z_obs − curvature_drop(d_i)) / d_i )

if φ_i > φ_max:
    # elevation band [φ_max, φ_i] is newly visible, at distance d_i
    ΔΩ = Δα · (sin φ_i − sin φ_max)
    S += ΔΩ · c(class_i) · w(d_i) · d_i^γ
    φ_max = φ_i
```

Initialise `φ_max` to −90°, or to the angle at the near cutoff.

Why this is exact rather than an approximation:

- **Occlusion is the running max.** Cells in a hidden valley behind a near
  ridge have `φ_i < φ_max` and contribute nothing.
- **`cos θ` foreshortening is automatic.** Terrain sloping away produces small
  increments in `φ`, hence small `ΔΩ`. A cliff face produces a large jump in
  `φ` at nearly constant `d`, hence large `ΔΩ`. You never compute a normal.
- **True surface area is automatic**, for the same reason.
- **`sin φ₂ − sin φ₁` is the exact solid angle** of an elevation band, since
  `∫cos φ dφ = sin φ`. Not a small-angle approximation.

Cost is `O(azimuths × range_steps)` — ~360 marches per point. At 30 m sampling
over 60 km that's 2,000 steps per ray, 720k DEM samples per point. Cheap,
cache-friendly, trivially parallel.

Apply the curvature-and-refraction drop while marching:
`curvature_drop(d) ≈ (1 − k) · d² / (2R)`, `k ≈ 0.13`, `R` = 6,371 km. At
100 km this is ~650 m — the difference between seeing a distant range and not.

### Keep the depth buffer, not just the scalar

The march produces, per azimuth, a list of `(elevation band, distance, class)`.
That's a compressed panoramic depth buffer, and every candidate metric is a
different reduction of it. Store it (or at least the per-azimuth horizon
profile and distance) so `γ`, `w`, and `c` can be retuned without re-marching.
Re-marching costs 720k samples per point; storing costs well under 2 kB.

---

## Class weighting `c(class)`

Sampled at the **hit cell**, not at the observer. From ESA WorldCover + RGI:

| Visible surface | Weight |
|---|---|
| Glacier (RGI polygon) | highest |
| Snow and ice | high |
| Bare / sparse vegetation — rock, scree, talus | high |
| Permanent water | high |
| Moss and lichen — tundra | medium-high |
| Grassland, Shrubland | medium |
| Tree cover | low |
| Cropland | low |
| Built-up | negative |

A refinement at no extra cost: weight by the elevation of the hit cell relative
to the observer. Terrain rising above your horizon reads differently from
terrain below it.

---

## Free byproducts of the same march

- **Sky fraction** — rays escaping without hitting terrain. Distinguishes
  exposed summits from enclosed basins.
- **Horizon profile** — `φ_max` per azimuth, 360 × int16 = 720 bytes/point.
  Preserves directionality, so an out-and-back can be scored asymmetrically.
- **Look-down** — minimum `φ` with terrain beyond the cutoff. Strongly negative
  means you're standing on an edge.
- **Maximum sightline distance**, **visible relief**, and the full distance
  histogram of visible terrain.

The distance histogram is worth keeping on its own: a bimodal one (near walls
*and* far horizon) is the signature of a col, and might be worth a bonus term
independent of `γ` if the exponent alone doesn't separate them enough.

---

## Resolution note

30 m DEM remains adequate. The metric needs the *angular* extent of terrain
features: a 200 m cliff at 2 km subtends 5.7°, and a 30 m DEM captures that
cliff's height fine even if it smears its horizontal position by a cell.
Sub-30 m features contribute sub-degree angular extents and are noise at this
level of crudeness.

---

---

# Part 2 — Ruggedness `R`

## Why this earns its place

The stated rationale — that being in dramatic country matters independently of
what's visible right now — is sound. But there's a stronger one:

**`V` is fragile and `R` is not.** The view score depends on exact observer
position. Step 50 m and a ridge occludes half the panorama; the score halves.
It's also sensitive to DEM error, since a single bad cell on a near ridgeline
can shadow everything behind it. Ruggedness is a smooth convolution — it varies
slowly in space, averages DEM noise away, and is nearly immune to position.

Combining a high-variance, position-sensitive signal with a low-variance
regional one is variance reduction, not just a second opinion. A trail that
dips behind a moraine for 300 m shouldn't crater in score, and `R` is what
prevents that.

The two terms are *correlated* — rugged country produces good views — and that's
fine. The value here isn't independence, it's stability.

## What "vertical change" should be

The proposal is a convolution, which is right. The question is what gets
convolved — and the obvious answers are all wrong in instructive ways.

**Not slope magnitude.** `∫K·|∇z|` measures roughness at the DEM's own scale
and can't tell a badlands from a fjord: both have large `|∇z|` at 30 m, but one
has 50 m of total relief and the other has 1,000 m.

**Not the standard deviation of elevation either.** This was the first
implementation and it is subtly but badly wrong. σ measures *total relief
spread*, not variety. A uniform tilted plane — 2000 m of rise over 5 km — has
σ ≈ 577 m, higher than almost any genuinely broken terrain, despite being
perfectly smooth. Tested against real ground it ranked the walls of the Lake
Chelan trough (essentially a giant tilted plane) **above** the Enchantments,
which is backwards. Subtracting the local *mean*, which σ already does, does
nothing about this; the problem is the local *tilt*.

**Not the RMS departure from a fitted plane either.** This looked like the
obvious fix — a uniform slope fits its own plane exactly and scores zero
however steep — and it is wrong for a subtler reason: the *residual amplitude*
scales with steepness, because steeper ground has proportionally deeper gullies
and sharper ribs. Implemented and measured, it came out **96% correlated with
mean slope**, worse than the σ it replaced, and still ranked a smooth hillside
above the Enchantments.

**Use variety in the slope vector.** The discriminator is not how steep the
ground is, nor how far it departs from a plane, but how often the gradient
*changes*. A hillside has a near-constant gradient however steep; a cirque
swings from flat slabs to vertical faces and back within a few hundred metres.

```
1.  g(x)     = least-squares plane gradient over GRAD_BASELINE (240 m)

2.  rough(x) = degrees( arctan( sqrt( Var(gx) + Var(gy) ) ) )
                                          over a window of ROUGH_INNER (600 m)

3.  R(x)     = (K * rough)(x)             over the big biweight kernel (5 km)
```

A uniform slope of any steepness gives exactly zero at step 2, by
construction. Step 3 averages a *local* quantity, which is what separates one
big valley (locally uniform almost everywhere) from genuinely complex ground.

**The unit is an angle, deliberately.** Gradient variance is dimensionless;
scaling it by the baseline would give metres, but metres *per 240 m*, which is
not a thing anyone can read off a legend. The angular form is self-describing:
"the slope here typically differs from the neighbourhood slope by N degrees".
arctan is monotonic so no ranking changes. Vertical-scale sensitivity does not
come from the output unit — it comes from GRAD_BASELINE, since a badlands with
50 m gullies cannot generate a large gradient measured over 240 m whereas an
alpine cirque can. (This is also why plain VRM is not used: it is dimensionless
*and* scale-blind, so it would rate a badlands and a cirque alike.)

Properties:

- **Separable filters inside, one FFT convolution outside.** O(n); the whole
  AOI takes a few seconds.
- **Smooth and continuous** — no `max`/`min` operators.
- **Three scales**, though only the outer one is a real tuning knob; the inner
  two are set by what "local" means underfoot.

Measured on the Alpine Lakes AOI: alpine cirques 20–23°, smooth steep
hillsides ~15°, forested valleys ~13–15°, lowland plains ~2°. Relief is kept
alongside as a companion layer in metres — see the README table, where a
39°-slope trough wall reads only 15.0° of ruggedness.

**Permanent water is excluded from both kernels.** A lake surface is a large
dead-flat area pinned at the window's low end, which makes the elevation
distribution bimodal; σ rewards bimodality independently of relief, so Lake
Chelan read ~14% high while lake-free valleys of comparable depth were
unaffected. Water still participates in the local gradient fit — shoreline
slopes are real — it just contributes no flatness to the averages.

### Diagnosing this class of failure

Correlation with mean slope is the tool, but the *unconditional* correlation is
nearly useless: it sits at 0.975 even for a good metric, because it is
dominated by the entirely legitimate contrast between plains and mountains.
Condition on slope instead. For the current metric:

| restricted to | r vs mean slope | R² |
|---|---|---|
| everything | 0.975 | 95% |
| slope > 20° | 0.874 | 76% |
| slope > 25° | 0.769 | 59% |
| slope > 30° | **0.515** | **27%** |

Three quarters of the variance among steep ground is something other than
steepness. A metric that stays above ~0.9 in the steep bins is a slope map
wearing a hat.

## The kernel gotcha

The intuitive "taper linearly from 1 at the centre to 0 at 10 km" does **not**
weight nearby terrain more. In polar coordinates the area element is `r dr dθ`,
so each annulus contributes proportionally to `r`, and there is 3× more area in
the outer half of a disc than the inner half.

For `K(r) = 1 − r/R`, the contribution from the inner half is
`∫₀^{R/2} r(1−r/R)dr = R²/12`, and from the outer half
`∫_{R/2}^{R} r(1−r/R)dr = R²/12` — **exactly equal**. A linear taper is
distance-*neutral*, not distance-weighted.

To actually favour the near field the kernel must fall faster than `1/r`.
Recommended starting point, a biweight:

```
K(r) ∝ (1 − (r/R)²)²      for r < R,  0 otherwise
```

This puts ~58% of the weight in the inner half — mildly near-favouring, smooth,
compactly supported, and zero-derivative at the edge so there's no ring
artefact. Raise the exponent to sharpen the near-field emphasis.

**Always sanity-check a kernel by integrating `K(r)·2πr dr` per annulus**, not
by looking at `K(r)`.

## Radius

`R = 10 km` is a reasonable default. Worth testing 5 / 10 / 20 km — the choice
determines whether "rugged" means "this valley" or "this range." A multiscale
version (sum of two or three radii) is defensible but costs parameters; start
with one.

## Caveats

- **A uniform planar slope used to score high** — this was flagged here as
  "note it, don't fix it yet", and reference points later showed it had to be
  fixed. The plane fit in step 1 is that fix. Worth remembering that the
  failure was invisible in aggregate statistics and only showed up when
  specific known places were ranked against each other.
- **Handle sea as `z = 0`, not nodata.** Otherwise the convolution produces
  garbage along every coastline, and coastal cliffs are exactly where you want
  this working.
- Keep `R` purely geometric — no land-cover weighting. The separation between
  "objective terrain" and "subjective view content" is clean and worth keeping.

---

# Part 3 — Combining `V` and `R`

## Power mean: one parameter spans the whole space

Normalise each term to its median over a reference region, so both are
dimensionless and O(1). Then:

```
S = ( a·V^p + (1−a)·R^p )^(1/p)
```

| `p` | Behaviour |
|---|---|
| 1 | arithmetic mean — fully compensatory, either can substitute for the other |
| → 0 | geometric mean — conjunctive, both required |
| −1 | harmonic mean — strongly conjunctive |
| → −∞ | minimum — the weaker term decides |

Two parameters (`a`, `p`), continuous, and `p` interpolates across every
combination rule you'd otherwise pick between discretely.

**Start at `p → 0` (geometric mean).** The conjunctive behaviour is what you
want: a summit above flat plains has high `V`, low `R`, and shouldn't be epic;
a fully occluded point in a gorge has high `R`, low `V`, and shouldn't be
either. An arithmetic mean lets one term fully mask the other, which is exactly
the failure mode. If field-testing shows genuinely epic points being punished
for a temporarily blocked view, raise `p` toward 0.5 rather than switching
formulas.

Normalising to medians rather than to fixed constants means only the *ratio* of
the two scales matters, and that ratio is absorbed into `a` — so there are no
hidden normalisation parameters.

## Compute asymmetry — use `R` as the gate

These two terms cost wildly different amounts:

| | Cost |
|---|---|
| `R` | 2 FFT convolutions per region — a full continental raster in minutes |
| `V` | ~720k DEM samples **per point** |

So compute `R` as a raster everywhere, first, and use it as a **pre-filter**:
only run the expensive view march where `R` exceeds a threshold. Since the
combination is conjunctive, low `R` guarantees a low final score regardless of
`V`, so those points can be skipped rather than computed and discarded.

Stacked with the canopy and built-up gates from
[scenery-scoring-data.md](scenery-scoring-data.md), this should cut the number
of points needing a view march by a large factor — most of the USA and Europe
is neither rugged nor open.

---

# Parameter inventory

Keeping this list short is a design goal in itself.

| Parameter | Role | Start at | Tune? |
|---|---|---|---|
| `γ` | extent vs dominance in `V` | 1.0 | **yes**, 0.8–1.3 |
| `d_haze` | atmospheric taper | 60 km | maybe, 50–80 km |
| near cutoff | ignore the cell underfoot | 300 m | no |
| `c(class)` | land-cover interest weights | table above | coarsely, ~4 groups |
| `R_kernel` | ruggedness radius | 10 km | **yes**, 5–20 km |
| kernel exponent | near-field emphasis | 2 (biweight) | rarely |
| `a` | view vs ruggedness weight | 0.5 | **yes** |
| `p` | compensatory vs conjunctive | → 0 | maybe, 0–0.5 |

Four knobs that genuinely matter: `γ`, `R_kernel`, `a`, `p`. Everything else
can be fixed at a defensible default.

---

# Open questions

1. **Reference set.** Pick 8–10 known points spanning the failure modes: a
   classic col, a cirque floor, a summit above a deep valley, a summit above
   farmland, a forest clearing, a lakeshore, a ridge walk, a valley trail.
   All four knobs get fitted to these, so choosing them well matters more than
   any individual parameter.
2. **Bimodality bonus** — does `γ` alone rank cols above summits, or does the
   distance histogram need an explicit near-and-far term?
3. **Ruggedness radius vs view range.** `R_kernel` = 10 km and the view's
   effective range of ~60 km describe different neighbourhoods. Is that the
   right split, or should they be closer?
4. **Does `R` want detrending** to stop uniform slopes scoring high? Defer
   until the reference set says whether it's a real problem.
