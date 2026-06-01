#!/usr/bin/env python3
"""Generate a 5x5 trajectory CSV for realtime overlay projection calibration.

The output schema matches ``living_trajectory_1min_0.2s.csv``:
timestamp,objid,x,y,z
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta
from pathlib import Path


X_RANGE = (206.0, 1554.0)
Z_RANGE = (-2879.0, -1258.0)
GROUND_Y = 89.5
START_TIME = datetime(2025, 1, 1, 0, 0, 0)
DEFAULT_OUTPUT = (
    "gist/netai/time_travel_summarization/artifacts/trajectory/"
    "overlay_calibration_grid_5x5.csv"
)


def _linspace(lo: float, hi: float, count: int) -> list[float]:
    if count <= 1:
        return [(lo + hi) * 0.5]
    step = (hi - lo) / float(count - 1)
    return [lo + step * idx for idx in range(count)]


def _timestamp(step: int, interval_s: float) -> str:
    dt = START_TIME + timedelta(seconds=step * interval_s)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def generate_rows(
    *,
    grid_size: int,
    duration_s: float,
    interval_s: float,
    y: float,
    x_range: tuple[float, float],
    z_range: tuple[float, float],
):
    xs = _linspace(x_range[0], x_range[1], grid_size)
    zs = _linspace(z_range[0], z_range[1], grid_size)
    points = []
    for z_idx, z in enumerate(zs):
        for x_idx, x in enumerate(xs):
            obj_num = z_idx * grid_size + x_idx + 1
            points.append((f"obj{obj_num:03d}", x, y, z))

    steps = int(round(duration_s / interval_s)) + 1
    for step in range(steps):
        ts = _timestamp(step, interval_s)
        for objid, x, y_value, z in points:
            yield {
                "timestamp": ts,
                "objid": objid,
                "x": f"{x:.1f}",
                "y": f"{y_value:.1f}",
                "z": f"{z:.1f}",
            }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--grid-size", type=int, default=5)
    parser.add_argument("--duration-seconds", type=float, default=10.0)
    parser.add_argument("--interval", type=float, default=0.2)
    parser.add_argument("--y", type=float, default=GROUND_Y)
    parser.add_argument("--x-min", type=float, default=X_RANGE[0])
    parser.add_argument("--x-max", type=float, default=X_RANGE[1])
    parser.add_argument("--z-min", type=float, default=Z_RANGE[0])
    parser.add_argument("--z-max", type=float, default=Z_RANGE[1])
    args = parser.parse_args()

    if args.grid_size < 2:
        raise SystemExit("--grid-size must be >= 2")
    if args.duration_seconds < 0.0:
        raise SystemExit("--duration-seconds must be >= 0")
    if args.interval <= 0.0:
        raise SystemExit("--interval must be > 0")
    if args.x_min >= args.x_max:
        raise SystemExit("--x-min must be less than --x-max")
    if args.z_min >= args.z_max:
        raise SystemExit("--z-min must be less than --z-max")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    rows = 0
    with output.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=("timestamp", "objid", "x", "y", "z"))
        writer.writeheader()
        for row in generate_rows(
            grid_size=args.grid_size,
            duration_s=args.duration_seconds,
            interval_s=args.interval,
            y=args.y,
            x_range=(args.x_min, args.x_max),
            z_range=(args.z_min, args.z_max),
        ):
            writer.writerow(row)
            rows += 1

    print(f"[generate] output={output}")
    print(f"[generate] rows={rows} objects={args.grid_size * args.grid_size} interval={args.interval}s")
    print(f"[generate] range x=({args.x_min}, {args.x_max}) y={args.y} z=({args.z_min}, {args.z_max})")


if __name__ == "__main__":
    main()
