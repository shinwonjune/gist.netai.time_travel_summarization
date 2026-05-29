#!/usr/bin/env python3
"""데이터레이크 적재 CLI: 합성/CSV 궤적 -> 시간 분할 청크 + manifest -> minIO(또는 file://).

예)
  # 합성 데이터 10객체 × 1시간, 60초 청크, CSV로 로컬에 적재
  python tools/lake_ingest.py --dest /tmp/lake/ds1 --objects 10 --duration 1h

  # 기존 CSV를 Parquet 청크로 minIO에 적재 (pyarrow + .env의 MINIO_* 필요)
  python tools/lake_ingest.py --source data/living_trajectory_1min_0.2s.csv \
      --dest s3://mybucket/trajectory/living --format parquet --chunk-seconds 60

config.json 에 아래를 넣으면 익스텐션이 윈도우 로딩으로 재생한다:
  "lake": {"enabled": true, "manifest_uri": "<dest>/manifest.json",
           "cache_chunks": 4, "prefetch_ahead": 2}
"""

import argparse
import csv
import sys
import time
from pathlib import Path

_EXT_ROOT = Path(__file__).resolve().parents[1]
if str(_EXT_ROOT) not in sys.path:
    sys.path.insert(0, str(_EXT_ROOT))

from gist.netai.time_travel_summarization.playback.lake_common import (  # noqa: E402
    generate_synthetic_rows,
    ingest_rows,
    manifest_uri,
)


def _parse_duration(text: str) -> float:
    """'60' | '30s' | '1min' | '2h' | '12h' -> 초(float)."""
    t = text.strip().lower()
    for suffix, mult in (("min", 60.0), ("h", 3600.0), ("s", 1.0)):
        if t.endswith(suffix):
            return float(t[: -len(suffix)]) * mult
    return float(t)


def _to_uri(dest: str) -> str:
    if "://" in dest:
        return dest
    return Path(dest).resolve().as_uri()


def _read_csv_rows(path: str):
    with open(path, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            yield row


def main(argv=None):
    p = argparse.ArgumentParser(description="Ingest trajectory data into a time-partitioned data lake.")
    p.add_argument("--dest", required=True, help="dataset URI/경로 (s3://bucket/ds | file:///tmp/ds | /tmp/ds)")
    p.add_argument("--source", default="synthetic", help="'synthetic'(기본) 또는 CSV 경로")
    p.add_argument("--objects", type=int, default=10, help="합성 객체 수")
    p.add_argument("--duration", default="60s", help="합성 길이 (예: 60s, 1min, 1h, 12h)")
    p.add_argument("--hz", type=float, default=5.0, help="샘플레이트 (기본 5Hz = 0.2s)")
    p.add_argument("--chunk-seconds", type=int, default=60, help="청크 시간 단위(초)")
    p.add_argument("--format", choices=("csv", "parquet"), default="csv", help="청크 포맷")
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args(argv)

    dataset_uri = _to_uri(args.dest)
    t0 = time.perf_counter()
    if args.source == "synthetic":
        duration_s = _parse_duration(args.duration)
        rows = generate_synthetic_rows(args.objects, duration_s, hz=args.hz, seed=args.seed)
        manifest = ingest_rows(rows, dataset_uri, chunk_seconds=args.chunk_seconds, fmt=args.format, hz=args.hz, dataset="synthetic")
    else:
        rows = _read_csv_rows(args.source)
        manifest = ingest_rows(rows, dataset_uri, chunk_seconds=args.chunk_seconds, fmt=args.format, hz=args.hz, dataset=Path(args.source).stem)
    elapsed = time.perf_counter() - t0

    print(f"[ingest] dataset_uri = {dataset_uri}")
    print(f"[ingest] manifest    = {manifest_uri(dataset_uri)}")
    print(f"[ingest] format={manifest['format']} chunk_seconds={manifest['chunk_seconds']} "
          f"objids={len(manifest['objids'])} rows={manifest['rows']} chunks={len(manifest['chunks'])}")
    print(f"[ingest] time span   = {manifest['start']} .. {manifest['end']}")
    print(f"[ingest] elapsed     = {elapsed:.2f}s")
    print("\nconfig.json 추가 예시:")
    print('  "lake": {"enabled": true, "manifest_uri": "%s", "cache_chunks": 4, "prefetch_ahead": 2}'
          % manifest_uri(dataset_uri))


if __name__ == "__main__":
    main()
