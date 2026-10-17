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
//
// It lives in its own pane that ignores the mouse. A canvas takes pointer
// events wherever it covers the map, and this one sits above the feature
// canvas, so in the shared overlay pane it swallowed every mousemove the
// moment a panorama opened: feature hover and the hover card simply stopped.
// Nothing drawn here is interactive, so letting events fall through costs
// nothing.
const PANO_OVERLAY_PANE = 'panoOverlay';
const panoOverlayRenderer = L.canvas({ padding: 0.3, pane: PANO_OVERLAY_PANE });

const map = L.map('map', {
  preferCanvas: true,
  renderer: vectorRenderer,
  zoomControl: true,
  minZoom: 6,
  maxZoom: 15,
});

map.createPane(PANO_OVERLAY_PANE);
// above the features (overlayPane is 400) and below markers and tooltips
map.getPane(PANO_OVERLAY_PANE).style.zIndex = 450;
map.getPane(PANO_OVERLAY_PANE).style.pointerEvents = 'none';

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

// ---------------------------------------------------------------------------
// feature hover: highlight the feature under the cursor and describe it in a
// card beside the pointer.
//
// Leaflet's canvas renderer already decides which feature is under the cursor
// (the topmost one within HIT_TOLERANCE_PX) and fires mouseover/mouseout on
// it, so there is no hit-testing here. The highlight is applied in place and
// undone on mouseout. bringToFront would make it stand out more, but on a
// shared canvas it permanently reorders the draw list: hover a lake once and
// it covers every trail that crosses it from then on.

const HOVER_WEIGHT_ADD = 2;       // px added to a hovered path's stroke
const HOVER_RADIUS_MUL = 1.45;    // a hovered point symbol grows by this
// A trail OSM splits into several ways is highlighted whole, every way alike.
// The ranked list marks its selection in a colour no other line uses.
const SELECT_COLOR = '#ffc23d';

// trail id -> every loaded tile layer of that trail. A way that spans several
// grid cells is loaded once per cell, and all copies are highlighted.
const trailLayers = new Map();
const HOVER_CARD_GAP = 14;        // px between the cursor and the card

const SAC_LABEL = {
  hiking: 'T1 · hiking',
  mountain_hiking: 'T2 · mountain hiking',
  demanding_mountain_hiking: 'T3 · demanding mountain hiking',
  alpine_hiking: 'T4 · alpine hiking',
  demanding_alpine_hiking: 'T5 · demanding alpine hiking',
  difficult_alpine_hiking: 'T6 · difficult alpine hiking',
};
const VISIBILITY_LABEL = {
  excellent: 'excellent', good: 'good', intermediate: 'intermittent',
  bad: 'hard to follow', horrible: 'barely visible', no: 'no trail',
};

let hovered = null;               // { layer, base, theme }

// ---- stored view scores for trails, peaks and passes ----------------------
//
// Hover one of these and press V: the server scores it (averaged every 50 m
// along a trail, at the summit for a peak, at the node for a pass; see
// scripts/featurescore.py) and stores the result keyed by OSM id. Stored
// scores are loaded once at start-up and shown in the hover card from then
// on, across reloads. The server drops any score computed under another
// version of the method, so what arrives here is always current.
const SCORABLE_THEMES = new Set(['trails', 'peaks', 'passes', 'water_areas']);

/** Whether a hovered feature can be scored. Water areas only when the tiles
 *  mark them as a lake (featurescore.lake_area: standing fresh water of at
 *  least 0.5 ha), so ponds, rivers and basins do not offer V. */
function isScorable(theme, feature) {
  if (!SCORABLE_THEMES.has(theme) || !feature || !feature.id) return false;
  return theme !== 'water_areas' || !!feature.lake;
}

/** Records scored along a path: a trail, or a lake's shoreline. */
const isPathRecord = (r) => r && (r.kind === 'line' || r.kind === 'shore');
const FS_POLL_MS = 500;
const featureScores = new Map();   // OSM id -> stored score summary
const featureJobs = new Map();     // OSM id -> { state, done, total, error }

async function loadFeatureScores() {
  try {
    const r = await fetch(`/api/feature_scores?slug=${AOI}`);
    if (!r.ok) return;
    const data = await r.json();
    // each record once (the ranked list), and every OSM way or node id
    // mapped to its record (the hover card)
    featureItems.clear();
    for (const [key, rec] of Object.entries(data.items || {})) featureItems.set(key, rec);
    for (const [id, key] of Object.entries(data.ids || {})) {
      const rec = data.items[key];
      if (rec) featureScores.set(id, rec);
    }
  } catch (err) { /* server without the endpoint: cards just offer V */ }
}

/** Re-render the card if `fid` is the feature under the cursor. */
function refreshHoverCardFor(fid) {
  if (!hovered || !hovered.layer.feature || hovered.layer.feature.id !== fid) return;
  renderHoverCard(hovered.theme, hovered.layer.feature);
  showBestPoint(hovered.layer.feature);
  // new content changes the card's size, so find it a spot again
  if (!hovered.follow) hovered.follow = !placeHoverCardFor(hovered.layer, hovered.theme);
}

// The highest-V samples of a scored trail, marked while the trail is hovered:
// white dots labelled with their V. A single-way trail gets its best point;
// a multi-way trail gets every way's best, the trail's own best among them.
// They sit on the mouse-transparent overlay pane, as the view wedge does, so
// they can never take the hover away from the trail they belong to, and the
// labels are tooltips, which Leaflet already keeps out of the pointer's way.
// Labels are placed highest V first, each on the first side of its dot that
// does not cover a label already placed; one with no free side is left off
// and its dot stands alone.
let bestPoint = null;              // [layer, ...] currently on the map
const BEST_LABEL_SIDES = [['top', [0, -8]], ['bottom', [0, 8]],
  ['right', [8, 0]], ['left', [-8, 0]]];

/** The points to mark: every way's best on a multi-way trail, else the
 *  trail's. Per-way bests come from the stored samples, rounded to 6
 *  decimals, so points within 2e-6 deg are one point. */
function bestPoints(rec) {
  const pts = rec.way_stats ? Object.values(rec.way_stats).map((w) => w.best) : [];
  if (!pts.length) pts.push(rec.best);
  const out = [];
  for (const p of pts.sort((a, b) => b.V - a.V)) {
    const dup = out.some((q) => Math.abs(q.lat - p.lat) < 2e-6 && Math.abs(q.lon - p.lon) < 2e-6);
    if (!dup) out.push(p);
  }
  return out;
}

function showBestPoint(feature) {
  hideBestPoint();
  const rec = feature && feature.id && featureScores.get(feature.id);
  if (!isPathRecord(rec) || !rec.best) return;
  bestPoint = [];
  const placed = [];
  for (const p of bestPoints(rec)) {
    const at = [p.lat, p.lon];
    bestPoint.push(L.circleMarker(at, {
      renderer: panoOverlayRenderer, interactive: false, radius: 6,
      color: '#11141a', weight: 2, fillColor: '#ffffff', fillOpacity: 1,
    }).addTo(map));
    for (const [direction, offset] of BEST_LABEL_SIDES) {
      const label = L.tooltip({ permanent: true, direction, offset, className: 'best-tip' })
        .setLatLng(at).setContent(vsFmt(p.V)).addTo(map);
      const r = label.getElement().getBoundingClientRect();
      if (!placed.some((o) => r.left < o.right + 2 && r.right > o.left - 2
          && r.top < o.bottom + 2 && r.bottom > o.top - 2)) {
        placed.push(r);
        bestPoint.push(label);
        break;
      }
      map.removeLayer(label);
    }
  }
}

function hideBestPoint() {
  if (!bestPoint) return;
  for (const l of bestPoint) map.removeLayer(l);
  bestPoint = null;
}

async function scoreFeature(theme, fid) {
  const cur = featureJobs.get(fid);
  if (featureScores.has(fid) || (cur && cur.state === 'running')) return;
  const started = performance.now();
  featureJobs.set(fid, { state: 'running', done: 0, total: null, started });
  refreshHoverCardFor(fid);
  try {
    const r = await fetch('/api/feature_score', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slug: AOI, theme, id: fid }),
    });
    let st = await r.json();
    if (!r.ok) throw new Error(st.error || `HTTP ${r.status}`);
    while (st.state === 'running') {
      featureJobs.set(fid, { state: 'running', done: st.done || 0, total: st.total, started });
      refreshHoverCardFor(fid);
      await new Promise((res) => setTimeout(res, FS_POLL_MS));
      const q = await fetch(`/api/feature_score_job?job=${st.job}`);
      st = await q.json();
      if (!q.ok) throw new Error(st.error || `HTTP ${q.status}`);
    }
    if (st.state === 'error') throw new Error(st.error);
    // every way of the trail shares the score; the tiles only know way ids
    featureScores.set(fid, st.record);
    for (const wid of st.record.ways || []) featureScores.set(wid, st.record);
    featureItems.set(st.record.id, st.record);
    featureJobs.delete(fid);
    renderRank();
  } catch (err) {
    featureJobs.set(fid, { state: 'error', error: err.message });
  }
  refreshHoverCardFor(fid);
}

