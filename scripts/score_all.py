"""Score every peak, pass and trail in an AOI's core with the view score V.

    python scripts/score_all.py [aoi-slug] [--force] [--limit N]

Same records as pressing V on a feature in the viewer (featurescore.py), for
everything at once: peaks and passes inside the core box, and every trail
with at least one way inside it, scored once per trail rather than once per
OSM way. Already-stored scores under the current version are skipped unless
--force, so an interrupted run resumes where it stopped.

Work is done in chunks of about CHUNK_POINTS sample points. After each chunk
the records go into the store and a status file is rewritten
(data/derived/<aoi>/score_all.json), which the viewer's ranked list polls to
show progress and pick up new scores as they land.

Why no culling or adaptive sampling: on the GPU (vscore_gpu.py) a point costs
~1.2 ms, so every trail at the full 50 m spacing is a couple of minutes.
Screening trails first, or sampling coarsely where V looks flat, would save
seconds and risk missing exactly the scenic stretches the ranking is for.
Without a GPU the same run falls back to the CPU pool (~240 ms a point),
where those trade-offs would be worth revisiting.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import featurescore as fs

CHUNK_POINTS = 8000


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def write_status(path: Path, status: dict) -> None:
    status["updated"] = time.time()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(status), encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    slug = args[0] if args else "alpine-lakes"
    force = "--force" in sys.argv
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    status_path = fs.ROOT / "data" / "derived" / slug / "score_all.json"
    t0 = time.time()
    status = {"state": "planning", "started": t0, "version": fs.version()}
    write_status(status_path, status)

    todo = fs.plan(slug)
    n_planned = len(todo)
    if not force:
        have = set(fs.summaries(slug)["ids"])
        todo = [(t, f) for t, f in todo if f not in have]
    if limit:
        todo = todo[:limit]
    counts = {}
    for t, _f in todo:
        counts[t] = counts.get(t, 0) + 1
    log(f"{n_planned} items in the core, {len(todo)} to score: {counts}")

    # Build every item first: sampling and summit snapping are CPU work, and
    # knowing every item's point count lets the chunks be sized evenly.
    status.update(state="sampling", items_total=len(todo), items_done=0)
    write_status(status_path, status)
    items = []
    for i, (theme, fid) in enumerate(todo):
        try:
            items.append(fs.build_item(slug, theme, fid))
        except Exception as exc:  # noqa: BLE001
            log(f"skip {theme} {fid}: {exc}")
        if (i + 1) % 1000 == 0:
            log(f"sampled {i + 1}/{len(todo)}")
    n_points = sum(len(it["points"]) for it in items)
    log(f"{len(items)} items, {n_points} sample points, sampled in {time.time() - t0:.0f}s")

    status.update(state="scoring", items_total=len(items), points_total=n_points,
                  points_done=0, items_done=0)
    write_status(status_path, status)
    t_score = time.time()
    done_items = done_points = 0
    chunk: list[dict] = []

    def flush(chunk_items):
        nonlocal done_items, done_points
        pts = [p for it in chunk_items for p in it["points"]]
        tc = time.time()
        vals = fs.score_points(slug, pts)
        per = (time.time() - tc) / max(1, len(pts))
        records, k = [], 0
        for it in chunk_items:
            n = len(it["points"])
            records.append(fs.finish_record(it, vals[k:k + n], per * n))
            k += n
        fs.save_many(slug, records)
        done_items += len(chunk_items)
        done_points += len(pts)
        rate = done_points / max(1e-6, time.time() - t_score)
        status.update(items_done=done_items, points_done=done_points,
                      eta_s=round((n_points - done_points) / max(rate, 1e-6)))
        write_status(status_path, status)
        log(f"{done_items}/{len(items)} items, {done_points}/{n_points} points, "
            f"{per * 1000:.2f} ms/point, eta {status['eta_s']}s")

    size = 0
    for it in items:
        chunk.append(it)
        size += len(it["points"])
        if size >= CHUNK_POINTS:
            flush(chunk)
            chunk, size = [], 0
    if chunk:
        flush(chunk)

    status.update(state="done", finished=time.time(),
                  elapsed_s=round(time.time() - t0))
    write_status(status_path, status)
    log(f"done: {done_items} items in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
