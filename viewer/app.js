/* Alpine Lakes raw-data viewer.
 *
 * Rasters come from an XYZ PNG pyramid, so the browser only ever fetches the
 * tiles currently on screen. Vectors are pre-simplified into zoom tiers and
 * swapped as you zoom, so we never hand the browser a 100 MB GeoJSON.
 */

const AOI = 'alpine-lakes';
const DATA_ROOT = `/data/web/${AOI}/`;

// zoom -> vector detail tier
const TIER_BREAKS = [
  { maxZoom: 9, tier: 'low' },
  { maxZoom: 11, tier: 'mid' },
  { maxZoom: 99, tier: 'high' },
];
const TIER_ORDER = ['low', 'mid', 'high'];

// Themes that should be *hidden* below a tier rather than fall back to a finer
// one. `resolveTier` climbs upward when nothing coarse enough exists, which is
// what `passes` and `poi` want — they only ship at `high` and should still be
// drawn when zoomed out. Trails are the opposite case: dropping `trails.low`
// to declutter z8 instead promoted them to the full 20k-feature `mid` tier,
// which is strictly worse than what it replaced.
const TIER_MIN_VISIBLE = { trails: 'mid' };

const RAMP_CSS = {
  hillshade: 'linear-gradient(90deg,#000,#fff)',
  dem: 'linear-gradient(90deg,#386842,#7a9c58,#c4c27c,#bc986a,#967a68,#ceced4,#fff)',
  slope: 'linear-gradient(90deg,#ffffe5,#fed98e,#fe9929,#d9481a,#7f1612)',
  ruggedness: 'linear-gradient(90deg,#440154,#3b528b,#21918c,#5ec962,#fde725)',
  relief_sigma: 'linear-gradient(90deg,#0c0723,#550f6d,#aa3375,#f3715c,#fcfdbf)',
  canopy_density: 'linear-gradient(90deg,#f7f6ee,#b0cd9e,#4f8f4f,#104a20)',
  builtup_density: 'linear-gradient(90deg,#fafafa,#f5cdbe,#e2745a,#b01414)',
};

const DEFAULT_ON = ['hillshade', 'dem'];
// Imagery is a base image, not a measurement painted over one, so it goes on
// opaque. NAIP sits above Sentinel-2 wherever it exists.
const DEFAULT_OPACITY = { hillshade: 1.0, dem: 0.75, imagery_s2: 1.0, imagery_naip: 1.0 };

const VECTOR_ORDER = [
  'protected', 'glaciers', 'water_areas', 'water_lines',
  'roads', 'trails', 'passes', 'poi', 'peaks',
];

// ---------------------------------------------------------------------------
// Surface quality -> dash pattern.
//
// Colour and width say what *kind* of way it is; the dash pattern says how
// improved it is. Those are independent axes, so they're computed separately:
// a paved track and a dirt state highway both exist.
//
// Level 0 is solid (paved); each step up is a rougher way, drawn with
// progressively more gap than stroke so the line reads as more intermittent.
const DASH_BY_IMPROVEMENT = [
  null,       // 0  paved
  '13,3',     // 1  compacted / good gravel
  '9,4',      // 2  gravel, ordinary dirt track
  '5,5',      // 3  dirt / ground / faint
  '2.5,6',    // 4  sand, grass, barely a trace
];

const IMPROVEMENT_LABELS = ['Paved', 'Compacted / good gravel',
  'Gravel / ordinary track', 'Dirt / faint', 'Trace, barely a way'];

const SURFACE_IMPROVEMENT = {
  paved: 0, asphalt: 0, concrete: 0, 'concrete:plates': 0, chipseal: 0,
  paving_stones: 0, sett: 0, metal: 0, wood: 0,
  compacted: 1, fine_gravel: 1,
  gravel: 2, pebblestone: 2, unpaved: 2, cobblestone: 2,
  dirt: 3, ground: 3, earth: 3, mud: 3,
  sand: 4, grass: 4, rock: 4, woodchips: 4,
};
const TRACKTYPE_IMPROVEMENT = { grade1: 0, grade2: 1, grade3: 2, grade4: 3, grade5: 4 };
const VISIBILITY_IMPROVEMENT = {
  excellent: 0, good: 1, intermediate: 2, bad: 3, horrible: 4, no: 4,
};
// sac_scale is nominally difficulty, but it tracks how built-up a trail is
// closely enough to be a useful fallback when surface/visibility are absent
const SAC_IMPROVEMENT = {
  hiking: 1, mountain_hiking: 2, demanding_mountain_hiking: 2,
  alpine_hiking: 3, demanding_alpine_hiking: 4, difficult_alpine_hiking: 4,
};

// OSM convention treats these as surfaced unless tagged otherwise; only
// highway=track carries an unpaved assumption.
const PAVED_BY_DEFAULT = new Set(['motorway', 'trunk', 'primary', 'secondary',
  'tertiary', 'residential', 'unclassified', 'service', 'living_street']);

function roadImprovement(p) {
  const s = SURFACE_IMPROVEMENT[p.surface];
  if (s === 0) return 0;                       // explicitly paved wins
  const cand = [];
  if (s !== undefined) cand.push(s);
  if (TRACKTYPE_IMPROVEMENT[p.tracktype] !== undefined) {
    cand.push(TRACKTYPE_IMPROVEMENT[p.tracktype]);
  }
  if (cand.length) return Math.max(...cand);
  if (p.highway === 'track') return 2;         // tracks are unpaved unless said
  return PAVED_BY_DEFAULT.has((p.highway || '').replace(/_link$/, '')) ? 0 : 1;
}

function trailImprovement(p) {
  const s = SURFACE_IMPROVEMENT[p.surface];
  if (s === 0) return 0;                       // a paved greenway is solid
  const cand = [];
  if (s !== undefined) cand.push(s);
  if (VISIBILITY_IMPROVEMENT[p.trail_visibility] !== undefined) {
    cand.push(VISIBILITY_IMPROVEMENT[p.trail_visibility]);
  }
  if (SAC_IMPROVEMENT[p.sac_scale] !== undefined) {
    cand.push(SAC_IMPROVEMENT[p.sac_scale]);
  }
  // the roughest signal wins — a trail is only as improved as its worst part
  if (cand.length) return Math.max(...cand);
  return (p.highway === 'footway' || p.highway === 'steps') ? 1 : 2;
}

// Per-theme category styling. Each theme has a `classes` table plus a
// `classify(props)` that picks one; `_default` catches the long tail so the
// tables only need to cover what's actually common.
// Roads: one colour for sealed, one for not. Width still comes from the class.
const ROAD_PAVED = '#3b2266';
const ROAD_UNPAVED = '#8a5a33';

const VECTOR_STYLE = {
  protected: {
    label: 'Protected areas', kind: 'poly', color: '#6ddf8f',
    classify: (p) => (
      p.boundary === 'national_park' ? 'national_park'
        : p.leisure === 'nature_reserve' ? 'nature_reserve'
          : /wilderness/i.test(p.name || '') || p.protect_class === '1b' ? 'wilderness'
            : '_default'),
    classes: {
      wilderness: { label: 'Wilderness', color: '#8ef0b0', weight: 1.8 },
      national_park: { label: 'National park', color: '#5fd18a', weight: 1.8 },
      nature_reserve: { label: 'Nature reserve', color: '#6ddf8f', weight: 1.2, dashArray: '5,3' },
      _default: { label: 'Other protected', color: '#5fae86', weight: 1.0, dashArray: '3,3' },
    },
  },
  glaciers: {
    label: 'Glaciers', kind: 'poly', color: '#cdf0f5',
    classify: () => '_default',
    classes: {
      _default: { label: 'Glacier', color: '#bfefff', weight: 0.8, fill: '#d7f2f8', fillOpacity: 0.55 },
    },
  },
  water_areas: {
    label: 'Lakes & reservoirs', kind: 'poly', color: '#2f6f9f',
    classify: (p) => (
      p.landuse === 'reservoir' || p.water === 'reservoir' ? 'reservoir'
        : ['pond', 'lagoon', 'basin'].includes(p.water) ? 'pond'
          : p.water === 'river' ? 'riverbank' : 'lake'),
    classes: {
      lake: { label: 'Lake', color: '#6bb6e8', weight: 0.9, fill: '#2f6f9f', fillOpacity: 0.8 },
      reservoir: { label: 'Reservoir', color: '#6bb6e8', weight: 0.9, fill: '#2b5f86', fillOpacity: 0.8, dashArray: '5,3' },
      pond: { label: 'Pond / basin', color: '#7fc4ee', weight: 0.7, fill: '#3b82ad', fillOpacity: 0.75 },
      riverbank: { label: 'River surface', color: '#5aa9df', weight: 0.6, fill: '#356f96', fillOpacity: 0.7 },
      _default: { label: 'Water', color: '#6bb6e8', weight: 0.8, fill: '#2f6f9f', fillOpacity: 0.75 },
    },
  },
  water_lines: {
    label: 'Rivers & streams', kind: 'line', color: '#4a9fe0',
    classify: (p) => (['river', 'canal', 'stream'].includes(p.waterway) ? p.waterway : '_default'),
    classes: {
      river: { label: 'River', color: '#4a9fe0', weight: 2.4 },
      canal: { label: 'Canal', color: '#4a9fe0', weight: 1.7, dashArray: '7,3' },
      stream: { label: 'Stream', color: '#6fb8ea', weight: 1.0 },
      _default: { label: 'Ditch / drain', color: '#7c9fb8', weight: 0.7, dashArray: '3,3' },
    },
  },
  // Road-atlas palette: blue interstates, red US/state highways, magenta
  // secondary, greys below that.
  roads: {
    // Surface sets the colour, class sets the width — two independent facts
    // shown on two independent channels. Every paved road is the same dark
    // purple and solid; every unpaved one is brown and dashed. Ramping colour
    // by class as well made the two questions ("is this sealed?" and "how big
    // is it?") compete for the same visual variable.
    label: 'Roads', kind: 'line', color: ROAD_PAVED,
    improvement: roadImprovement,
    classify: (p) => {
      const h = (p.highway || '').replace(/_link$/, '');
      return VECTOR_STYLE.roads.classes[h] ? h : '_default';
    },
    classes: {
      motorway: { label: 'Interstate / freeway', color: ROAD_PAVED, weight: 3.8 },
      trunk: { label: 'US / divided highway', color: ROAD_PAVED, weight: 3.2 },
      primary: { label: 'State highway', color: ROAD_PAVED, weight: 2.6 },
      secondary: { label: 'Secondary road', color: ROAD_PAVED, weight: 2.0 },
      tertiary: { label: 'Local through road', color: ROAD_PAVED, weight: 1.6 },
      // the reference width: an ordinary street, and what a plain unpaved road
      // matches so the two read as the same rank in different surfaces
      residential: { label: 'Residential', color: ROAD_PAVED, weight: 1.3 },
      unclassified: { label: 'Unclassified', color: ROAD_PAVED, weight: 1.3 },
      service: { label: 'Service', color: ROAD_PAVED, weight: 0.9 },
      track: { label: 'Track', color: ROAD_UNPAVED, weight: 0.9 },
      _default: { label: 'Other road', color: ROAD_PAVED, weight: 1.3 },
    },
  },
  // Trails in CalTopo red. Difficulty is carried by the dash pattern via
  // trailImprovement, so the colour stays within the red family and the
  // sub-types differ only in shade and width.
  trails: {
    label: 'Trails & paths', kind: 'line', color: '#d6332b',
    improvement: trailImprovement,
    classify: (p) => (VECTOR_STYLE.trails.classes[p.highway] ? p.highway : '_default'),
    classes: {
      path: { label: 'Trail', color: '#d6332b', weight: 1.7 },
      footway: { label: 'Footpath', color: '#e2635a', weight: 1.3 },
      bridleway: { label: 'Bridleway', color: '#b2453e', weight: 1.4 },
      steps: { label: 'Steps', color: '#9d241f', weight: 2.1 },
      track: { label: 'Track (old road)', color: '#a3844f', weight: 1.4 },
      _default: { label: 'Other path', color: '#d6332b', weight: 1.3 },
    },
  },
  passes: {
    label: 'Passes & saddles', kind: 'point', color: '#e08cff',
    classify: (p) => (p.mountain_pass === 'yes' ? 'pass' : 'saddle'),
    classes: {
      pass: { label: 'Mountain pass', color: '#e08cff', shape: 'bowtie', radius: 5 },
      saddle: { label: 'Saddle', color: '#b98ad6', shape: 'bowtie', radius: 4 },
      _default: { label: 'Pass', color: '#e08cff', shape: 'bowtie', radius: 4 },
    },
  },
  poi: {
    label: 'Huts, camps, viewpoints', kind: 'point', color: '#ff7b7b',
    classify: (p) => (
      p.tourism === 'viewpoint' ? 'viewpoint'
        : p.tourism === 'camp_site' ? 'camp_site'
          : (p.tourism === 'alpine_hut' || p.tourism === 'wilderness_hut') ? 'hut'
            : p.amenity === 'shelter' ? 'shelter'
              : (p.amenity === 'drinking_water' || p.natural === 'spring') ? 'water'
                : '_default'),
    classes: {
      viewpoint: { label: 'Viewpoint', color: '#ffd98a', shape: 'diamond', radius: 5 },
      camp_site: { label: 'Campsite', color: '#7fd28f', shape: 'tent', radius: 5 },
      hut: { label: 'Hut', color: '#ff9d6e', shape: 'house', radius: 5 },
      shelter: { label: 'Shelter', color: '#d5a6ff', shape: 'square', radius: 4 },
      water: { label: 'Spring / water', color: '#6fc7ff', shape: 'drop', radius: 4.5 },
      _default: { label: 'Other', color: '#ff7b7b', shape: 'circle', radius: 3.5 },
    },
  },
  peaks: {
    label: 'Peaks', kind: 'point', color: '#ffd166',
    classify: (p) => (p.natural === 'volcano' ? 'volcano'
      : _peakEle(p) >= 2000 ? 'high' : '_default'),
    classes: {
      volcano: { label: 'Volcano', color: '#ff7043', shape: 'triangle', radius: 6 },
      high: { label: 'Peak ≥ 2000 m', color: '#ffe08a', shape: 'triangle', radius: 5 },
      _default: { label: 'Peak', color: '#ffd166', shape: 'triangle', radius: 3.6 },
    },
  },
};

