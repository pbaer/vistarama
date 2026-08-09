"""Static server for the viewer, plus a point-sampling API.

    python scripts/serve.py [port]

Serves the repository root so /viewer/ and /data/web/ are both reachable, and
redirects / to the viewer.

Two things beyond plain static serving:

* **Cache headers.** Tiles and vector chunks are content-stable (regenerating
  them rewrites the whole layer), so they get a long max-age. Without this the
  browser re-fetches every tile on every pan, which defeats the entire point of
  precomputing a pyramid. The viewer's own source and the manifest stay
  no-store so edits show up on reload.

* **/api/point.** Samples every derived raster at one lat/lon. Doing this
  server-side with rasterio is far simpler and more accurate than decoding
  colour values back out of PNG tiles in the browser.
"""
from __future__ import annotations

import json
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 8765
TILE_MAX_AGE = 86400

_lock = threading.Lock()
_transformers: dict[str, object] = {}

# id -> (path relative to repo root, unit, decimals)
SAMPLE_LAYERS = {
    "dem": ("data/interim/{slug}/dem_5070.tif", "m", 0),
    "slope": ("data/derived/{slug}/slope.tif", "°", 1),
    # ruggedness is an angle (RMS slope-vector deviation), relief is a length
    "ruggedness": ("data/derived/{slug}/ruggedness.tif", "°", 1),
    "relief_sigma": ("data/derived/{slug}/relief_sigma.tif", "m", 0),
    "hillshade": ("data/derived/{slug}/hillshade.tif", "/255", 0),
    "canopy_density": ("data/derived/{slug}/canopy_density.tif", "%", 0),
    "builtup_density": ("data/derived/{slug}/builtup_density.tif", "%", 0),
    "landcover": ("data/interim/{slug}/landcover_5070.tif", "", 0),
}

WORLDCOVER_LABELS = {
    10: "Tree cover", 20: "Shrubland", 30: "Grassland", 40: "Cropland",
    50: "Built-up", 60: "Bare / sparse", 70: "Snow and ice",
    80: "Permanent water", 90: "Herbaceous wetland", 95: "Mangroves",
    100: "Moss and lichen",
}


def _transformer(dst_crs: str):
    from pyproj import Transformer
    if dst_crs not in _transformers:
        _transformers[dst_crs] = Transformer.from_crs(
            "EPSG:4326", dst_crs, always_xy=True)
    return _transformers[dst_crs]


def sample_point(slug: str, lon: float, lat: float) -> dict:
    """Sample every derived raster at one point.

    Datasets are opened per request rather than cached. Holding them open
    would be marginally faster, but on Windows an open GDAL handle blocks
    deleting or overwriting the file, so a running viewer would make
    `process.py` fail with "Device or resource busy" every time you
    regenerate a layer. A few ms on a debounced hover endpoint is a fine
    price for not booby-trapping the rebuild loop.
    """
    import rasterio

    out: dict[str, object] = {}
    with _lock:
        for key, (tmpl, unit, nd) in SAMPLE_LAYERS.items():
            path = ROOT / tmpl.format(slug=slug)
            if not path.exists():
                continue
            try:
                with rasterio.open(path) as ds:
                    x, y = _transformer(ds.crs.to_string()).transform(lon, lat)
                    left, bottom, right, top = ds.bounds
                    if not (left <= x <= right and bottom <= y <= top):
                        out[key] = None
                        continue
                    val = float(next(ds.sample([(x, y)]))[0])
                    if ds.nodata is not None and val == ds.nodata:
                        val = None
                    elif key == "dem" and val is not None and val < -500:
                        val = None
                entry: dict[str, object] = {"value": val, "unit": unit,
                                            "decimals": nd}
                if key == "landcover" and val is not None:
                    entry["label"] = WORLDCOVER_LABELS.get(int(val), "unknown")
                out[key] = entry
            except Exception as exc:  # noqa: BLE001
                out[key] = {"error": str(exc)}
    return out


