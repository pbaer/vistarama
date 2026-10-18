"""The view score V on the GPU, for many points at once.

Same method as `vscore.view_score` (and the same `version_tag`), reduced to the
single number V: no per-band or per-class breakdown, which is what bulk scoring
of trails, peaks and passes needs. Validated against the CPU version, see
`validate()`.

One CUDA thread per ray. Each walks its ray outward step by step exactly as
the CPU march does across a whole array at once: bilinear DEM sample (invalid
corners dropped and the weights renormalised), curvature and refraction drop,
elevation angle, running maximum, and for every newly visible band its solid
angle times the land-cover weight, the haze taper and d^gamma. A ray stops at
its first sample with no valid DEM corner, as on the CPU. Rays never share
work, so there is no synchronisation at all, and the DEM, its validity mask
and the land cover stay resident on the card between batches.

Ray geometry is computed on the GPU too. Preparing it on the CPU with pyproj
cost 24-32 ms a point against 0.46 ms for the march itself, so the CPU was
the whole bottleneck. A second kernel, one thread per knot, steps along each
ray with Vincenty's direct geodesic on WGS84 (an observer-centred AEQD ray is
exactly a geodesic of that azimuth, which is what the CPU march transforms)
and projects with Snyder's ellipsoidal Albers, the EPSG:5070 the DEM is in,
which pyproj reaches from EPSG:4326 with a null datum shift. Both in double
precision; checked against pyproj to a thousandth of a cell. The march then
interpolates the knots linearly, as the CPU march does with projected
coordinates (the affine DEM transform makes the two identical).

Two numerical details differ from the float64 CPU code. The kernel works in
float32, and it computes each band's solid angle as
2 cos((a+b)/2) sin((a-b)/2) rather than sin(a) - sin(b), which avoids the
cancellation float32 would otherwise suffer for thin bands. Per-ray sums are
accumulated in float64.
"""
from __future__ import annotations

import math
import time
from pathlib import Path

import numpy as np
from pyproj import Transformer

import panorama
import vscore