function _peakEle(p) {
  const v = parseFloat(String(p.ele || '').split(' ')[0]);
  return Number.isFinite(v) ? v : 0;
}

function classOf(theme, props) {
  const s = VECTOR_STYLE[theme];
  const key = s.classify ? s.classify(props || {}) : '_default';
  return s.classes[key] || s.classes._default;
}

const DEFAULT_VECTORS_ON = ['trails', 'water_areas', 'roads'];

// ---------------------------------------------------------------------------

// Custom point symbols, drawn straight onto the canvas renderer. Using
// L.divIcon markers instead would mean thousands of DOM nodes at high zoom;
// subclassing CircleMarker keeps canvas performance and inherits its hit
// testing for free.
L.Canvas.include({
  _updateShape(layer) {
    if (!this._drawing || layer._empty()) return;
    const p = layer._point;
    const ctx = this._ctx;
    const r = layer._radius;
    ctx.beginPath();
    switch (layer.options.shape) {
      case 'triangle':
        ctx.moveTo(p.x, p.y - r * 1.3);
        ctx.lineTo(p.x + r * 1.15, p.y + r * 0.8);
        ctx.lineTo(p.x - r * 1.15, p.y + r * 0.8);
        ctx.closePath();
        break;
      case 'tent':   // triangle with a door notch, so it reads apart from a peak
        ctx.moveTo(p.x, p.y - r * 1.3);
        ctx.lineTo(p.x + r * 1.25, p.y + r * 0.9);
        ctx.lineTo(p.x + r * 0.32, p.y + r * 0.9);
        ctx.lineTo(p.x, p.y + r * 0.1);
        ctx.lineTo(p.x - r * 0.32, p.y + r * 0.9);
        ctx.lineTo(p.x - r * 1.25, p.y + r * 0.9);
        ctx.closePath();
        break;
      case 'diamond':
        ctx.moveTo(p.x, p.y - r * 1.3);
        ctx.lineTo(p.x + r * 1.1, p.y);
        ctx.lineTo(p.x, p.y + r * 1.3);
        ctx.lineTo(p.x - r * 1.1, p.y);
        ctx.closePath();
        break;
      case 'square':
        ctx.rect(p.x - r * 0.95, p.y - r * 0.95, r * 1.9, r * 1.9);
        break;
      case 'house':
        ctx.moveTo(p.x, p.y - r * 1.35);
        ctx.lineTo(p.x + r, p.y - r * 0.2);
        ctx.lineTo(p.x + r, p.y + r);
        ctx.lineTo(p.x - r, p.y + r);
        ctx.lineTo(p.x - r, p.y - r * 0.2);
        ctx.closePath();
        break;
      case 'bowtie':  // two peaks meeting at a col
        ctx.moveTo(p.x - r * 1.3, p.y + r * 0.9);
        ctx.lineTo(p.x - r * 0.45, p.y - r * 0.9);
        ctx.lineTo(p.x, p.y + r * 0.15);
        ctx.lineTo(p.x + r * 0.45, p.y - r * 0.9);
        ctx.lineTo(p.x + r * 1.3, p.y + r * 0.9);
        ctx.closePath();
        break;
      case 'drop':
        ctx.moveTo(p.x, p.y - r * 1.35);
        ctx.quadraticCurveTo(p.x + r * 1.25, p.y + r * 0.3, p.x, p.y + r * 1.2);
        ctx.quadraticCurveTo(p.x - r * 1.25, p.y + r * 0.3, p.x, p.y - r * 1.35);
        ctx.closePath();
        break;
      default:
        ctx.arc(p.x, p.y, r, 0, Math.PI * 2, false);
    }
    this._fillStroke(ctx, layer);
  },
});

const ShapeMarker = L.CircleMarker.extend({
  options: { shape: 'circle' },
  _updatePath() {
    if (this._renderer._updateShape) this._renderer._updateShape(this);
    else this._renderer._updateCircle(this);   // SVG fallback
  },
});

// `tolerance` pads the hit area used for hover, tooltips and clicks. At the
// default of 0 a 1 px trail is essentially unhittable.
const HIT_TOLERANCE_PX = 12;
const vectorRenderer = L.canvas({ tolerance: HIT_TOLERANCE_PX, padding: 0.3 });
// The view wedge redraws on every panorama frame. Sharing `vectorRenderer`
// meant each of those forced a full repaint of the canvas holding 70k roads
// and 28k trails, at a cost proportional to map area — which is exactly why
// the panorama got choppier the larger the map pane was. Its own canvas keeps
// that repaint to three shapes.
const panoOverlayRenderer = L.canvas({ padding: 0.3 });

const map = L.map('map', {
  preferCanvas: true,
  renderer: vectorRenderer,
  zoomControl: true,
  minZoom: 6,
  maxZoom: 15,
});

L.control.scale({ imperial: true, metric: true, position: 'bottomleft' }).addTo(map);
map.attributionControl.setPrefix('');

// appended to every tile / vector URL so that regenerating the data busts the
// browser cache, which otherwise holds them for a day
let CACHE_BUST = '';

const rasterLayers = {};   // id -> L.TileLayer
const vectorLayers = {};   // theme -> { group, tiers, currentTier, enabled, cache }
let manifest = null;

// exposed for console poking / automated checks
window.mapView = { map, rasterLayers, vectorLayers, get manifest() { return manifest; } };

// ---------------------------------------------------------------------------
// helpers

function tierForZoom(z) {
  for (const b of TIER_BREAKS) if (z <= b.maxZoom) return b.tier;
  return 'high';
}

/** Nearest available tier at or below the requested detail level. */
function resolveTier(available, wanted) {
  const names = available.map((t) => t.tier);
  if (names.includes(wanted)) return wanted;
  const wantIdx = TIER_ORDER.indexOf(wanted);
  for (let i = wantIdx; i >= 0; i--) if (names.includes(TIER_ORDER[i])) return TIER_ORDER[i];
  for (let i = wantIdx; i < TIER_ORDER.length; i++) if (names.includes(TIER_ORDER[i])) return TIER_ORDER[i];
  return names[0];
}

function el(tag, cls, html) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
}