class Handler(SimpleHTTPRequestHandler):
    # SimpleHTTPRequestHandler defaults to HTTP/1.0, which closes the socket
    # after every response. With seven raster layers stacked that is ~400 tile
    # requests per view, each paying a fresh TCP handshake — enough to leave
    # tiles visibly trickling in. 1.1 keeps connections alive; Content-Length
    # is already set on every response path here, which 1.1 requires.
    protocol_version = "HTTP/1.1"

    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".geojson": "application/geo+json",
        ".json": "application/json",
        ".js": "text/javascript",
        ".css": "text/css",
        ".png": "image/png",
        ".jpg": "image/jpeg",
    }

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/point":
            return self._api_point(parse_qs(parsed.query))
        if parsed.path == "/api/panorama":
            return self._api_panorama(parse_qs(parsed.query))
        if parsed.path == "/api/terrain":
            return self._api_terrain(parse_qs(parsed.query))
        if parsed.path == "/api/snap":
            return self._api_snap(parse_qs(parsed.query))
        if parsed.path == "/api/texture":
            return self._api_texture(parse_qs(parsed.query))
        if parsed.path in ("/", "/index.html"):
            self.send_response(302)
            self.send_header("Location", "/viewer/index.html")
            self.end_headers()
            return
        super().do_GET()

    def _api_point(self, q: dict):
        try:
            lat = float(q.get("lat", ["nan"])[0])
            lon = float(q.get("lon", ["nan"])[0])
            slug = q.get("slug", ["alpine-lakes"])[0]
            payload = json.dumps({
                "lat": lat, "lon": lon,
                "layers": sample_point(slug, lon, lat),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
        except Exception as exc:  # noqa: BLE001
            body = json.dumps({"error": str(exc)}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def send_response_only(self, code, message=None):
        self._status = code
        super().send_response_only(code, message)

    def _api_panorama(self, q: dict):
        try:
            import panorama
            lat = float(q.get("lat", ["nan"])[0])
            lon = float(q.get("lon", ["nan"])[0])
            slug = q.get("slug", ["alpine-lakes"])[0]
            width = int(q.get("width", ["2160"])[0])
            fov = float(q.get("fov", ["60"])[0])
            el_center = float(q.get("el", ["-2"])[0])
            rng = float(q.get("range_km", ["60"])[0])
            base = ROOT / "data"
            payload = json.dumps(panorama.render(
                dem_path=base / "interim" / slug / "dem_5070.tif",
                hillshade_path=base / "derived" / slug / "hillshade.tif",
                landcover_path=base / "interim" / slug / "landcover_5070.tif",
                peaks_path=base / "raw" / "osm" / f"{slug}_peaks.geojson",
                lat=lat, lon=lon, width=width, fov_deg=fov,
                el_center=el_center, range_km=rng,
            )).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            body = json.dumps({"error": str(exc)}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def _api_terrain(self, q: dict):
        """Polar terrain mesh as binary: [u32 header_len][header JSON][payload]."""
        try:
            import struct
            import panorama
            lat = float(q.get("lat", ["nan"])[0])
            lon = float(q.get("lon", ["nan"])[0])
            slug = q.get("slug", ["alpine-lakes"])[0]
            rng = float(q.get("range_km", ["60"])[0])
            # must be a power of two: panorama.py rounds down, and every ring's
            # azimuth count has to divide the next one for the mesh to stitch
            n_az = int(q.get("n_az", ["2048"])[0])
            # only bounds the near-field ramp now; the ring count itself is
            # derived from the target spacing in terrain_mesh
            n_r = int(q.get("n_r", ["300"])[0])
            snap_m = float(q.get("snap_m", ["150"])[0])
            base = ROOT / "data"
            header, payload = panorama.terrain_mesh(
                dem_path=base / "interim" / slug / "dem_5070.tif",
                hillshade_path=base / "derived" / slug / "hillshade.tif",
                landcover_path=base / "interim" / slug / "landcover_5070.tif",
                peaks_path=base / "raw" / "osm" / f"{slug}_peaks.geojson",
                lat=lat, lon=lon, range_km=rng, n_az=n_az, n_r=n_r,
                snap_radius_m=snap_m,
            )
            head = json.dumps(header).encode()
            body = struct.pack("<I", len(head)) + head + payload
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            msg = json.dumps({"error": str(exc)}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def _api_snap(self, q: dict):
        """Just the snapped viewpoint — nothing else.

        Exists so the client can start fetching imagery *while* the mesh is
        still building. Textures have to be centred on the snapped point, not
        the clicked one, or they sit offset from the terrain; that dependency
        used to serialise the two stages. With the DEM cached this answers in
        milliseconds, so the two now overlap and the load costs
        max(mesh, textures) instead of their sum.
        """
        try:
            import panorama
            lat = float(q.get("lat", ["nan"])[0])
            lon = float(q.get("lon", ["nan"])[0])
            slug = q.get("slug", ["alpine-lakes"])[0]
            snap_m = float(q.get("snap_m", ["150"])[0])
            dem_path = ROOT / "data" / "interim" / slug / "dem_5070.tif"
            dem, crs, transform, _bounds, nodata = panorama._read_full(dem_path)
            snap = (panorama.snap_to_local_max(dem, transform, crs, lat, lon,
                                               nodata, snap_m)
                    if snap_m > 0 else {"snapped": False, "lat": lat, "lon": lon})
            body = json.dumps({"lat": snap["lat"], "lon": snap["lon"],
                               "snapped": snap["snapped"]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:  # noqa: BLE001
            body = json.dumps({"error": str(exc)}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def _api_texture(self, q: dict):
        """One LOD level of the panorama's world-space imagery texture."""
        try:
            import panorama
            lat = float(q.get("lat", ["nan"])[0])
            lon = float(q.get("lon", ["nan"])[0])
            slug = q.get("slug", ["alpine-lakes"])[0]
            half_m = float(q.get("half_m", ["4000"])[0])
            size = min(4096, max(256, int(q.get("size", ["2048"])[0])))
            base = ROOT / "data"
            interim = base / "interim" / slug
            # priority order: the sharpest source that covers each pixel wins
            sources = [interim / "imagery_naip_5070.tif",
                       interim / "imagery_s2_5070.tif"]
            # Rendered inline, on the server's own thread.
            #
            # A ProcessPoolExecutor was tried here on the theory that the GIL
            # was capping concurrency: four levels fetched together gave only
            # 2.05x against an ideal 4x. Processes moved that to just 2.27x on
            # a 32-core machine and made end-to-end load *worse* (8.5 -> 10.7 s)
            # once the cost of pickling several MB of JPEG back was counted.
            # The ceiling is disk throughput on the imagery mosaics, not CPU,
            # so the pool bought nothing and cost complexity.
            # no hillshade: the imagery already carries real illumination, and
            # ours points 159 deg away from it (see texture_patch)
            jpeg, used = panorama.texture_patch(
                sources, lat, lon, half_m, size)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpeg)))
            self.send_header("X-Imagery-Sources", ",".join(used) or "none")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(jpeg)
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            msg = json.dumps({"error": str(exc)}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def end_headers(self):
        path = urlparse(self.path).path
        # Only ever cache successful responses. Caching a 404 — which happens
        # readily if the browser polls while a layer is being regenerated —
        # pins the miss for a full day and the layer silently never appears.
        ok = getattr(self, "_status", 200) in (200, 206, 304)
        cacheable = ok and ("/data/web/" in path
                            and ("/layers/" in path or "/vectors/" in path))
        if cacheable:
            self.send_header("Cache-Control", f"public, max-age={TILE_MAX_AGE}")
        elif not path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def log_message(self, fmt, *args):
        msg = fmt % args
        if " 200 " in msg or " 302 " in msg or " 304 " in msg:
            return  # only surface problems
        sys.stderr.write(f"{self.address_string()} {msg}\n")


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    handler = partial(Handler, directory=str(ROOT))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"serving {ROOT} at http://127.0.0.1:{port}/  (Ctrl-C to stop)",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