/** The card's view-score rows for a scorable feature. */
function scoreRows(fid) {
  const rows = [];
  const rec = featureScores.get(fid);
  const job = featureJobs.get(fid);
  if (rec) {
    if (rec.kind === 'shore') {
      rows.push(['View score', `${vsFmt(rec.V)} avg along the shore`, true]);
    } else if (rec.kind === 'line') {
      // the trail's average, and on a multi-way trail the hovered way's own;
      // the best points are marked on the map, so they are not repeated here
      const seg = rec.n_ways > 1 && rec.way_stats && rec.way_stats[fid];
      rows.push(['View score', seg
        ? `${vsFmt(rec.V)} avg · segment ${vsFmt(seg.V)}`
        : `${vsFmt(rec.V)}${rec.n > 1 ? ' avg' : ''}`, true]);
    } else {
      rows.push(['View score', vsFmt(rec.V), true]);
    }
  } else if (job && job.state === 'running') {
    let txt = 'starting…';
    if (job.total) {
      txt = `computing ${job.done}/${job.total}`;
      // time left from the rate so far, once there is a rate to go on
      if (job.done >= 3) {
        const left = (performance.now() - job.started) / job.done * (job.total - job.done) / 1000;
        txt += left >= 90 ? ` · ~${Math.round(left / 60)} min left` : ` · ~${Math.max(1, Math.round(left))} s left`;
      } else {
        txt += '…';
      }
    }
    rows.push(['View score', txt, true]);
  } else if (job && job.state === 'error') {
    rows.push(['View score', `failed: ${job.error}`, true]);
  } else {
    rows.push(['View score', 'press V', true]);
  }
  return rows;
}

document.addEventListener('keydown', (ev) => {
  if (ev.key !== 'v' && ev.key !== 'V') return;
  if (ev.ctrlKey || ev.metaKey || ev.altKey || ev.repeat) return;
  const t = ev.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
  if (!hovered || !isScorable(hovered.theme, hovered.layer.feature)
      || $('hovercard').hidden) return;
  const fid = hovered.layer.feature && hovered.layer.feature.id;
  if (!fid) return;
  ev.preventDefault();
  scoreFeature(hovered.theme, fid);
});

/** Tag values read better without OSM's underscores. */
const tagText = (v) => String(v).replace(/_/g, ' ');

function formatEle(props) {
  const m = parseFloat(String(props.ele || '').split(' ')[0]);
  if (!Number.isFinite(m)) return null;
  return `${Math.round(m).toLocaleString()} m · ${Math.round(m / 0.3048).toLocaleString()} ft`;
}

/** The few facts worth showing per theme, as [label, value] pairs. Only tags
 *  that are present make it in, so a sparsely tagged feature gets a short
 *  card rather than a column of dashes. */
function hoverFacts(theme, p) {
  const f = [];
  const add = (k, v) => { if (v !== undefined && v !== null && v !== '') f.push([k, v]); };
  switch (theme) {
    case 'trails':
      add('Difficulty', SAC_LABEL[p.sac_scale] || (p.sac_scale && tagText(p.sac_scale)));
      add('Surface', p.surface ? tagText(p.surface) : null);
      add('Visibility', VISIBILITY_LABEL[p.trail_visibility]);
      add('Ref', p.ref);
      break;
    case 'roads':
      add('Surface', IMPROVEMENT_LABELS[roadImprovement(p)]
        + (p.surface ? ` (${tagText(p.surface)})` : ''));
      add('Ref', p.ref);
      add('Speed limit', p.maxspeed);
      add('Lanes', p.lanes);
      add('Access', p.access && p.access !== 'yes' ? tagText(p.access) : null);
      break;
    case 'peaks':
      add('Elevation', formatEle(p));
      add('Prominence', p.prominence ? `${p.prominence} m` : null);
      add('Volcano', p['volcano:status'] ? tagText(p['volcano:status']) : null);
      break;
    case 'passes':
      add('Elevation', formatEle(p));
      add('Crossed by', p.highway ? tagText(p.highway) : null);
      break;
    case 'poi':
      add('Elevation', formatEle(p));
      add('Backcountry', p.backcountry === 'yes' ? 'yes' : null);
      add('Fee', p.fee);
      add('Drinking water', p.drinking_water);
      add('Toilets', p.toilets);
      add('Reservation', p.reservation ? tagText(p.reservation) : null);
      add('Operator', p.operator);
      break;
    case 'water_areas':
      add('Elevation', formatEle(p));
      add('Intermittent', p.intermittent === 'yes' ? 'yes' : null);
      break;
    case 'water_lines':
      add('Intermittent', p.intermittent === 'yes' ? 'yes' : null);
      add('Underground', p.tunnel ? 'yes' : null);
      break;
    case 'glaciers':
      add('Elevation', formatEle(p));
      break;
    case 'protected':
      add('Designation', p.protection_title);
      add('Operator', p.operator);
      add('IUCN class', p.protect_class);
      break;
    default:
      add('Elevation', formatEle(p));
  }
  return f;
}

/** Length rows for a trail way: the segment and, when OSM splits the trail
 *  into several ways, the whole trail. Both come from the tile, measured on
 *  the raw geometry by process.py. */
function trailLengthRows(f) {
  if (!f || f.len === undefined) return [];
  if (!f.trail) return [['Length', formatDistance(f.len)]];
  return [['Length', `${formatDistance(f.len)} of ${formatDistance(f.trail_len)}`
    + ` (${f.trail_n} segments)`]];
}

function renderHoverCard(theme, feature) {
  const props = (feature && feature.properties) || {};
  const fid = feature && feature.id;
  const s = VECTOR_STYLE[theme];
  const c = classOf(theme, props);
  const card = $('hovercard');
  card.replaceChildren();
  const head = el('div', 'hc-head');
  const sw = el('i');
  sw.style.background = c.color;
  const name = el('b');
  name.textContent = props.name || c.label || s.label;
  head.append(sw, name);
  const sub = el('div', 'hc-sub');
  sub.textContent = props.name && c.label !== s.label ? `${c.label} · ${s.label}` : s.label;
  card.append(head, sub);
  const facts = hoverFacts(theme, props);
  if (theme === 'trails') facts.push(...trailLengthRows(feature));
  if (theme === 'water_areas' && feature && feature.area) facts.push(['Area', formatArea(feature.area)]);
  for (const [k, v] of facts) {
    const row = el('div', 'hc-row');
    const kk = el('span', 'k');
    kk.textContent = k;
    const vv = el('span', 'v');
    vv.textContent = String(v);
    row.append(kk, vv);
    card.appendChild(row);
  }
  const scorable = isScorable(theme, feature);
  if (scorable) {
    scoreRows(fid).forEach(([k, v, main], i) => {
      const row = el('div', `hc-row hc-score${i === 0 ? ' first' : ''}`);
      const kk = el('span', 'k');
      kk.textContent = k;
      const vv = el('span', main ? 'v main' : 'v');
      vv.textContent = v;
      row.append(kk, vv);
      card.appendChild(row);
    });
  }
  const foot = el('div', 'hc-foot');
  foot.textContent = scorable && !featureScores.has(fid)
    ? 'V: view score · Alt-click for all tags' : 'Alt-click for all tags';
  card.appendChild(foot);
  card.hidden = false;
}

/** A surface area in m², as hectares or km². */
function formatArea(m2) {
  return m2 >= 1e6 ? `${(m2 / 1e6).toFixed(m2 >= 1e7 ? 0 : 1)} km²`
    : `${(m2 / 1e4).toFixed(m2 >= 1e5 ? 0 : 1)} ha`;
}

/** Keep the card beside the pointer, flipping to the other side near the
 *  window edges so it never runs off screen. */
function placeHoverCard(clientX, clientY) {
  const card = $('hovercard');
  if (card.hidden) return;
  const w = card.offsetWidth, h = card.offsetHeight;
  let x = clientX + HOVER_CARD_GAP, y = clientY + HOVER_CARD_GAP;
  if (x + w > innerWidth - 4) x = clientX - HOVER_CARD_GAP - w;
  if (y + h > innerHeight - 4) y = clientY - HOVER_CARD_GAP - h;
  card.style.left = `${Math.max(4, x)}px`;
  card.style.top = `${Math.max(4, y)}px`;
}

// ---- where the hover card goes --------------------------------------------
//
// Following the cursor put the card right over whatever was next to the
// feature, often the rest of the trail. For a line (a trail, road or river)
// the card is instead placed once, beside the middle of the hovered way's
// visible part: the nearest spot, searched outward in every direction, that
// stays inside the visible map and keeps HOVER_CARD_BUFFER px clear of the
// way, of the best-V dots and their labels, and of the panels floating over
// the map. It first tries to stay clear of the whole trail too, within
// HOVER_CARD_TRAIL_SLACK px of extra distance, else just the hovered way.
// A point gets the same search around itself. An area, or a line with nowhere
// free, falls back to following the cursor.

const HOVER_CARD_BUFFER = 14;      // px clear of the feature and other obstacles
const HOVER_CARD_DIRS = 16;        // directions tried around the anchor
const HOVER_CARD_STEP = 12;        // px between candidate distances
const HOVER_CARD_MAX_D = 640;      // furthest a card may sit from its anchor
// Clearing the whole trail is worth at most this much extra distance from the
// hovered way; past it, staying near the way wins. Without a limit a short
// segment in a tangle of trail sent the card 190 px away to a corner.
const HOVER_CARD_TRAIL_SLACK = 80;
const INSPECT_FULL_H = 280;        // the cursor card filled in: 200 px, ~260 with a viewpoint open

/** The part of the map not covered by the side panels, in client pixels. */
function visibleMapRect() {
  const m = map.getContainer().getBoundingClientRect();
  let l = m.left;
  const panel = $('panel');
  if (!panel.classList.contains('hidden')) l = Math.max(l, panel.getBoundingClientRect().right);
  return { l: l + 4, t: m.top + 4, r: m.right - 4, b: m.bottom - 4 };
}

