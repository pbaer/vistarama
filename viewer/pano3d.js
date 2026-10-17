/* Interactive 3D terrain view, raw WebGL — no framework.
 *
 * The server sends a polar heightfield centred on the observer, with earth
 * curvature already baked into the vertex heights (z - drop(r)). That means an
 * ordinary flat-earth perspective camera reproduces true elevation angles and
 * the shader needs no curvature maths.
 *
 * Because the mesh is independent of the viewer, changing eye height only
 * moves the camera — no refetch, no re-render server-side. Peak visibility is
 * therefore recomputed in the browser against the same height grid, so labels
 * appear and disappear correctly as you rise.
 */
(function (global) {
  'use strict';

  // ---- tiny mat4 helpers ---------------------------------------------------
  function perspective(fovyDeg, aspect, near, far) {
    const f = 1 / Math.tan((fovyDeg * Math.PI / 180) / 2);
    const nf = 1 / (near - far);
    return new Float32Array([
      f / aspect, 0, 0, 0,
      0, f, 0, 0,
      0, 0, (far + near) * nf, -1,
      0, 0, 2 * far * near * nf, 0,
    ]);
  }

  function lookAt(eye, dir, up) {
    // forward
    let [fx, fy, fz] = dir;
    let l = Math.hypot(fx, fy, fz) || 1; fx /= l; fy /= l; fz /= l;
    // side = forward x up
    let sx = fy * up[2] - fz * up[1];
    let sy = fz * up[0] - fx * up[2];
    let sz = fx * up[1] - fy * up[0];
    l = Math.hypot(sx, sy, sz) || 1; sx /= l; sy /= l; sz /= l;
    // true up = side x forward
    const ux = sy * fz - sz * fy;
    const uy = sz * fx - sx * fz;
    const uz = sx * fy - sy * fx;
    return new Float32Array([
      sx, ux, -fx, 0,
      sy, uy, -fy, 0,
      sz, uz, -fz, 0,
      -(sx * eye[0] + sy * eye[1] + sz * eye[2]),
      -(ux * eye[0] + uy * eye[1] + uz * eye[2]),
      fx * eye[0] + fy * eye[1] + fz * eye[2], 1,
    ]);
  }

  function mul(a, b) {
    const o = new Float32Array(16);
    for (let i = 0; i < 4; i++) {
      for (let j = 0; j < 4; j++) {
        let s = 0;
        for (let k = 0; k < 4; k++) s += a[k * 4 + j] * b[i * 4 + k];
        o[i * 4 + j] = s;
      }
    }
    return o;
  }

  function compile(gl, type, src) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
      throw new Error('shader: ' + gl.getShaderInfoLog(s));
    }
    return s;
  }

  function program(gl, vs, fs) {
    const p = gl.createProgram();
    gl.attachShader(p, compile(gl, gl.VERTEX_SHADER, vs));
    gl.attachShader(p, compile(gl, gl.FRAGMENT_SHADER, fs));
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) {
      throw new Error('link: ' + gl.getProgramInfoLog(p));
    }
    return p;
  }

  // ---- shaders -------------------------------------------------------------
  const TERRAIN_VS = `
    attribute vec3 aPos;
    attribute vec3 aColor;
    uniform mat4 uMVP;
    uniform vec3 uEye;
    varying vec3 vColor;
    varying float vDist;
    varying vec2 vWorld;
    void main() {
      gl_Position = uMVP * vec4(aPos, 1.0);
      vColor = aColor;
      vDist = length(aPos.xy - uEye.xy);
      vWorld = aPos.xy;          // metres east/north of the observer
    }`;

  // Imagery is sampled here, per fragment, from world position — not
  // interpolated from per-vertex colour. Per-vertex colour would cap the
  // texture at the mesh's own density (15 m of arc between neighbours), which
  // throws away everything NAIP's 0.6 m has to offer. Sampling in the fragment
  // shader decouples the two: the DEM sets the geometry, the imagery sets the
  // detail, and they no longer have to agree on a resolution.
  //
  // Four levels because the resolution a panorama *needs* scales with
  // distance — one screen pixel covers ~0.8 m at 1 km and ~49 m at 60 km — so
  // a single texture is either impossibly large or uselessly coarse. Each
  // level is a world-space square centred on the observer; the fragment picks
  // by radius and cross-fades so no ring is visible at the handover.
  const TERRAIN_FS = `
    precision highp float;
    varying vec3 vColor;
    varying float vDist;
    varying vec2 vWorld;
    uniform float uHaze;
    uniform vec3 uHazeColor;
    uniform sampler2D uTex0, uTex1, uTex2, uTex3;
    uniform sampler2D uTexF0, uTexF1, uTexF2, uTexF3;
    uniform vec4 uHalf;          // half-extent in metres of each level
    uniform float uTexMix;       // 0 = vertex colour, 1 = imagery
    // focus patches, coarsest first: centre x, y, half, texel (0 = unused)
    uniform vec4 uFocus0, uFocus1, uFocus2, uFocus3;
    uniform float uPix;          // radians per device pixel

    vec3 sampleLevel(sampler2D t, float half_m) {
      vec2 uv = vWorld / (2.0 * half_m) + 0.5;
      uv.y = 1.0 - uv.y;         // texture rows run north to south
      return texture2D(t, clamp(uv, 0.002, 0.998)).rgb;
    }

    // Weight of a level at radius r: full inside 55% of its extent, gone by
    // 97%. Starting the fade at 55% meant each level only ever served the
    // inner half of its own extent, so the band between two levels was carried
    // mostly by the coarser one — at 15 km that was 68 m/texel, blended out of
    // a level that had 19 m available. 72% keeps each level working most of
    // the way to its edge.
    // Fading on *radius* rather than on distance-to-the-square's-edge is
    // what removes the seam — the old test faded over the outer 2% of the
    // texture, which at 8 km across is 160 m, so the resolution step landed in
    // a band too narrow to read as anything but a line.
    float wt(float half_m, float r) {
      return 1.0 - smoothstep(half_m * 0.72, half_m * 0.97, r);
    }

    // A focus patch over whatever is beneath it (see Pano3D.updateFocus).
    // It has no mipmaps, so it also fades out wherever it would be minified,
    // which leaves those fragments to the mipmapped ladder.
    vec3 focus(vec3 img, sampler2D t, vec4 f) {
      if (f.w <= 0.0) return img;
      vec2 d = (vWorld - f.xy) / f.z;
      float w = 1.0 - smoothstep(0.88, 0.99, max(abs(d.x), abs(d.y)));
      w *= 1.0 - smoothstep(1.3, 2.0, vDist * uPix / f.w);
      vec2 uv = vec2(d.x, -d.y) * 0.5 + 0.5;
      return mix(img, texture2D(t, clamp(uv, 0.0005, 0.9995)).rgb, w);
    }

    void main() {
      vec3 base = vColor;
      if (uTexMix > 0.0) {
        float r = length(vWorld);
        // coarsest first, each finer level blended over it. The outermost
        // spans the whole mesh, so there is always something to fall back to.
        vec3 img = sampleLevel(uTex3, uHalf.w);
        img = mix(img, sampleLevel(uTex2, uHalf.z), wt(uHalf.z, r));
        img = mix(img, sampleLevel(uTex1, uHalf.y), wt(uHalf.y, r));
        img = mix(img, sampleLevel(uTex0, uHalf.x), wt(uHalf.x, r));
        // focus patches last, so the finest one wins where they overlap
        img = focus(img, uTexF0, uFocus0);
        img = focus(img, uTexF1, uFocus1);
        img = focus(img, uTexF2, uFocus2);
        img = focus(img, uTexF3, uFocus3);
        base = mix(vColor, img, uTexMix);
      }
      // exponential extinction toward the haze colour — atmospheric perspective
      float f = clamp(1.0 - exp(-vDist / uHaze), 0.0, 0.93);
      gl_FragColor = vec4(mix(base, uHazeColor, f), 1.0);
    }`;

  // Sky is drawn as a full-screen quad whose colour comes from the *world*
  // ray direction, so the gradient stays locked to the horizon as you pitch.
  const SKY_VS = `
    attribute vec2 aQuad;
    uniform mat4 uInvVP;
    varying vec3 vRay;
    void main() {
      vec4 near = uInvVP * vec4(aQuad, -1.0, 1.0);
      vec4 far  = uInvVP * vec4(aQuad,  1.0, 1.0);
      vRay = normalize(far.xyz / far.w - near.xyz / near.w);
      gl_Position = vec4(aQuad, 0.999, 1.0);
    }`;

  const SKY_FS = `
    precision mediump float;
    varying vec3 vRay;
    uniform vec3 uHorizon;
    uniform vec3 uTop;
    void main() {
      float t = clamp(normalize(vRay).z * 3.2, 0.0, 1.0);
      gl_FragColor = vec4(mix(uHorizon, uTop, t), 1.0);
    }`;

  // Two summits sharing a name within this distance are treated as one
  // mountain for labelling purposes.
  const DEDUPE_NAME_M = 1500;

  // ...and two summits with *different* names can still be one mountain. OSM
  // carries both "Mount Rainier" and "Columbia Crest" on the same 4392 m
  // summit, so they share a key saddle and report identical prominence — which
  // means the prominence test admits both, correctly but uselessly. Equal
  // prominence within a few hundred metres is the signature of one summit
  // listed twice; the nearer node wins, as it does for snapping.
  const COINCIDENT_M = 600;
  const COINCIDENT_PROM_TOL_M = 1.0;

  // A summit only earns a marker if it reads as a distinct feature: it must
  // clear the ridge in front of it by a minimum *angle*, which already means a
  // far peak needs proportionally more standing height than a near one.
  //
  // That alone is not enough, because the number of summits in a distance band
  // grows with its area — constant angular size still leaves the horizon
  // crowded. So the requirement itself also grows with range, doubling by
  // CLEAR_DIST_SCALE_M: ~0.22 deg up close, ~0.44 deg at 60 km, ~0.66 deg at
  // 120 km, which by then demands well over a kilometre of standing height.
  const MIN_CLEAR_DEG = 0.22;
  const CLEAR_DIST_SCALE_M = 60000;

  // A summit earns a label only if it is a mountain in its own right, which
  // is what topographic prominence measures: elevation minus the highest col
  // on the lowest path to any higher ground.
  //
  // This replaces a geometric stand-in. Sub-peaks used to be removed by
  // suppressing any summit within 15% of the observer distance of a higher
  // one — a constant fitted by hand against the Rainier massif, which held
  // there and had no principle behind it anywhere else. Prominence answers the
  // same question by definition and needs no radius: Rainier is 4113 m,
  // Liberty Cap on its shoulder is 149 m.
  //
  // The bar scales with distance for the same reason MIN_CLEAR_DEG does: the
  // number of summits in a distance band grows with its area, so a fixed
  // metre threshold leaves the far horizon crowded. Expressed as an angle, it
  // asks that a peak's prominence subtend at least this much from the eye.
  const MIN_PROM_DEG = 0.35;

  // Prominence assumed for a summit the source does not list. Its floor is
  // 100 ft; anything missing is at or below that.
  const UNMATCHED_PROM_M = 30.5;

  // ---- viewer ---------------------------------------------------------------
  function Pano3D(canvas) {
    this.canvas = canvas;
    const gl = canvas.getContext('webgl', { antialias: true, alpha: false });
    if (!gl) throw new Error('WebGL is not available in this browser');
    this.gl = gl;

    this.terrainProg = program(gl, TERRAIN_VS, TERRAIN_FS);
    this.skyProg = program(gl, SKY_VS, SKY_FS);
    this.quad = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, this.quad);
    gl.bufferData(gl.ARRAY_BUFFER,
      new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);

    this.pos = gl.createBuffer();
    this.col = gl.createBuffer();
    this.idx = gl.createBuffer();
    this.indexCount = 0;

    // Imagery LOD textures, coarsest last. Half-extents in metres; the outer
    // one is resized to the mesh range when a panorama loads.
    this.texHalf = [2500, 9000, 33000, 120000];
    // Two separate flags, not one. `texWanted` is the user's choice of
    // surface; `texReady` is whether the imagery has actually arrived. Folding
    // them into a single "mix" value meant switching to land cover was
    // indistinguishable from imagery not having loaded yet, so the next
    // texture to land would silently override the user.
    this.texWanted = true;
    this.texReady = false;
    this._texToken = 0;                  // discards textures from a stale point
    this.texSizes = null;
    // four ladder levels, then the four focus patches
    this.tex = [0, 1, 2, 3, 4, 5, 6, 7].map(() => {
      const t = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, t);
      // 1x1 grey placeholder so the sampler is always complete — an incomplete
      // texture makes the whole draw call render black on some drivers
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, 1, 1, 0, gl.RGB,
        gl.UNSIGNED_BYTE, new Uint8Array([128, 128, 128]));
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      return t;
    });
    this.focusSlots = this.tex.slice(4).map((tex) =>
      ({ tex, patch: null, pending: null, token: 0 }));

    this.yaw = 0;          // compass bearing the camera faces, degrees
    this.pitch = -4;       // degrees, positive = up
    this.fov = 55;         // vertical field of view, degrees
    this.eyeHeight = 2;    // metres above ground
    // Horizontal camera nudge, metres east/north of the mesh centre. Purely a
    // camera move — nothing is refetched, so the mesh and textures stay those
    // of the original point. See moveCamera / CAM_OFFSET_WARN_M.
    this.camOffset = [0, 0];
    this.hazeKm = 22;
    this.data = null;
    this.onChange = null;

    this._installControls();
    // No depth test: the mesh is a heightfield in polar coordinates centred on
    // the camera, which never translates, so horizontal radius increases
    // monotonically along every view ray and drawing bands outermost-first is
    // exactly correct occlusion. That matters because a depth buffer cannot
    // serve this scene: spanning 5 cm to 84 km, a 24-bit buffer quantises
    // depth to kilometres at the far end, so any near plane close enough to
    // avoid clipping the ground at your feet makes distant ridges z-fight.
    gl.disable(gl.DEPTH_TEST);
    gl.clearColor(0.06, 0.07, 0.09, 1);
  }

  /** Fetch and upload the three imagery LOD levels for the current viewpoint.
   *
   *  Resolves once every level is in, but each is applied the moment it
   *  arrives: the coarse level covers the whole view immediately and the finer
   *  ones sharpen the near field a second later, which is far better than a
   *  blank panorama until the last byte lands.
   */
  Pano3D.prototype.loadTextures = function (lat, lon, slug, rangeKm) {
    const gl = this.gl;
    // Four levels on a geometric ladder rather than three. The step between
    // levels is what sets how blurry the far side of a handover looks — the
    // texel grows by exactly that ratio while the screen's demand does not —
    // so three levels over 2.5..120 km meant a 5x jump, and four means 3.6x.
    // Four levels, weighted toward the near and middle distance rather than
    // spaced geometrically.
    //
    // A geometric ladder over 2.5..120 km gave the 9-33 km band a single level
    // at 32 m/texel, and that band is where most of the terrain you actually
    // look at lives — 10-15 km read as blurry even zoomed in. Meanwhile the
    // outermost level was spending the same ratio on 33-120 km, which the haze
    // has already washed out. So the first three levels are pinned to fixed
    // near/middle extents and the last one simply mops up whatever is left.
    // range may be passed explicitly: textures now start before the mesh has
    // landed, so `this.data` is often still null at this point
    const rangeM = (rangeKm || (this.data ? this.data.header.range_km : 60)) * 1000;
    this.texHalf = [
      Math.min(2500, rangeM / 8),
      Math.min(10000, rangeM / 3),
      Math.min(30000, rangeM / 1.5),
      rangeM,
    ];
    const max = gl.getParameter(gl.MAX_TEXTURE_SIZE);
    // Sized per level, and the extra pixels go to L1 rather than L0.
    //
    // The blur just past a handover is set by the *incoming* level's texel, so
    // sharpening L0 does nothing for the seam at L0's own edge — L1 is what
    // has to carry it, and that first handover is the most visible: nearest,
    // largest on screen, and where NAIP-sharp foreground gives way. L0 at 2048
    // is already 2.44 m/texel, which is what NAIP at 2 m actually carries;
    // more there would only interpolate.
    //
    // VRAM is the ceiling. Four 4096 textures with mipmaps on top of a 1.7M
    // vertex mesh made the last upload come back corrupt: the far level read
    // back as 26% pure red, RGB (121, 2, 2), with green and blue at zero — a
    // partially uploaded image, not imagery. The server's JPEG for the same
    // request is provably clean, so this is the browser side running out of
    // room. The far level is the cheapest one to give up: it spans 240 km,
    // which the haze has already washed out. L2 joins L1 at 4096: it carries
    // everything from ~7 km to ~29 km, which is most of what you look at.
    //
    // Zooming in used to scale this whole ladder up, but with every other
    // level already at its cap that only ever enlarged L0, interpolating the
    // 2 m source to 1.2 m. Zoom detail now comes from the focus patch instead
    // (updateFocus), which spends the same 64 MB where the camera is pointing.
    const sizes = [2048, 4096, 4096, 2048].map((s) => Math.min(s, max));
    this.texSizes = sizes;
    const token = ++this._texToken;
    // focus patches belong to the old viewpoint's frame
    for (const sl of this.focusSlots) {
      sl.token++; sl.patch = null; sl.pending = null;
    }

    return Promise.all(this.texHalf.map((half, i) => new Promise((resolve) => {
      const img = new Image();
      img.onload = () => {
        if (token !== this._texToken) return resolve(false);   // superseded
        gl.bindTexture(gl.TEXTURE_2D, this.tex[i]);
        gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, img);
        // power-of-two, so mipmaps are legal — and necessary: the far level
        // squeezes 120 km into 2048 px and without them the distance shimmers
        gl.generateMipmap(gl.TEXTURE_2D);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER,
          gl.LINEAR_MIPMAP_LINEAR);
        this.texReady = true;
        this.render();
        resolve(true);
      };
      img.onerror = () => resolve(false);
      img.src = `/api/texture?lat=${lat}&lon=${lon}&slug=${slug}`
        + `&half_m=${Math.round(half)}&size=${sizes[i]}`;
    })));
  };

  // Focus patches: a few extra squares of imagery placed where the camera is
  // looking, at the source's native resolution or whatever the screen needs.
  //
  // The ladder is observer-centred, so its resolution can only fall with
  // distance, while the resolution the screen *needs* also scales with zoom:
  // at fov 8 one device pixel covers 0.8 m at 5 km, and the ladder offers
  // 4.9 m/texel there. Enlarging the ladder cannot close that gap within the
  // VRAM ceiling, because it spends its texels on every direction at once and
  // a zoomed view only sees a few degrees of them.
  //
  // Four 2048 patches rather than one 4096: the same 64 MB, but a single
  // patch has to pick one distance. Aiming it at the screen centre failed on
  // the first real view tried, looking at Cashmere from 5 km at fov 8: the
  // centre ray slipped past the summit and landed on a ridge 67 km away, so
  // the patch went there and Cashmere stayed at 4.9 m/texel. Several smaller
  // patches, placed by what they gain across the whole screen, can serve the
  // near mountain and the far skyline at once.
  const FOCUS_N = 4;
  const FOCUS_SIZE = 2048;
  // The NAIP mosaic's own resolution. Finer than this the server can only
  // interpolate, so a patch never asks for it.
  const FOCUS_MIN_TEXEL_M = 2;
  // The planner's screen sampling grid.
  const FOCUS_GRID_X = 32, FOCUS_GRID_Y = 16;
  // The shader fades a patch out where it would be minified by more than this
  // (it has no mipmaps), so the planner counts those fragments as uncovered.
  const FOCUS_MAX_MINIFY = 1.3;
  // Stop adding patches once the best one gains less than this, in mean
  // log-resolution per screen sample.
  const FOCUS_MIN_GAIN = 0.02;

  /** Radians per device pixel, vertically. */
  Pano3D.prototype.pixAngle = function () {
    return 2 * Math.tan(this.fov * Math.PI / 360) / Math.max(1, this.canvas.height);
  };

  /** Texel size, in metres, of the ladder level that fully covers radius r. */
  Pano3D.prototype.ladderTexel = function (r) {
    const sizes = this.texSizes || [2048, 4096, 4096, 2048];
    for (let i = 0; i < 3; i++) {
      if (r < this.texHalf[i] * 0.72) return 2 * this.texHalf[i] / sizes[i];
    }
    return 2 * this.texHalf[3] / sizes[3];
  };

  /** Ground samples under a grid of screen points: where each lands, the
   *  texel the screen needs there, and the texel the ladder currently gives. */
  Pano3D.prototype._focusSamples = function () {
    const W = this.canvas.clientWidth, H = this.canvas.clientHeight;
    const pix = this.pixAngle();
    const s = [];
    for (let j = 0; j < FOCUS_GRID_Y; j++) {
      for (let i = 0; i < FOCUS_GRID_X; i++) {
        const hit = this.pickGround((i + 0.5) * W / FOCUS_GRID_X,
          (j + 0.5) * H / FOCUS_GRID_Y);
        if (!hit) continue;
        const a = hit.az * Math.PI / 180;
        s.push({ x: hit.dist * Math.sin(a), y: hit.dist * Math.cos(a),
                 need: Math.max(FOCUS_MIN_TEXEL_M, hit.dist * pix),
                 eff: this.ladderTexel(hit.dist) });
      }
    }
    return s;
  };

  function focusCovers(p, q) {
    return Math.abs(q.x - p.x) <= p.half && Math.abs(q.y - p.y) <= p.half
      && q.need <= p.texel * FOCUS_MAX_MINIFY;
  }

  /** Summed gain in log-resolution over the samples patch p would improve. */
  function focusGain(p, s) {
    let gain = 0;
    for (const q of s) {
      if (!focusCovers(p, q)) continue;
      const g = Math.log(q.eff / Math.max(p.texel, q.need));
      if (g > 0) gain += g;
    }
    return gain;
  }

  function focusApply(p, s) {
    for (const q of s) {
      if (focusCovers(p, q)) q.eff = Math.min(q.eff, Math.max(p.texel, q.need));
    }
  }

  /** The new patch that would gain the most. Candidates are centred on the
   *  samples themselves, at the texel that sample needs and at 2x and 4x
   *  that, which trades sharpness for coverage. */
  function focusBest(s) {
    let best = null;
    for (const c of s) {
      if (c.need * 1.25 >= c.eff) continue;        // already sharp enough here
      for (const t of [c.need, c.need * 2, c.need * 4]) {
        const p = { x: c.x, y: c.y, half: t * FOCUS_SIZE / 2, texel: t };
        p.gain = focusGain(p, s);
        if (!best || p.gain > best.gain) best = p;
      }
    }
    return best;
  }

  /** Which focus slots to keep and which new patches to fetch for this view.
   *
   *  Greedy over screen samples: each stands for an equal area of screen, so
   *  summing gain over them maximises sharpness where the pixels are rather
   *  than where the ground is. Patches already loaded (or loading) go first,
   *  and keep their slot as long as they still earn it. Planning from scratch
   *  re-picked slightly different centres on every small pan and refetched
   *  half the patches for a 2 degree turn. A kept patch is only swapped out
   *  when a new one would earn at least twice as much. */
  Pano3D.prototype.focusPlan = function () {
    if (!this.data || !this.texSizes) return { keep: [], fetch: [] };
    const s = this._focusSamples();
    const minGain = FOCUS_MIN_GAIN * s.length;
    const have = this.focusSlots
      .map((sl, i) => ({ i, p: sl.pending || sl.patch })).filter((h) => h.p);
    const keep = [];
    while (have.length) {
      let bk = -1, bg = 0;
      have.forEach((h, k) => {
        const g = focusGain(h.p, s);
        if (g > bg) { bg = g; bk = k; }
      });
      if (bk < 0 || bg < minGain) break;
      const h = have.splice(bk, 1)[0];
      h.gain = bg;
      keep.push(h);
      focusApply(h.p, s);
    }
    const fetch = [];
    while (keep.length + fetch.length < FOCUS_N) {
      const c = focusBest(s);
      if (!c || c.gain < minGain) break;
      fetch.push(c);
      focusApply(c, s);
    }
    // Slots full: swap out the weakest kept patch while a new one would earn
    // twice as much. The candidate's gain is counted with the weakest still
    // applied, so it can only understate what the swap is worth.
    while (keep.length && keep.length + fetch.length >= FOCUS_N) {
      const c = focusBest(s);
      const weakest = keep.reduce((a, b) => (b.gain < a.gain ? b : a));
      if (!c || c.gain < minGain || c.gain < 2 * weakest.gain) break;
      keep.splice(keep.indexOf(weakest), 1);
      fetch.push(c);
      focusApply(c, s);
    }
    return { keep: keep.map((h) => h.i), fetch };
  };

  /** Bring the focus patches in line with the current view. A slot that is
   *  not kept carries on showing its old patch until a new one replaces it:
   *  that is still correctly registered imagery, just aimed somewhere less
   *  useful. */
  Pano3D.prototype.updateFocus = function (slug) {
    const { keep, fetch } = this.focusPlan();
    const slots = this.focusSlots;
    // empty slots first, then ones holding a patch this view no longer uses
    const free = slots.map((sl, i) => i).filter((i) => !keep.includes(i))
      .sort((a, b) => Number(!!slots[a].patch) - Number(!!slots[b].patch));
    const gl = this.gl;
    const h = this.data && this.data.header;
    return Promise.all(fetch.slice(0, free.length).map((want, n) => {
      const sl = slots[free[n]];
      const token = ++sl.token;
      const vp = this._texToken;
      sl.pending = want;
      return new Promise((resolve) => {
        const img = new Image();
        img.onload = () => {
          if (token !== sl.token || vp !== this._texToken) return resolve(false);
          gl.bindTexture(gl.TEXTURE_2D, sl.tex);
          gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
          gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, img);
          // No mipmaps: they would add a third to the patches' 64 MB, and
          // that third is what tips the total over the VRAM ceiling described
          // in loadTextures. The shader fades a patch out wherever it would
          // be minified instead, so the ladder's mipmaps carry those fragments.
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
          sl.patch = want;
          sl.pending = null;
          this.render();
          resolve(true);
        };
        img.onerror = () => {
          if (token === sl.token) sl.pending = null;
          resolve(false);
        };
        img.src = `/api/texture?lat=${h.lat}&lon=${h.lon}&slug=${slug}`
          + `&half_m=${Math.round(want.half)}&size=${FOCUS_SIZE}`
          + `&cx=${Math.round(want.x)}&cy=${Math.round(want.y)}`;
      });
    }));
  };

  Pano3D.prototype.load = function (header, buffer) {
    const gl = this.gl;
    const { n_r, counts, offsets, radii } = header;
    const n = header.n_vert;
    const z = new Float32Array(buffer, 0, n);
    const rgb = new Uint8Array(buffer, n * 4, n * 3);
    // 1 where the DEM actually had data; past the AOI edge the heights are
    // extrapolated and must not be allowed to occlude anything
    const valid = new Uint8Array(buffer, n * 4 + n * 3, n);

    // positions: polar (azimuth, radius) -> local ENU metres. Rings carry
    // different azimuth counts, so walk them rather than indexing a grid.
    const pos = new Float32Array(n * 3);
    for (let r = 0; r < n_r; r++) {
      const c = counts[r], off = offsets[r], rad = radii[r];
      for (let a = 0; a < c; a++) {
        const th = (a / c) * 2 * Math.PI;    // 0 = north, clockwise
        const i = off + a;
        pos[i * 3] = rad * Math.sin(th);     // east
        pos[i * 3 + 1] = rad * Math.cos(th); // north
        pos[i * 3 + 2] = z[i];               // up (curvature already applied)
      }
    }

    // Stitch consecutive rings. An inner edge spans `ratio` outer edges, so it
    // becomes a fan of ratio+1 triangles: ratio of them reaching out to the
    // finer ring, one closing back to the inner vertex.
    //
    // This is written for a general integer ratio rather than special-casing
    // 1 and 2. An earlier version assumed "not 1 means 2", and when the cap
    // was 720 the 512 -> 720 transition (ratio 1.40625) silently fell into the
    // 2 branch and emitted a fan of long garbage triangles.
    //
    // Bands are emitted **outermost first**. GL rasterises primitives in
    // buffer order, so that alone sorts the whole mesh back-to-front and the
    // depth test can be switched off (see render()).
    // Counted first, then filled into a preallocated typed array. Pushing onto
    // a plain JS array cost roughly 25 bytes per index and reallocated as it
    // grew — survivable at 0.36M triangles, ruinous at the ~5M the isotropic
    // mesh produces (~340 MB of boxed numbers before the copy to a typed
    // array). Two passes over the ring table is nothing by comparison.
    let nTri = 0;
    for (let r = 0; r < n_r - 1; r++) {
      const ratio = counts[r + 1] / counts[r];
      if (!Number.isInteger(ratio) || ratio < 1) {
        throw new Error(`ring ${r}: azimuth counts ${counts[r]} -> `
          + `${counts[r + 1]} are not an integer ratio, so the mesh cannot `
          + 'be stitched');
      }
      nTri += counts[r] * (ratio + 1);
    }
    const Idx = n > 65535 ? Uint32Array : Uint16Array;
    if (n > 65535 && !gl.getExtension('OES_element_index_uint')) {
      throw new Error('this browser cannot index a mesh this large');
    }
    const idx = new Idx(nTri * 3);
    let w = 0;
    for (let r = n_r - 2; r >= 0; r--) {
      const ci = counts[r], co = counts[r + 1];
      const oi = offsets[r], oo = offsets[r + 1];
      const ratio = co / ci;
      for (let a = 0; a < ci; a++) {
        const inA = oi + a, inB = oi + ((a + 1) % ci);
        const base = a * ratio;
        for (let k = 0; k < ratio; k++) {
          idx[w++] = inA;
          idx[w++] = oo + ((base + k) % co);
          idx[w++] = oo + ((base + k + 1) % co);
        }
        idx[w++] = inA;
        idx[w++] = oo + ((base + ratio) % co);
        idx[w++] = inB;
      }
    }

    const colf = new Float32Array(n * 3);
    for (let i = 0; i < n * 3; i++) colf[i] = rgb[i] / 255;

    gl.bindBuffer(gl.ARRAY_BUFFER, this.pos);
    gl.bufferData(gl.ARRAY_BUFFER, pos, gl.STATIC_DRAW);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.col);
    gl.bufferData(gl.ARRAY_BUFFER, colf, gl.STATIC_DRAW);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.idx);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, idx, gl.STATIC_DRAW);

    this.indexType = Idx === Uint32Array ? gl.UNSIGNED_INT : gl.UNSIGNED_SHORT;
    this.indexCount = idx.length;
    this.data = { header, z, valid, radii, n_r,
                  counts: header.counts, offsets: header.offsets };
    this._vis = null;            // invalidate the cached occlusion test
    this._visEye = null;
    this.camOffset = [0, 0];     // a new mesh means a new centre
    this._meshGen = (this._meshGen || 0) + 1;   // and new camera matrices
    // NOT invalidating textures here: they are now fetched in parallel with
    // this mesh and may already have landed for the same point. renderPanorama
    // invalidates once, up front, before either request goes out.
    this.render();
  };

  /** Drop the current mesh and clear the canvas, so a stale panorama is not
   *  left on screen while the next one is being built. */
  Pano3D.prototype.blank = function () {
    this.indexCount = 0;
    this.data = null;
    this._meshGen = (this._meshGen || 0) + 1;
    this._vis = null;
    this._visEye = null;
    const gl = this.gl;
    this.resize();
    gl.clearColor(0.06, 0.07, 0.09, 1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
  };

  /** Mesh surface height at a world position, or null outside the mesh.
   *
   *  Returns the *rendered* height — curvature drop already subtracted — so it
   *  is directly comparable with vertex z and with the camera's own height.
   */
  Pano3D.prototype.heightAtWorld = function (x, y) {
    if (!this.data) return null;
    const { radii, counts, offsets, n_r, z } = this.data;
    const r = Math.hypot(x, y);
    if (r > radii[n_r - 1]) return null;
    const az = ((Math.atan2(x, y) * 180 / Math.PI) % 360 + 360) % 360;
    const hAt = (i) => {
      const c = counts[i];
      const f = az / 360 * c;
      const a0 = Math.floor(f) % c, a1 = (a0 + 1) % c, t = f - Math.floor(f);
      return z[offsets[i] + a0] * (1 - t) + z[offsets[i] + a1] * t;
    };
    let i = 1;
    while (i < n_r - 1 && radii[i] < r) i++;
    const t = (r - radii[i - 1]) / Math.max(1e-9, radii[i] - radii[i - 1]);
    return hAt(i - 1) * (1 - t) + hAt(i) * t;
  };

  Pano3D.prototype.eye = function () {
    const g = this.data ? this.data.header.ground_m : 0;
    const [ox, oy] = this.camOffset;
    // eye height is held above the *surface*, so walking downhill lowers the
    // camera with the ground rather than leaving it floating
    const surf = (ox || oy) ? this.heightAtWorld(ox, oy) : null;
    return [ox, oy, (surf === null ? g : surf) + this.eyeHeight];
  };

  /** Nudge the camera horizontally, in metres east/north. */
  Pano3D.prototype.moveCamera = function (dx, dy) {
    this.camOffset = [this.camOffset[0] + dx, this.camOffset[1] + dy];
    this.render();
    return Math.hypot(this.camOffset[0], this.camOffset[1]);
  };

  Pano3D.prototype.dir = function () {
    const y = this.yaw * Math.PI / 180, p = this.pitch * Math.PI / 180;
    return [Math.sin(y) * Math.cos(p), Math.cos(y) * Math.cos(p), Math.sin(p)];
  };

  Pano3D.prototype.matrices = function () {
    // Memoised per view state. The overlay asks for this once per compass tick
    // (24 of them), once for the peaks and once for the horizon line, and each
    // rebuild is a perspective, a lookAt and a 4x4 multiply — plus, now that
    // the camera can be nudged, a mesh height lookup inside eye(). Recomputing
    // it 26 times to draw one frame was most of the overlay's cost.
    //
    // The key must include the mesh. The eye's altitude is the mesh's ground
    // height plus eye height, and the far plane is its range, so a new
    // viewpoint with an unchanged yaw/pitch/fov/eye height otherwise reused
    // the previous viewpoint's matrices: the camera stayed at the old
    // altitude, 1.2 km in the air after moving from Stuart's summit to the
    // Icicle valley, or underground going the other way.
    const key = `${this._meshGen},${this.yaw},${this.pitch},${this.fov},${this.eyeHeight},`
      + `${this.camOffset[0]},${this.camOffset[1]},`
      + `${this.canvas.width},${this.canvas.height}`;
    if (this._mtxKey === key) return this._mtx;
    const c = this.canvas;
    const aspect = c.width / Math.max(1, c.height);
    const far = this.data ? this.data.header.range_km * 1000 * 1.4 : 100000;
    // 5 cm near plane. Nothing is gained by pushing it out — there is no depth
    // buffer to preserve precision in — and at 2 m it clipped the ground under
    // the observer's feet on steep terrain.
    const proj = perspective(this.fov, aspect, 0.05, far);
    const view = lookAt(this.eye(), this.dir(), [0, 0, 1]);
    this._mtxKey = key;
    this._mtx = { proj, view, mvp: mul(proj, view) };
    return this._mtx;
  };

  Pano3D.prototype.resize = function () {
    const c = this.canvas;
    const dpr = Math.min(global.devicePixelRatio || 1, 2);
    const w = Math.round(c.clientWidth * dpr), h = Math.round(c.clientHeight * dpr);
    if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
    this.gl.viewport(0, 0, c.width, c.height);
  };

  Pano3D.prototype.render = function () {
    const gl = this.gl;
    this.resize();
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    if (!this.indexCount) return;
    const { mvp } = this.matrices();

    // sky first, behind everything
    gl.useProgram(this.skyProg);
    gl.depthMask(false);
    const inv = invert(mvp);
    gl.uniformMatrix4fv(gl.getUniformLocation(this.skyProg, 'uInvVP'), false, inv);
    gl.uniform3f(gl.getUniformLocation(this.skyProg, 'uHorizon'), 0.77, 0.84, 0.91);
    gl.uniform3f(gl.getUniformLocation(this.skyProg, 'uTop'), 0.29, 0.47, 0.72);
    const aq = gl.getAttribLocation(this.skyProg, 'aQuad');
    gl.bindBuffer(gl.ARRAY_BUFFER, this.quad);
    gl.enableVertexAttribArray(aq);
    gl.vertexAttribPointer(aq, 2, gl.FLOAT, false, 0, 0);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    gl.depthMask(true);

    gl.useProgram(this.terrainProg);
    const e = this.eye();
    gl.uniformMatrix4fv(gl.getUniformLocation(this.terrainProg, 'uMVP'), false, mvp);
    gl.uniform3f(gl.getUniformLocation(this.terrainProg, 'uEye'), e[0], e[1], e[2]);
    gl.uniform1f(gl.getUniformLocation(this.terrainProg, 'uHaze'), this.hazeKm * 1000);
    for (let i = 0; i < 4; i++) {
      gl.activeTexture(gl.TEXTURE0 + i);
      gl.bindTexture(gl.TEXTURE_2D, this.tex[i]);
      gl.uniform1i(gl.getUniformLocation(this.terrainProg, `uTex${i}`), i);
    }
    // focus patches bound coarsest first, so the finest is blended last
    const patches = this.focusSlots.filter((sl) => sl.patch)
      .sort((a, b) => b.patch.texel - a.patch.texel);
    for (let i = 0; i < 4; i++) {
      const sl = patches[i];
      gl.activeTexture(gl.TEXTURE4 + i);
      gl.bindTexture(gl.TEXTURE_2D, sl ? sl.tex : this.tex[4 + i]);
      gl.uniform1i(gl.getUniformLocation(this.terrainProg, `uTexF${i}`), 4 + i);
      const p = sl && sl.patch;
      gl.uniform4f(gl.getUniformLocation(this.terrainProg, `uFocus${i}`),
        p ? p.x : 0, p ? p.y : 0, p ? p.half : 1, p ? p.texel : 0);
    }
    gl.uniform1f(gl.getUniformLocation(this.terrainProg, 'uPix'), this.pixAngle());
    gl.uniform4f(gl.getUniformLocation(this.terrainProg, 'uHalf'),
      this.texHalf[0], this.texHalf[1], this.texHalf[2], this.texHalf[3]);
    gl.uniform1f(gl.getUniformLocation(this.terrainProg, 'uTexMix'),
      (this.texWanted && this.texReady) ? 1 : 0);
    gl.uniform3f(gl.getUniformLocation(this.terrainProg, 'uHazeColor'), 0.72, 0.78, 0.85);

    const ap = gl.getAttribLocation(this.terrainProg, 'aPos');
    gl.bindBuffer(gl.ARRAY_BUFFER, this.pos);
    gl.enableVertexAttribArray(ap);
    gl.vertexAttribPointer(ap, 3, gl.FLOAT, false, 0, 0);
    const ac = gl.getAttribLocation(this.terrainProg, 'aColor');
    gl.bindBuffer(gl.ARRAY_BUFFER, this.col);
    gl.enableVertexAttribArray(ac);
    gl.vertexAttribPointer(ac, 3, gl.FLOAT, false, 0, 0);

    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.idx);
    gl.drawElements(gl.TRIANGLES, this.indexCount, this.indexType, 0);

    if (this.onChange) this.onChange(this);
  };

  /** Occlusion test for every named peak, cached against eye height.
   *
   *  This is the expensive half — up to ~1000 peaks each walking up to 300
   *  rings of atan2 — but it depends only on where the eye is, not on where it
   *  is looking. Caching it means dragging and zooming cost only the
   *  projection below, and the horizon walk re-runs solely when the height
   *  slider moves or a new mesh arrives.
   */
  Pano3D.prototype._visibility = function () {
    if (this._visEye === this.eyeHeight && this._vis) return this._vis;
    const { header, z, valid, radii, n_r, counts, offsets } = this.data;
    const eyeZ = header.ground_m + this.eyeHeight;
    const R = header.earth_r, k = header.refraction_k;
    const out = [];

    for (const p of header.peaks) {
      const drop = (1 - k) * p.dist * p.dist / (2 * R);
      const phi = Math.atan2(p.ele - eyeZ - drop, p.dist);

      // horizon along this azimuth, stopping well short of the summit so a
      // big massif is not occluded by its own flank
      const back = Math.max(400, 0.12 * p.dist);
      let horizon = -Math.PI / 2;
      for (let r = 1; r < n_r; r++) {
        const d = radii[r];
        if (d >= p.dist - back) break;
        const c = counts[r];                     // rings differ in azimuth count
        const i = offsets[r] + (Math.round(p.az / 360 * c) % c);
        if (!valid[i]) continue;                 // extrapolated ground occludes nothing
        const a = Math.atan2(z[i] - eyeZ, d);
        if (a > horizon) horizon = a;
      }
      const clear = phi - horizon;
      const need = MIN_CLEAR_DEG * (1 + p.dist / CLEAR_DIST_SCALE_M)
        * Math.PI / 180;
      if (clear < need) continue;

      out.push({
        name: p.name, ele: p.ele, az: p.az, dist: p.dist, drop,
        volcano: p.volcano, prom: p.prom,
        dist_km: p.dist / 1000,
        clear_deg: clear * 180 / Math.PI,
        // Metres the summit stands above the ridge in front of it. Clamped
        // below 90 deg: a close summit seen over a steeply downhill horizon
        // can clear it by more than a right angle, and tan() would then go
        // negative and rank a towering peak last.
        stands: p.dist * Math.tan(Math.min(clear, 1.4)),
      });
    }
    out.sort((a, b) => b.stands - a.stands);

    // Some massifs carry the same name on more than one OSM summit node —
    // Mount Daniel has two, 728 m apart — and labelling both is pure noise.
    // Drop a peak when a more prominent one of the same name is already kept
    // nearby. Proximity matters: 43 names repeat across this AOI (five Red
    // Mountains, five Iron Mountains), and those are genuinely different
    // hills. 1500 m sits in a clear gap in the data — every same-name pair
    // closer than 1.3 km is one massif, and the next closest distinct pair is
    // 2.4 km away.
    const kept = [];
    const byName = new Map();
    for (const p of out) {
      const th = p.az * Math.PI / 180;
      const x = p.dist * Math.sin(th), y = p.dist * Math.cos(th);
      const prev = byName.get(p.name);
      if (prev && prev.some((q) => Math.hypot(q.x - x, q.y - y) < DEDUPE_NAME_M)) {
        continue;
      }
      if (prev) prev.push({ x, y }); else byName.set(p.name, [{ x, y }]);
      kept.push(p);
    }

    // Drop summits that are not independent mountains.
    //
    // One test replaces the old two-stage dance of ranking by elevation and
    // then suppressing neighbours inside a fitted radius. A sub-peak fails
    // this because its prominence is small, wherever it happens to sit.
    const final = kept.filter((p) => {
      const prom = p.prom !== undefined ? p.prom : UNMATCHED_PROM_M;
      return Math.atan2(prom, p.dist) * 180 / Math.PI >= MIN_PROM_DEG;
    });

    // collapse summits that are the same mountain under two names
    const merged = [];
    for (const p of final) {
      const th = p.az * Math.PI / 180;
      const x = p.dist * Math.sin(th), y = p.dist * Math.cos(th);
      const j = merged.findIndex((q) =>
        Math.hypot(q._cx - x, q._cy - y) < COINCIDENT_M
        && p.prom !== undefined && q.prom !== undefined
        && Math.abs(p.prom - q.prom) <= COINCIDENT_PROM_TOL_M);
      p._cx = x; p._cy = y;
      if (j < 0) { merged.push(p); continue; }
      // Same mountain: keep the nearer node. The loop runs in standing-height
      // order, not distance order, so simply keeping the first would have
      // labelled Rainier "Columbia Crest".
      if (p.dist < merged[j].dist) merged[j] = p;
    }

    this._vis = merged;
    this._visEye = this.eyeHeight;
    return merged;
  };

  /** Visible peaks projected to screen, in standing-height order (callers
   *  re-rank by prominence for labelling). */
  Pano3D.prototype.visiblePeaks = function () {
    if (!this.data) return [];
    const { mvp } = this.matrices();
    const W = this.canvas.clientWidth, H = this.canvas.clientHeight;
    const out = [];
    for (const p of this._visibility()) {
      const th = p.az * Math.PI / 180;
      const wx = p.dist * Math.sin(th), wy = p.dist * Math.cos(th);
      const wz = p.ele - p.drop;
      const cw = mvp[3] * wx + mvp[7] * wy + mvp[11] * wz + mvp[15];
      if (cw <= 0) continue;                     // behind the camera
      const cx = mvp[0] * wx + mvp[4] * wy + mvp[8] * wz + mvp[12];
      const sx = (cx / cw * 0.5 + 0.5) * W;
      if (sx < -80 || sx > W + 80) continue;
      const cy = mvp[1] * wx + mvp[5] * wy + mvp[9] * wz + mvp[13];
      const sy = (1 - (cy / cw * 0.5 + 0.5)) * H;
      if (sy < -40 || sy > H + 40) continue;
      // prom has to come through: app.js ranks the standing labels by it,
      // and without it every peak fell back to the same floor value and the
      // labels went out in standing-height order instead
      out.push({ name: p.name, ele: p.ele, dist_km: p.dist_km, prom: p.prom,
                 stands: p.stands, volcano: p.volcano, x: sx, y: sy });
    }
    return out;
  };

  /** Where the ground under a screen pixel is, as {az, dist} from the observer.
   *
   *  A ray leaving the eye keeps a constant compass azimuth — only its height
   *  changes with distance — so this is not a 3D march through the mesh but a
   *  1D walk outward along one azimuth, comparing the ray's height against the
   *  terrain profile. First crossing is the hit, refined by bisection between
   *  the bracketing rings.
   *
   *  Returns null when the pixel is sky (the ray never catches the ground).
   */
  Pano3D.prototype.pickGround = function (px, py) {
    if (!this.data) return null;
    const W = this.canvas.clientWidth, H = this.canvas.clientHeight;
    const tanV = Math.tan(this.fov * Math.PI / 360);
    const tanH = tanV * (W / Math.max(1, H));
    const nx = (2 * px / W - 1) * tanH;
    const ny = (1 - 2 * py / H) * tanV;

    // camera basis: forward, side = forward x up, true up = side x forward
    const f = this.dir();
    let sx = f[1] * 1 - f[2] * 0, sy = f[2] * 0 - f[0] * 1, sz = 0;
    let l = Math.hypot(sx, sy, sz) || 1; sx /= l; sy /= l; sz /= l;
    const ux = sy * f[2] - sz * f[1];
    const uy = sz * f[0] - sx * f[2];
    const uz = sx * f[1] - sy * f[0];
    const dx = f[0] + nx * sx + ny * ux;
    const dy = f[1] + nx * sy + ny * uy;
    const dz = f[2] + nx * sz + ny * uz;

    const flat = Math.hypot(dx, dy);
    if (flat < 1e-9) return null;                 // straight up or down
    const eye = this.eye();
    const { radii, n_r } = this.data;
    const maxR = radii[n_r - 1];

    // Marched in world space, stepping outward from the camera.
    //
    // The constant-azimuth shortcut this used to rely on only holds when the
    // camera sits at the mesh centre. Once it can be nudged sideways, a view
    // ray no longer keeps a fixed bearing about that centre, so each sample is
    // converted from camera-relative to world and looked up there.
    const rx = dx / flat, ry = dy / flat, slope = dz / flat;
    let prevT = 0, prevGap = null;
    for (let t = 2; t < maxR * 2; t *= 1.02) {
      const wx = eye[0] + rx * t, wy = eye[1] + ry * t;
      if (Math.hypot(wx, wy) > maxR) break;
      const h = this.heightAtWorld(wx, wy);
      if (h === null) break;
      const gap = h - (eye[2] + slope * t);       // terrain minus ray
      if (gap >= 0) {
        let lo = prevGap === null ? 0 : prevT, hi = t;
        for (let k = 0; k < 24; k++) {            // bisect the crossing
          const mid = (lo + hi) / 2;
          const g2 = this.heightAtWorld(eye[0] + rx * mid, eye[1] + ry * mid);
          if (g2 === null) break;
          if (g2 - (eye[2] + slope * mid) >= 0) hi = mid; else lo = mid;
        }
        const d = (lo + hi) / 2;
        const px = eye[0] + rx * d, py = eye[1] + ry * d;
        return { az: ((Math.atan2(px, py) * 180 / Math.PI) % 360 + 360) % 360,
                 dist: Math.hypot(px, py) };
      }
      prevT = t; prevGap = gap;
    }
    return null;                                   // sky
  };

  /** Screen y of the true horizontal, i.e. elevation angle 0 from the eye.
   *
   *  The camera has no roll (up is always world +Z), so the 0-degree line
   *  projects to a horizontal line and one projected point fixes it. Note this
   *  is *not* the visible horizon: curvature drops that below 0, and the gap
   *  between the two is the whole reason the line is worth drawing.
   */
  Pano3D.prototype.horizonY = function () {
    if (!this.data) return null;
    const m = this.matrices().mvp;
    const eyeZ = this.data.header.ground_m + this.eyeHeight;
    const d = 1e6;                       // far along the current bearing…
    const th = this.yaw * Math.PI / 180;
    const wx = d * Math.sin(th), wy = d * Math.cos(th);
    const cw = m[3] * wx + m[7] * wy + m[11] * eyeZ + m[15];
    if (cw <= 0) return null;
    const cy = m[1] * wx + m[5] * wy + m[9] * eyeZ + m[13];
    return (1 - (cy / cw * 0.5 + 0.5)) * this.canvas.clientHeight;
  };

  /** Screen x of a compass azimuth, taken on the true (curvature-dropped)
   *  horizon. Returns null when that direction is behind the camera. */
  Pano3D.prototype.projectAzimuth = function (azDeg) {
    if (!this.data) return null;
    const h = this.data.header;
    const d = h.range_km * 1000 * 0.85;
    const th = azDeg * Math.PI / 180;
    const drop = (1 - h.refraction_k) * d * d / (2 * h.earth_r);
    const wx = d * Math.sin(th), wy = d * Math.cos(th);
    const wz = h.ground_m + this.eyeHeight - drop;
    const m = this.matrices().mvp;
    const cx = m[0] * wx + m[4] * wy + m[8] * wz + m[12];
    const cw = m[3] * wx + m[7] * wy + m[11] * wz + m[15];
    if (cw <= 0) return null;
    return (cx / cw * 0.5 + 0.5) * this.canvas.clientWidth;
  };

  /** The inverse of pickGround: where a ground point at (az, dist) lands on
   *  screen, or null if it is behind the camera, off-pane, or hidden.
   *
   *  Height comes from the mesh itself rather than from a fresh DEM lookup, so
   *  the dot sits on the surface the renderer actually drew — curvature drop
   *  included, since that is already baked into the vertex heights.
   *
   *  Occlusion reuses the running-max rule the whole panorama is built on: the
   *  point is hidden if anything nearer along its own azimuth subtends a
   *  greater elevation angle.
   */
  Pano3D.prototype.projectGround = function (azDeg, dist) {
    if (!this.data || !(dist > 0)) return null;
    const { radii, counts, offsets, n_r, z } = this.data;
    if (dist > radii[n_r - 1]) return null;
    const az = ((azDeg % 360) + 360) % 360;
    const hAt = (i) => {
      const c = counts[i];
      return z[offsets[i] + (Math.round(az / 360 * c) % c)];
    };
    // ring bracket, then linear interpolation along the radius
    let i = 1;
    while (i < n_r - 1 && radii[i] < dist) i++;
    const t = (dist - radii[i - 1]) / Math.max(1e-9, radii[i] - radii[i - 1]);
    const wz = hAt(i - 1) * (1 - t) + hAt(i) * t;

    const eyeZ = this.data.header.ground_m + this.eyeHeight;
    let horizon = -Math.PI / 2;
    for (let k = 1; k < i; k++) {
      const a = Math.atan2(hAt(k) - eyeZ, radii[k]);
      if (a > horizon) horizon = a;
    }
    const hidden = Math.atan2(wz - eyeZ, dist) < horizon - 1e-6;

    const th = az * Math.PI / 180;
    const wx = dist * Math.sin(th), wy = dist * Math.cos(th);
    const m = this.matrices().mvp;
    const cw = m[3] * wx + m[7] * wy + m[11] * wz + m[15];
    if (cw <= 0) return null;                       // behind the camera
    const cx = m[0] * wx + m[4] * wy + m[8] * wz + m[12];
    const cy = m[1] * wx + m[5] * wy + m[9] * wz + m[13];
    const W = this.canvas.clientWidth, H = this.canvas.clientHeight;
    const sx = (cx / cw * 0.5 + 0.5) * W;
    const sy = (1 - (cy / cw * 0.5 + 0.5)) * H;
    if (sx < -40 || sx > W + 40 || sy < -40 || sy > H + 40) return null;
    return { x: sx, y: sy, hidden };
  };

  Pano3D.prototype.setView = function (o) {
    if (o.yaw !== undefined) this.yaw = ((o.yaw % 360) + 360) % 360;
    if (o.pitch !== undefined) this.pitch = Math.max(-85, Math.min(85, o.pitch));
    if (o.fov !== undefined) this.fov = Math.max(8, Math.min(100, o.fov));
    // not 0: at exactly ground level the camera is coincident with the centre
    // vertex, so it sits *in* the surface and sees through it
    if (o.eyeHeight !== undefined) this.eyeHeight = Math.max(0.1, o.eyeHeight);
    this.render();
  };

  Pano3D.prototype._installControls = function () {
    const c = this.canvas;
    let drag = null;
    c.addEventListener('mousedown', (e) => {
      drag = { x: e.clientX, y: e.clientY, yaw: this.yaw, pitch: this.pitch };
      c.style.cursor = 'grabbing';
      e.preventDefault();
    });
    global.addEventListener('mousemove', (e) => {
      if (!drag) return;
      // scale by FOV so dragging feels the same when zoomed in
      const s = this.fov / Math.max(1, c.clientHeight);
      this.setView({
        yaw: drag.yaw - (e.clientX - drag.x) * s * 1.6,
        pitch: drag.pitch + (e.clientY - drag.y) * s * 1.1,
      });
    });
    global.addEventListener('mouseup', () => {
      drag = null; c.style.cursor = 'grab';
    });
    c.addEventListener('wheel', (e) => {
      e.preventDefault();
      this.setView({ fov: this.fov * (e.deltaY > 0 ? 1.12 : 1 / 1.12) });
    }, { passive: false });
    c.style.cursor = 'grab';
  };

  // full 4x4 inverse — only needed once per frame for the sky ray
  function invert(m) {
    const o = new Float32Array(16);
    const a00 = m[0], a01 = m[1], a02 = m[2], a03 = m[3];
    const a10 = m[4], a11 = m[5], a12 = m[6], a13 = m[7];
    const a20 = m[8], a21 = m[9], a22 = m[10], a23 = m[11];
    const a30 = m[12], a31 = m[13], a32 = m[14], a33 = m[15];
    const b00 = a00 * a11 - a01 * a10, b01 = a00 * a12 - a02 * a10;
    const b02 = a00 * a13 - a03 * a10, b03 = a01 * a12 - a02 * a11;
    const b04 = a01 * a13 - a03 * a11, b05 = a02 * a13 - a03 * a12;
    const b06 = a20 * a31 - a21 * a30, b07 = a20 * a32 - a22 * a30;
    const b08 = a20 * a33 - a23 * a30, b09 = a21 * a32 - a22 * a31;
    const b10 = a21 * a33 - a23 * a31, b11 = a22 * a33 - a23 * a32;
    let det = b00 * b11 - b01 * b10 + b02 * b09 + b03 * b08 - b04 * b07 + b05 * b06;
    if (!det) return o;
    det = 1.0 / det;
    o[0] = (a11 * b11 - a12 * b10 + a13 * b09) * det;
    o[1] = (a02 * b10 - a01 * b11 - a03 * b09) * det;
    o[2] = (a31 * b05 - a32 * b04 + a33 * b03) * det;
    o[3] = (a22 * b04 - a21 * b05 - a23 * b03) * det;
    o[4] = (a12 * b08 - a10 * b11 - a13 * b07) * det;
    o[5] = (a00 * b11 - a02 * b08 + a03 * b07) * det;
    o[6] = (a32 * b02 - a30 * b05 - a33 * b01) * det;
    o[7] = (a20 * b05 - a22 * b02 + a23 * b01) * det;
    o[8] = (a10 * b10 - a11 * b08 + a13 * b06) * det;
    o[9] = (a01 * b08 - a00 * b10 - a03 * b06) * det;
    o[10] = (a30 * b04 - a31 * b02 + a33 * b00) * det;
    o[11] = (a21 * b02 - a20 * b04 - a23 * b00) * det;
    o[12] = (a11 * b07 - a10 * b09 - a12 * b06) * det;
    o[13] = (a00 * b09 - a01 * b07 + a02 * b06) * det;
    o[14] = (a31 * b01 - a30 * b03 - a32 * b00) * det;
    o[15] = (a20 * b03 - a21 * b01 + a22 * b00) * det;
    return o;
  }

  global.Pano3D = Pano3D;
})(window);