_KNOTS_SRC = r"""
// One thread per (point, ray, knot): the knot's fractional DEM cell
// coordinates. Vincenty direct on the WGS84 ellipsoid, then Albers equal-area
// conic on GRS80 (EPSG:5070), then the DEM's inverse affine transform.
extern "C" __global__
void knots(const double* __restrict__ lat0, const double* __restrict__ lon0,
           const int n_points, const int n_az, const int n_knots,
           const double knot_m,
           const double ax0, const double ay0, const double px,
           float* __restrict__ kc, float* __restrict__ kr)
{
    const long long t = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    const long long total = (long long)n_points * n_az * n_knots;
    if (t >= total) return;
    const int k = (int)(t % n_knots);
    const int a = (int)((t / n_knots) % n_az);
    const int p = (int)(t / ((long long)n_knots * n_az));
    const double PI = 3.14159265358979323846;
    const double D2R = PI / 180.0;

    // --- Vincenty direct, WGS84 ---
    const double A = 6378137.0, F = 1.0 / 298.257223563, B = A * (1.0 - F);
    const double s = (double)k * knot_m;
    const double alpha1 = (360.0 * a / n_az) * D2R;
    const double phi1 = lat0[p] * D2R, lam1 = lon0[p] * D2R;
    double phi2 = phi1, lam2 = lam1;
    if (s > 0.0) {
        const double sa1 = sin(alpha1), ca1 = cos(alpha1);
        const double tanU1 = (1.0 - F) * tan(phi1);
        const double cU1 = 1.0 / sqrt(1.0 + tanU1 * tanU1), sU1 = tanU1 * cU1;
        const double sig1 = atan2(tanU1, ca1);
        const double sAlpha = cU1 * sa1;
        const double c2Alpha = 1.0 - sAlpha * sAlpha;
        const double u2 = c2Alpha * (A * A - B * B) / (B * B);
        const double AA = 1.0 + u2 / 16384.0 * (4096.0 + u2 * (-768.0 + u2 * (320.0 - 175.0 * u2)));
        const double BB = u2 / 1024.0 * (256.0 + u2 * (-128.0 + u2 * (74.0 - 47.0 * u2)));
        double sig = s / (B * AA), sigp, c2sm, ss, cs;
        for (int it = 0; it < 20; ++it) {
            c2sm = cos(2.0 * sig1 + sig);
            ss = sin(sig); cs = cos(sig);
            const double dsig = BB * ss * (c2sm + BB / 4.0 * (cs * (-1.0 + 2.0 * c2sm * c2sm)
                - BB / 6.0 * c2sm * (-3.0 + 4.0 * ss * ss) * (-3.0 + 4.0 * c2sm * c2sm)));
            sigp = sig;
            sig = s / (B * AA) + dsig;
            if (fabs(sig - sigp) < 1e-12) break;
        }
        c2sm = cos(2.0 * sig1 + sig); ss = sin(sig); cs = cos(sig);
        const double tmp = sU1 * ss - cU1 * cs * ca1;
        phi2 = atan2(sU1 * cs + cU1 * ss * ca1,
                     (1.0 - F) * sqrt(sAlpha * sAlpha + tmp * tmp));
        const double lam = atan2(ss * sa1, cU1 * cs - sU1 * ss * ca1);
        const double C = F / 16.0 * c2Alpha * (4.0 + F * (4.0 - 3.0 * c2Alpha));
        const double L = lam - (1.0 - C) * F * sAlpha
            * (sig + C * ss * (c2sm + C * cs * (-1.0 + 2.0 * c2sm * c2sm)));
        lam2 = lam1 + L;
    }

    // --- Albers equal-area conic, GRS80, EPSG:5070 ---
    const double Ag = 6378137.0, Fg = 1.0 / 298.257222101;
    const double e2 = Fg * (2.0 - Fg), e = sqrt(e2);
    #define QF(ph) ((1.0 - e2) * (sin(ph) / (1.0 - e2 * sin(ph) * sin(ph)) \
        - 1.0 / (2.0 * e) * log((1.0 - e * sin(ph)) / (1.0 + e * sin(ph)))))
    #define MF(ph) (cos(ph) / sqrt(1.0 - e2 * sin(ph) * sin(ph)))
    const double la0 = 23.0 * D2R, la1 = 29.5 * D2R, la2 = 45.5 * D2R, lo0 = -96.0 * D2R;
    const double m1 = MF(la1), m2 = MF(la2);
    const double q0 = QF(la0), q1 = QF(la1), q2 = QF(la2);
    const double n = (m1 * m1 - m2 * m2) / (q2 - q1);
    const double Cc = m1 * m1 + n * q1;
    const double rho0 = Ag * sqrt(Cc - n * q0) / n;
    const double q = QF(phi2);
    const double rho = Ag * sqrt(Cc - n * q) / n;
    const double th = n * (lam2 - lo0);
    const double x = rho * sin(th), y = rho0 - rho * cos(th);
    #undef QF
    #undef MF

    kc[t] = (float)((x - ax0) / px);
    kr[t] = (float)((ay0 - y) / px);
}
"""