/** Rectangles the card must not cover: floating panels and best-V markers. */
function cardObstacles() {
  const out = [];
  const add = (r, pad = 4) => {
    if (r && r.width && r.height) {
      out.push({ l: r.left - pad, t: r.top - pad, r: r.right + pad, b: r.bottom + pad });
    }
  };
  for (const id of ['inspect', 'vscore', 'rank-toggle', 'panel-toggle']) {
    const e = $(id);
    if (!e || e.hidden || e.offsetParent === null) continue;
    const r = e.getBoundingClientRect();
    if (id === 'inspect') {
      // The cursor card fills in a moment after the pointer moves (its
      // lookup is debounced), so at placement time it is often still the
      // one-line hint. Reserve the height it grows to, or the hover card
      // lands where it is about to be.
      add({ left: r.left, top: r.top, right: r.right, width: r.width,
            bottom: r.top + Math.max(r.height, INSPECT_FULL_H), height: 1 });
    } else {
      add(r);
    }
  }
  const mr = map.getContainer().getBoundingClientRect();
  for (const l of bestPoint || []) {
    if (l.getElement && l.getElement() && !(l instanceof L.CircleMarker)) {
      add(l.getElement().getBoundingClientRect(), HOVER_CARD_BUFFER / 2);
    } else if (l.getLatLng) {
      const p = map.latLngToContainerPoint(l.getLatLng());
      const r = 8 + HOVER_CARD_BUFFER / 2;
      out.push({ l: mr.left + p.x - r, t: mr.top + p.y - r, r: mr.left + p.x + r, b: mr.top + p.y + r });
    }
  }
  return out;
}

/** A layer's lines as client-pixel polylines. */
function clientLines(layer) {
  const mr = map.getContainer().getBoundingClientRect();
  const ll = layer.getLatLngs();
  const lines = ll.length && ll[0] instanceof L.LatLng ? [ll] : ll.flat(1);
  return lines.map((line) => line.map((p) => {
    const c = map.latLngToContainerPoint(p);
    return [mr.left + c.x, mr.top + c.y];
  }));
}

/** Segments [x1, y1, x2, y2] with their bounding boxes, for fast rejection. */
function toSegments(lines) {
  const out = [];
  for (const line of lines) {
    for (let i = 0; i + 1 < line.length; i++) {
      const [x1, y1] = line[i], [x2, y2] = line[i + 1];
      out.push([x1, y1, x2, y2, Math.min(x1, x2), Math.min(y1, y2), Math.max(x1, x2), Math.max(y1, y2)]);
    }
  }
  return out;
}

/** Whether a segment comes within `buf` of a rectangle. */
function segNearRect(s, R, buf) {
  const l = R.l - buf, t = R.t - buf, r = R.r + buf, b = R.b + buf;
  if (s[6] < l || s[4] > r || s[7] < t || s[5] > b) return false;   // bbox miss
  const inside = (x, y) => x >= l && x <= r && y >= t && y <= b;
  if (inside(s[0], s[1]) || inside(s[2], s[3])) return true;
  // otherwise it is near only if it crosses the expanded rectangle's edges
  const cross = (ax, ay, bx, by, cx, cy, dx, dy) => {
    const d = (bx - ax) * (dy - cy) - (by - ay) * (dx - cx);
    if (!d) return false;
    const u = ((cx - ax) * (dy - cy) - (cy - ay) * (dx - cx)) / d;
    const v = ((cx - ax) * (by - ay) - (cy - ay) * (bx - ax)) / d;
    return u >= 0 && u <= 1 && v >= 0 && v <= 1;
  };
  const [x1, y1, x2, y2] = s;
  return cross(x1, y1, x2, y2, l, t, r, t) || cross(x1, y1, x2, y2, r, t, r, b)
    || cross(x1, y1, x2, y2, l, b, r, b) || cross(x1, y1, x2, y2, l, t, l, b);
}

/** The point halfway along the part of the lines inside `vis`. */
function visibleMidpoint(lines, vis) {
  const inV = (p) => p[0] >= vis.l && p[0] <= vis.r && p[1] >= vis.t && p[1] <= vis.b;
  const segs = [];
  let total = 0;
  for (const line of lines) {
    for (let i = 0; i + 1 < line.length; i++) {
      if (!inV(line[i]) || !inV(line[i + 1])) continue;
      const len = Math.hypot(line[i + 1][0] - line[i][0], line[i + 1][1] - line[i][1]);
      segs.push([line[i], line[i + 1], len]);
      total += len;
    }
  }
  if (!segs.length) return null;
  let acc = 0;
  for (const [a, b, len] of segs) {
    if (acc + len >= total / 2) {
      const f = len ? (total / 2 - acc) / len : 0;
      return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f];
    }
    acc += len;
  }
  return segs[segs.length - 1][1];
}

/** The nearest free spot for a w x h card around `anchor`, or null. */
function findCardSpot(anchor, w, h, segments, rects, vis, buf) {
  const ok = (R) => {
    if (R.l < vis.l || R.t < vis.t || R.r > vis.r || R.b > vis.b) return false;
    for (const o of rects) if (R.l < o.r && R.r > o.l && R.t < o.b && R.b > o.t) return false;
    for (const s of segments) if (segNearRect(s, R, buf)) return false;
    return true;
  };
  for (let d = HOVER_CARD_STEP; d <= HOVER_CARD_MAX_D; d += HOVER_CARD_STEP) {
    for (let k = 0; k < HOVER_CARD_DIRS; k++) {
      const a = (k / HOVER_CARD_DIRS) * 2 * Math.PI;
      // the card's nearest edge sits d from the anchor, along this direction
      const cx = anchor[0] + Math.cos(a) * (d + w / 2);
      const cy = anchor[1] + Math.sin(a) * (d + h / 2);
      const R = { l: cx - w / 2, t: cy - h / 2, r: cx + w / 2, b: cy + h / 2, d };
      if (ok(R)) return R;
    }
  }
  return null;
}

/** Place the card for the hovered feature; false means "follow the cursor". */
function placeHoverCardFor(layer, theme) {
  const card = $('hovercard');
  if (card.hidden) return false;
  const w = card.offsetWidth, h = card.offsetHeight;
  const vis = visibleMapRect();
  const rects = cardObstacles();
  let anchor = null;
  const tiers = [];
  if (layer.getLatLngs && !layer.options.fill) {
    const own = toSegments(clientLines(layer));
    anchor = visibleMidpoint(clientLines(layer), vis);
    const trail = layer.feature && layer.feature.trail;
    if (trail) {
      const all = [];
      for (const l of trailLayers.get(trail) || []) if (map.hasLayer(l)) all.push(...toSegments(clientLines(l)));
      tiers.push(all);                  // clear of the whole trail, if it can be
    }
    tiers.push(own);                    // otherwise clear of the hovered way
  } else if (layer.getLatLng) {
    const mr = map.getContainer().getBoundingClientRect();
    const p = map.latLngToContainerPoint(layer.getLatLng());
    anchor = [mr.left + p.x, mr.top + p.y];
    const r = (layer.getRadius ? layer.getRadius() : 6) + 2;
    rects.push({ l: anchor[0] - r, t: anchor[1] - r, r: anchor[0] + r, b: anchor[1] + r });
    tiers.push([]);
  }
  if (!anchor) return false;
  // the buffer is a visible gap: measured from the line's edge, not its
  // centre, so add half the drawn stroke (the hovered way is the widest)
  const buf = HOVER_CARD_BUFFER + (layer.options.weight || 2) / 2;
  const spots = tiers.map((segs) => findCardSpot(anchor, w, h, segs, rects, vis, buf));
  let R = spots[0];
  const near = spots[spots.length - 1];      // clear of the hovered way only
  if (!R || (near && R.d > near.d + HOVER_CARD_TRAIL_SLACK)) R = near;
  if (!R) return false;
  card.style.left = `${Math.round(R.l)}px`;
  card.style.top = `${Math.round(R.t)}px`;
  return true;
}

const pathBase = (o) => ({ weight: o.weight, opacity: o.opacity,
  fillOpacity: o.fillOpacity, color: o.color, dashArray: o.dashArray });

function hoverStart(theme, layer, ev) {
  if (hovered && hovered.layer !== layer) hoverEnd();
  if (!hovered) {
    const o = layer.options;
    if (layer.setRadius) {
      hovered = { layer, theme, base: { radius: layer.getRadius(), weight: o.weight, color: o.color } };
      layer.setRadius(hovered.base.radius * HOVER_RADIUS_MUL);
      layer.setStyle({ weight: 2, color: '#ffffff' });
    } else {
      hovered = { layer, theme, base: pathBase(o), rest: [] };
      const st = { weight: (o.weight || 1) + HOVER_WEIGHT_ADD, opacity: 1 };
      if (o.fill) st.fillOpacity = Math.min(0.95, (o.fillOpacity ?? 0.5) + 0.15);
      layer.setStyle(st);
      // the rest of a multi-way trail, drawn the same as the hovered way
      const trail = layer.feature && layer.feature.trail;
      for (const other of (trail && trailLayers.get(trail)) || []) {
        if (other === layer) continue;
        hovered.rest.push({ layer: other, base: pathBase(other.options) });
        other.setStyle({ weight: (other.options.weight || 1) + HOVER_WEIGHT_ADD, opacity: 1 });
      }
    }
  }
  renderHoverCard(theme, layer.feature);
  if (theme === 'trails' || theme === 'water_areas') showBestPoint(layer.feature);
  hovered.follow = !placeHoverCardFor(layer, theme);
  if (hovered.follow && ev && ev.originalEvent) {
    placeHoverCard(ev.originalEvent.clientX, ev.originalEvent.clientY);
  }
}

