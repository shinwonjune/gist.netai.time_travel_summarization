#!/usr/bin/env python3
"""Generate trajectory CSVs within the existing living-room coordinate range.

The output schema matches ``living_trajectory_1min_0.2s.csv``:
timestamp,objid,x,y,z
"""

from __future__ import annotations

import argparse
import csv
import math
import random
from datetime import datetime, timedelta
from pathlib import Path


X_RANGE = (206.0, 1554.0)
Y_RANGE = (89.5, 200.0)
Z_RANGE = (-2879.0, -1258.0)
START_TIME = datetime(2025, 1, 1, 0, 0, 0)


def _unit_direction(rng: random.Random) -> list[float]:
    theta = rng.uniform(0.0, 2.0 * math.pi)
    phi = rng.uniform(-math.pi / 6.0, math.pi / 6.0)
    x = math.cos(theta) * math.cos(phi)
    y = math.sin(phi)
    z = math.sin(theta) * math.cos(phi)
    norm = math.sqrt(x * x + y * y + z * z)
    return [x / norm, y / norm, z / norm]


def _normalize(v: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in v))
    if norm == 0:
        return [1.0, 0.0, 0.0]
    return [x / norm for x in v]


def _clip(value: float, bounds: tuple[float, float]) -> float:
    return min(bounds[1], max(bounds[0], value))


def _timestamp(step: int, interval_s: float) -> str:
    dt = START_TIME + timedelta(seconds=step * interval_s)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def generate_rows(
    *,
    duration_hours: float,
    interval_s: float,
    num_objects: int,
    min_speed: float,
    max_speed: float,
    seed: int,
):
    rng = random.Random(seed)
    total_steps = int(round(duration_hours * 3600.0 / interval_s)) + 1
    objects = []
    for idx in range(num_objects):
        objects.append(
            {
                "id": f"obj{idx + 1:03d}",
                "position": [
                    rng.uniform(*X_RANGE),
                    rng.uniform(*Y_RANGE),
                    rng.uniform(*Z_RANGE),
                ],
                "velocity": _unit_direction(rng),
                "speed": rng.uniform(min_speed, max_speed),
                "direction_change_counter": 0,
                "direction_change_interval": rng.randint(20, 100),
            }
        )

    ranges = (X_RANGE, Y_RANGE, Z_RANGE)
    for step in range(total_steps):
        ts = _timestamp(step, interval_s)
        for obj in objects:
            position = obj["position"]
            yield {
                "timestamp": ts,
                "objid": obj["id"],
                "x": f"{position[0]:.1f}",
                "y": f"{position[1]:.1f}",
                "z": f"{position[2]:.1f}",
            }

            if step >= total_steps - 1:
                continue

            obj["direction_change_counter"] += 1
            if obj["direction_change_counter"] >= obj["direction_change_interval"]:
                target_velocity = _unit_direction(rng)
                obj["velocity"] = _normalize(
                    [
                        0.8 * obj["velocity"][i] + 0.2 * target_velocity[i]
                        for i in range(3)
                    ]
                )
                obj["direction_change_counter"] = 0
                obj["direction_change_interval"] = rng.randint(20, 100)
                obj["speed"] = rng.uniform(min_speed, max_speed)

            next_position = [
                position[i] + obj["velocity"][i] * obj["speed"] * interval_s
                for i in range(3)
            ]
            for axis in range(3):
                lo, hi = ranges[axis]
                if next_position[axis] <= lo or next_position[axis] >= hi:
                    obj["velocity"][axis] *= -1.0
                    next_position[axis] = _clip(next_position[axis], ranges[axis])

            noisy_velocity = [
                obj["velocity"][i] + rng.gauss(0.0, 0.02)
                for i in range(3)
            ]
            obj["velocity"] = _normalize(noisy_velocity)
            obj["position"] = next_position


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-hours", type=float, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--objects", type=int, default=4)
    parser.add_argument("--interval", type=float, default=0.2)
    parser.add_argument("--min-speed", type=float, default=150.0)
    parser.add_argument("--max-speed", type=float, default=200.0)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    with output.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=("timestamp", "objid", "x", "y", "z"))
        writer.writeheader()
        for row in generate_rows(
            duration_hours=args.duration_hours,
            interval_s=args.interval,
            num_objects=args.objects,
            min_speed=args.min_speed,
            max_speed=args.max_speed,
            seed=args.seed,
        ):
            writer.writerow(row)
            rows += 1

    print(f"[generate] output={output}")
    print(f"[generate] rows={rows} objects={args.objects} interval={args.interval}s")
    print(f"[generate] range x={X_RANGE} y={Y_RANGE} z={Z_RANGE}")


if __name__ == "__main__":
    main()