_KERNEL_SRC = r"""
#define MAX_REACH 64
extern "C" __global__
void march(const float* __restrict__ dem, const unsigned char* __restrict__ bad,
           const unsigned char* __restrict__ lc, const float* __restrict__ lut,
           const unsigned char* __restrict__ tcount, const float tscale,
           const int H, const int W,
           const float* __restrict__ kc, const float* __restrict__ kr,
           const float* __restrict__ eye_z,
           const int n_az, const int n_knots, const int n_steps,
           const int n_rays,
           const float step, const float knot_m, const float cutoff,
           const float d_haze, const float gamma, const float d_alpha,
           const float curv, const int n_reach, const float canopy_h,
           const float canopy_mfp,
           const float loom_gain, const float loom_full, const float lift,
           double* __restrict__ out, float* __restrict__ hz_out)
{
    const int ray = blockIdx.x * blockDim.x + threadIdx.x;
    if (ray >= n_rays) return;
    const int p = ray / n_az;
    const float* KC = kc + (size_t)ray * n_knots;
    const float* KR = kr + (size_t)ray * n_knots;
    const float ez = eye_z[p];
    float run = -1.57079632679f;
    double v = 0.0;
    // the first n_reach samples' ground (curvature dropped) and forest share,
    // for the canopy transmittance of every later sightline (see vscore.py)
    float g_near[MAX_REACH];
    float t_near[MAX_REACH];
    int n_near = 0;
    for (int j = 0; j < n_steps; ++j) {
        const float d = (float)(j + 1) * step;
        int k = (int)floorf(d / knot_m);
        if (k > n_knots - 2) k = n_knots - 2;
        const float f = (d - (float)k * knot_m) / knot_m;
        const float fc = KC[k] + (KC[k + 1] - KC[k]) * f;
        const float fr = KR[k] + (KR[k + 1] - KR[k]) * f;

        // bilinear between cell centres, dropping invalid corners
        const float c = fc - 0.5f, r = fr - 0.5f;
        const int c0 = (int)floorf(c), r0 = (int)floorf(r);
        const float tx = c - (float)c0, ty = r - (float)r0;
        float acc = 0.f, ws = 0.f;
        #pragma unroll
        for (int q = 0; q < 4; ++q) {
            const int rr = r0 + (q >> 1), cc = c0 + (q & 1);
            const float w = ((q & 1) ? tx : 1.f - tx) * ((q >> 1) ? ty : 1.f - ty);
            if (rr >= 0 && rr < H && cc >= 0 && cc < W) {
                const size_t i = (size_t)rr * W + cc;
                if (!bad[i]) { acc += dem[i] * w; ws += w; }
            }
        }
        if (ws <= 1e-9f) break;          // the data edge: nothing beyond to see
        const float z = acc / ws;

        const float g = z - curv * d * d;
        const float phi = atan2f(g - ez, d);
        int ci = (int)floorf(fc), ri = (int)floorf(fr);
        ci = ci < 0 ? 0 : (ci > W - 1 ? W - 1 : ci);
        ri = ri < 0 ? 0 : (ri > H - 1 ? H - 1 : ri);
        const unsigned char cls = lc[(size_t)ri * W + ci];
        if (phi > run) {
            if (d >= cutoff) {
                const float cw = lut[cls];
                const float ds = 2.f * cosf(0.5f * (phi + run)) * sinf(0.5f * (phi - run));
                // canopy: the line to the band's top, from the standing eye
                float tau = 0.f;
                const float tn = tanf(phi) + lift / d;
                const float es = ez - lift;
                for (int k = 0; k < n_near; ++k) {
                    if (es + (float)(k + 1) * step * tn - g_near[k] < canopy_h)
                        tau += step * t_near[k];
                }
                const float lm = fminf(fmaxf(0.5f * (phi + run) / loom_full, 0.f), 1.f);
                v += (double)(d_alpha * ds * cw * expf(-d / d_haze)
                              * powf(d * 0.001f, gamma) * expf(-tau / canopy_mfp)
                              * (1.f + loom_gain * lm));
            }
            run = phi;
        }
        if (j < n_reach) {
            g_near[j] = g;
            t_near[j] = (float)tcount[(size_t)ri * W + ci] * tscale;
            n_near = j + 1;
        }
    }
    out[ray] = v;
    hz_out[ray] = run;                 // the skyline's elevation angle, radians
}
"""

BATCH = 256           # points per launch: 184k march threads, ~220 MB of knots