function tagTable(props) {
  const keys = Object.keys(props || {}).sort();
  if (!keys.length) return '<em>no tags</em>';
  const name = props.name ? `<h4>${escapeHtml(props.name)}</h4>` : '';
  const rows = keys
    .filter((k) => k !== 'name')
    .map((k) => `<tr><td>${escapeHtml(k)}</td><td>${escapeHtml(String(props[k]))}</td></tr>`)
    .join('');
  return `${name}<table>${rows}</table>`;
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

// ---------------------------------------------------------------------------
// rasters

function buildRasters(layers) {
  const list = document.getElementById('raster-list');
  layers.forEach((spec) => {
    const opacity = DEFAULT_OPACITY[spec.id] ?? 0.75;
    const layer = L.tileLayer(DATA_ROOT + spec.url + CACHE_BUST, {
      minZoom: 6,
      maxZoom: 15,
      minNativeZoom: spec.minzoom,
      maxNativeZoom: spec.maxzoom,
      opacity,
      tileSize: 256,
      attribution: spec.attribution || '',
      className: `raster-${spec.id}`,
    });
    rasterLayers[spec.id] = layer;

    const on = DEFAULT_ON.includes(spec.id);
    const row = el('div', `layer${on ? '' : ' off'}`);

    const head = el('label', 'layer-head');
    const cb = el('input');
    cb.type = 'checkbox';
    cb.checked = on;
    head.appendChild(cb);
    head.appendChild(el('span', 'name', spec.title));
    head.appendChild(el('span', 'count', `${spec.tiles.toLocaleString()} tiles`));
    row.appendChild(head);

    if (spec.description) row.appendChild(el('div', 'layer-desc', spec.description));

    const opRow = el('div', 'opacity');
    const slider = el('input');
    slider.type = 'range';
    slider.min = 0; slider.max = 100; slider.value = Math.round(opacity * 100);
    const val = el('span', null, `${Math.round(opacity * 100)}%`);
    opRow.appendChild(slider);
    opRow.appendChild(val);
    row.appendChild(opRow);

    slider.addEventListener('input', () => {
      layer.setOpacity(slider.value / 100);
      val.textContent = `${slider.value}%`;
    });
    cb.addEventListener('change', () => {
      row.classList.toggle('off', !cb.checked);
      if (cb.checked) layer.addTo(map); else map.removeLayer(layer);
      renderLegend();
    });

    if (on) layer.addTo(map);
    list.appendChild(row);
  });
  // keep a stable stacking order: first in the manifest sits lowest
  layers.forEach((spec) => {
    const l = rasterLayers[spec.id];
    if (map.hasLayer(l)) l.bringToFront();
  });
}

// ---------------------------------------------------------------------------
// vectors

function styleFor(theme, props) {
  const s = VECTOR_STYLE[theme];
  const c = classOf(theme, props);
  const st = {
    color: c.color,
    weight: c.weight ?? 1.2,
    dashArray: c.dashArray,
    fill: false,
  };
  // themes with an improvement function derive their dashes per feature:
  // solid when paved, increasingly intermittent as the way gets rougher
  if (s.improvement) {
    const lvl = s.improvement(props || {});
    st.dashArray = DASH_BY_IMPROVEMENT[Math.max(0, Math.min(4, lvl))] || undefined;
    // roads recolour on surface rather than on class; trails keep their own
    if (theme === 'roads') st.color = lvl === 0 ? ROAD_PAVED : ROAD_UNPAVED;
  }
  if (s.kind === 'poly') {
    st.fill = !!c.fill;
    st.fillColor = c.fill || undefined;
    st.fillOpacity = c.fillOpacity ?? 0.5;
  }
  return st;
}

function makeGeoJsonLayer(theme, data) {
  const s = VECTOR_STYLE[theme];
  const opts = {
    renderer: vectorRenderer,
    pointToLayer: (f, latlng) => {
      const c = classOf(theme, f.properties);
      return new ShapeMarker(latlng, {
        shape: c.shape || 'circle',
        radius: c.radius || 4,
        color: '#11141a',
        weight: 1,
        opacity: 0.85,
        fillColor: c.color,
        fillOpacity: 0.95,
      });
    },
    onEachFeature: (f, lyr) => {
      // Point themes also contain the occasional area feature (a campsite or
      // hut mapped as a way). Those become paths, which the point branch never
      // styles, so give them the class colour rather than Leaflet's default.
      if (s.kind === 'point' && !(lyr instanceof L.CircleMarker) && lyr.setStyle) {
        const c = classOf(theme, f.properties);
        lyr.setStyle({
          color: c.color, weight: 1.4,
          fill: true, fillColor: c.color, fillOpacity: 0.3,
        });
      }
      lyr.bindPopup(tagTable(f.properties), { maxWidth: 320 });
      if (f.properties && f.properties.name) {
        lyr.bindTooltip(f.properties.name, { direction: 'top', opacity: 0.9 });
      }
    },
  };
  // L.GeoJSON applies `style` to every layer exposing setStyle(), which
  // includes the markers pointToLayer just built. Passing a line/polygon
  // style here silently overrode their fill and weight and rendered every
  // point layer invisible — so only set it for non-point themes.
  if (s.kind !== 'point') opts.style = (f) => styleFor(theme, f.properties);
  const layer = L.geoJSON(data, opts);
  // remember which categories this chunk actually contains, so the legend can
  // list only what's on screen rather than every class the table defines
  layer._classKeys = new Set(
    (data.features || []).map((f) => (
      s.classify ? s.classify(f.properties || {}) : '_default')));
  return layer;
}

/** Which grid cells of a tier intersect the current viewport. */
function visibleCells(entry) {
  const g = entry.grid;
  const [w, s, e, n] = g.bounds;
  const b = map.getBounds();
  const dx = (e - w) / g.cols;
  const dy = (n - s) / g.rows;
  const clamp = (v, hi) => Math.max(0, Math.min(hi - 1, v));
  const c0 = clamp(Math.floor((b.getWest() - w) / dx), g.cols);
  const c1 = clamp(Math.floor((b.getEast() - w) / dx), g.cols);
  const r0 = clamp(Math.floor((b.getSouth() - s) / dy), g.rows);
  const r1 = clamp(Math.floor((b.getNorth() - s) / dy), g.rows);
  const out = [];
  for (let r = r0; r <= r1; r++) {
    for (let c = c0; c <= c1; c++) {
      const name = `r${r}c${c}`;
      if (g.cells.includes(name)) out.push(name);
    }
  }
  return out;
}

/** Fetch one (tier, cell) chunk, memoised. */
async function loadChunk(theme, entry, cell) {
  const state = vectorLayers[theme];
  const key = `${entry.tier}|${cell || '_'}`;
  if (state.cache[key]) return state.cache[key];
  const url = DATA_ROOT + (cell
    ? entry.pattern.replace('{cell}', cell)
    : entry.file) + CACHE_BUST;
  const pending = fetch(url)
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
    .then((data) => makeGeoJsonLayer(theme, data))
    .catch((err) => { console.warn('vector load failed', url, err); return null; });
  state.cache[key] = pending;
  return pending;
}

/** Reconcile what's on the map with what the current zoom/viewport needs. */
async function syncVectorTier(theme) {
  const state = vectorLayers[theme];
  if (!state || !state.enabled) return;

  const zTier = tierForZoom(map.getZoom());
  const floor = TIER_MIN_VISIBLE[theme];
  if (floor && TIER_ORDER.indexOf(zTier) < TIER_ORDER.indexOf(floor)) {
    for (const [key, layer] of state.onMap) {
      state.group.removeLayer(layer);
      state.onMap.delete(key);
    }
    state.currentTier = null;
    state.cellCount = 0;
    updateStatus();
    renderLegend();
    return;
  }

  const wanted = resolveTier(state.tiers, zTier);
  const entry = state.tiers.find((t) => t.tier === wanted);
  if (!entry) return;

  const cells = entry.grid ? visibleCells(entry) : [null];
  const wantKeys = new Set(cells.map((c) => `${wanted}|${c || '_'}`));

  // drop anything from a different tier or an off-screen cell
  for (const [key, layer] of state.onMap) {
    if (!wantKeys.has(key)) {
      state.group.removeLayer(layer);
      state.onMap.delete(key);
    }
  }

  const token = ++state.token;
  await Promise.all(cells.map(async (cell) => {
    const key = `${wanted}|${cell || '_'}`;
    if (state.onMap.has(key)) return;
    const layer = await loadChunk(theme, entry, cell);
    // a later zoom/pan superseded this request, or the layer was disabled
    if (!layer || token !== state.token || !state.enabled) return;
    if (!wantKeys.has(key)) return;
    state.group.addLayer(layer);
    state.onMap.set(key, layer);
  }));

  state.currentTier = wanted;
  state.cellCount = cells.filter(Boolean).length;
  updateStatus();
  renderLegend();
}

function buildVectors(vlayers) {
  const list = document.getElementById('vector-list');
  const themes = VECTOR_ORDER.filter((t) => vlayers[t] && vlayers[t].length);
  if (!themes.length) {
    list.appendChild(el('div', 'layer-desc', 'No vector layers in manifest yet.'));
    return;
  }
  themes.forEach((theme) => {
    const s = VECTOR_STYLE[theme];
    const group = L.layerGroup();
    const tiers = vlayers[theme];
    vectorLayers[theme] = {
      group,
      tiers,
      cache: {},          // "tier|cell" -> Promise<L.GeoJSON>
      onMap: new Map(),   // "tier|cell" -> layer currently added
      token: 0,           // guards against out-of-order async loads
      currentTier: null,
      cellCount: 0,
      enabled: DEFAULT_VECTORS_ON.includes(theme),
    };

    const maxFeatures = Math.max(...tiers.map((t) => t.features));
    const row = el('div', `layer${vectorLayers[theme].enabled ? '' : ' off'}`);
    const head = el('label', 'layer-head');
    const cb = el('input');
    cb.type = 'checkbox';
    cb.checked = vectorLayers[theme].enabled;
    const sw = el('span', 'swatch');
    sw.style.background = s.color;
    sw.style.borderColor = s.color;
    head.appendChild(cb);
    head.appendChild(sw);
    head.appendChild(el('span', 'name', s.label));
    head.appendChild(el('span', 'count', maxFeatures.toLocaleString()));
    row.appendChild(head);
    list.appendChild(row);

    cb.addEventListener('change', async () => {
      const st = vectorLayers[theme];
      st.enabled = cb.checked;
      row.classList.toggle('off', !cb.checked);
      if (cb.checked) {
        group.addTo(map);
        await syncVectorTier(theme);
      } else {
        map.removeLayer(group);
      }
      updateStatus();
      renderLegend();
    });

    if (vectorLayers[theme].enabled) {
      group.addTo(map);
      syncVectorTier(theme);
    }
  });
}

// ---------------------------------------------------------------------------
// extents

function buildExtents(aoi) {
  const list = document.getElementById('extent-list');
  const defs = [
    ['core', 'Core AOI', '#ffd166', true],
    ['vector', 'Vector extent', '#6fb2ff', false],
    ['buffered', 'Raster extent (+60 km)', '#ff7b7b', true],
  ];
  defs.forEach(([key, label, color, on]) => {
    const b = aoi[key];
    if (!b) return;
    const rect = L.rectangle(
      [[b.south, b.west], [b.north, b.east]],
      { color, weight: 1.4, fill: false, dashArray: '5,4', interactive: false },
    );
    const row = el('div', `layer${on ? '' : ' off'}`);
    const head = el('label', 'layer-head');
    const cb = el('input');
    cb.type = 'checkbox';
    cb.checked = on;
    const sw = el('span', 'swatch');
    sw.style.background = 'transparent';
    sw.style.borderColor = color;
    head.appendChild(cb);
    head.appendChild(sw);
    head.appendChild(el('span', 'name', label));
    row.appendChild(head);
    list.appendChild(row);
    cb.addEventListener('change', () => {
      row.classList.toggle('off', !cb.checked);
      if (cb.checked) rect.addTo(map); else map.removeLayer(rect);
    });
    if (on) rect.addTo(map);
  });
}

// ---------------------------------------------------------------------------
// legend

function renderLegend() {
  const box = document.getElementById('legend');
  const section = document.getElementById('legend-section');
  box.innerHTML = '';
  let any = false;

  (manifest.raster_layers || []).forEach((spec) => {
    if (!map.hasLayer(rasterLayers[spec.id])) return;
    any = true;
    const g = el('div', 'legend-group');
    g.appendChild(el('h3', null, spec.title));
    if (spec.legend) {
      spec.legend.forEach((item) => {
        const row = el('div', 'legend-item');
        const i = el('i');
        i.style.background = `rgb(${item.color.join(',')})`;
        row.appendChild(i);
        row.appendChild(el('span', null, item.label));
        g.appendChild(row);
      });
    } else if (RAMP_CSS[spec.id]) {
      const bar = el('div', 'legend-bar');
      bar.style.background = RAMP_CSS[spec.id];
      g.appendChild(bar);
      // units and range come from the manifest — never inferred from prose
      const u = spec.units || '';
      // pick decimals from the range's magnitude so both ends match
      const dp = Math.abs(spec.vmax ?? 0) >= 100 ? 0 : 1;
      const fmt = (v) => `${(+v).toFixed(dp)}${u}`;
      const lo = spec.lo_label ?? (spec.vmin !== undefined ? fmt(spec.vmin) : null);
      const hi = spec.hi_label ?? (spec.vmax !== undefined ? fmt(spec.vmax) : null);
      if (lo !== null && hi !== null) {
        const sc = el('div', 'legend-scale');
        sc.appendChild(el('span', null, lo));
        sc.appendChild(el('span', null, hi));
        g.appendChild(sc);
      }
      if (spec.description) g.appendChild(el('div', 'legend-note', spec.description));
    }
    box.appendChild(g);
  });

  // vector categories — only the classes actually present in the loaded chunks
  VECTOR_ORDER.forEach((theme) => {
    const st = vectorLayers[theme];
    if (!st || !st.enabled || !st.onMap.size) return;
    const s = VECTOR_STYLE[theme];
    const present = new Set();
    st.onMap.forEach((l) => (l._classKeys || []).forEach((k) => present.add(k)));
    const keys = Object.keys(s.classes).filter((k) => present.has(k));
    if (keys.length < 2) return;   // nothing worth explaining
    any = true;
    const g = el('div', 'legend-group');
    g.appendChild(el('h3', null, s.label));
    keys.forEach((k) => {
      const c = s.classes[k];
      const row = el('div', 'legend-item');
      const i = el('i');
      if (s.kind === 'point') {
        i.style.background = c.color;
        i.style.borderRadius = c.shape === 'circle' ? '50%' : '2px';
      } else if (s.kind === 'poly') {
        i.style.background = c.fill || 'transparent';
        i.style.borderColor = c.color;
      } else {
        i.style.background = c.color;
        i.style.height = `${Math.max(2, Math.min(6, (c.weight || 1) * 1.5))}px`;
        i.style.borderColor = 'transparent';
      }
      row.appendChild(i);
      row.appendChild(el('span', null, c.label));
      g.appendChild(row);
    });
    box.appendChild(g);
  });

  // the dash scale is shared by roads and trails, so it gets its own group
  if (['roads', 'trails'].some((t) => vectorLayers[t] && vectorLayers[t].enabled)) {
    any = true;
    const g = el('div', 'legend-group');
    g.appendChild(el('h3', null, 'Surface (dash pattern)'));
    IMPROVEMENT_LABELS.forEach((label, i) => {
      const row = el('div', 'legend-item');
      const dash = DASH_BY_IMPROVEMENT[i];
      const holder = el('span', null,
        `<svg width="30" height="8" style="display:block"><line x1="0" y1="4" x2="30" y2="4"`
        + ` stroke="#cbd2d9" stroke-width="2"`
        + (dash ? ` stroke-dasharray="${dash}"` : '') + `/></svg>`);
      holder.style.cssText = 'flex:none;line-height:0;width:30px';
      row.appendChild(holder);
      row.appendChild(el('span', null, label));
      g.appendChild(row);
    });
    box.appendChild(g);
  }

  section.hidden = !any;
}

// ---------------------------------------------------------------------------
// status

function updateStatus() {
  const c = map.getCenter();
  document.getElementById('pos').textContent =
    `${c.lat.toFixed(4)}°, ${c.lng.toFixed(4)}°`;
  document.getElementById('zoom').textContent = `z${map.getZoom()}`;
  const live = Object.values(vectorLayers).filter((s) => s.enabled && s.currentTier);
  const tiers = [...new Set(live.map((s) => s.currentTier))];
  const chunks = live.reduce((a, s) => a + s.onMap.size, 0);
  document.getElementById('tier').textContent = live.length
    ? `vector LOD: ${tiers.join('/')} · ${chunks} chunk${chunks === 1 ? '' : 's'} loaded`
    : 'vector LOD: —';
}

// ---------------------------------------------------------------------------
// cursor inspector — reports every layer, not just the visible ones

const INSPECT_TOL_PX = HIT_TOLERANCE_PX;   // keep hover and inspector in sync
const INSPECT_DEBOUNCE_MS = 110;
const RASTER_ORDER = ['dem', 'slope', 'ruggedness', 'relief_sigma',
  'canopy_density', 'builtup_density', 'landcover', 'hillshade'];
const RASTER_LABEL = {
  dem: 'Elevation', slope: 'Slope', ruggedness: 'Ruggedness',
  relief_sigma: 'Relief', canopy_density: 'Canopy',
  builtup_density: 'Built-up', landcover: 'Land cover', hillshade: 'Hillshade',
};
const SUMMARY_KEYS = ['highway', 'sac_scale', 'trail_visibility', 'surface',
  'waterway', 'natural', 'water', 'tourism', 'amenity', 'ele', 'mountain_pass',
  'leisure', 'boundary', 'operator', 'ref'];

let inspectSeq = 0;
let inspectTimer = null;

/** Flatten Leaflet's nested latlng arrays into a list of rings. */
function ringsOf(latlngs) {
  if (!latlngs || !latlngs.length) return [];
  if (latlngs[0] instanceof L.LatLng) return [latlngs];
  return latlngs.flatMap(ringsOf);
}

function pointInRing(pt, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const yi = ring[i].lat, xi = ring[i].lng;
    const yj = ring[j].lat, xj = ring[j].lng;
    if (((yi > pt.lat) !== (yj > pt.lat))
      && (pt.lng < (xj - xi) * (pt.lat - yi) / (yj - yi) + xi)) inside = !inside;
  }
  return inside;
}

