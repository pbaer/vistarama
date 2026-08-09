# Data sources for a hiking map — USA + Europe

Scope: free, permissively licensed for **non-commercial** use, high resolution,
covering at least CONUS and Western/Central Europe.

Status of research: July 2026.

Sizes marked **(measured)** come from the publisher. Sizes marked **(est.)** are
computed from resolution × area and typical compression ratios — see
[Sizing model](#sizing-model) at the bottom.

---

## 0. Recommended starter stack

| Layer | Pick | License | Disk (USA+EU) |
|---|---|---|---|
| Terrain, Europe | Sonny's LiDAR DTM 1″ | CC BY 4.0 | 4.8 GB (measured) |
| Terrain, USA | USGS 3DEP 1/3 arc-sec seamless | Public domain | ~100–160 GB (est.), or ~15 GB at 30 m |
| Terrain, gap-fill / global | Copernicus DEM GLO-30 | Free, open (ESA terms) | ~25–35 GB (est.) |
| Vectors (all of b) | OpenStreetMap | ODbL 1.0 | 50 GB raw pbf, ~2–5 GB filtered |
| Hydrography detail | OSM + HydroRIVERS/HydroLAKES | ODbL / CC BY 4.0 | +1.5 GB |
| Land cover | ESA WorldCover 10 m v200 → migrate to Copernicus LCFM LCM-10 | CC BY 4.0 / Copernicus open | ~8–15 GB (est.) |
| Built-up detail | Overture buildings or MS GlobalML | ODbL / CDLA-Permissive-2.0 | ~10–20 GB regional |

Total for a working dataset at 10 m terrain over Europe + 30 m over USA:
**~80 GB**. Going to 10 m terrain everywhere: **~200 GB**. Going to 1 m lidar
anywhere non-trivial: **terabytes** — do it per-AOI only.

---

## a) Elevation

### The DSM vs DTM problem (read this first)

For a hiking map this is *the* decision. A **DSM** (surface model) includes tree
canopy and buildings; a **DTM** (terrain model) is bare earth. Contours and
hillshades derived from a DSM look terrible in forest — the canopy adds 20–30 m
of noise, contours go wiggly, and hillshade turns to static. Copernicus GLO-30
and SRTM are DSMs. Lidar-derived national products are DTMs.

Any serious hiking map wants a DTM.

### Copernicus DEM GLO-30 — the global baseline

- 30 m (1 arc-second) global **DSM**, 1°×1° Cloud-Optimized GeoTIFF tiles,
  ~26,500 tiles, float32, EPSG:4326.
- Free for the general public under ESA's Copernicus DEM licence — no
  registration, no auth on S3.
- Access: `s3://copernicus-dem-30m/` (AWS Open Data, requester pays = no), also
  Microsoft Planetary Computer, OpenTopography, Google Earth Engine. Latest
  instance is the 2024_1 release.
- Gotcha: "GLO-30 Public" has a handful of countries withheld (notably Armenia,
  Azerbaijan; historically also some others). GLO-90 covers everything.
- Size: ~4.4 MB per 1000 km² uncompressed float32; ~1.5–2 MB/1000 km² as
  DEFLATE COG (est.). CONUS ≈ 36 GB uncompressed / ~15 GB compressed. Europe
  (~6M km² land) ≈ 27 GB / ~11 GB.
- https://registry.opendata.aws/copernicus-dem/

### FABDEM v1-2 — bare-earth 30 m global, but license-encumbered

- Copernicus GLO-30 with forest and building height biases removed by ML
  (University of Bristol). Genuinely the best free global *bare-earth* 30 m
  product.
- **CC BY-NC-SA 4.0.** Non-commercial only — which fits the stated constraint —
  but the **ShareAlike is viral**: hillshades, contours, and relief tiles you
  derive from it are adaptations and must be redistributed under CC BY-NC-SA
  too. That contaminates any tileset that mixes it with other layers, and it
  permanently forecloses a commercial pivot. Fathom sells commercial licenses
  (FABDEM+).
- Verdict: excellent for prototyping and for regions with no open lidar. Avoid
  as the backbone of a tileset you might want to relicense later. For Europe,
  Sonny's data (below) is both better *and* freer.
- https://data.bris.ac.uk/data/dataset/s5hqmjcdj8yo2ibzi9b4ew3sn

### Sonny's LiDAR DTMs — the answer for Europe

This is the single highest-leverage find for European hiking terrain.

- One person (Sonny, Austria) has been aggregating and normalizing national
  open lidar DTMs since 2016 into SRTM-format `.hgt` tiles.
- Coverage: Austria, Belgium, Croatia, Cyprus, Czechia, Denmark (+Faroes,
  Greenland), Estonia, Finland, France, Germany, Hungary, Iceland, Ireland,
  Italy, Latvia, Lithuania, Luxembourg, Netherlands, Norway, Poland, Portugal,
  Romania, Slovakia, Slovenia, Spain, Sweden, Switzerland, UK. (~28 countries —
  essentially all of Western/Central/Northern Europe.)
- Resolutions: 3″, 1″ (~20×30 m), 0.5″ for Alpine countries; plus 20 m and 50 m
  GeoTIFF variants.
- **CC BY 4.0** — commercial use allowed, attribution only. No ShareAlike.
- Size: **4.8 GB for all of Europe at 1″**, 1.1 GB at 3″ (measured, zipped).
- Distribution: Google Drive zips. Not scriptable in the nice way — you'll want
  to mirror it once into your own storage and treat it as a vendored input.
- Missing: Balkans beyond Croatia/Slovenia/Romania, Greece, Turkey, Ukraine,
  most of Eastern Europe. Fill those with Copernicus GLO-30 or FABDEM.
- https://sonny.4lima.de/

### USGS 3DEP — the answer for the USA

- Public domain (US federal work). No attribution required, though courtesy
  citation is normal.
- **1/3 arc-second (~10 m) seamless** — full CONUS + HI + territories, the
  practical default. ~1,900 1°×1° tiles at 10812×10812 float32.
  Size est.: ~40 MB per 1000 km² uncompressed → **~320 GB CONUS uncompressed,
  ~100–160 GB as compressed COG**.
- **1 arc-second (~30 m)** — CONUS + Alaska, the lightweight option (~15 GB est.).
- **1 m lidar-derived DEM** — 10 km × 10 km COG tiles, expanding coverage.
  As of end of FY2025, **99% of the nation has 3DEP-spec baseline lidar
  available or in progress**; initial national collection expected complete
  during 2026. There is now also a *Seamless 1 m* product.
  Size: 4 MB/km² uncompressed → **CONUS at 1 m ≈ 32 TB**. Per-AOI only.
- Also: raw **3DEP lidar point clouds** on AWS Open Data (EPT format) if you
  ever want to derive your own products.
- Bulk download is deliberately awkward (The National Map GUI, or email
  tnminfo@usgs.gov). Better paths:
  - **OpenTopography** — clips 3DEP and Copernicus to a bbox and reprojects,
    has an API. Best for AOI-driven work.
  - AWS `s3://prd-tnm/StagedProducts/Elevation/` — direct, scriptable.
- https://www.usgs.gov/3d-elevation-program/about-3dep-products-services

### National European lidar (if you want to go past Sonny)

Mostly relevant if you need sub-metre for a specific region, or a country Sonny
hasn't folded in yet.

| Country | Product | Res | License |
|---|---|---|---|
| France | IGN LiDAR HD / RGE ALTI | 0.5–1 m | Etalab 2.0 (≈CC BY) |
| Switzerland | swissALTI3D | 0.5 m | swisstopo open data, free |
| Norway | Nasjonal Høydemodell (Kartverket) | 1 m | CC BY 4.0 |
| Spain | LiDAR-PNOA (IGN) | 1–2 m | CC BY-style, free |
| Netherlands | AHN4 / AHN5 | 0.5 m | CC0 / CC BY |
| UK (Eng) | EA National LIDAR Programme | 1 m | OGL v3 |
| Scotland | Scottish Public Sector LiDAR | 0.25–1 m | OGL v3 |
| Germany | DGM per-Bundesland | 1 m | varies by state, mostly DL-DE/BY 2.0 |
| Austria | DGM per-Bundesland | 1 m | CC BY 4.0 |

Note France's LiDAR HD national programme was scheduled to complete coverage
of the country in 2026.

### Fallbacks / not recommended

- **SRTM 1″ (NASADEM)** — 30 m, 60°N–56°S, public domain. Misses Scandinavia
  and most of the Alps' northern approaches poorly; superseded by Copernicus.
- **ALOS AW3D30** — 30 m global DSM, JAXA, free with registration. Useful as a
  third opinion for void filling.
- **EU-DEM v1.1** — 25 m, Copernicus, but it's a 2016-era fusion of SRTM+ASTER
  and is worse than GLO-30. Skip.
- **Copernicus DEM EEA-10** — 10 m over 39 European countries, and it would be
  ideal, but access is **restricted to eligible institutional users**; as of
  2025/2026 it opened to "Public Authorities" via the Copernicus Data Space.
  A hobby/indie project will not qualify. Skip.

---

## b) Vectors: roads, trails, campsites, and hiking furniture

### OpenStreetMap — this is the whole answer

There is no competitive alternative for trails at continental scale. Official
government trail data exists but is patchier, less current, and inconsistently
attributed. In Europe especially, OSM is *better* than most national datasets.

- **ODbL 1.0.** Attribution required, and **ShareAlike applies to derived
  databases**. Rendered raster/vector tiles are usually treated as a "Produced
  Work" — you must attribute but need not open the database. If you distribute
  a *filtered/enriched vector database* (which a vector-tile hiking app
  arguably does), you're in Derivative Database territory and share-alike bites.
  Worth a careful read before you pick a distribution format.
- Sizes (measured, July 2026):
  - `planet.osm.pbf` — **87.1 GB** (weekly), bz2 XML 163 GB, raw XML 2.25 TB
  - Geofabrik **Europe — 32.3 GB**, **North America — 17.9 GB**
  - Per-country extracts from Geofabrik / BBBike are far smaller
- Filtering with `osmium tags-filter` down to hiking-relevant tags typically
  cuts this by 10–30×.

Tags that matter for a hiking map:

| Purpose | Tags |
|---|---|
| Trails | `highway=path/footway/bridleway/track/steps`, `route=hiking/foot` relations |
| Difficulty | `sac_scale`, `trail_visibility`, `mtb:scale`, `via_ferrata_scale` |
| Surface/quality | `surface`, `smoothness`, `width`, `incline` |
| Waymarking | `osmc:symbol`, `marked_trail:*`, `network=iwn/nwn/rwn/lwn` |
| Camping | `tourism=camp_site/caravan_site/wilderness_hut/alpine_hut/chalet`, `camp_site=*`, `backcountry=yes` |
| Shelter/water | `amenity=shelter`, `shelter_type`, `amenity=drinking_water`, `natural=spring`, `man_made=water_well` |
| Navigation aids | `information=guidepost/route_marker`, `man_made=cairn`, `tourism=viewpoint` |
| Terrain features | `natural=peak/saddle/ridge/arete/cliff/scree/glacier`, `mountain_pass=yes` |
| Hazards/crossings | `ford`, `bridge`, `barrier=gate/stile`, `access`, `seasonal` |
| Land access | `boundary=protected_area`, `leisure=nature_reserve`, `access=private/permissive` |

### Overture Maps — worth knowing, secondary for hiking

- GeoParquet on AWS + Azure, monthly releases (`2026-07-22.0`, schema v1.18.0).
- Themes GA: base, buildings, divisions, places, transportation. Addresses alpha.
- Transportation: 755.5M OSM-derived features (94.4M km) + 9.9M TomTom features.
- **License: CDLA-Permissive-2.0 in general, but ODbL for anything OSM-derived**
  — which is base, buildings, and most of transportation. So it doesn't escape
  ODbL for the layers you'd want.
- Where it wins: stable GERS IDs, conflated **places** (POIs) with a clean
  category taxonomy, conflated **buildings**, and the sheer convenience of
  querying GeoParquet with DuckDB instead of wrangling PBF.
- Where it loses: it normalizes away exactly the long-tail hiking tags
  (`sac_scale`, `osmc:symbol`) that make a hiking map good.
- Verdict: use OSM as the trail source of truth; consider Overture for
  buildings/places if you want the conflation work done for you.

### USA supplements (all public domain / open)

- **USFS National Forest System Trails** and **Roads** — FSGeodata
  Clearinghouse, shapefile + FGDB + ArcGIS services. Authoritative trail
  numbers, allowed-use matrices (hike/horse/bike/motor by season), maintenance
  level. Genuinely additive over OSM for attributes.
- **NPS** trails and park boundaries — NPS Open Data / IRMA.
- **PAD-US** (USGS Protected Areas Database) — land ownership and access status
  nationwide. This is what tells you "can I legally walk here."
- **RIDB / Recreation.gov API** — campgrounds, permits, reservations, with
  amenity detail OSM won't have.
- **GNIS** — official place names for peaks, lakes, features.

### Europe supplements

Much thinner, and usually not worth it versus OSM:

- **Natura 2000** + **CDDA** (EEA) — protected area boundaries, free.
- Waymarked Trails (waymarkedtrails.org) — not a separate dataset, it's a
  rendering of OSM route relations, but its data model for route hierarchy is
  worth copying.
- National mapping agencies (IGN France, OS GB, swisstopo) have open trail
  layers of varying quality; Switzerland's SchweizMobil network is notably
  good but licensing is restrictive.

---

## c) Rivers and lakes

### OSM — again the cartographic default

`waterway=river/stream/brook/ditch/canal`, `natural=water` + `water=lake/pond/
reservoir`, `natural=coastline`, `waterway=riverbank` (legacy). Already in your
PBF from (b) — no extra acquisition. Best for map rendering because geometry
is drawn to match imagery and names are consistent.

Weakness: no network topology, no discharge/order attributes, so you have
nothing to prioritize labels or scale line widths by at low zoom.

### HydroSHEDS family — for hierarchy and styling

- **HydroRIVERS** — global river network with Strahler order, discharge,
  upstream area. Exactly the attribute you need to decide "draw this river at
  z9, that stream only at z13."
- **HydroLAKES** — 1.4M lakes >10 ha with area/depth/volume.
  **Measured: 820 MB shapefile / 763 MB geodatabase**; pour points ~80 MB.
- **HydroATLAS** — a huge attribute join onto the above (climate, land cover,
  hydrology per reach). Optional.
- **CC BY 4.0**, explicitly free for scientific, educational *and commercial*
  use. Clean license.
- Resolution: 15 arc-second (~500 m) derived from SRTM — coarse. Use it as an
  **attribute source to join onto OSM geometry**, not as geometry.
- https://www.hydrosheds.org/products

### USA

- **NHD was retired 1 Oct 2023** — still downloadable, no longer maintained.
- **NHDPlus High Resolution** is the richest currently-usable product
  (National Release 2, Feb 2025) — 1:24,000-scale flowlines with flow
  direction, value-added attributes, catchments. Public domain.
- **3DHP (3D Hydrography Program)** is the replacement: lidar-derived
  elevation-derived hydrography, higher resolution than NHD, rolling out by
  watershed with quarterly service updates. Coverage is incomplete — check
  your AOIs. Long-term this is the right target.

### Europe

- **EU-Hydro** (Copernicus Land Monitoring Service) — river network + drainage,
  photo-interpreted from 2.5 m imagery, EEA39 coverage, free and open.
- **WISE / EEA** water body reference datasets — free.
- National hydrography (BD TOPO hydrographie in France, etc.) is usually
  better still, but per-country integration cost is high.

### Derived option

Flow accumulation from your DTM (WhiteboxTools / RichDEM / GRASS `r.watershed`)
gives you a synthetic stream network that's perfectly consistent with your
terrain and contours. Useful for filling in ephemeral drainages that OSM
doesn't map, and it costs nothing but CPU.

---

## d) Land cover / land use

### ESA WorldCover 10 m v200 — the current safe default

- Global, 10 m, 2021, Sentinel-1+2 derived. 11 classes: Tree cover, Shrubland,
  Grassland, Cropland, **Built-up**, Bare/sparse, Snow and ice, Permanent water,
  Herbaceous wetland, Mangrove, Moss/lichen. Exactly the legend a hiking map
  wants.
- 2,651 COG tiles, 3°×3°, EPSG:4326, grouped into 20°×20° zips.
- **CC BY 4.0**, no restriction of use.
- Overall accuracy 76.7% globally — good enough for "is this forest," not for
  fine vegetation typing.
- Access: `s3://esa-worldcover/` (AWS Open Data), or worldcover2021.esa.int.
- Size est.: uint8 at 10 m ≈ 10 MB/1000 km² uncompressed, ~1 MB/1000 km²
  DEFLATE-compressed (land cover RLEs extremely well). **CONUS + Europe
  ≈ 8–15 GB.**
- https://esa-worldcover.org/en/data-access

### Copernicus LCFM LCM-10 — the successor, migrate when ready

- The Copernicus Global Land Cover and Forest Monitoring service (JRC + VITO
  consortium, contract signed Nov 2023) released its first products **June
  2025**: global **10 m Land Cover Map (LCM-10) for 2020** plus a pan-tropical
  Tree Cover Density map.
- Annual LCM-10 for **2021–2025 rolling out through 2026**, plus land cover
  *change* products (LCCM-10) and monthly Land Surface Categories.
- Free and open under Copernicus principles. Access via Copernicus Data Space
  Ecosystem (S3 + Copernicus Browser).
- This is where the ecosystem is heading — WorldCover was a one-off demonstrator,
  LCFM is an operational service with annual updates. Design your pipeline so
  the land cover source is swappable.

### Europe-specific, higher quality

- **CLC+ Backbone** — 10 m raster land cover, pan-European, 3-yearly (2018 and
  later editions). Better than WorldCover over Europe.
- **Copernicus HRL Tree Cover Density** — 10 m, **0–100% continuous canopy
  density**, yearly from 2018. This is much better than a binary "tree cover"
  class for rendering — you can drive a graduated green ramp off it, or fade
  forest fill by density. Strongly recommended for European coverage.
- **HRL Imperviousness Density** — 10 m, 0–100% sealed surface. The precise
  answer to "is this patch covered by human structures," better than a
  built-up class.
- **CORINE Land Cover 2018** — 100 m raster / vector, 44 classes, 6-yearly.
  Coarse, but the *vector* form is lovely for stylized zone fills at low zoom
  because the polygons are generalized already.
- All free and open under Copernicus full/open/free access policy.

### USA-specific

- **Annual NLCD** (released Oct 2024, MRLC/USGS) — 30 m, **annual 1985–2024**
  for CONUS, six science products (land cover, change, fractional impervious,
  spectral change...). Public domain. Access via EarthExplorer, ScienceBase,
  MRLC viewer, or **USGS AWS S3 in us-west-2**.
- **NLCD Tree Canopy Cover** — 30 m percent canopy, the US analogue of HRL TCD.
- 30 m is coarser than Europe's 10 m — mixing the two means your map's forest
  edges will visibly differ in crispness across the Atlantic. Consider using
  WorldCover/LCFM 10 m *everywhere* for consistency and NLCD only as an
  attribute enrichment.

### Canopy height (optional but very nice)

- **Meta + WRI High Resolution Canopy Height Maps** — **1 m**, global,
  CC BY 4.0, on AWS Open Data + GEE. v2 (CHMv2, DINOv3-based) is
  **22.65 TB in 213,109 COGs** of 32,768² px at ~1.2 m spacing.
  Obviously per-AOI only, but at 1 m you can see individual trees, treelines,
  and forest/meadow boundaries with startling fidelity. A hiking map that
  renders actual treeline would be distinctive.
- **ETH Global Canopy Height** — 10 m, much lighter, CC BY 4.0. Better default.
- **Hansen Global Forest Change** — 30 m tree cover 2000 + annual loss/gain,
  CC BY 4.0. Use the loss layer to catch clearcuts that make your forest fill
  wrong.

### Built-up / structures

- **Overture buildings** — conflated OSM + Microsoft + Google + Esri
  footprints, GeoParquet. **ODbL** (because OSM is in the mix).
- **Microsoft GlobalMLBuildingFootprints** — **1.4B buildings, 113 GB across
  30,344 gzipped CSVs (measured)**, from Bing imagery 2014–2024. Now released
  under **CDLA-Permissive-2.0** (earlier versions were ODbL — check which
  release you pull).
- **Google Open Buildings** — CC BY 4.0, but Global South only. Not useful here.
- **GHSL (Global Human Settlement Layer)** — built-up surface fraction at 10 m
  and 100 m, plus a settlement-typology raster. Free, EC/JRC. For the
  "is this patch human-covered" question at raster scale, GHSL built-up
  surface or HRL Imperviousness is a cleaner signal than counting footprints.

---

## e) True-colour imagery

Added for the panorama, where flat land-cover colours read as a diagram rather
than a place. Two sources, layered by preference rather than chosen once —
`scripts/imagery.py` takes the sharpest one that covers each pixel and falls
back to the next, so the same code works in Washington and in the Alps.

### Sentinel-2 L2A — the base everywhere

- **10 m**, free and open (Copernicus), global. The only candidate that also
  covers the Alps, Pyrenees and Scandinavia, so it is the layer the project can
  actually standardise on.
- **Access:** STAC at `https://earth-search.aws.element84.com/v1/search`,
  collection `sentinel-2-l2a`. Anonymous, plain HTTPS, range requests work.
  Assets are COGs on `sentinel-cogs.s3.us-west-2.amazonaws.com`.
- **Use the `visual` asset**, not B04/B03/B02. It is the True Colour Image:
  already 8-bit and already stretched. Deriving RGB from raw reflectance means
  inventing a stretch that has to hold across snow, rock, conifer and water at
  once, and the ESA product does that well enough that there is nothing to win.
- **Season:** search 15 Jul – 5 Oct. Later than early October risks termination
  dust on the high country; earlier leaves snow below the icefields.
- **Alpine Lakes result:** 2023-08-15, 14 scenes over 9 MGRS tiles, all
  EPSG:32610, 100.00% coverage, 0.00% cloud. One date means no seams — no
  differing sun angle, snowline or haze across the mosaic. 771 MB at 10 m over
  the buffered box; 18,202 JPEG tiles to z14.
- Attribution: "Contains modified Copernicus Sentinel data".

### NAIP — the United States only, sixteen times sharper

- **0.6 m native**, public domain (USDA). Flown state by state on a multi-year
  cycle. Downloaded here at **2 m**, which is the useful ceiling: it is well
  past what a 30 m DEM can shape, and it is the panorama's near-field texture
  that consumes it.
- **Access:** Planetary Computer STAC,
  `https://planetarycomputer.microsoft.com/api/stac/v1/search`, collection
  `naip`, assets signed with a free account-less SAS token from
  `/api/sas/v1/token/naip`.
- **Not Earth Search for NAIP.** It also carries a `naip` collection, but its
  asset hrefs are `s3://naip-analytic/...` — a **requester-pays** bucket, which
  needs AWS credentials and bills the caller.
- **Quad selection:** summer acquisitions beat recency. A late-October quad
  carries fresh snow that an August quad two years older does not.
- **Cost:** ~187 quads for the core AOI, ~35 minutes at 12 concurrent
  connections. Native resolution over this AOI would be ~40 GB; at 2 m through
  the COG overviews it is a small fraction of that.

### Ruled out

- **Google, Bing, Mapbox, Esri World Imagery, Apple** — terms forbid bulk
  download, tile scraping and derivative rasters. Unusable regardless of how
  good they look.

---

## Sizing model

For estimating any raster before you download it:

```
bytes = area_km² × 1e6 / (resolution_m²) × bytes_per_pixel
```

Per 1000 km², uncompressed:

| Resolution | uint8 | int16 | float32 |
|---|---|---|---|
| 1 m | 1000 MB | 2000 MB | 4000 MB |
| 10 m | 10 MB | 20 MB | 40 MB |
| 30 m | 1.1 MB | 2.2 MB | 4.4 MB |

Compression factors observed in practice: elevation DEFLATE/LERC ≈ 2.5–4×,
categorical land cover DEFLATE ≈ 8–15×.

Reference areas: CONUS 8.08M km², USA incl. Alaska 9.8M km², EU-27 4.2M km²,
"Europe west of Ukraine incl. Norway/UK/Balkans" ≈ 6M km².

Sanity check on the model: Sonny's Europe at 1″ is int16 → 6M km² × 2.2 MB/1000km²
= 13 GB uncompressed, and the published zips total 4.8 GB. ✓

---

## Licensing map — the thing to get right early

Three license families are in play and they don't mix freely:

1. **Public domain / CC0** — US federal (3DEP, NLCD, NHD, USFS, NPS, PAD-US,
   GNIS). Do anything.
2. **Attribution-only (CC BY 4.0, Etalab, OGL, CDLA-Permissive)** — Sonny,
   HydroSHEDS, ESA WorldCover, Copernicus products, Meta/WRI canopy, MS
   buildings (current release). Keep an attributions page; otherwise free.
3. **ShareAlike (ODbL)** — OpenStreetMap, Overture base/buildings/transportation.
   Attribution *plus* obligations on derived databases.
4. **Non-commercial ShareAlike (CC BY-NC-SA)** — FABDEM only. Viral and
   commercially terminal.

Practical consequences:

- Rasters (hillshade, contours, land cover fills) derived from family 1–2 are
  yours. Keep those pipelines free of FABDEM if you can.
- The moment OSM enters, your **vector tiles** are the question mark. Rendered
  raster tiles are a Produced Work (attribute and move on). A distributable
  vector tileset that preserves OSM attributes is closer to a Derivative
  Database — plan to publish it under ODbL, which is fine, just decide
  deliberately.
- If you use FABDEM, isolate it to a layer you can swap out. Sonny (Europe) +
  3DEP (USA) + GLO-30 (gaps) covers the target regions with no NC clause at
  all, which makes FABDEM largely unnecessary for this project.

---

## Tooling

- **GDAL** — everything raster. `gdalbuildvrt` + `gdal_translate -co COMPRESS=`
  for mosaicking DEM tiles; `gdaldem hillshade/slope`; `gdal_contour`.
- **osmium-tool** — `tags-filter`, `extract` for carving hiking-relevant data
  out of a continental PBF fast.
- **DuckDB + spatial extension** — query Overture GeoParquet directly off S3
  without downloading it.
- **Planetiler** — builds a planet vector tileset from OSM in a few hours on a
  workstation. The realistic path to your own basemap.
- **tippecanoe** — vector tiles from arbitrary GeoJSON.
- **PMTiles** — single-file tile archive, HTTP range requests, no tile server.
  Protomaps publishes daily planet builds at ~**120 GB for z0–15**; each extra
  zoom level roughly doubles size, and `pmtiles extract` pulls a region out of
  the remote planet without downloading it. Excellent reference point for what
  a finished basemap costs on disk.
- **WhiteboxTools / RichDEM / GRASS** — hydrological derivation from DEMs.

---

## Open questions to resolve when the project has a shape

- Distribution model (server-rendered raster / self-hosted vector tiles /
  offline bundles) determines how much the ODbL share-alike question matters.
- Whether a commercial option needs to stay open — if yes, FABDEM is out
  entirely and the answer above already avoids needing it.
- Target zoom range — z15 vs z17 is a ~4× difference in tile storage.
- Whether terrain is served as tiles (Terrarium/Mapbox RGB-encoded PNG) or
  pre-rendered hillshade. RGB terrain tiles for CONUS+Europe at z12 are
  roughly 20–40 GB.

## Topographic prominence

**Kirmse & de Ferranti global prominence** — every summit on earth with at
least 100 ft (30.5 m) of prominence, derived from SRTM and viewfinder DEMs.

| | |
|---|---|
| Source | <http://www.andrewkirmse.com/prominence> (code: <https://github.com/akirmse/mountains>) |
| Citation | Kirmse, A. & de Ferranti, J. (2017). *Calculating the prominence and isolation of every mountain in the world.* Progress in Physical Geography 41(6). |
| Format | `lat, lon, elev_ft, saddle_lat, saddle_lon, prominence_ft` |
| Size | 112 MB zipped (global), `AllPeaksProminenceP100Sorted.zip` |
| Access | Google Drive; large-file downloads need the confirm-token step |
| **Licence** | **None stated.** See the caveat below. |

Used for panorama peak selection (`scripts/prominence.py` joins it to our OSM
summits; see the README's "Peak labels").

⚠️ **The dataset carries no stated licence.** The page was checked and contains
no terms — the only licence text on it belongs to Google's own JavaScript. It is
publicly downloadable and universally cited in the prominence community, and we
use it on that basis with attribution, but this is weaker footing than the
CC BY / public-domain sources everything else here rests on. The alternative, if
that ever matters, is to recompute from our own DEM: the algorithm is a
sort-by-elevation plus union-find over the raster, tractable in one pass over a
single AOI.