function hoverEnd(layer) {
  if (!hovered || (layer && hovered.layer !== layer)) return;
  const { layer: l, base, rest } = hovered;
  if (l.setRadius) {
    l.setRadius(base.radius);
    l.setStyle({ weight: base.weight, color: base.color });
  } else {
    l.setStyle(base);
  }
  for (const r of rest || []) r.layer.setStyle(r.base);
  hovered = null;
  $('hovercard').hidden = true;
  hideBestPoint();
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
      // the full tag table, behind Alt-click (a plain click picks a viewpoint)
      lyr.bindPopup(tagTable(f.properties), { maxWidth: 320 });
      lyr.on('mouseover', (e) => hoverStart(theme, lyr, e));
      lyr.on('mouseout', () => hoverEnd(lyr));
      // a chunk swapped out for another LOD tier never sends mouseout
      lyr.on('remove', () => hoverEnd(lyr));
      // a selected trail or lake in the ranked list stays marked as its
      // tiles reload
      if (theme === 'trails' || theme === 'water_areas') lyr.on('add', () => markRankLayer(lyr));
      if (f.trail) {
        lyr.on('add', () => {
          if (!trailLayers.has(f.trail)) trailLayers.set(f.trail, new Set());
          trailLayers.get(f.trail).add(lyr);
        });
        lyr.on('remove', () => {
          const set = trailLayers.get(f.trail);
          if (set) { set.delete(lyr); if (!set.size) trailLayers.delete(f.trail); }
        });
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
      cb: null,           // its checkbox, so the ranked list can switch it on
    };

    const maxFeatures = Math.max(...tiers.map((t) => t.features));
    const row = el('div', `layer${vectorLayers[theme].enabled ? '' : ' off'}`);
    const head = el('label', 'layer-head');
    const cb = el('input');
    cb.type = 'checkbox';
    cb.checked = vectorLayers[theme].enabled;
    vectorLayers[theme].cb = cb;
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
// cursor inspector — reports every raster layer, not just the visible ones.
// Vector features are described by the hover card instead (hoverStart).

const INSPECT_DEBOUNCE_MS = 110;
const RASTER_ORDER = ['dem', 'slope', 'ruggedness', 'relief_sigma',
  'canopy_density', 'builtup_density', 'landcover', 'hillshade'];
const RASTER_LABEL = {
  dem: 'Elevation', slope: 'Slope', ruggedness: 'Ruggedness',
  relief_sigma: 'Relief', canopy_density: 'Canopy',
  builtup_density: 'Built-up', landcover: 'Land cover', hillshade: 'Hillshade',
};

let inspectSeq = 0;
let inspectTimer = null;

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

function renderInspector(latlng, rasters) {
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
}

async function updateInspector(latlng) {
  const seq = ++inspectSeq;
  let rasters = null;
  try {
    const r = await fetch(
      `/api/point?lat=${latlng.lat}&lon=${latlng.lng}&slug=${AOI}`);
    if (r.ok) rasters = (await r.json()).layers;
  } catch (err) { /* server down — the rows show as missing */ }
  if (seq !== inspectSeq) return;   // a later move superseded this one
  renderInspector(latlng, rasters);
}

map.on('mousemove', (ev) => {
  clearTimeout(inspectTimer);
  inspectTimer = setTimeout(() => updateInspector(ev.latlng), INSPECT_DEBOUNCE_MS);
  // not debounced: this is pure arithmetic against data already in memory, and
  // a laggy dot would defeat the point of the pairing
  showMapCursorOnPano(ev.latlng);
  if (hovered && hovered.follow) placeHoverCard(ev.originalEvent.clientX, ev.originalEvent.clientY);
});
map.on('mouseout', () => { showMapCursorOnPano(null); hoverEnd(); });
// the feature under a still cursor changes when the map moves under it
map.on('movestart zoomstart', () => hoverEnd());
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

/** Make the map's viewpoint wherever the panorama camera now stands.
 *
 *  Walking with WASD moves the camera off the mesh centre. As far as the map
 *  is concerned that is the same as having clicked there: the marker moves,
 *  the wedge starts from it, the cursor card measures from it, and a range
 *  change rebuilds the panorama there. The leader line from a click to the
 *  summit it snapped to belongs to the original click, so it shows only
 *  while the camera is back on that summit. */
function syncViewpointToCamera(c) {
  panoLatLng = L.latLng(c.lat, c.lon);
  if (panoMarker) panoMarker.setLatLng(panoLatLng);
  if (panoSnapLine) {
    const show = c.moved < 0.5;
    if (show && !map.hasLayer(panoSnapLine)) panoSnapLine.addTo(map);
    else if (!show && map.hasLayer(panoSnapLine)) map.removeLayer(panoSnapLine);
  }
}

/** Draw where the panorama camera is looking, as a wedge on the map, from
 *  where the camera stands. */
function updatePanoFovOnMap(p) {
  if (!p || !p.data) return;
  const h = p.data.header;
  const c = cameraLatLng(p);
  // the camera's vertical fov is the parameter; the wedge needs horizontal
  const hfov = panoHfovDeg(p);
  const key = `${p.yaw.toFixed(1)}|${hfov.toFixed(1)}|${h.range_km}`
    + `|${c.lat.toFixed(6)}|${c.lon.toFixed(6)}`;
  if (key === panoFovKey) return;         // nothing moved; skip the redraw
  panoFovKey = key;
  syncViewpointToCamera(c);

  const R = h.range_km * 1000;
  const pts = [[c.lat, c.lon]];
  const n = 24;
  for (let i = 0; i <= n; i++) {
    pts.push(destPoint(c.lat, c.lon, p.yaw - hfov / 2 + hfov * i / n, R));
  }
  const axis = [[c.lat, c.lon], destPoint(c.lat, c.lon, p.yaw, R)];

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
// drawing it, so it does not run inline with the render loop. It is
// throttled: at most once per MAP_SYNC_IDLE_MS, the first change drawn at
// once so a single arrow tap feels instant, and the last one always drawn.
// A throttle rather than a debounce, so the viewpoint marker and wedge
// follow the camera during a WASD walk instead of jumping when it stops.
const MAP_SYNC_IDLE_MS = 100;
let mapSyncTimer = null;
let mapSyncLast = 0;

function scheduleMapSync(p) {
  const now = performance.now();
  if (now - mapSyncLast > MAP_SYNC_IDLE_MS) {   // idle: update straight away
    clearTimeout(mapSyncTimer);
    mapSyncTimer = null;
    mapSyncLast = now;
    updatePanoFovOnMap(p);
    return;
  }
  if (mapSyncTimer) return;                     // one is already due
  mapSyncTimer = setTimeout(() => {
    mapSyncTimer = null;
    mapSyncLast = performance.now();
    if (pano && !$('pano').hidden) updatePanoFovOnMap(pano);
  }, MAP_SYNC_IDLE_MS - (now - mapSyncLast));
}

function clearPanoFovOnMap() {
  clearTimeout(mapSyncTimer);
  mapSyncTimer = null;
  if (panoFovCasing) { map.removeLayer(panoFovCasing); panoFovCasing = null; }
  if (panoFov) { map.removeLayer(panoFov); panoFov = null; }
  if (panoAxis) { map.removeLayer(panoAxis); panoAxis = null; }
  panoFovKey = '';
}
let panoMarker = null;
let panoLatLng = null;
let panoSeq = 0;

const $ = (id) => document.getElementById(id);

// A plain left click on the map picks a viewpoint. It is intercepted on
// `document` in the CAPTURE phase rather than via map.on('click'): Leaflet's
// popup binding calls stopPropagation() when a feature is clicked, so the
// map's own click event never fires whenever the cursor happens to be over a
// peak or a trail. Capturing at the document level runs before any Leaflet
// handler, so the pick works everywhere. Alt-click is let through, so a
// feature's full tag table is still one click away.
let pickDownPt = null;

function pickTarget(ev) {
  const t = ev.target;
  return map.getContainer().contains(t)
    && !t.closest('.leaflet-control-container, .leaflet-popup');
}

document.addEventListener('mousedown', (ev) => {
  pickDownPt = ev.button === 0 && pickTarget(ev)
    ? { x: ev.clientX, y: ev.clientY } : null;
}, true);

document.addEventListener('click', (ev) => {
  if (ev.button !== 0 || ev.altKey || !pickTarget(ev)) return;
  // a click that ends a pan should not drop a viewpoint
  if (!pickDownPt || Math.hypot(ev.clientX - pickDownPt.x,
    ev.clientY - pickDownPt.y) > 6) { pickDownPt = null; return; }
  pickDownPt = null;
  ev.preventDefault();
  ev.stopPropagation();          // keeps Leaflet from opening a feature popup
  // the second click of a double-click is a zoom, not a second pick
  if (ev.detail > 1) return;
  const latlng = map.containerPointToLatLng(map.mouseEventToContainerPoint(ev));
  renderPanorama(latlng);
}, true);

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
  scheduleFocus();
  updateVsFov(p);

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
  // pickGround measures from the mesh centre, so that places the dot; the
  // label is measured from the viewpoint, which walks with the camera
  const h = pano.data.header;
  const p = destPoint(h.lat, h.lon, hit.az, hit.dist);
  const ll = L.latLng(p[0], p[1]);
  const from = geoInverse(panoLatLng, ll);
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
    `${(from.dist / 1000).toFixed(1)} km · ${Math.round(from.bearing) % 360}°`);
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
          return pano.loadTextures(sn.lat, sn.lon, AOI, rangeKm);
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

  // the score needs only the snapped point, so it runs alongside the imagery
  loadViewScore(header.lat, header.lon);

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
  // the view may already be zoomed in from the previous point
  scheduleFocus();

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

  // manual snap button: offers the nearest higher summit, further out than the
  // automatic snap reaches, and greys out when there is none within range
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
  scheduleFocus();
}
$('pano-surface').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-surface]');
  if (b) setPanoSurface(b.dataset.surface);
});