/** Distance test in screen space, so tolerance stays constant at any zoom. */
function pathNear(layer, p, tolPx, isPoly) {
  for (const ring of ringsOf(layer.getLatLngs())) {
    let prev = null;
    for (const ll of ring) {
      const q = map.latLngToLayerPoint(ll);
      if (prev) {
        if (L.LineUtil.pointToSegmentDistance(p, prev, q) <= tolPx) return true;
      } else if (p.distanceTo(q) <= tolPx) return true;
      prev = q;
    }
    if (isPoly && pointInRing(map.layerPointToLatLng(p), ring)) return true;
  }
  return false;
}

function hitTestVectors(latlng) {
  const p = map.latLngToLayerPoint(latlng);
  const size = map.getSize();
  const b = map.getBounds();
  const tolX = (b.getEast() - b.getWest()) / size.x * INSPECT_TOL_PX;
  const tolY = (b.getNorth() - b.getSouth()) / size.y * INSPECT_TOL_PX;
  const box = L.latLngBounds(
    [latlng.lat - tolY, latlng.lng - tolX],
    [latlng.lat + tolY, latlng.lng + tolX],
  );

  const hits = [];
  for (const theme of [...VECTOR_ORDER].reverse()) {
    const st = vectorLayers[theme];
    if (!st || !st.enabled) continue;
    const isPoly = VECTOR_STYLE[theme].kind === 'poly';
    st.onMap.forEach((gj) => {
      gj.eachLayer((l) => {
        if (hits.length >= 24) return;
        if (l.getLatLng) {
          if (box.contains(l.getLatLng())) hits.push({ theme, f: l.feature });
          return;
        }
        if (!l.getBounds) return;
        const lb = l.getBounds();
        // cheap bbox reject first; the segment test below is the expensive bit
        if (!lb.isValid() || !lb.overlaps(box)) return;
        if (pathNear(l, p, INSPECT_TOL_PX, isPoly)) hits.push({ theme, f: l.feature });
      });
    });
  }
  return hits;
}

function summarise(props) {
  const parts = [];
  for (const k of SUMMARY_KEYS) {
    if (props[k] !== undefined) parts.push(`${k}=${props[k]}`);
  }
  return parts.slice(0, 4).join('   ');
}

const COMPASS_16 = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
                    'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];

/** Great-circle distance (m) and initial bearing (deg from north) between two
 *  points. The inverse of `destPoint`, on the same sphere. */
function geoInverse(from, to) {
  const R = 6371008.8;
  const p1 = from.lat * Math.PI / 180, p2 = to.lat * Math.PI / 180;
  const dp = p2 - p1, dl = (to.lng - from.lng) * Math.PI / 180;
  const a = Math.sin(dp / 2) ** 2
    + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  const dist = 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
  const y = Math.sin(dl) * Math.cos(p2);
  const x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
  return { dist, bearing: (Math.atan2(y, x) * 180 / Math.PI + 360) % 360 };
}

function formatDistance(m) {
  if (m < 1000) return `${Math.round(m)} m`;
  return `${(m / 1000).toFixed(m < 10000 ? 2 : 1)} km`;
}

/** Distance and bearing from the panorama viewpoint, so a feature on the map
 *  can be matched to what the panorama shows at that compass heading. */
function renderViewpointOffset(latlng) {
  const vbox = document.getElementById('inspect-view');
  vbox.innerHTML = '';
  if (!panoLatLng || $('pano').hidden) return;
  const { dist, bearing } = geoInverse(panoLatLng, latlng);
  vbox.appendChild(el('div', 'ins-sep', 'From viewpoint'));
  const row = (k, v) => {
    const d = el('div', 'ins-row');
    d.appendChild(el('span', 'k', k));
    d.appendChild(el('span', 'v', v));
    vbox.appendChild(d);
  };
  row('distance', formatDistance(dist));
  row('bearing', dist < 1
    ? '—'
    : `${Math.round(bearing) % 360}° ${COMPASS_16[Math.round(bearing / 22.5) % 16]}`);
}

function renderInspector(latlng, rasters, hits) {
  document.getElementById('inspect-hint').hidden = true;
  document.getElementById('inspect-pos').textContent =
    `${latlng.lat.toFixed(5)}, ${latlng.lng.toFixed(5)}`;
  renderViewpointOffset(latlng);

  const rbox = document.getElementById('inspect-raster');
  rbox.innerHTML = '';
  RASTER_ORDER.forEach((id) => {
    const e = rasters && rasters[id];
    const row = el('div', 'ins-row');
    row.appendChild(el('span', 'k', RASTER_LABEL[id] || id));
    let text = '—', cls = 'v null';
    if (e && e.value !== null && e.value !== undefined && !e.error) {
      cls = 'v';
      text = e.label
        ? escapeHtml(e.label)
        : `${(+e.value).toFixed(e.decimals || 0)}${e.unit || ''}`;
    }
    row.appendChild(el('span', cls, text));
    rbox.appendChild(row);
  });

  const vbox = document.getElementById('inspect-vector');
  vbox.innerHTML = '';
  if (!hits.length) return;
  vbox.appendChild(el('div', 'ins-sep',
    `${hits.length} feature${hits.length === 1 ? '' : 's'} near cursor`));
  hits.slice(0, 12).forEach(({ theme, f }) => {
    const s = VECTOR_STYLE[theme];
    const props = (f && f.properties) || {};
    const c = classOf(theme, props);
    const d = el('div', 'ins-feature');
    const t = el('div', 'ft');
    const sw = el('i');
    sw.style.background = c.color;
    t.appendChild(sw);
    t.appendChild(el('span', null, escapeHtml(props.name || c.label || s.label)));
    d.appendChild(t);
    const detail = summarise(props);
    if (detail) d.appendChild(el('div', 'fsub', escapeHtml(detail)));
    vbox.appendChild(d);
  });
}