class GpuScorer:
    """Holds the rasters on the GPU and scores batches of points."""

    def __init__(self, dem_path: Path, landcover_path: Path):
        import cupy as cp
        from rasterio.enums import Resampling
        self.cp = cp
        dem, crs, tf, bounds, nodata = panorama._read_full(dem_path)
        bad = ~np.isfinite(dem) | (dem <= panorama.ELEV_FLOOR_M)
        if nodata is not None:
            bad |= dem == nodata
        lc, _ = panorama._read_window(Path(landcover_path), bounds, dem.shape,
                                      Resampling.nearest)
        self.dem_np, self.bad_np = dem, bad
        self.crs, self.tf, self.inv = crs, tf, ~tf
        self.H, self.W = dem.shape
        self.dem = cp.asarray(np.where(bad, 0, dem).astype(np.float32))
        self.bad = cp.asarray(bad.astype(np.uint8))
        self.lc = cp.asarray(np.ascontiguousarray(lc, dtype=np.uint8))
        self.lut = cp.asarray(vscore._class_lut().astype(np.float32))
        self.tcount = cp.asarray(np.ascontiguousarray(
            vscore.tree_count(Path(landcover_path), bounds, dem.shape)))
        self.kernel = cp.RawKernel(_KERNEL_SRC, "march")
        self.knot_kernel = cp.RawKernel(_KNOTS_SRC, "knots")
        self.to_dem = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        if abs(tf.a - (-tf.e)) > 1e-9 or tf.b or tf.d:
            raise ValueError("the GPU knots assume a north-up DEM with square cells")
        if "aea" not in crs.to_proj4() or "lat_1=29.5" not in crs.to_proj4():
            raise ValueError("the GPU knots implement EPSG:5070 only")

        self.n_az = vscore.N_AZ
        self.step = vscore.STEP_M
        self.n_steps = int(vscore.RANGE_M // self.step)
        # samples within the canopy reach, as vscore counts them
        self.n_reach = int(vscore.CANOPY_REACH_M // self.step)
        if self.n_reach > 64:
            raise ValueError("CANOPY_REACH_M / STEP_M exceeds the kernel's MAX_REACH")
        d_last = self.n_steps * self.step
        self.knots = np.arange(0.0, d_last + vscore.KNOT_M, vscore.KNOT_M)
        ar = np.radians(np.arange(self.n_az) * (360.0 / self.n_az))
        self.kx = np.sin(ar)[:, None] * self.knots[None, :]
        self.ky = np.cos(ar)[:, None] * self.knots[None, :]
    def gpu_knots(self, lats, lons):
        """Fractional DEM cell coordinates of every ray's knots, on the GPU,
        shaped (points, rays, knots)."""
        cp = self.cp
        n = len(lats)
        nk = self.knots.size
        kc = cp.empty((n, self.n_az, nk), dtype=cp.float32)
        kr = cp.empty_like(kc)
        total = n * self.n_az * nk
        threads = 256
        self.knot_kernel(
            ((total + threads - 1) // threads,), (threads,),
            (cp.asarray(np.asarray(lats, np.float64)),
             cp.asarray(np.asarray(lons, np.float64)),
             np.int32(n), np.int32(self.n_az), np.int32(nk),
             np.float64(vscore.KNOT_M),
             np.float64(self.tf.c), np.float64(self.tf.f), np.float64(self.tf.a),
             kc, kr))
        return kc, kr

    def eye_z(self, lats, lons):
        """Ground height + eye height at each point, NaN off the DEM."""
        X, Y = self.to_dem.transform(np.asarray(lons), np.asarray(lats))
        oc, orr = self.inv * (np.asarray(X), np.asarray(Y))
        gz, gok = panorama._bilinear(self.dem_np, np.asarray(oc), np.asarray(orr),
                                     self.bad_np)
        return np.where(gok, gz + vscore.EYE_HEIGHT_M + vscore.TERRAIN_LIFT_M, np.nan)

    def _prepare(self, lat: float, lon: float):
        """Fractional DEM cell coordinates of every ray's knots, and eye z."""
        aeqd = (f"+proj=aeqd +lat_0={lat} +lon_0={lon} +x_0=0 +y_0=0 "
                f"+datum=WGS84 +units=m +no_defs")
        a2d = Transformer.from_crs(aeqd, self.crs, always_xy=True)
        X, Y = a2d.transform(self.kx.ravel(), self.ky.ravel())
        fc, fr = self.inv * (np.asarray(X), np.asarray(Y))
        ox, oy = self.to_dem.transform(lon, lat)
        oc, orr = self.inv * (ox, oy)
        gz, gok = panorama._bilinear(self.dem_np, np.array([oc]), np.array([orr]),
                                     self.bad_np)
        if not bool(gok[0]):
            return None
        return (np.asarray(fc, np.float32), np.asarray(fr, np.float32),
                float(gz[0]) + vscore.EYE_HEIGHT_M + vscore.TERRAIN_LIFT_M)

    def score(self, points: list[tuple[float, float]], progress=None) -> list[float | None]:
        """V for each (lat, lon); None where the point is off the DEM."""
        out: list[float | None] = [None] * len(points)
        for idx, res, _hz in self._batches(points, progress):
            per_point = res.reshape(len(idx), self.n_az).sum(axis=1).get()
            for i, v in zip(idx, per_point):
                out[i] = float(v)
        return out

    def score_rays(self, points: list[tuple[float, float]]) -> list:
        """Per ray, for each point: (V of each of the N_AZ rays, the skyline's
        elevation angle along each in degrees), or None off the DEM."""
        out: list = [None] * len(points)
        for idx, res, hz in self._batches(points, None):
            v = res.reshape(len(idx), self.n_az).get()
            h = np.degrees(hz.reshape(len(idx), self.n_az).get())
            for k, i in enumerate(idx):
                out[i] = (v[k], h[k])
        return out

    def _batches(self, points, progress):
        """Run the march in batches: yields (point indices, per-ray V,
        per-ray skyline angle), the arrays still on the GPU."""
        cp = self.cp
        nk = self.knots.size
        lats_all = np.array([p[0] for p in points], np.float64)
        lons_all = np.array([p[1] for p in points], np.float64)
        ez_all = self.eye_z(lats_all, lons_all)
        for b0 in range(0, len(points), BATCH):
            idx = [i for i in range(b0, min(b0 + BATCH, len(points)))
                   if np.isfinite(ez_all[i])]
            if idx:
                kc, kr = self.gpu_knots(lats_all[idx], lons_all[idx])
                ez = ez_all[idx].astype(np.float32)
                n_rays = len(idx) * self.n_az
                res = cp.zeros(n_rays, dtype=cp.float64)
                hz = cp.zeros(n_rays, dtype=cp.float32)
                threads = 128
                self.kernel(
                    ((n_rays + threads - 1) // threads,), (threads,),
                    (self.dem, self.bad, self.lc, self.lut, self.tcount,
                     np.float32(1.0 / vscore.CANOPY_SUBCELLS ** 2),
                     np.int32(self.H), np.int32(self.W),
                     kc, kr, cp.asarray(ez),
                     np.int32(self.n_az), np.int32(nk), np.int32(self.n_steps),
                     np.int32(n_rays),
                     np.float32(self.step), np.float32(vscore.KNOT_M),
                     np.float32(vscore.NEAR_CUTOFF_M), np.float32(vscore.D_HAZE_M),
                     np.float32(vscore.GAMMA),
                     np.float32(2.0 * math.pi / self.n_az),
                     np.float32((1.0 - panorama.REFRACTION_K) / (2.0 * panorama.EARTH_R)),
                     np.int32(self.n_reach), np.float32(vscore.CANOPY_H_M),
                     np.float32(vscore.CANOPY_MFP_M),
                     np.float32(vscore.LOOM_GAIN),
                     np.float32(math.radians(vscore.LOOM_FULL_DEG)),
                     np.float32(vscore.TERRAIN_LIFT_M),
                     res, hz))
                yield idx, res, hz
            if progress:
                progress(min(len(points), b0 + BATCH), len(points))


def validate(dem: Path, lc: Path, n: int = 24, seed: int = 3) -> dict:
    """Compare against the CPU march at random points in the core AOI."""
    import json
    from aoi import AOI_DIR
    aoi = json.loads((AOI_DIR / "alpine-lakes.json").read_text("utf-8"))["core"]
    rng = np.random.default_rng(seed)
    pts = [(float(rng.uniform(aoi["south"], aoi["north"])),
            float(rng.uniform(aoi["west"], aoi["east"]))) for _ in range(n)]
    pts += [(47.475131, -120.902302), (47.4985, -120.834)]   # Stuart, Colchuck
    g = GpuScorer(dem, lc)
    t = time.time()
    gv = g.score(pts)
    t_gpu = time.time() - t
    t = time.time()
    cv = [vscore.view_score(dem, lc, la, lo)["V"] for la, lo in pts]
    t_cpu = time.time() - t
    rel = [abs(a - b) / max(abs(b), 1e-9) for a, b in zip(gv, cv)]
    return {"points": len(pts), "max_rel_diff": max(rel),
            "mean_rel_diff": sum(rel) / len(rel),
            "gpu_s": t_gpu, "cpu_s": t_cpu,
            "pairs": [(round(a, 4), round(b, 4)) for a, b in zip(gv, cv)][-2:]}
