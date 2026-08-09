# Terrain metric review — how ours compare to the literature

> **Status: recommendations 1–3 implemented.** Ruggedness now uses the exact
> spherical SD of normals; a 1.5 km near-field band ships alongside the 5 km
> one; positive/negative/defect openness are new layers. Relief is unchanged
> (still σ, with the water mask). See the README table for current values.

Findings from a research pass over the published terrain-ruggedness /
relief literature, with the standard measures implemented and tested on our
own Alpine Lakes DEM rather than assessed on paper.

---

## The one test that organises everything

**Does a perfectly smooth tilted plane of slope θ score zero?** That single
question partitions every published measure cleanly, and it is the failure mode
we burned two iterations on.

| Scores **zero** on a plane (true complexity) | Scores **∝ tan θ** (steepness in disguise) |
|---|---|
| SD of slope · SD of curvature · all curvatures | **TRI (Riley)** = `L·tanθ·√6` |
| Vector dispersion · **VRM** · **spherical SD of normals** | `gdaldem roughness` |
| **Topographic openness** (= 90.000° exactly) | **Surface-area ratio** = `sec θ` |
| SD of *detrended* elevation · TPI · DEV | **SD of elevation** = `0.250·D·tanθ` |
| Strahler ruggedness number `H·Dd` | **local relief (max−min)** = `D·tanθ` |
| **ours** | **p95−p5** = `0.805·D·tanθ` · IQR = `0.404·D·tanθ` |
| | **Melton** `H/√A` and **Schumm** `H/L` = `tan θ` exactly |
| | Sky-view factor = `1 − θ/π` · SAGA Protection Index ≈ `θ/π` |

TRI, `gdaldem roughness`, surface-area ratio and SD-of-elevation differ on a
plane **only by a constant multiplier**. They are one measurement wearing four
names. Trevisani et al. (2023) measured TRI–slope r = 0.97–0.989 and state that
TRI "should not be applied directly to a DEM, otherwise it will represent a
proxy of slope."

Verified here from first principles: openness is exactly 90.000000° on a plane
at every slope tested, because the horizon angle along azimuth φ is
`arctan(tanθ·cos φ)` and antipodal pairs cancel.

---

## Our ruggedness is a rediscovery — of the right thing

