"""데이터레이크 윈도우 재생 성능 측정.

측정 항목:
  - ingest: 합성 데이터 생성+분할+기록 시간, 청크 총 용량
  - cold seek: 캐시에 없는 청크로 점프 시 첫 프레임까지(=청크 GET+디코드) 지연
  - warm seek: 활성 청크 내부 lookup 지연(순수 조회)
  - continuous: 전체 구간 forward 재생 시 프리페치 ON/OFF 비교
      stall = 재생 중 동기 청크 로드(sync_loads) 횟수, cache hit rate

의존성 없이 file:// + CSV로 동작. 직접 실행:
  python -m gist.netai.time_travel_summarization.tests.lake_benchmark
"""

import datetime
import statistics
import sys
import tempfile
import time
from pathlib import Path

from gist.netai.time_travel_summarization.playback.lake_common import ingest_synthetic, manifest_uri
from gist.netai.time_travel_summarization.playback.lake_repository import LakeTrajectoryRepository

HZ = 5.0
CHUNK_SECONDS = 60


def _dir_bytes(dataset_uri: str) -> int:
    path = Path(dataset_uri.replace("file://", ""))
    return sum(f.stat().st_size for f in path.glob("*") if f.is_file())


def _percentile(xs, q):
    xs = sorted(xs)
    if not xs:
        return 0.0
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def _cold_seek(dataset_uri, n_chunks):
    """프리페치 끈 상태에서 각 청크 첫 방문(=동기 로드) 지연 측정."""
    lake = LakeTrajectoryRepository(cache_chunks=n_chunks + 2, prefetch_ahead=0)
    lake.load_from_uri(dataset_uri)
    lake._stop_prefetch()  # 순수 cold 비용만 측정
    start = lake.data_start_time
    times = []
    for i in range(n_chunks):
        t = start + datetime.timedelta(seconds=i * CHUNK_SECONDS + 0.5)
        t0 = time.perf_counter()
        lake.get_data_at_time(t)
        times.append((time.perf_counter() - t0) * 1e6)
    lake.clear()
    return times[1:]  # 첫 청크는 load 시 이미 활성


def _warm_seek(dataset_uri, n_queries=5000):
    """활성 청크 내부 반복 조회(순수 lookup) 지연."""
    lake = LakeTrajectoryRepository(cache_chunks=4, prefetch_ahead=1)
    lake.load_from_uri(dataset_uri)
    lake.set_lookup_mode("bisect")
    start = lake.data_start_time
    lake.get_data_at_time(start)  # 청크0 활성화
    times = []
    for i in range(n_queries):
        t = start + datetime.timedelta(seconds=(i % int(CHUNK_SECONDS * HZ)) / HZ)
        t0 = time.perf_counter()
        lake.get_data_at_time(t)
        times.append((time.perf_counter() - t0) * 1e6)
    lake.clear()
    return times


def _continuous(dataset_uri, n_chunks, prefetch: bool):
    """전체 구간 forward 재생. prefetch ON/OFF 비교."""
    if prefetch:
        lake = LakeTrajectoryRepository(cache_chunks=6, prefetch_ahead=2)
    else:
        lake = LakeTrajectoryRepository(cache_chunks=1, prefetch_ahead=0)
    lake.load_from_uri(dataset_uri)
    if not prefetch:
        lake._stop_prefetch()
    lake.set_lookup_mode("bisect")
    start = lake.data_start_time
    total_s = n_chunks * CHUNK_SECONDS
    step_s = 0.5  # 0.5초 data-time씩 전진
    n_steps = int(total_s / step_s)
    t0 = time.perf_counter()
    for i in range(n_steps):
        lake.get_data_at_time(start + datetime.timedelta(seconds=i * step_s))
        if prefetch:
            time.sleep(0.0008)  # 프레임 간 wall-clock 간격(프리페치 워커가 따라잡을 시간)
    wall = time.perf_counter() - t0
    s = dict(lake.stats)
    hit_rate = s["cache_hits"] / max(1, s["cache_hits"] + s["cache_misses"])
    lake.clear()
    # stall = 첫 청크 이후 발생한 동기 로드
    return {"sync_loads": s["sync_loads"], "stalls": max(0, s["sync_loads"] - 1),
            "prefetch_loads": s["prefetch_loads"], "hit_rate": hit_rate, "wall_s": wall}