// The imagery ladder is built for a wide view. Zooming in raises the screen's
// angular resolution past what it holds, so once the view settles a sharper
// focus patch is fetched around whatever is in the middle of the screen (see
// Pano3D.updateFocus). Debounced, because the wheel and the drag fire
// continuously and each patch is a couple of seconds of server warping.
const FOCUS_SETTLE_MS = 350;
let focusTimer = null;

function scheduleFocus() {
  if (!pano || !pano.data || !pano.texWanted || !pano.texReady
    || !imageryAvailable()) return;
  clearTimeout(focusTimer);
  focusTimer = setTimeout(() => {
    if (!pano || !pano.data || !pano.texWanted) return;
    pano.updateFocus(AOI);
  }, FOCUS_SETTLE_MS);
}

// ---------------------------------------------------------------------------
// View score V (scripts/vscore.py, docs/visibility-metric.md).
//
// Computed for the panorama's own snapped viewpoint, in parallel with the
// imagery, and again wherever the camera is walked to with WASD/arrows once it
// has stood still for VS_MOVE_SETTLE_MS. It is a property of the place, not
// of the view: eye height (2 m), haze (60 km) and range (150 km) are the
// score's own fixed parameters, so the sliders and the gaze do not change it.

let vsSeq = 0;
const vsCache = new Map();         // "lat,lon" -> result, most recent last
const VS_CACHE_MAX = 32;
const VS_MOVE_SETTLE_MS = 500;
let vsMoveTimer = null;
const VS_DIR_BIN = 10;             // degrees per column in the direction chart
const VS_MIN_SHARE = 0.001;        // classes below this fold into "other"

const vsFmt = (x) => {
  const a = Math.abs(x);
  return a >= 100 ? x.toFixed(0) : a >= 10 ? x.toFixed(1) : x.toFixed(2);
};
const vsPct = (x) => {
  const p = x * 100;
  return `${Math.abs(p) >= 10 || p === 0 ? p.toFixed(0) : p.toFixed(1)}%`;
};
const vsKm = (km) => (km >= 10 ? `${km.toFixed(0)} km` : km >= 1
  ? `${km.toFixed(1)} km` : `${Math.round(km * 1000)} m`);

/** Horizontal field of view of the panorama camera, degrees. */
function panoHfovDeg(p) {
  const aspect = p.canvas.clientWidth / Math.max(1, p.canvas.clientHeight);
  return 2 * Math.atan(Math.tan(p.fov * Math.PI / 360) * aspect) * 180 / Math.PI;
}

/** Score a point and show it.
 *
 *  `movedM` is set when the point is the camera walked away from the
 *  viewpoint: the old breakdown then stays up, dimmed, until the new one
 *  lands, rather than blanking the panel on every step. */
async function loadViewScore(lat, lon, movedM) {
  clearTimeout(vsMoveTimer);
  const box = $('vscore');
  box.hidden = false;
  $('vs-where').textContent = `${lat.toFixed(4)}, ${lon.toFixed(4)}`
    + (movedM >= 0.5 ? ` · moved ${Math.round(movedM)} m` : '');
  const key = `${lat.toFixed(6)},${lon.toFixed(6)}`;
  vsPoint = { lat, lon };
  // the slider-height comparison waits for the 2 m score; until then the one
  // on screen belongs to the previous point
  clearTimeout(vsAltTimer);
  vsAltSeq++;
  $('vs-alt').classList.add('stale');
  // a range change re-renders the panorama at the same point, and 0 walks the
  // camera back to it; V depends on neither, so reuse what was computed
  const hit = vsCache.get(key);
  if (hit) {
    vsSeq++;                                     // drop anything in flight
    vsCache.delete(key); vsCache.set(key, hit);  // most recent last
    renderViewScore(hit);
    loadAltScore();
    return;
  }
  const seq = ++vsSeq;
  $('vs-state').textContent = 'computing…';
  if (movedM === undefined) {
    $('vs-value').textContent = '…';
    $('vs-body').replaceChildren();
  } else {
    box.classList.add('stale');
  }
  try {
    const r = await fetch(`/api/vscore?lat=${lat}&lon=${lon}&slug=${AOI}`);
    const data = await r.json();
    if (seq !== vsSeq) return;                   // superseded by a later point
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    vsCache.set(key, data);
    if (vsCache.size > VS_CACHE_MAX) vsCache.delete(vsCache.keys().next().value);
    renderViewScore(data);
    loadAltScore();
  } catch (err) {
    if (seq !== vsSeq) return;
    box.classList.remove('stale');
    $('vs-value').textContent = '—';
    $('vs-state').textContent = `failed: ${err.message}`;
  }
}

/** Where the camera stands, as lat/lon: the mesh centre plus the camera
 *  offset, which is metres east and north in the mesh's observer-centred
 *  frame, so a bearing and a distance from the centre. */
function cameraLatLng(p) {
  const h = p.data.header;
  const [ox, oy] = p.camOffset;
  const d = Math.hypot(ox, oy);
  if (d < 0.5) return { lat: h.lat, lon: h.lon, moved: 0 };
  const [lat, lon] = destPoint(h.lat, h.lon, Math.atan2(ox, oy) * 180 / Math.PI, d);
  return { lat, lon, moved: d };
}

/** Called on every camera nudge and key release: mark the score stale now,
 *  and recompute it where the camera stands once movement has settled, i.e.
 *  no movement key is held and none has moved the camera for
 *  VS_MOVE_SETTLE_MS. Waiting on the keys as well as the clock matters: when
 *  frames arrive slowly (a busy or background tab) the gap between two steps
 *  of one held key can exceed the timeout, which fetched mid-walk. */
function scheduleScoreAtCamera() {
  if (!pano || !pano.data || $('vscore').hidden) return;
  $('vscore').classList.add('stale');
  $('vs-state').textContent = 'moved…';
  clearTimeout(vsMoveTimer);
  if (camHeld.size) return;                // keyup schedules it
  vsMoveTimer = setTimeout(() => {
    if (!pano || !pano.data || $('pano').hidden) return;
    const c = cameraLatLng(pano);
    loadViewScore(c.lat, c.lon, c.moved);
  }, VS_MOVE_SETTLE_MS);
}

// --- the same score at the panorama's eye height ---------------------------
//
// V is always scored at a standing 2 m, so it compares between points. This
// is a second figure beside it: V for the same spot at whatever height the
// eye slider is set to, to show how much a viewpoint gains from height. It is
// never used in place of the 2 m score, and it is fetched only after that one
// has landed, so the real number always arrives first.

const VS_STD_EYE_M = 2;
const VS_ALT_MIN_DIFF_M = 0.5;     // nearer 2 m than this is the same score
const VS_ALT_SETTLE_MS = 500;
let vsPoint = null;                // { lat, lon } the panel is scoring
let vsMainV = null;                // the 2 m score at vsPoint, once known
let vsAltSeq = 0;
let vsAltTimer = null;
let vsAltShown = null;             // { eye, v } currently displayed
const vsAltCache = new Map();

/** The slider's eye height, rounded so that small slider jitter maps to the
 *  same request: half metres below 10 m, whole metres above. */
function vsAltEye() {
  const h = pano ? pano.eyeHeight : VS_STD_EYE_M;
  return h < 10 ? Math.round(h * 2) / 2 : Math.round(h);
}

function vsEyeLabel(eye) {
  return eye < 1000 ? `${eye} m` : `${(eye / 1000).toFixed(2)} km`;
}

/** Draw the comparison line; `v` null means it is still being computed. */
// V's units depend on γ (sr·km^γ), so they come from the server's result
let vsUnits = 'sr·km';

/** Write the units into `node`, with any exponent as a superscript. */
function setUnits(node) {
  const [base, exp] = vsUnits.split('^');
  node.replaceChildren(document.createTextNode(base));
  if (exp) node.appendChild(el('sup', null, exp));
}

function showAltScore(eye, v) {
  vsAltShown = { eye, v };
  const box = $('vs-alt');
  const lbl = el('span', 'k');
  lbl.textContent = `At ${vsEyeLabel(eye)} eye height`;
  const val = el('span', 'v');
  if (v === null) {
    val.textContent = 'computing…';
  } else {
    const b = el('b');
    b.textContent = vsFmt(v);
    const u = el('span');
    setUnits(u);
    val.append(b, document.createTextNode(' '), u);
    if (vsMainV) {
      const d = v / vsMainV - 1;
      const pct = el('small');
      pct.textContent = ` ${d >= 0 ? '+' : '−'}${vsPct(Math.abs(d))} vs 2 m`;
      val.appendChild(pct);
    }
  }
  box.replaceChildren(lbl, val);
  box.hidden = false;
}