async function updateInspector(latlng) {
  const seq = ++inspectSeq;
  const hits = hitTestVectors(latlng);
  let rasters = null;
  try {
    const r = await fetch(
      `/api/point?lat=${latlng.lat}&lon=${latlng.lng}&slug=${AOI}`);
    if (r.ok) rasters = (await r.json()).layers;
  } catch (err) { /* server down — still show the vector hits */ }
  if (seq !== inspectSeq) return;   // a later move superseded this one
  renderInspector(latlng, rasters, hits);
}

map.on('mousemove', (ev) => {
  clearTimeout(inspectTimer);
  inspectTimer = setTimeout(() => updateInspector(ev.latlng), INSPECT_DEBOUNCE_MS);
  // not debounced: this is pure arithmetic against data already in memory, and
  // a laggy dot would defeat the point of the pairing
  showMapCursorOnPano(ev.latlng);
});
map.on('mouseout', () => showMapCursorOnPano(null));
window.mapView.inspect = updateInspector;

// ---------------------------------------------------------------------------
// "view from here" panorama

// eye height slider is exponential: 2 m (standing) to 2 km (aircraft)
const EYE_MIN = 2, EYE_MAX = 2000;
const sliderToEye = (t) => EYE_MIN * Math.pow(EYE_MAX / EYE_MIN, t / 100);

// Range slider. The upper limit is the physical one: the furthest two summits
// can be from each other and still see each other's tops.
//
// Our curvature model drops a point at distance d by (1-k)d^2/(2R), so a
// summit of height h sits exactly on the horizon at d = sqrt(2Rh/(1-k)), and
// two such summits reach twice that. For 15,000 ft (4572 m), R = 6371 km and
// k = 0.13 that is 2 x 258.8 = 517 km. (Without refraction it would be 483 km
// — the 7% is why k is not optional.) Nothing further can ever be visible
// from any peak on earth, so there is no reason for the slider to go past it.
const RANGE_MIN_KM = 10, RANGE_MAX_KM = 517, DEFAULT_RANGE_KM = 120;
// Geometric, like the eye slider: linear travel to 517 km would squeeze the
// 10-120 km band everyone actually uses into the first fifth of the track.
const sliderToRange = (t) =>
  Math.round(RANGE_MIN_KM * Math.pow(RANGE_MAX_KM / RANGE_MIN_KM, t / 100));
const rangeToSlider = (km) =>
  Math.round(100 * Math.log(km / RANGE_MIN_KM) / Math.log(RANGE_MAX_KM / RANGE_MIN_KM));

let pano = null;                 // Pano3D instance, created on first use
let panoSnapLine = null;         // click -> snapped-summit leader line
let panoNearestMax = null;       // target offered by the manual snap button
let panoFov = null;
let panoFovCasing = null;              // field-of-view wedge drawn on the map
let panoAxis = null;             // centre-of-view bearing line
let panoFovKey = '';             // last drawn yaw/fov/range, to skip redraws

/** Destination point on a sphere, for drawing the view wedge. */
function destPoint(lat, lon, bearingDeg, distM) {
  const R = 6371008.8;
  const p1 = lat * Math.PI / 180, l1 = lon * Math.PI / 180;
  const t = bearingDeg * Math.PI / 180, d = distM / R;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(d)
    + Math.cos(p1) * Math.sin(d) * Math.cos(t));
  const l2 = l1 + Math.atan2(Math.sin(t) * Math.sin(d) * Math.cos(p1),
    Math.cos(d) - Math.sin(p1) * Math.sin(p2));
  return [p2 * 180 / Math.PI, ((l2 * 180 / Math.PI + 540) % 360) - 180];
}

/** Draw where the panorama camera is looking, as a wedge on the map. */
function updatePanoFovOnMap(p) {
  if (!p || !p.data) return;
  const h = p.data.header;
  const aspect = p.canvas.clientWidth / Math.max(1, p.canvas.clientHeight);
  // the camera's vertical fov is the parameter; the wedge needs horizontal
  const hfov = 2 * Math.atan(Math.tan(p.fov * Math.PI / 360) * aspect)
    * 180 / Math.PI;
  const key = `${p.yaw.toFixed(1)}|${hfov.toFixed(1)}|${h.range_km}|${h.lat}`;
  if (key === panoFovKey) return;         // nothing moved; skip the redraw
  panoFovKey = key;

  const R = h.range_km * 1000;
  const pts = [[h.lat, h.lon]];
  const n = 24;
  for (let i = 0; i <= n; i++) {
    pts.push(destPoint(h.lat, h.lon, p.yaw - hfov / 2 + hfov * i / n, R));
  }
  const axis = [[h.lat, h.lon], destPoint(h.lat, h.lon, p.yaw, R)];

  if (!panoFov) {
    // The wedge competes with hillshade, imagery and a dense trail network, so
    // a 1 px translucent outline disappeared into it. Heavier stroke, a warm
    // hue nothing else on the map uses, and a dark casing so it reads over
    // both snow and forest.
    panoFovCasing = L.polygon(pts, {
      renderer: panoOverlayRenderer,
      color: '#10141a', weight: 5, opacity: 0.45,
      fill: false, interactive: false,
    }).addTo(map);
    panoFov = L.polygon(pts, {
      renderer: panoOverlayRenderer,
      color: '#ffd166', weight: 2.5, opacity: 1,
      fillColor: '#ffd166', fillOpacity: 0.34, interactive: false,
    }).addTo(map);
    panoAxis = L.polyline(axis, {
      renderer: panoOverlayRenderer,
      color: '#fff3d0', weight: 2, opacity: 0.95,
      dashArray: '7,5', interactive: false,
    }).addTo(map);
  } else {
    panoFovCasing.setLatLngs(pts);
    panoFov.setLatLngs(pts);
    panoAxis.setLatLngs(axis);
  }
}

// The map indicator is a *consequence* of the panorama view, not part of
// drawing it, so it no longer runs inline with the render loop. A trailing
// timer means a drag or a zoom updates the map once when it settles instead of
// once per frame. Leading edge fires immediately so a single arrow tap still
// feels instant.
const MAP_SYNC_IDLE_MS = 100;
let mapSyncTimer = null;
let mapSyncLast = 0;

function scheduleMapSync(p) {
  const now = performance.now();
  if (now - mapSyncLast > MAP_SYNC_IDLE_MS) {   // idle: update straight away
    mapSyncLast = now;
    updatePanoFovOnMap(p);
    return;
  }
  clearTimeout(mapSyncTimer);
  mapSyncTimer = setTimeout(() => {
    mapSyncLast = performance.now();
    if (pano && !$('pano').hidden) updatePanoFovOnMap(pano);
  }, MAP_SYNC_IDLE_MS);
}

function clearPanoFovOnMap() {
  clearTimeout(mapSyncTimer);
  if (panoFovCasing) { map.removeLayer(panoFovCasing); panoFovCasing = null; }
  if (panoFov) { map.removeLayer(panoFov); panoFov = null; }
  if (panoAxis) { map.removeLayer(panoAxis); panoAxis = null; }
  panoFovKey = '';
}
let picking = false;
let panoMarker = null;
let panoLatLng = null;
let panoSeq = 0;

const $ = (id) => document.getElementById(id);

// Picking is intercepted on `document` in the CAPTURE phase rather than via
// map.on('click'). Leaflet's popup binding calls stopPropagation() when a
// feature is clicked, so the map's own click event never fires and the picker
// silently did nothing whenever the cursor happened to be over a peak or a
// trail. Capturing at the document level runs before any Leaflet handler, so
// the pick works everywhere and no popup opens while armed.
let pickDownPt = null;

function setPicking(on) {
  picking = on;
  $('pick-view').classList.toggle('armed', on);
  $('pick-view').textContent = on ? 'Click the map…  (Esc to cancel)' : 'Pick a viewpoint';
  map.getContainer().classList.toggle('picking', on);
  if (on) {
    document.addEventListener('mousedown', onPickDown, true);
    document.addEventListener('click', onPickClick, true);
    document.addEventListener('keydown', onPickKey, true);
  } else {
    pickDownPt = null;
    document.removeEventListener('mousedown', onPickDown, true);
    document.removeEventListener('click', onPickClick, true);
    document.removeEventListener('keydown', onPickKey, true);
  }
}

function inMap(target) {
  return map.getContainer().contains(target);
}

function onPickDown(ev) {
  pickDownPt = inMap(ev.target) ? { x: ev.clientX, y: ev.clientY } : null;
}

function onPickKey(ev) {
  if (ev.key === 'Escape') setPicking(false);
}

function onPickClick(ev) {
  if (!inMap(ev.target)) return;
  // a click that ends a pan should not drop a viewpoint; stay armed
  if (pickDownPt && Math.hypot(ev.clientX - pickDownPt.x,
    ev.clientY - pickDownPt.y) > 6) { pickDownPt = null; return; }
  ev.preventDefault();
  ev.stopPropagation();          // keeps Leaflet from opening a feature popup
  const latlng = map.containerPointToLatLng(map.mouseEventToContainerPoint(ev));
  setPicking(false);
  renderPanorama(latlng);
}

// Label budget. A wide view can put 400+ named summits on screen at once, and
// labelling them all is unreadable — so only the most prominent get a standing
// label and everything else becomes a tick you can hover.
const MAX_ALWAYS_LABELS = 16;
const MIN_STANDS_FOR_LABEL = 40;   // metres above the ridge in front
const LABEL_ROW_H = 18;            // label box height incl. padding
// Labels live in a band under the compass strip and reach down to their summit
// on a stem, rather than sitting just above it. Anchoring them to the peak put
// them wherever the skyline happened to be — which is to say, on top of the
// landscape. Up here they only ever cover sky.
const LABEL_BAND_TOP = 20;
const LABEL_BAND_ROWS = 4;

let panoMinorPeaks = [];           // {x, y, name, ele, dist_km} for hover

/** Whether any true-colour imagery was built, so the panorama knows whether to
 *  ask for textures at all. Absent imagery is a normal state — it comes from a
 *  separate acquisition step — and the panorama falls back to land-cover
 *  colour without complaint. */
function imageryAvailable() {
  return (manifest && manifest.raster_layers || []).some((l) => l.rgb);
}

/** Marker class for a panorama peak, mirroring VECTOR_STYLE.peaks.classify so
 *  a summit wears the same icon in the panorama as it does on the map. */
function panoPeakClass(k) {
  if (k.volcano) return 'volcano';
  return k.ele >= 2000 ? 'high' : 'minor';
}

/** Redraw the HTML overlay (peak labels, compass) on top of the WebGL canvas.
 *  Called by Pano3D after every frame. */