def run(scales):
    rows_md = []
    for n_objects, duration_s in scales:
        with tempfile.TemporaryDirectory() as d:
            dataset_uri = (Path(d) / f"ds_{n_objects}_{int(duration_s)}").resolve().as_uri()
            t0 = time.perf_counter()
            manifest = ingest_synthetic(dataset_uri, n_objects=n_objects, duration_s=duration_s,
                                        hz=HZ, chunk_seconds=CHUNK_SECONDS, fmt="csv")
            ingest_s = time.perf_counter() - t0
            n_chunks = len(manifest["chunks"])
            size_mb = _dir_bytes(dataset_uri) / 1e6

            cold = _cold_seek(dataset_uri, n_chunks)
            warm = _warm_seek(dataset_uri)
            on = _continuous(dataset_uri, n_chunks, prefetch=True)
            off = _continuous(dataset_uri, n_chunks, prefetch=False)

            label = f"{n_objects}obj×{int(duration_s)}s"
            rows_md.append({
                "scale": label, "rows": manifest["rows"], "chunks": n_chunks, "size_mb": size_mb,
                "ingest_s": ingest_s,
                "cold_mean_us": statistics.mean(cold) if cold else 0.0,
                "cold_p95_us": _percentile(cold, 0.95),
                "warm_mean_us": statistics.mean(warm),
                "on_stalls": on["stalls"], "on_hit": on["hit_rate"], "on_prefetch": on["prefetch_loads"],
                "off_stalls": off["stalls"], "off_hit": off["hit_rate"],
            })
            print(f"  done {label}: rows={manifest['rows']} chunks={n_chunks} "
                  f"cold~{statistics.mean(cold) if cold else 0:.0f}us warm~{statistics.mean(warm):.2f}us "
                  f"stalls ON={on['stalls']} OFF={off['stalls']}")
    return rows_md


def format_table(rows):
    lines = [
        "| Scale | Rows | Chunks | Size(MB) | Ingest(s) | Cold seek mean/p95 (μs) | Warm seek (μs) | Stalls ON/OFF | Hit ON/OFF |",
        "|-------|------|--------|----------|-----------|-------------------------|----------------|---------------|------------|",
    ]
    for r in rows:
        lines.append(
            f"| {r['scale']} | {r['rows']} | {r['chunks']} | {r['size_mb']:.2f} | {r['ingest_s']:.2f} | "
            f"{r['cold_mean_us']:.0f} / {r['cold_p95_us']:.0f} | {r['warm_mean_us']:.3f} | "
            f"{r['on_stalls']} / {r['off_stalls']} | {r['on_hit']*100:.0f}% / {r['off_hit']*100:.0f}% |"
        )
    return "\n".join(lines)


def main(argv=None):
    # 기본 스케일(빠름). 큰 스케일은 인자로: e.g. "100x600 10x3600"
    scales = [(10, 300), (100, 300), (10, 3600)]
    if argv:
        scales = []
        for tok in argv:
            o, s = tok.lower().split("x")
            scales.append((int(o), int(s)))
    print(f"[lake_benchmark] scales={scales} hz={HZ} chunk_seconds={CHUNK_SECONDS}")
    rows = run(scales)
    table = format_table(rows)
    print("\n" + table)
    out = Path(__file__).resolve().parent.parent / "data" / "lake_benchmark.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"# Lake windowed playback benchmark\n\nhz={HZ}, chunk_seconds={CHUNK_SECONDS}\n\n{table}\n", encoding="utf-8")
    print(f"\n[lake_benchmark] saved {out}")


if __name__ == "__main__":
    main(sys.argv[1:])