async function loadAltScore() {
  clearTimeout(vsAltTimer);
  const box = $('vs-alt');
  box.classList.remove('stale');
  if (!vsPoint || !pano) { box.hidden = true; return; }
  const eye = vsAltEye();
  if (Math.abs(eye - VS_STD_EYE_M) < VS_ALT_MIN_DIFF_M) {
    vsAltSeq++;
    vsAltShown = null;
    box.hidden = true;
    return;
  }
  const { lat, lon } = vsPoint;
  const key = `${lat.toFixed(6)},${lon.toFixed(6)},${eye}`;
  const hit = vsAltCache.get(key);
  if (hit !== undefined) { vsAltSeq++; showAltScore(eye, hit); return; }
  const seq = ++vsAltSeq;
  showAltScore(eye, null);
  try {
    const r = await fetch(`/api/vscore?lat=${lat}&lon=${lon}&slug=${AOI}&eye_m=${eye}`);
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    vsAltCache.set(key, data.V);
    if (vsAltCache.size > VS_CACHE_MAX) vsAltCache.delete(vsAltCache.keys().next().value);
    if (seq === vsAltSeq) showAltScore(eye, data.V);
  } catch (err) {
    if (seq !== vsAltSeq) return;
    const val = el('span', 'v');
    val.textContent = `failed: ${err.message}`;
    box.replaceChildren(val);
  }
}

/** The eye slider moved: dim the comparison now, refetch once it settles. */
function scheduleAltScore() {
  if (!vsPoint || $('vscore').hidden) return;
  clearTimeout(vsAltTimer);
  $('vs-alt').classList.add('stale');
  vsAltTimer = setTimeout(loadAltScore, VS_ALT_SETTLE_MS);
}

/** A label/value row; both set as text, never as markup. */
function vsRow(k, v) {
  const r = el('div', 'vs-row');
  r.append(el('span', 'k'), el('span', 'v'));
  r.firstChild.textContent = k;
  r.lastChild.textContent = v;
  return r;
}

function vsSection(title) {
  const s = el('div', 'vs-sec');
  const h = el('h3');
  h.textContent = title;
  s.appendChild(h);
  return s;
}

/** Share-of-V bars: one grid row per item, value and share at the tip. */
function vsBars(items, maxShare) {
  const g = el('div', 'vs-bars');
  for (const it of items) {
    const lbl = el('span', 'lbl');
    if (it.swatch) {
      const sw = el('span', 'vs-sw');
      sw.style.background = it.swatch;
      lbl.appendChild(sw);
    }
    lbl.appendChild(document.createTextNode(it.label));
    const trk = el('span', 'trk');
    const bar = el('span', `bar${it.share < 0 ? ' neg' : ''}`);
    bar.style.width = `${Math.min(100, 100 * Math.abs(it.share) / maxShare)}%`;
    trk.appendChild(bar);
    const num = el('span', 'num');
    const b = el('b');
    b.textContent = vsFmt(it.v);
    const sm = el('small');
    sm.textContent = ` ${vsPct(it.share)} · ${it.omega.toFixed(2)} sr`;
    num.append(b, sm);
    g.append(lbl, trk, num);
  }
  return g;
}

function renderViewScore(d) {
  const vis = d.visible, ax = d.axes, P = d.params;
  $('vscore').classList.remove('stale');
  vsMainV = d.V;
  vsUnits = d.units || vsUnits;
  $('vs-value').textContent = vsFmt(d.V);
  setUnits(document.querySelector('#vscore .vs-unit'));
  $('vs-state').textContent = `${d.elapsed_ms} ms`;
  const body = $('vs-body');
  body.replaceChildren();

  // --- the terms of the integral, summarised --------------------------------
  const s1 = vsSection('What went in');
  s1.append(
    vsRow('Visible terrain, ΔΩ',
      `${vis.omega_sr.toFixed(2)} sr (${vsPct(vis.omega_frac_sphere)} of sphere)`),
    vsRow('Median distance, by solid angle', vsKm(vis.median_dist_km)),
    vsRow('dᵞ at that distance', (vis.median_dist_km ** P.gamma).toFixed(2)),
    vsRow('Mean land-cover weight c', vis.mean_class_weight.toFixed(2)),
    vsRow('Haze factor w', `${vis.haze_factor.toFixed(2)} (V ${vsFmt(ax.v_no_haze)} without)`),
    vsRow('Canopy factor T', `${vis.canopy_factor.toFixed(2)} (V ${vsFmt(ax.v_no_canopy)} without trees)`),
    vsRow('Looming factor u', `${vis.loom_factor.toFixed(2)} (V ${vsFmt(ax.v_no_loom)} without)`),
  );
  const s1b = el('div', 'vs-note');
  s1b.textContent = 'The same sum at the two extremes of γ:';
  s1.append(s1b,
    vsRow('Dominance, γ = 0 (solid angle)', `${ax.dominance_sr.toFixed(2)} sr`),
    vsRow('Extent, γ = 2 (visible area)', `${vsFmt(ax.extent_km2)} km²`));
  body.appendChild(s1);

  // --- by distance band ------------------------------------------------------
  const s2 = vsSection('By distance');
  const bands = d.by_distance;
  const cut = bands.filter((b) => b.coverage < 0.99);
  s2.appendChild(vsBars(bands.map((b) => ({
    label: `${b.from_km}–${b.to_km} km${b.coverage < 0.99 ? '*' : ''}`,
    v: b.v, share: b.share, omega: b.omega_sr,
  })), Math.max(...bands.map((b) => Math.abs(b.share)), 1e-9)));
  if (cut.length) {
    const n = el('p', 'vs-note');
    n.textContent = `* the DEM ends inside ${cut.length === 1 ? 'this band' : 'these bands'}`
      + ` on some rays (${cut.map((b) => `${b.from_km}–${b.to_km} km: `
      + `${vsPct(b.coverage)} covered`).join(', ')}), so their V is a lower bound.`;
    s2.appendChild(n);
  }
  body.appendChild(s2);

  // --- by land cover at the visible terrain ----------------------------------
  const s3 = vsSection('By land cover at the visible terrain');
  const colours = {};
  const lcSpec = (manifest && manifest.raster_layers || []).find((l) => l.id === 'landcover');
  for (const e of (lcSpec && lcSpec.legend) || []) {
    colours[e.label] = `rgb(${e.color.join(',')})`;
  }
  const main = d.by_class.filter((c) => Math.abs(c.share) >= VS_MIN_SHARE);
  const rest = d.by_class.filter((c) => Math.abs(c.share) < VS_MIN_SHARE);
  const items = main.map((c) => ({
    label: `${c.name} · c ${c.weight}`, swatch: colours[c.name] || '#8d97a3',
    v: c.v, share: c.share, omega: c.omega_sr,
  }));
  if (rest.length) {
    items.push({
      label: `Other (${rest.length})`,
      v: rest.reduce((a, c) => a + c.v, 0),
      share: rest.reduce((a, c) => a + c.share, 0),
      omega: rest.reduce((a, c) => a + c.omega_sr, 0),
    });
  }
  s3.appendChild(vsBars(items, Math.max(...items.map((i) => Math.abs(i.share)), 1e-9)));
  body.appendChild(s3);

  // --- by direction ------------------------------------------------------------
  const s4 = vsSection('By direction');
  s4.appendChild(vsDirectionChart(d.by_azimuth.v, d.V));
  const n4 = el('p', 'vs-note');
  n4.textContent = 'Shaded: where the panorama camera is pointing.';
  s4.appendChild(n4);
  body.appendChild(s4);

  // --- by-products of the same march -----------------------------------------
  const s5 = vsSection('Also from the march');
  s5.append(
    vsRow('Sky', `${vsPct(vis.sky_frac_sphere)} of sphere`),
    vsRow('Horizon', `${vis.horizon_min_deg.toFixed(2)}° to ${vis.horizon_max_deg >= 0 ? '+' : ''}`
      + `${vis.horizon_max_deg.toFixed(2)}°`),
    vsRow('Farthest visible terrain', vsKm(vis.max_sightline_km)),
  );
  if (vis.lookdown_deg !== null) {
    s5.appendChild(vsRow('Steepest look-down',
      `${vis.lookdown_deg.toFixed(1)}° at ${vsKm(vis.lookdown_dist_km)}, `
      + `bearing ${Math.round(vis.lookdown_az)}°`));
  }
  if (vis.relief_min_m !== null) {
    s5.appendChild(vsRow('Visible elevations',
      `${Math.round(vis.relief_min_m)}–${Math.round(vis.relief_max_m)} m`));
  }
  const cov = d.coverage;
  if (cov.nearest_edge_km !== null) {
    s5.appendChild(vsRow('DEM edge along the rays',
      `${vsKm(cov.nearest_edge_km)} nearest, ${vsKm(cov.median_edge_km)} median`));
  }
  body.appendChild(s5);

  // --- parameters ------------------------------------------------------------
  const det = el('details', 'vs-sec vs-params');
  const sum = el('summary');
  sum.textContent = 'Parameters';
  det.appendChild(sum);
  det.append(
    vsRow('γ, distance exponent', String(P.gamma)),
    vsRow('w(d): near cutoff', `${P.near_cutoff_m} m`),
    vsRow('w(d): haze taper exp(−d / dₕ)', `dₕ = ${P.d_haze_km} km`),
    vsRow('u(φ): looming, above eye level',
      `up to ×${1 + P.loom_gain}, full at ${P.loom_full_deg}°`),
    vsRow('T: canopy exp(−path in trees / λ)',
      `λ = ${P.canopy_mfp_m} m, below ${P.canopy_h_m} m, first ${P.canopy_reach_m} m`),
    vsRow('Observer', `${d.ground_m} m ground + ${d.eye_m} m eye`),
    vsRow('March', `${P.n_az} rays × ${P.step_m} m to ${P.range_km} km`),
    vsRow('Curvature + refraction', `k = ${P.refraction_k}`),
  );
  const cw = el('div', 'vs-bars');
  for (const c of P.class_weights) {
    const lbl = el('span', 'lbl');
    const sw = el('span', 'vs-sw');
    sw.style.background = colours[c.name] || '#8d97a3';
    lbl.append(sw, document.createTextNode(c.name));
    const num = el('span', 'num');
    num.textContent = `c = ${c.weight}`;
    cw.append(lbl, el('span'), num);
  }
  det.appendChild(cw);
  body.appendChild(det);

  if (pano) updateVsFov(pano);
}