function updatePanoOverlay(p) {
  const box = $('pano-labels');
  const ticks = $('pano-ticks');
  box.innerHTML = '';
  ticks.innerHTML = '';
  panoMinorPeaks = [];

  // Ranked by prominence where it is known, standing height otherwise. Which
  // summits deserve a permanent label is a question about the mountains, not
  // about this particular view, and prominence is the answer to it.
  const PROM_FLOOR = 30.5;          // the source's own 100 ft cut
  const promOf = (q) => (q.prom !== undefined ? q.prom : PROM_FLOOR);
  const peaks = p.visiblePeaks().slice().sort((x, y) => promOf(y) - promOf(x));
  const placed = [];
  let shown = 0;

  // Width has to be estimated, not measured: measuring each candidate would
  // mean a DOM reflow per peak, hundreds of times per frame. Tuned to the
  // 11.5px bold name plus the 10.5px "1234 m · 12.3 km" suffix and padding —
  // if this drifts from the CSS, labels start colliding.
  const estWidth = (k) => k.name.length * 5.2 + 112;

  peaks.forEach((k) => {
    let y = null;
    if (shown < MAX_ALWAYS_LABELS && k.stands >= MIN_STANDS_FOR_LABEL) {
      const w = estWidth(k);
      for (let row = 0; row < LABEL_BAND_ROWS; row++) {
        const cand = LABEL_BAND_TOP + row * LABEL_ROW_H;
        // never sit on its own summit: `cand` is the box top, so clear a full
        // row height plus a few pixels of stem below it
        if (cand > k.y - LABEL_ROW_H - 6) break;
        const clash = placed.some((q) =>
          Math.abs(q.y - cand) < LABEL_ROW_H && Math.abs(q.x - k.x) < (q.w + w) / 2 + 6);
        if (!clash) { y = cand; break; }
      }
    }
    // Every peak gets a marker, labelled or not — the label says which summit
    // it is, the marker says where it is, and dropping it for labelled peaks
    // left the most prominent summits as the only ones with nothing on them.
    // Same triangle classes as the map layer, so the two read as one legend.
    const t = el('div', `pk-mark ${panoPeakClass(k)}`);
    t.style.left = `${k.x}px`;
    t.style.top = `${k.y}px`;
    ticks.appendChild(t);

    if (y === null) {
      panoMinorPeaks.push(k);      // demoted: label appears on hover
      return;
    }
    placed.push({ x: k.x, y, w: estWidth(k) });
    const d = el('div', 'pk',
      `<b>${escapeHtml(k.name)}</b> <i>${Math.round(k.ele)} m · ${k.dist_km.toFixed(1)} km</i>`);
    d.style.left = `${k.x}px`;
    d.style.top = `${y}px`;
    d.style.setProperty('--stem', `${Math.max(4, k.y - y - 10)}px`);
    box.appendChild(d);
    shown++;
  });

  // true horizontal reference; the visible horizon sits below it by the
  // curvature drop, which at 100 km is ~650 m
  const hy = p.horizonY();
  const hz = $('pano-horizon');
  if (hy === null || hy < -20 || hy > p.canvas.clientHeight + 20) {
    hz.hidden = true;
  } else {
    hz.hidden = false;
    hz.style.top = `${hy}px`;
  }

  // compass ticks, projected through the same camera so they track the view
  const cmp = $('pano-compass');
  cmp.innerHTML = '';
  const CARD = { 0: 'N', 45: 'NE', 90: 'E', 135: 'SE', 180: 'S', 225: 'SW', 270: 'W', 315: 'NW' };
  const W = p.canvas.clientWidth;
  for (let a = 0; a < 360; a += 15) {
    const x = p.projectAzimuth(a);
    if (x === null || x < 8 || x > W - 8) continue;
    const isCard = CARD[a] !== undefined;
    const d = el('div', `cmp${isCard ? ' card' : ''}`, isCard ? CARD[a] : `${a}`);
    d.style.left = `${x}px`;
    cmp.appendChild(d);
  }

  scheduleMapSync(p);
  scheduleTextureDetail();

  $('pano-status').textContent =
    `bearing ${Math.round(p.yaw)}° · pitch ${p.pitch.toFixed(1)}° · fov `
    + `${p.fov.toFixed(0)}° · ${peaks.length} peaks visible, ${shown} labelled, `
    + `${panoMinorPeaks.length} on hover · earth curvature + refraction `
    + `(k=${p.data.header.refraction_k})`;
}

/** The inverse of showPanoCursorOnMap: put the map's cursor on the panorama.
 *
 *  Uses the same geoInverse the "From viewpoint" readout does, so the bearing
 *  and distance are exactly what the panel shows, then asks the mesh where
 *  that lands on screen. Dimmed rather than hidden when the point is behind
 *  terrain — "it is over there but you cannot see it" is useful to know. */
function showMapCursorOnPano(latlng) {
  const el = $('pano-cursor');
  if (!el) return;
  if (!latlng || !pano || !pano.data || $('pano').hidden) { el.hidden = true; return; }
  const g = geoInverse(L.latLng(pano.data.header.lat, pano.data.header.lon), latlng);
  const p = pano.projectGround(g.bearing, g.dist);
  if (!p) { el.hidden = true; return; }
  el.style.left = `${p.x}px`;
  el.style.top = `${p.y}px`;
  el.classList.toggle('occluded', !!p.hidden);
  el.hidden = false;
}

let panoCursorMarker = null;

/** Show the panorama's hovered ground point on the map.
 *
 *  `hit` is {az, dist} from pickGround — a compass bearing and a ground
 *  distance from the observer, which is exactly what destPoint converts. Note
 *  that is a spherical forward against the server's ellipsoidal geodesic, so
 *  the dot can sit a few tens of metres off at 100 km; fine for "which ridge
 *  is that", not a survey mark. */
function showPanoCursorOnMap(hit) {
  if (!hit || !panoLatLng || !pano || !pano.data) {
    if (panoCursorMarker) { map.removeLayer(panoCursorMarker); panoCursorMarker = null; }
    return;
  }
  const h = pano.data.header;
  const p = destPoint(h.lat, h.lon, hit.az, hit.dist);
  const ll = L.latLng(p[0], p[1]);
  if (!panoCursorMarker) {
    panoCursorMarker = L.circleMarker(ll, {
      radius: 5, color: '#fff', weight: 2, opacity: 0.9,
      fillColor: '#ff3b30', fillOpacity: 1, interactive: false,
    }).addTo(map).bindTooltip('', { direction: 'top', offset: [0, -6] });
  } else {
    panoCursorMarker.setLatLng(ll);
  }
  // setTooltipContent, not bindTooltip: this runs on every mousemove and
  // rebinding would leak a listener per frame
  panoCursorMarker.setTooltipContent(
    `${(hit.dist / 1000).toFixed(1)} km · ${Math.round(hit.az)}°`);
}

// hover a tick to name it, without permanently spending screen space on it
function panoHover(ev) {
  if (!pano || $('pano').hidden) return;
  const r = $('pano-canvas').getBoundingClientRect();
  const mx = ev.clientX - r.left, my = ev.clientY - r.top;
  let best = null, bestD = 15;      // px
  for (const k of panoMinorPeaks) {
    const d = Math.hypot(k.x - mx, k.y - my);
    if (d < bestD) { bestD = d; best = k; }
  }
  // mirror the cursor onto the map, so "where is that?" is one glance
  showPanoCursorOnMap(pano.pickGround(mx, my));

  const box = $('pano-hover');
  if (!best) { box.hidden = true; return; }
  box.innerHTML = `<b>${escapeHtml(best.name)}</b> `
    + `<i>${Math.round(best.ele)} m · ${best.dist_km.toFixed(1)} km</i>`;
  box.style.left = `${best.x}px`;
  box.style.top = `${best.y}px`;
  box.hidden = false;
}
$('pano-canvas').addEventListener('mousemove', panoHover);
$('pano-canvas').addEventListener('mouseleave', () => {
  $('pano-hover').hidden = true;
  showPanoCursorOnMap(null);
});