It is **not** VRM. It is the **spherical standard deviation of surface
normals**, Lindsay, Newman & Francioni 2019, *Geosciences* 9(7):322
([doi](https://doi.org/10.3390/geosciences9070322)), shipping in WhiteboxTools
as `SphericalStdDevOfNormals`:

```
σs = sqrt( −2·ln(R/n) ) · 180/π        R = ‖Σ n̂ᵢ‖
```

reported **in degrees**, computed after a **Gaussian blur of σ = radius/3** —
functionally our 240 m fitted-plane baseline. The paper states it is *"zero for
flat **and inclined** planes and increases infinitely with greater surface
complexity"*: our design goal, published in 2019.

Correlation against ours on this DEM: **0.9926** at the 600 m stage, **0.9976**
after the 5 km kernel. Same layer.

### Where our stated rationale was wrong

The docstring claimed the angular units gave vertical-scale sensitivity that
VRM lacks. Tested directly:

| terrain | ours | SSDN@240 | VRM@240 | VRM cell-level |
|---|---|---|---|---|
| badlands, 100 m gullies / 25 m relief | 3.07 | 3.07 | 0.0015 | 0.108 |
| cirque, 1000 m / 300 m relief | 27.32 | 25.36 | 0.0976 | 0.114 |

Cell-level VRM fails as predicted — but **VRM on the same 240 m baseline
discriminates just as well as ours**. The baseline is doing the work, not the
units, and arctan is monotone so the units cannot be. Docstrings corrected.

### One genuine virtue, one genuine defect

**Virtue:** ours is *exactly* invariant to superimposing a plane — adding a
plane adds a constant to the gradient, and variance ignores constants. SSDN and
VRM both decay slightly with tilt. Our decoupling is stronger in that specific
sense.

**Defect:** gradient space is a gnomonic projection, so for fixed *angular*
dispersion ours inflates by **sec²θ** — 1.7× at 40°, 4× at 60°. Residual slope
coupling above 45°: ours 0.396, SSDN 0.259, VRM@240 0.251. The 5 km kernel
washes most of it out.

### Conditional-on-slope correlation, our own diagnostic, at the 600 m stage

| metric | all slopes | slope > 45° |
|---|---|---|
| **ours** | 0.405 | 0.396 |
| SSDN @240 m | 0.324 | **0.259** |
| VRM @240 m | **0.219** | 0.251 |
| SD of slope | 0.320 | 0.302 |
| Trevisani RRI | 0.455 | 0.538 |
| **Riley TRI** | 0.849 | 0.627 |
| **σ of elevation** | 0.969 | 0.904 |

We beat TRI by a mile and beat Trevisani's 2023 fix for TRI. We lose slightly
to VRM/SSDN at the same baseline.

**Verdict: keep it, cite it.** Aligning to the exact SSDN form is a two-line
change that removes the sec²θ inflation and buys a citation, but changes the
map by essentially nothing (r = 0.998). Low priority.

---

## Relief

- `r(σ, p95−p5) = 0.978` — the interpercentile range is the same map.
- `r(σ, max−min) = 0.681` — materially different, and **worse**: max−min is a
  two-pixel estimator. A plateau with one spire scores like a field of ridges.
- **p95−p5 is immune to the lake artefact**, which would let us delete the
  WorldCover water-mask dependency.
- But: **no primary literature uses an interpercentile range of elevation as a
  relief metric** — roughly 200 searches found none. It is defensible and
  trivially computable, but novel, with no citation to hang it on.
- **Melton `H/√A` and Schumm `H/L` are exactly `tan θ`** and are *basin*-scale
  anyway. Not applicable. Their own agencies describe Melton as "an indirect
  index of catchment slope."
- Standard practice in tectonic geomorphology is **max−min in a 10 km
  *diameter* circle** (Montgomery & Brandon 2002). Ours is a 10 km *radius*
  biweight — 2× wider than the convention.

**Verdict: keep σ.** The lake artefact is already fixed by the water mask, and
σ is the better estimator. Revisit if the water mask becomes a maintenance
burden.

---

## What we should add

Complementarity against our existing ruggedness layer, measured on this DEM:

| candidate | r with ruggedness | verdict |
|---|---|---|
| **Yokoyama openness (Φ − Ψ)** | **0.162** | **add** |
| **plan curvature** | **0.073** | **add** |
| negative openness alone | −0.517 | useful |
| profile curvature | 0.509 | marginal |
| SD of mean curvature | 0.780 | redundant |

**Topographic openness** (Yokoyama, Shirasawa & Pike 2002, *PE&RS* 68(3):257–265)
is the strongest addition:

```
Φ_L = mean over 8 azimuths of (90° − max horizon angle within L)    positive
Ψ_L = mean over 8 azimuths of (90° + min horizon angle within L)    negative
```

- Exactly 90° on any plane, at any slope, any radius, any rotation — verified.
- **Negative openness is precisely the "depression angle / cliff-edge" term
  [scenery-scoring-data.md](scenery-scoring-data.md) already asks for**, with a
  published name and a standard formula.
- `Φ + Ψ ≤ 180°`, with equality **iff every radial profile is straight**. The
  *defect* `180° − (Φ+Ψ)` is therefore a single slope-invariant complexity
  number — arguably the quantity hiding behind the pair.
- O(n × directions) with early termination, far cheaper than a total viewshed,
  and we already have the ray-march machinery in `panorama.py`.

⚠️ **Do not** use sky-view factor as a substitute: it clips at the mathematical
horizon, which destroys the antipodal cancellation and leaves
`SVF = 1 − θ/π` — a first-order slope term. SAGA's Morphometric Protection
Index has the same flaw and its documentation wrongly claims equivalence to
positive openness; the source clips at zero.

---

## Two literature findings that challenge our current design

**1. The near field dominates, and our kernels don't reflect that.**
Schirpke et al. (2013) fitted scenic-beauty distance-zone weights of
**0.64 / 0.22 / 0.14** for 0–1.5 km / 1.5–10 km / 10–50 km. Two-thirds of
perceived beauty comes from the nearest 1.5 km. Our 5 km and 10 km kernels put
far more weight in the middle distance than the only fitted evidence supports.
Separately, NatureScot's Scotland wildness mapping uses **SD of curvature
within a 250 m radius**, justified as "the area of greatest immediate influence
when walking through a landscape" — and Chang Chien, Carver & Comber (2020)
validated that layer against crowdsourced ScenicOrNot ratings. That is the only
terrain-complexity metric anywhere with published validation against *perceived*
scenic quality, and its radius is 250 m, not 5 km.

**2. No fitted scenic-beauty model contains a positive relief term.**
Across Seresinhe/ScenicOrNot, Schirpke, ESTIMAP and Komossa, terrain enters
almost entirely through *viewshed geometry and visible land cover*. The one
study regressing perceived beauty on elevation range (Austria 2024, Flickr) got
a **negative** direct effect. Our relief layer is a reasonable prior but it is
an assumption, not a literature finding.

Also worth stealing: Schirpke found views with **no near-zone content rated
worst** — a distant panorama over an empty foreground is bad. With
prospect-refuge theory that implies openness should enter **non-monotonically**,
not as "more is better".

---

## Fractal / multi-scale

Variogram analysis of our DEM gives Hurst **H = 0.82**, fractal **D = 2.18**,
with H falling smoothly from 0.97 at 30–60 m to 0.50 at 1–2 km — **no scale
break**. There is no characteristic scale to discover; 240 m is a legitimate
choice but not a privileged one. Adjacent-scale roughness correlations run
0.76–0.84, so Lindsay's **multi-scale max-across-scales** variant
(`MultiscaleStdDevNormals`, which also outputs the scale at which the maximum
occurs) would add real information rather than restate ours.

Caveat: the semivariogram fractal estimator is visibly biased as H → 0 and
H → 1, so treat D as indicative.

---

## Sources

Primary where possible. Several claims were explicitly flagged as
unverified by the researchers — spectral–fractal relations, Zevenbergen &
Thorne's primary text, WhiteboxTools' proprietary `Openness` implementation
(test it on a synthetic plane before trusting either sign convention), and
specific Melton debris-flow thresholds, which are mutually inconsistent across
the literature and should not be quoted as settled.

- Lindsay, Newman & Francioni 2019 — <https://doi.org/10.3390/geosciences9070322>
- Yokoyama, Shirasawa & Pike 2002 — <https://www.asprs.org/wp-content/uploads/pers/2002journal/march/2002_mar_257-265.pdf>
- Grohmann, Smith & Riccomini 2011 — <https://doi.org/10.1109/TGRS.2010.2053546> (open preprint: <https://eartharxiv.org/repository/view/1628/>)
- Sappington et al. 2007 (VRM) — <https://doi.org/10.2193/2005-723>
- Riley et al. 1999 (TRI) — <https://arc.lib.montana.edu/ojs/index.php/IJS/article/view/2914/2558>
- Zakšek, Oštir & Kokalj 2011 (SVF) — <https://doi.org/10.3390/rs3020398>
- Montgomery & Brandon 2002 — <https://gis.ess.washington.edu/grg/publications/pdfs/Montgomery_Brandon.pdf>
- Minár, Evans & Jenčo 2020 (curvature) — <https://doi.org/10.1016/j.earscirev.2020.103414>
- Trevisani et al. 2023 (RRI) — <https://doi.org/10.1016/j.geomorph.2023.108838>
- Weiss 2001 (TPI) — <https://www.jennessent.com/downloads/TPI-poster-TNC_18x22.pdf>