/** Columns of V per VS_DIR_BIN degrees, compass-labelled, with a hover
 *  readout per column and a band for the camera's current field of view. */
function vsDirectionChart(perDeg, total) {
  const n = 360 / VS_DIR_BIN;
  const bins = new Array(n).fill(0);
  perDeg.forEach((v, i) => { bins[Math.floor(i / VS_DIR_BIN) % n] += v; });
  const max = Math.max(...bins, 1e-9);
  const W = 360, H = 56, gap = 2, colW = W / n - gap, r = 3;
  const NS = 'http://www.w3.org/2000/svg';
  const mk = (tag, attrs) => {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  };
  const wrap = el('div', 'vs-dir');
  const svg = mk('svg', { viewBox: `0 0 ${W} ${H + 14}`, preserveAspectRatio: 'none' });
  const fov = mk('g', {});
  fov.id = 'vs-fov';
  svg.appendChild(fov);
  const tip = el('div', 'vs-tip');
  tip.hidden = true;
  bins.forEach((v, i) => {
    const x = i * (W / n) + gap / 2;
    const h = Math.max(0, (v / max) * (H - 4));
    const y = H - h;
    // rounded at the data end, square on the baseline
    const rr = Math.min(r, h, colW / 2);
    const col = mk('path', {
      class: 'col',
      d: h <= 0 ? '' : `M${x},${H} V${y + rr} Q${x},${y} ${x + rr},${y} `
        + `H${x + colW - rr} Q${x + colW},${y} ${x + colW},${y + rr} V${H} Z`,
    });
    // the hit target is the whole column slot, not just the bar
    const hit = mk('rect', { class: 'hit', x: i * (W / n), y: 0, width: W / n, height: H });
    const show = () => {
      col.classList.add('hot');
      const a0 = i * VS_DIR_BIN;
      const b = el('b');
      b.textContent = vsFmt(v);
      const s = el('span');
      s.textContent = ` ${vsPct(total ? v / total : 0)} · ${a0}–${a0 + VS_DIR_BIN}°`;
      tip.replaceChildren(b, s);
      tip.style.left = `${((i + 0.5) / n) * 100}%`;
      tip.style.top = `${(y / (H + 14)) * 100}%`;
      tip.hidden = false;
    };
    const hide = () => { col.classList.remove('hot'); tip.hidden = true; };
    hit.addEventListener('pointerenter', show);
    hit.addEventListener('pointerleave', hide);
    svg.append(hit, col);
  });
  svg.appendChild(mk('line', { class: 'axis', x1: 0, x2: W, y1: H + 0.5, y2: H + 0.5 }));
  for (const [deg, lab] of [[0, 'N'], [90, 'E'], [180, 'S'], [270, 'W'], [360, 'N']]) {
    const t = mk('text', {
      class: 'tick', x: deg, y: H + 12,
      'text-anchor': deg === 0 ? 'start' : deg === 360 ? 'end' : 'middle',
    });
    t.textContent = lab;
    svg.appendChild(t);
  }
  wrap.append(svg, tip);
  return wrap;
}

/** Shade the camera's horizontal field of view on the direction chart. */
function updateVsFov(p) {
  const g = document.getElementById('vs-fov');
  if (!g || !p || !p.data) return;
  const half = Math.min(180, panoHfovDeg(p) / 2);
  const a = ((p.yaw - half) % 360 + 360) % 360;
  const spans = a + 2 * half <= 360 ? [[a, 2 * half]] : [[a, 360 - a], [0, a + 2 * half - 360]];
  const key = spans.map((s) => s.map((v) => v.toFixed(1)).join(':')).join('|');
  if (g.dataset.key === key) return;
  g.dataset.key = key;
  g.replaceChildren();
  for (const [x, w] of spans) {
    const rct = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rct.setAttribute('class', 'fov');
    rct.setAttribute('x', x);
    rct.setAttribute('y', 0);
    rct.setAttribute('width', w);
    rct.setAttribute('height', 56);
    g.appendChild(rct);
  }
}

$('vs-toggle').addEventListener('click', () => {
  const box = $('vscore');
  const collapsed = box.classList.toggle('collapsed');
  $('vs-toggle').setAttribute('aria-expanded', String(!collapsed));
  $('vs-toggle').title = collapsed ? 'Expand' : 'Collapse';
  try { localStorage.setItem('vs-collapsed', collapsed ? '1' : '0'); } catch (e) { /* private mode */ }
});
try {
  if (localStorage.getItem('vs-collapsed') === '1') {
    $('vscore').classList.add('collapsed');
    $('vs-toggle').setAttribute('aria-expanded', 'false');
    $('vs-toggle').title = 'Expand';
  }
} catch (e) { /* storage unavailable: start expanded */ }

// ---------------------------------------------------------------------------
// Ranked list: every scored trail, peak and pass, sortable by V and by the
// stats stored with it. Fed by /api/feature_scores (the same store the hover
// card reads); while scripts/score_all.py runs, its status file is polled and
// the list refills as chunks land. Selecting a row zooms the map to the item
// and marks it until something else is selected.

const RANK_PAGE = 200;             // rows rendered per "show more"
const RANK_POLL_MS = 2000;         // while a bulk scoring job is running
const RANK_IDLE_POLL_MS = 15000;   // otherwise, to notice a job started later
const RANK_COLS = [
  // key, header, title, value getter, formatter, numeric
  ['name', 'Name', 'Name', (r) => r.name || '', null, false],
  ['V', 'V', 'View score: the average along a trail or a lake\'s shoreline, or at a peak or pass',
    (r) => r.V, (v) => v.toFixed(2), true],
  ['V_max', 'Best', 'Highest V anywhere along the trail or shoreline',
    (r) => (isPathRecord(r) ? r.V_max : null), (v) => v.toFixed(2), true],
  ['area_m2', 'Area', 'Lake surface area', (r) => (r.kind === 'shore' ? r.area_m2 : null),
    formatArea, true, ['water_areas']],
  ['length_m', 'Length', 'Trail length (whole trail), or a lake\'s shoreline',
    (r) => (isPathRecord(r) ? r.length_m : null),
    (v) => (v >= 10000 ? `${(v / 1000).toFixed(0)} km` : `${(v / 1000).toFixed(1)} km`), true],
  ['ele_min_m', 'Min', 'Lowest elevation, m',
    (r) => r.ele_min_m, (v) => Math.round(v).toLocaleString(), true,
    ['trails', 'peaks', 'passes', 'all']],   // a lake has one elevation: Max
  ['ele_max_m', 'Max', 'Highest elevation, m (a peak, pass or lake: its elevation)',
    (r) => r.ele_max_m, (v) => Math.round(v).toLocaleString(), true],
  ['gain_m', 'Gain', 'Elevation gain, m, each segment walked in its uphill direction',
    (r) => (r.kind === 'line' ? r.gain_m : null), (v) => Math.round(v).toLocaleString(), true,
    ['trails', 'all']],
];
// A column with a 7th entry is shown only for those list types.
const rankCols = () => RANK_COLS.filter((c) => !c[6] || c[6].includes(rankState.type));
const RANK_THEME_LABEL = { trails: 'trail', peaks: 'peak', passes: 'pass', water_areas: 'lake' };

const featureItems = new Map();    // record key -> stored summary
// Trails default to 1 km and up. Averaged V favours the very short: a 10 m
// spur on a summit or a 100 m bootpath averages higher than any real trail,
// and without a floor they fill the top of the list.
const rankState = { type: 'trails', named: true, minLen: 1000, sort: 'V', dir: -1,
  shown: RANK_PAGE };
let rankSel = null;                // { key, theme, layers: [], marker }
let rankLastUpdate = null;          // the status file's last write we acted on

function rankRows() {
  const col = RANK_COLS.find((c) => c[0] === rankState.sort);
  const get = col[3];
  const rows = [];
  for (const r of featureItems.values()) {
    if (rankState.type !== 'all' && r.theme !== rankState.type) continue;
    if (rankState.named && !r.name) continue;
    if (r.kind === 'line' && r.length_m < rankState.minLen) continue;
    rows.push(r);
  }
  rows.sort((a, b) => {
    const x = get(a), y = get(b);
    // missing values always sink, whichever way the column is sorted
    const xm = x === null || x === undefined || x === '';
    const ym = y === null || y === undefined || y === '';
    if (xm || ym) return xm === ym ? 0 : (xm ? 1 : -1);
    if (col[5]) return (x - y) * rankState.dir;
    return String(x).localeCompare(String(y)) * rankState.dir;
  });
  return rows;
}