async function renderPanorama(latlng, opts) {
  const seq = ++panoSeq;
  panoLatLng = latlng;
  const rangeKm = sliderToRange(+$('pano-range').value);

  $('pano').hidden = false;
  // wipe the previous panorama immediately — leaving the old one up while a
  // new viewpoint loads reads as though the click did nothing
  ['pano-labels', 'pano-ticks'].forEach((id) => { $(id).innerHTML = ''; });
  $('pano-horizon').hidden = true;
  $('pano-hover').hidden = true;
  panoMinorPeaks = [];
  if (pano) pano.blank();
  $('pano-loading').hidden = false;
  $('pano-loading-text').textContent = `building ${rangeKm} km terrain…`;
  $('pano-status').textContent = `loading ${rangeKm} km terrain mesh…`;
  $('pano-meta').textContent = '';
  setTimeout(syncLayoutVars, 60);

  // the server build dominates; tick the sub-line so it does not look stalled.
  // `phase` is mutable so the elapsed counter keeps running across the mesh
  // and imagery stages instead of each one resetting it.
  const t0 = performance.now();
  let phase = 'sampling elevation along 1024 azimuths';
  const setPhase = (text, sub) => {
    $('pano-loading-text').textContent = text;
    phase = sub;
  };
  const ticker = setInterval(() => {
    $('pano-loading-sub').textContent =
      `${phase} · ${((performance.now() - t0) / 1000).toFixed(1)} s`;
  }, 100);
  const stopTicker = () => clearInterval(ticker);

  if (panoMarker) map.removeLayer(panoMarker);
  if (panoSnapLine) { map.removeLayer(panoSnapLine); panoSnapLine = null; }
  clearPanoFovOnMap();
  panoMarker = L.circleMarker(latlng, {
    radius: 6, color: '#fff', weight: 2, fillColor: '#6fb2ff', fillOpacity: 0.9,
  }).addTo(map);

  // Created before either fetch: the snap callback needs it, and it only wants
  // the canvas. Invalidating stale textures also moves here, so that the mesh
  // arriving later cannot discard textures that already landed for this point.
  if (!pano) {
    pano = new Pano3D($('pano-canvas'));
    pano.onChange = updatePanoOverlay;
    window.mapView.pano = pano;
  }
  pano.texReady = false;
  pano._texToken++;

  let header, payload;
  // Imagery is kicked off from a cheap snap lookup rather than waiting for the
  // mesh. Textures must be centred on the *snapped* point or they sit offset
  // from the terrain, and that dependency used to serialise the two stages;
  // /api/snap answers in milliseconds, so they now overlap.
  let texturePromise = null;
  try {
    const snapParam = (opts && opts.snap !== undefined) ? `&snap_m=${opts.snap}` : '';
    if (imageryAvailable()) {
      texturePromise = fetch(`/api/snap?lat=${latlng.lat}&lon=${latlng.lng}`
        + `&slug=${AOI}${snapParam}`)
        .then((r) => (r.ok ? r.json() : null))
        .then((sn) => {
          if (!sn || seq !== panoSeq || !pano || !pano.texWanted) return null;
          return pano.loadTextures(sn.lat, sn.lon, AOI,
            wantedTexDetail(pano.fov), rangeKm);
        })
        .catch(() => null);
    }
    const r = await fetch(`/api/terrain?lat=${latlng.lat}&lon=${latlng.lng}`
      + `&slug=${AOI}&range_km=${rangeKm}${snapParam}`);
    if (!r.ok) {
      let msg = `HTTP ${r.status}`;
      try { msg = (await r.json()).error || msg; } catch (e) { /* binary */ }
      throw new Error(msg);
    }
    const buf = await r.arrayBuffer();
    const hlen = new DataView(buf).getUint32(0, true);
    header = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, hlen)));
    // slice so the typed-array views start at offset 0 and stay aligned
    payload = buf.slice(4 + hlen);
  } catch (err) {
    if (seq !== panoSeq) return;
    stopTicker();
    $('pano-loading').hidden = true;
    $('pano-status').textContent = `failed: ${err.message}`;
    return;
  }
  if (seq !== panoSeq) { stopTicker(); return; }  // superseded by a later click

  setPhase('uploading mesh…', 'building triangles');
  try {
    pano.eyeHeight = sliderToEye(+$('pano-height').value);
    pano.load(header, payload);
    // Wait for the imagery before revealing the panorama. Showing the mesh as
    // soon as it uploads meant every load flashed land-cover colours for a
    // second and then snapped to satellite, which reads as a glitch. The
    // textures take ~1-3 s, so the loading overlay simply stays up and says
    // so. Uses the *snapped* point from the header, not the click, or the
    // texture would sit offset from the terrain.
    if (!hazeTouched) {
      $('pano-haze').value = visKmToSlider(bestVisibilityKm(header.ground_m));
    }
    applyHaze(false);
    if (texturePromise) {
      setPhase('fetching imagery…', 'satellite texture, 4 detail levels');
      await texturePromise;
      if (seq !== panoSeq) { stopTicker(); return; }
    }
  } catch (err) {
    stopTicker();
    $('pano-loading').hidden = true;
    $('pano-status').textContent = `WebGL failed: ${err.message}`;
    return;
  }
  stopTicker();
  $('pano-loading').hidden = true;

  // the viewpoint may have snapped to a nearby summit — show where it landed,
  // and leave a leader line back to where the click actually was
  const snap = header.snap || {};
  const at = L.latLng(header.lat, header.lon);
  panoLatLng = at;                       // so a range change reuses this point
  map.removeLayer(panoMarker);
  panoMarker = L.circleMarker(at, {
    radius: 6, color: '#fff', weight: 2, fillColor: '#6fb2ff', fillOpacity: 0.9,
  }).addTo(map);
  if (snap.snapped) {
    panoSnapLine = L.polyline(
      [[snap.from_lat, snap.from_lon], [header.lat, header.lon]],
      { color: '#6fb2ff', weight: 1.5, dashArray: '3,3', interactive: false },
    ).addTo(map);
  }

  // manual snap button: offers the nearest local maximum further out than the
  // automatic snap reaches, and greys out when we are already standing on one
  const nm = header.nearest_max || {};
  const btn = $('pano-snapmax');
  if (nm.found) {
    btn.disabled = false;
    $('pano-snapmax-d').textContent = `${Math.round(nm.dist_m)} m`;
    btn.title = `nearest higher summit: ${Math.round(nm.dist_m)} m away, `
      + `+${nm.rise_m} m, to ${nm.ele_m} m`;
    panoNearestMax = nm;
  } else {
    btn.disabled = true;
    $('pano-snapmax-d').textContent = '—';
    btn.title = nm.at_max
      ? 'already on a local maximum'
      : `no higher summit within ${nm.search_radius_m || 500} m`;
    panoNearestMax = null;
  }

  $('pano-title').textContent = 'View from '
    + `${header.lat.toFixed(4)}, ${header.lon.toFixed(4)}`;
  $('pano-meta').textContent =
    `ground ${header.ground_m} m · ${header.n_az}×${header.n_r} mesh · `
    + `${header.peaks.length} named peaks in range`
    + (snap.snapped
      ? ` · snapped ${snap.moved_m} m to a local summit (+${snap.rise_m} m)`
      : ` · no summit within ${snap.search_radius_m || 150} m, `
        + 'using the clicked point');
}

/** Surface picker: satellite imagery or land-cover classes.
 *
 *  Switching to satellite when the textures were never fetched — because the
 *  panorama was first drawn in land-cover mode — has to fetch them now, so
 *  this is not a pure uniform flip. */
async function setPanoSurface(mode) {
  const sat = mode === 'sat';
  $('pano-surface-sat').classList.toggle('on', sat);
  $('pano-surface-lc').classList.toggle('on', !sat);
  if (!pano) return;
  pano.texWanted = sat;
  if (sat && !pano.texReady && pano.data && imageryAvailable()) {
    $('pano-status').textContent = 'fetching imagery…';
    const h = pano.data.header;
    await pano.loadTextures(h.lat, h.lon, AOI);
  }
  pano.render();
}
$('pano-surface').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-surface]');
  if (b) setPanoSurface(b.dataset.surface);
});

// Textures are built for the field of view in force at the time. Zooming in
// raises the screen's angular resolution and makes every level too coarse at
// once, so a narrower fov has to refetch a sharper ladder. Debounced, because
// the wheel fires continuously and each refetch is several seconds of server
// warping.
const TEX_BASE_FOV = 55;
let texDetailTimer = null;

function wantedTexDetail(fov) {
  return Math.max(1, Math.min(2, Math.round(TEX_BASE_FOV / Math.max(fov, 1))));
}

function scheduleTextureDetail() {
  if (!pano || !pano.data || !pano.texWanted || !imageryAvailable()) return;
  const want = wantedTexDetail(pano.fov);
  if (want === (pano.texDetail || 1)) return;
  clearTimeout(texDetailTimer);
  texDetailTimer = setTimeout(() => {
    if (!pano || !pano.data) return;
    const w = wantedTexDetail(pano.fov);
    if (w === (pano.texDetail || 1)) return;
    const h = pano.data.header;
    $('pano-status').textContent = w > 1
      ? 'zoomed in — refetching imagery at higher detail…'
      : 'refetching imagery…';
    pano.loadTextures(h.lat, h.lon, AOI, w);
  }, 600);
}

/** Publish the panel width and panorama height as CSS variables.
 *
 *  The panorama used to be `left: 0; bottom: 0` at z-index 1200, so it sat on
 *  top of both the map and the layer panel. Making it a sibling that takes its
 *  own space means the map has to know how tall it is and the panorama has to
 *  know how wide the panel is — hence two variables rather than a fixed inset,
 *  since either can be hidden independently.
 *
 *  Leaflet caches the map's pixel size, so it has to be told after the box
 *  changes or the projection stays wrong until the next window resize.
 */
function syncLayoutVars() {
  const panel = $('panel'), pane = $('pano');
  const pw = panel.classList.contains('hidden')
    ? 0 : panel.getBoundingClientRect().width;
  const ph = pane.hidden ? 0 : pane.getBoundingClientRect().height;
  const root = document.documentElement.style;
  root.setProperty('--panel-w', `${Math.round(pw)}px`);
  root.setProperty('--pano-h', `${Math.round(ph)}px`);
  map.invalidateSize();
}

// ---- haze -----------------------------------------------------------------
//
// The slider is calibrated in *meteorological visual range* rather than in the
// shader's e-folding distance, because visual range is the number people
// actually quote. Koschmieder relates them: a 2% contrast threshold gives
// V = 3.912 / beta, and the shader's uHaze is 1/beta, so V = 3.912 * uHaze.
const HAZE_VIS_MIN_KM = 1;        // "nothing visible beyond 1 km"
const HAZE_VIS_MAX_KM = 2000;     // last stop is treated as vacuum outright
const sliderToVisKm = (t) => (t >= 100 ? Infinity
  : HAZE_VIS_MIN_KM * Math.pow(HAZE_VIS_MAX_KM / HAZE_VIS_MIN_KM, t / 100));
const visKmToSlider = (v) => (!Number.isFinite(v) ? 100 : Math.round(
  100 * Math.log(v / HAZE_VIS_MIN_KM) / Math.log(HAZE_VIS_MAX_KM / HAZE_VIS_MIN_KM)));

/** Visibility on an excellent real-world day at a given altitude.
 *
 *  Extinction is modelled as two terms, because they thin out at very
 *  different rates and that is the whole reason summit views are so much
 *  clearer than valley ones:
 *
 *    Rayleigh   beta 1.16e-5 /m at sea level, scale height ~8.5 km
 *    aerosol    the rest of a good-day beta, scale height ~1.5 km
 *
 *  Rayleigh alone would give 337 km at sea level, but that is the theoretical
 *  ceiling with every particle removed and is never actually met. Anchoring
 *  the sea-level total at 130 km — a genuinely excellent day, not a
 *  once-a-decade one — and letting the aerosol term decay away with height
 *  gives ~150 km at 500 m and ~330 km from a 2700 m summit, which is about
 *  right for the long Cascade sight lines (Rainier from Stuart at 95 km sharp,
 *  Hood at 250 km on the best days).
 */
const BETA_RAYLEIGH = 1.16e-5;                       // /m, 550 nm, sea level
const BETA_AEROSOL = 3.912 / 130000 - BETA_RAYLEIGH; // good-day residual
function bestVisibilityKm(groundM) {
  const h = Math.max(0, groundM);
  const beta = BETA_RAYLEIGH * Math.exp(-h / 8500)
             + BETA_AEROSOL * Math.exp(-h / 1500);
  return 3.912 / beta / 1000;
}

let hazeTouched = false;          // once set by hand, stop overriding it

function applyHaze(persistTouch) {
  const v = sliderToVisKm(+$('pano-haze').value);
  $('pano-haze-val').textContent = Number.isFinite(v)
    ? `${v < 10 ? v.toFixed(1) : Math.round(v)} km`
    : 'vacuum';
  if (persistTouch) hazeTouched = true;
  if (pano) {
    // Infinity would reach the shader as a non-finite uniform; a very large
    // finite scale is indistinguishable and safe.
    pano.hazeKm = Number.isFinite(v) ? v / 3.912 : 1e9;
    pano.render();
  }
}
$('pano-haze').addEventListener('input', () => applyHaze(true));

// ---- resizable panorama pane ----------------------------------------------
const PANO_H_DEFAULT = 320, PANO_H_MIN = 120, PANO_MAP_MIN = 160;
const panoMaxH = () => Math.max(PANO_H_MIN, window.innerHeight - PANO_MAP_MIN);

/** Set the stage height and tell everything that cares.
 *
 *  The WebGL canvas is sized in device pixels from its CSS box, so it has to
 *  be re-measured; `render()` does that via resize() and also re-runs the
 *  overlay, which re-lays out the peak labels for the new height. */
function setPanoHeight(px, persist) {
  const h = Math.round(Math.min(panoMaxH(), Math.max(PANO_H_MIN, px)));
  $('pano-stage').style.height = `${h}px`;
  if (pano) pano.render();
  syncLayoutVars();
  if (persist) {
    try { localStorage.setItem('panoHeight', String(h)); } catch (e) { /* private mode */ }
  }
  return h;
}

