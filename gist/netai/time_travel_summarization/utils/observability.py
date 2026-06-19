"""Sampling-rate observability analysis for object-object collisions.

A collision is only *detectable* if it actually appears in a rendered frame. A
frame is a snapshot of object positions at the content sampling instants
{t0 + k/f}. If the true overlap window (objects within contact distance) contains
no sampling instant, the collision is invisible at rate f — no model can recover
it. This script measures that ceiling.

Method (trajectory mode, rigorous):
  1. Read a HIGH-RATE position trace (TraceRecorder output, ~30Hz) as the
     near-continuous reference truth.
  2. For each object pair, reconstruct overlap intervals using the SAME contact
     rule the simulator uses: horizontal center distance < collision_distance
     (physics/wander_controller.py:330-332, collision_distance = 2.2*radius @
     app/facade.py:665). Each interval is one true collision event of duration tau.
  3. For each candidate content rate f, an event is "observable" iff a rate-f
     grid point falls inside its window -> observable fraction = recall ceiling.
  4. Report observability(f), the tau distribution, and the analytic phase-average
     min(1, tau*f) as a cross-check.

Fallback mode (--collisions-only): when no trajectory is available, treat each
recorded collision as an instant and sweep assumed windows W against each rate.

Usage:
  python -m utils.observability --trajectory trace.csv --meta video.meta.json \
      --rates 5 10 15 30 --collisions collisions.csv
  python -m utils.observability --collisions-only --collisions collisions.csv \
      --rates 5 10 --windows 50 100 150 200
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import math
import statistics
from itertools import combinations
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# --------------------------------------------------------------------------- #
# parsing (mirrors playback/trajectory_repository.py:174-189)
# --------------------------------------------------------------------------- #
def parse_ts(s: str) -> datetime.datetime:
    s = (s or "").strip().replace("Z", "+00:00")
    for p in (
        datetime.datetime.fromisoformat,
        lambda v: datetime.datetime.strptime(v, "%Y-%m-%d %H:%M:%S.%f"),
        lambda v: datetime.datetime.strptime(v, "%Y-%m-%d %H:%M:%S"),
    ):
        try:
            return p(s)
        except ValueError:
            continue
    raise ValueError(f"unparseable timestamp: {s!r}")


def load_trajectory(path: Path) -> Tuple[List[datetime.datetime], Dict]:
    """Return (sorted_times, {time: {objid: (x, y, z)}})."""
    frames: Dict[datetime.datetime, Dict[str, Tuple[float, float, float]]] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            dt = parse_ts(row["timestamp"])
            frames.setdefault(dt, {})[row["objid"]] = (
                float(row["x"]), float(row["y"]), float(row["z"])
            )
    return sorted(frames), frames


def median_dt(times: List[datetime.datetime]) -> float:
    diffs = [(times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1)]
    return statistics.median(diffs) if diffs else 0.0


# --------------------------------------------------------------------------- #
# trajectory-mode observability
# --------------------------------------------------------------------------- #
def contact_events(
    times: List[datetime.datetime], frames: Dict, d_contact: float, axes: Tuple[int, int]
) -> List[Tuple[Tuple[str, str], datetime.datetime, datetime.datetime]]:
    """Maximal runs where horizontal distance < d_contact -> (pair, t_start, t_last)."""
    i, j = axes
    d2 = d_contact * d_contact
    objids = sorted({o for fr in frames.values() for o in fr})
    events = []
    for a, b in combinations(objids, 2):
        run_start = run_last = None
        for t in times:
            fr = frames[t]
            contact = False
            if a in fr and b in fr:
                pa, pb = fr[a], fr[b]
                contact = (pa[i] - pb[i]) ** 2 + (pa[j] - pb[j]) ** 2 < d2
            if contact:
                if run_start is None:
                    run_start = t
                run_last = t
            elif run_start is not None:
                events.append(((a, b), run_start, run_last))
                run_start = run_last = None
        if run_start is not None:
            events.append(((a, b), run_start, run_last))
    return events


def grid_hits_window(start_s: float, end_s: float, f: float) -> bool:
    """Does any grid point k/f (k integer) fall in [start_s, end_s)?"""
    if end_s <= start_s:
        return False
    klo = math.ceil(start_s * f - 1e-9)
    return klo < end_s * f - 1e-12 or abs(klo - start_s * f) < 1e-9


def analyze_trajectory(path: Path, rates: List[float], d_contact: float,
                       axes: Tuple[int, int]) -> Dict:
    times, frames = load_trajectory(path)
    if len(times) < 2:
        raise SystemExit(f"trajectory {path} has too few frames")
    ref_dt = median_dt(times)
    ref_hz = 1.0 / ref_dt if ref_dt else 0.0
    t0 = times[0]
    events = contact_events(times, frames, d_contact, axes)

    # effective window = [start, last + ref_dt): a True sample covers one ref frame.
    spans = [((s - t0).total_seconds(), (e - t0).total_seconds() + ref_dt) for _, s, e in events]
    taus = [end - st for st, end in spans]

    per_rate = {}
    for f in rates:
        obs = sum(grid_hits_window(st, end, f) for st, end in spans)
        frac = obs / len(spans) if spans else 0.0
        expected = (sum(min(1.0, tau * f) for tau in taus) / len(taus)) if taus else 0.0
        per_rate[f] = {
            "observable_fraction": round(frac, 4),
            "expected_phaseavg": round(expected, 4),
            "observable_events": obs,
        }

    taus_ms = sorted(t * 1000 for t in taus)
    tau_stats = {}
    if taus_ms:
        tau_stats = {
            "median_ms": round(statistics.median(taus_ms), 1),
            "p25_ms": round(taus_ms[len(taus_ms) // 4], 1),
            "p75_ms": round(taus_ms[(3 * len(taus_ms)) // 4], 1),
            "frac_under_100ms": round(sum(t < 100 for t in taus_ms) / len(taus_ms), 4),
            "frac_under_200ms": round(sum(t < 200 for t in taus_ms) / len(taus_ms), 4),
        }
    return {
        "mode": "trajectory",
        "trajectory": str(path),
        "reference_hz": round(ref_hz, 2),
        "collision_distance": d_contact,
        "axes": axes,
        "n_events": len(events),
        "tau_stats": tau_stats,
        "per_rate": per_rate,
    }


# --------------------------------------------------------------------------- #
# fallback: collisions-only window sweep
# --------------------------------------------------------------------------- #
def load_collision_instants(path: Path, kinds=("object",)) -> List[datetime.datetime]:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if kinds and r.get("kind") not in kinds:
                continue
            rows.append(parse_ts(r["timestamp"]))
    rows.sort()
    # object collisions emit 2 rows at the same instant -> collapse near-duplicates.
    events = []
    for t in rows:
        if events and (t - events[-1]).total_seconds() <= 0.05:
            continue
        events.append(t)
    return events


def analyze_collisions_only(path: Path, rates: List[float], windows_ms: List[float]) -> Dict:
    events = load_collision_instants(path)
    if not events:
        raise SystemExit(f"no object collisions in {path}")
    t0 = events[0]
    offs = [(t - t0).total_seconds() for t in events]
    grid = {}
    for w in windows_ms:
        half = (w / 1000.0) / 2.0
        row = {}
        for f in rates:
            obs = sum(grid_hits_window(o - half, o + half, f) for o in offs)
            row[f] = round(obs / len(offs), 4)
        grid[w] = row
    return {
        "mode": "collisions_only",
        "collisions": str(path),
        "n_events": len(events),
        "windows_ms": windows_ms,
        "rates": rates,
        "observable_fraction": grid,  # {window_ms: {rate: frac}}
    }


# --------------------------------------------------------------------------- #
def _print_trajectory(res: Dict) -> None:
    print("=" * 72)
    print(f"Observability (trajectory)  ref={res['reference_hz']}Hz  "
          f"events={res['n_events']}  D_contact={res['collision_distance']}")
    if res["tau_stats"]:
        s = res["tau_stats"]
        print(f"overlap tau: median={s['median_ms']}ms  p25={s['p25_ms']}  p75={s['p75_ms']}  "
              f"<100ms={s['frac_under_100ms']*100:.0f}%  <200ms={s['frac_under_200ms']*100:.0f}%")
    print("-" * 72)
    print(f"{'rate(Hz)':>9}  {'observable':>11}  {'expected min(1,tau*f)':>22}")
    for f, r in res["per_rate"].items():
        print(f"{f:>9g}  {r['observable_fraction']*100:>10.1f}%  {r['expected_phaseavg']*100:>21.1f}%")
    print("=" * 72)


def _print_collisions_only(res: Dict) -> None:
    print("=" * 72)
    print(f"Observability (collisions-only, assumed windows)  events={res['n_events']}")
    print("-" * 72)
    header = "window\\rate " + "  ".join(f"{f:>7g}Hz" for f in res["rates"])
    print(header)
    for w in res["windows_ms"]:
        cells = "  ".join(f"{res['observable_fraction'][w][f]*100:>7.1f}%" for f in res["rates"])
        print(f"{w:>7g}ms   {cells}")
    print("=" * 72)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trajectory", type=Path, help="high-rate position trace CSV (timestamp,objid,x,y,z)")
    ap.add_argument("--collisions", type=Path, help="collisions CSV (fallback source / cross-check)")
    ap.add_argument("--collisions-only", action="store_true", help="use collisions CSV + assumed windows")
    ap.add_argument("--meta", type=Path, help="capture sidecar; reads collision_distance if present")
    ap.add_argument("--collision-distance", type=float, default=None, help="contact distance in world units")
    ap.add_argument("--rates", type=float, nargs="+", default=[5, 10, 15, 30])
    ap.add_argument("--windows", type=float, nargs="+", default=[50, 100, 150, 200],
                    help="assumed overlap windows in ms (collisions-only mode)")
    ap.add_argument("--axes", choices=["xz", "xy"], default="xz",
                    help="horizontal plane axes (xz for Y-up sim default)")
    ap.add_argument("--out", type=Path, help="write result JSON here")
    args = ap.parse_args()

    if args.collisions_only:
        if not args.collisions:
            ap.error("--collisions-only requires --collisions")
        res = analyze_collisions_only(args.collisions, args.rates, args.windows)
        _print_collisions_only(res)
    else:
        if not args.trajectory:
            ap.error("trajectory mode requires --trajectory (or use --collisions-only)")
        d = args.collision_distance
        if d is None and args.meta and args.meta.exists():
            d = json.loads(args.meta.read_text(encoding="utf-8")).get("collision_distance")
        if d is None:
            ap.error("need --collision-distance or a --meta with collision_distance")
        axes = (0, 2) if args.axes == "xz" else (0, 1)
        res = analyze_trajectory(args.trajectory, args.rates, float(d), axes)
        _print_trajectory(res)
        if args.collisions and args.collisions.exists():
            n_rec = len(load_collision_instants(args.collisions))
            print(f"cross-check: {n_rec} recorded object-collision events vs "
                  f"{res['n_events']} reconstructed (offline contact rule).")

    if args.out:
        args.out.write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