function renderRank() {
  const box = $('rank');
  if (!box || box.hidden) return;
  const rows = rankRows();
  const head = $('rank-table').tHead;
  head.replaceChildren();
  const hr = el('tr');
  hr.appendChild(el('th', 'num', '#'));
  for (const [key, label, title] of rankCols()) {
    const th = el('th', key === 'name' ? 'name' : 'num');
    th.textContent = label + (rankState.sort === key ? (rankState.dir < 0 ? ' ▾' : ' ▴') : '');
    th.title = `${title}. Click to sort.`;
    th.classList.toggle('sorted', rankState.sort === key);
    th.addEventListener('click', () => {
      if (rankState.sort === key) rankState.dir = -rankState.dir;
      else {
        rankState.sort = key;
        rankState.dir = RANK_COLS.find((c) => c[0] === key)[5] ? -1 : 1;
      }
      rankState.shown = RANK_PAGE;
      renderRank();
    });
    hr.appendChild(th);
  }
  head.appendChild(hr);

  const body = $('rank-table').tBodies[0];
  body.replaceChildren();
  rows.slice(0, rankState.shown).forEach((r, i) => {
    const tr = el('tr');
    tr.dataset.key = r.id;
    if (rankSel && rankSel.key === r.id) tr.classList.add('sel');
    const n = el('td', 'num rank-n');
    n.textContent = String(i + 1);
    tr.appendChild(n);
    for (const [key, , , get, fmt] of rankCols()) {
      const td = el('td', key === 'name' ? 'name' : 'num');
      if (key === 'name') {
        const sw = el('i', `rank-sw t-${r.theme}`);
        td.appendChild(sw);
        const nm = el('span');
        nm.textContent = r.name || `unnamed ${RANK_THEME_LABEL[r.theme]}`;
        td.appendChild(nm);
        if (r.kind === 'line' && r.n_ways > 1) td.title = `${r.n_ways} OSM ways`;
      } else {
        const v = get(r);
        td.textContent = v === null || v === undefined ? '' : fmt(v);
      }
      tr.appendChild(td);
    }
    tr.addEventListener('click', () => selectRankItem(r));
    body.appendChild(tr);
  });
  const more = $('rank-more');
  more.hidden = rows.length <= rankState.shown;
  more.textContent = `Show ${Math.min(RANK_PAGE, rows.length - rankState.shown)} more`;
  const kind = { all: 'items', water_areas: 'lakes' }[rankState.type] || rankState.type;
  const minLen = rankState.minLen && (rankState.type === 'trails' || rankState.type === 'all')
    ? `, trails ≥ ${rankState.minLen / 1000} km` : '';
  $('rank-count').textContent = `${rows.length.toLocaleString()} ${kind}`
    + (rankState.named ? ', named only' : '') + minLen;
}

/** Restore the map styling of whatever is currently selected. */
function clearRankSelection() {
  if (!rankSel) return;
  for (const l of rankSel.layers) {
    if (map.hasLayer(l)) l.setStyle(styleFor(rankSel.theme, l.feature.properties));
  }
  if (rankSel.marker) map.removeLayer(rankSel.marker);
  rankSel = null;
}

const RANK_SEL_STYLE = (w) => ({ color: SELECT_COLOR, weight: (w || 1) + 3,
  opacity: 1, dashArray: null });

/** Mark a selected trail's or lake's layers; called again as tiles load. A
 *  lake keeps its fill and gains the selection outline. */
function markRankLayer(lyr) {
  if (!rankSel || !lyr.feature) return;
  const f = lyr.feature;
  if (rankSel.theme === 'trails') {
    if (f.trail !== rankSel.key && f.id !== rankSel.key) return;
  } else if (rankSel.theme !== 'water_areas' || f.id !== rankSel.key) {
    return;
  }
  if (!rankSel.layers.includes(lyr)) rankSel.layers.push(lyr);
  lyr.setStyle(RANK_SEL_STYLE(styleFor(rankSel.theme, f.properties).weight));
}

function selectRankItem(r) {
  clearRankSelection();
  rankSel = { key: r.id, theme: r.theme, layers: [], marker: null };
  // make sure the item's layer is on, or there is nothing to look at
  const st = vectorLayers[r.theme];
  if (st && !st.enabled && st.cb) { st.cb.checked = true; st.cb.dispatchEvent(new Event('change')); }
  // The map runs underneath the left panel, so centre on the part of it that
  // is actually visible: pad the fit by the panel's width on that side.
  const panelW = $('panel').classList.contains('hidden') ? 0 : $('panel').getBoundingClientRect().width;
  const fit = { paddingTopLeft: [panelW + 50, 50], paddingBottomRight: [50, 50] };
  if (isPathRecord(r) && r.bbox) {
    const [w, s, e, n] = r.bbox;
    map.fitBounds([[s, w], [n, e]], { ...fit, maxZoom: 15 });
    if (st) st.onMap.forEach((gj) => gj.eachLayer(markRankLayer));
  } else if (r.lat !== undefined) {
    const z = Math.max(map.getZoom(), 14);
    map.fitBounds(L.latLng(r.lat, r.lon).toBounds(1), { ...fit, maxZoom: z });
    rankSel.marker = L.circleMarker([r.lat, r.lon], {
      renderer: panoOverlayRenderer, interactive: false, radius: 12,
      color: SELECT_COLOR, weight: 3, fill: false,
    }).addTo(map);
  }
  renderRank();
}

async function pollScoreAll() {
  let st;
  try {
    st = await (await fetch(`/api/score_all_status?slug=${AOI}`)).json();
  } catch (err) {
    setTimeout(pollScoreAll, RANK_IDLE_POLL_MS);
    return;
  }
  const prog = $('rank-progress');
  const running = ['planning', 'sampling', 'scoring'].includes(st.state);
  if (running) {
    const eta = st.eta_s !== undefined ? ` · ~${st.eta_s >= 90 ? `${Math.round(st.eta_s / 60)} min` : `${st.eta_s} s`} left` : '';
    prog.textContent = st.state === 'scoring'
      ? `Scoring: ${(st.items_done || 0).toLocaleString()} of ${(st.items_total || 0).toLocaleString()} items${eta}`
      : `Scoring: ${st.state}…`;
    prog.hidden = false;
  } else {
    prog.hidden = true;
  }
  // reload only when the job has written something new since last time
  if (st.updated !== rankLastUpdate || rankLastUpdate === null) {
    rankLastUpdate = st.updated;
    await loadFeatureScores();
    renderRank();
  }
  setTimeout(pollScoreAll, running ? RANK_POLL_MS : RANK_IDLE_POLL_MS);
}

function setRankVisible(on) {
  $('rank').hidden = !on;
  $('rank-toggle').hidden = on;
  try { localStorage.setItem('rank-hidden', on ? '0' : '1'); } catch (e) { /* no storage */ }
  syncLayoutVars();
  if (on) renderRank();
}

$('rank-close').addEventListener('click', () => setRankVisible(false));
$('rank-toggle').addEventListener('click', () => setRankVisible(true));
$('rank-more').addEventListener('click', () => { rankState.shown += RANK_PAGE; renderRank(); });
$('rank-minlen').addEventListener('change', (e) => {
  rankState.minLen = +e.target.value; rankState.shown = RANK_PAGE; renderRank();
});
$('rank-named').addEventListener('change', (e) => {
  rankState.named = e.target.checked; rankState.shown = RANK_PAGE; renderRank();
});
$('rank-type').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-type]');
  if (!b) return;
  rankState.type = b.dataset.type;
  rankState.shown = RANK_PAGE;
  for (const x of $('rank-type').querySelectorAll('button')) x.classList.toggle('on', x === b);
  renderRank();
});
try {
  if (localStorage.getItem('rank-hidden') === '1') { $('rank').hidden = true; $('rank-toggle').hidden = false; }
} catch (e) { /* no storage: start shown */ }

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
  const rank = $('rank');
  const rw = !rank || rank.hidden ? 0 : rank.getBoundingClientRect().width;
  const root = document.documentElement.style;
  root.setProperty('--panel-w', `${Math.round(pw)}px`);
  root.setProperty('--pano-h', `${Math.round(ph)}px`);
  root.setProperty('--rank-w', `${Math.round(rw)}px`);
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
  scheduleScoreAtCamera();
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
  const wasMoving = camHeld.size > 0;
  camHeld.clear();
  if (camRaf) cancelAnimationFrame(camRaf);
  camRaf = null;
  // a blur ends the walk without a keyup, so it has to schedule the score too
  if (wasMoving) scheduleScoreAtCamera();
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
  const wasMoving = camHeld.delete(k);
  if (ev.key === 'Shift') camFast = false;
  if (!camHeld.size && camRaf) { cancelAnimationFrame(camRaf); camRaf = null; }
  if (wasMoving && !camHeld.size) scheduleScoreAtCamera();
});
// a key held while the window loses focus never sends keyup
window.addEventListener('blur', camStop);

$('pano-close').addEventListener('click', () => {
  $('pano').hidden = true;
  $('vscore').hidden = true;
  vsSeq++;                                  // drop a score still in flight
  vsAltSeq++;
  clearTimeout(vsAltTimer);
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
  scheduleAltScore();
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

// no map.on('click') here — see pickTarget() for why picking is captured on
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
  // loads the stored scores, fills the ranked list, and keeps polling while
  // a bulk scoring job is running
  pollScoreAll();
  syncLayoutVars();

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