(function initPanoResize() {
  const grip = $('pano-resize');
  let startY = 0, startH = 0, id = null;
  grip.addEventListener('pointerdown', (e) => {
    id = e.pointerId;
    grip.setPointerCapture(id);
    startY = e.clientY;
    startH = $('pano-stage').getBoundingClientRect().height;
    grip.classList.add('dragging');
    document.body.classList.add('pano-resizing');
    e.preventDefault();
  });
  grip.addEventListener('pointermove', (e) => {
    if (id === null) return;
    // dragging up (negative dy) makes the pane taller
    setPanoHeight(startH - (e.clientY - startY), false);
  });
  const end = () => {
    if (id === null) return;
    try { grip.releasePointerCapture(id); } catch (err) { /* already gone */ }
    id = null;
    grip.classList.remove('dragging');
    document.body.classList.remove('pano-resizing');
    setPanoHeight($('pano-stage').getBoundingClientRect().height, true);
  };
  grip.addEventListener('pointerup', end);
  grip.addEventListener('pointercancel', end);
  grip.addEventListener('dblclick', () => setPanoHeight(PANO_H_DEFAULT, true));

  let saved = null;
  try { saved = localStorage.getItem('panoHeight'); } catch (e) { /* private mode */ }
  if (saved) setPanoHeight(+saved, false);
  syncLayoutVars();
  // a window that shrinks below the stored height would otherwise leave no map
  window.addEventListener('resize', () => {
    const h = $('pano-stage').getBoundingClientRect().height;
    if (h > panoMaxH()) setPanoHeight(panoMaxH(), false);
  });
})();

// ---- arrow-key camera nudge -----------------------------------------------
//
// Moves the camera within the existing mesh — nothing is refetched. Eye height
// is held above the *surface*, so walking down a slope lowers the camera with
// the ground instead of leaving it hanging.
// Held keys drive a requestAnimationFrame loop rather than the browser's own
// key-repeat. Repeat only starts after the OS delay — typically half a second —
// so a held key moved once, stalled, then ran. Metres per *second* here, not
// per keypress, so the motion is smooth and frame-rate independent.
const CAM_SPEED_M_S = 25;
const CAM_SPEED_FAST_M_S = 100;
const CAM_TAP_M = 1.5;           // guarantees a quick tap still nudges

// gaze-relative: [sideways, forward]
const CAM_MOVE_KEYS = {
  ArrowUp: [0, 1], w: [0, 1],
  ArrowDown: [0, -1], s: [0, -1],
  ArrowLeft: [-1, 0], a: [-1, 0],
  ArrowRight: [1, 0], d: [1, 0],
};

// Where the precomputed data starts to visibly lie. Derived, not guessed:
//   * Textures are world-space squares, so registration stays exact at any
//     offset — only coverage shrinks, and the near level has 2.5 km of half-
//     extent to give.
//   * The curvature bake carries drop(r) measured from the original centre.
//     Moving d changes the correct drop by ~(1-k)*d*r/R, whose *angular* error
//     is (1-k)*d/R — independent of distance. That reaches one screen pixel
//     only at ~6 km.
//   * What actually degrades first is the foreground: the fine near-field ring
//     ramp covers just the first 25 m at 1.6 m spacing, and past it the floor
//     is 15 m. Still finer than the 30 m DEM, so no real detail is lost, but
//     the triangles under the camera grow and eventually facet.
// 300 m is where that foreground coarseness becomes the honest answer to "is
// this still the view from here?", well before any of the geometry is wrong.
const CAM_OFFSET_WARN_M = 300;

const camHeld = new Set();
let camRaf = null, camLastT = 0, camFast = false;

function updateCamOffsetBadge() {
  const el = $('pano-offset');
  if (!el || !pano) return;
  const [ox, oy] = pano.camOffset;
  const d = Math.hypot(ox, oy);
  // clear the state as well as hiding it, or a reset leaves the warning class
  // and text sitting on a hidden element
  if (d < 0.5) { el.hidden = true; el.classList.remove('warn'); el.textContent = ''; return; }
  const brg = ((Math.atan2(ox, oy) * 180 / Math.PI) % 360 + 360) % 360;
  const far = d > CAM_OFFSET_WARN_M;
  el.classList.toggle('warn', far);
  el.textContent = far
    ? `moved ${Math.round(d)} m — beyond ${CAM_OFFSET_WARN_M} m the foreground `
      + 'is no longer finely meshed; press 0 to recentre'
    : `moved ${Math.round(d)} m at ${Math.round(brg)}° · arrows/WASD to adjust, 0 to reset`;
  el.hidden = false;
}

/** Move the camera by `metres` along the gaze-relative [sideways, forward]. */
function camNudge(sideways, forward, metres) {
  const len = Math.hypot(sideways, forward);
  if (!len) return;
  const yaw = pano.yaw * Math.PI / 180;
  const fwd = [Math.sin(yaw), Math.cos(yaw)];
  const rgt = [fwd[1], -fwd[0]];
  const sx = sideways / len, sy = forward / len;   // diagonals are not faster
  pano.moveCamera((fwd[0] * sy + rgt[0] * sx) * metres,
                  (fwd[1] * sy + rgt[1] * sx) * metres);
  updateCamOffsetBadge();
}

function camStop() {
  camHeld.clear();
  if (camRaf) cancelAnimationFrame(camRaf);
  camRaf = null;
}

function camLoop(t) {
  if (!camHeld.size || !pano || !pano.data || $('pano').hidden) { camRaf = null; return; }
  // clamped so a backgrounded tab does not resume with one enormous jump
  const dt = camLastT ? Math.min(0.05, (t - camLastT) / 1000) : 0;
  camLastT = t;
  let sx = 0, sy = 0;
  for (const k of camHeld) {
    const m = CAM_MOVE_KEYS[k];
    if (m) { sx += m[0]; sy += m[1]; }
  }
  camNudge(sx, sy, (camFast ? CAM_SPEED_FAST_M_S : CAM_SPEED_M_S) * dt);
  camRaf = requestAnimationFrame(camLoop);
}

document.addEventListener('keydown', (ev) => {
  if ($('pano').hidden || !pano || !pano.data) return;
  const t = ev.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
  const k = ev.key.length === 1 ? ev.key.toLowerCase() : ev.key;
  if (k === '0') {
    pano.camOffset = [0, 0];
    pano.render(); updateCamOffsetBadge(); ev.preventDefault(); return;
  }
  const mv = CAM_MOVE_KEYS[k];
  if (!mv) return;
  ev.preventDefault();
  camFast = ev.shiftKey;
  if (ev.repeat) return;                 // the loop is already driving this
  camHeld.add(k);
  camNudge(mv[0], mv[1], CAM_TAP_M);     // so a tap always registers
  if (!camRaf) { camLastT = 0; camRaf = requestAnimationFrame(camLoop); }
});

document.addEventListener('keyup', (ev) => {
  const k = ev.key.length === 1 ? ev.key.toLowerCase() : ev.key;
  camHeld.delete(k);
  if (ev.key === 'Shift') camFast = false;
  if (!camHeld.size && camRaf) { cancelAnimationFrame(camRaf); camRaf = null; }
});
// a key held while the window loses focus never sends keyup
window.addEventListener('blur', camStop);

$('pick-view').addEventListener('click', () => setPicking(!picking));
$('pano-close').addEventListener('click', () => {
  $('pano').hidden = true;
  if (panoMarker) { map.removeLayer(panoMarker); panoMarker = null; }
  if (panoSnapLine) { map.removeLayer(panoSnapLine); panoSnapLine = null; }
  clearPanoFovOnMap();
  syncLayoutVars();
});
$('pano-range').addEventListener('input', () => {
  $('pano-range-val').textContent = `${sliderToRange(+$('pano-range').value)} km`;
});
$('pano-range').addEventListener('change', () => {
  if (panoLatLng) renderPanorama(panoLatLng);   // range changes the mesh
});

// Eye height only moves the camera — the mesh is viewer-independent — so this
// is instant with no server round trip. Peak visibility is recomputed against
// the same height grid, so labels appear as you rise above obstructions.
$('pano-height').addEventListener('input', () => {
  const h = sliderToEye(+$('pano-height').value);
  $('pano-height-val').textContent = h < 1000 ? `${h.toFixed(0)} m`
    : `${(h / 1000).toFixed(2)} km`;
  if (pano) pano.setView({ eyeHeight: h });
});

$('pano-reset').addEventListener('click', () => {
  if (pano) pano.setView({ yaw: 0, pitch: -4, fov: 55 });
});

$('pano-snapmax').addEventListener('click', () => {
  if (!panoNearestMax) return;
  // go exactly where the button said; the automatic snap would otherwise be
  // free to pick a different (higher) summit and contradict the label
  renderPanorama(L.latLng(panoNearestMax.lat, panoNearestMax.lon), { snap: 0 });
});

const panoResize = () => { if (pano && !$('pano').hidden) pano.render(); };
window.addEventListener('resize', panoResize);

// no map.on('click') here — see setPicking() for why picking is captured on
// document instead

// ---------------------------------------------------------------------------

document.getElementById('panel-toggle').addEventListener('click', () => {
  document.getElementById('panel').classList.toggle('hidden');
  syncLayoutVars();
});

map.on('moveend zoomend', () => {
  updateStatus();
  Object.keys(vectorLayers).forEach(syncVectorTier);
});

(async function init() {
  try {
    const resp = await fetch(DATA_ROOT + 'manifest.json');
    if (!resp.ok) throw new Error(`manifest.json -> HTTP ${resp.status}`);
    manifest = await resp.json();
  } catch (err) {
    document.getElementById('aoi-name').textContent = 'Failed to load manifest';
    document.getElementById('aoi-sub').textContent = String(err);
    return;
  }

  CACHE_BUST = manifest.built ? `?v=${manifest.built}` : '';

  // Set the range slider from the constants rather than trusting the markup,
  // so the default cannot drift away from RANGE_MIN/MAX if either changes.
  $('pano-range').value = rangeToSlider(DEFAULT_RANGE_KM);
  $('pano-range-val').textContent = `${sliderToRange(+$('pano-range').value)} km`;

  // Imagery is acquired by a separate step and may simply not be there. Say so
  // in the control rather than leaving a button that quietly does nothing.
  if (!imageryAvailable()) {
    const sat = $('pano-surface-sat');
    sat.disabled = true;
    sat.classList.remove('on');
    sat.title = 'no imagery built — run scripts/imagery.py';
    $('pano-surface-lc').classList.add('on');
  }

  const aoi = manifest.aoi;
  document.getElementById('aoi-name').textContent = aoi.name;
  document.title = `${aoi.name} — Vistarama`;
  const c = aoi.core;
  document.getElementById('aoi-sub').textContent =
    `${c.south}–${c.north}°N, ${Math.abs(c.west)}–${Math.abs(c.east)}°W`;

  map.fitBounds([[c.south, c.west], [c.north, c.east]]);

  buildRasters(manifest.raster_layers || []);
  buildVectors(manifest.vector_layers || {});
  buildExtents(aoi);
  renderLegend();
  updateStatus();
})();
