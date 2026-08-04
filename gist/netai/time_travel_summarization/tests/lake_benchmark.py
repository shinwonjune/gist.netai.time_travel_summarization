"""데이터레이크 윈도우 재생 성능 측정.

두 모드:
1) 합성 모드(기본): file:// + CSV 합성 데이터로 ingest/cold/warm/continuous 측정.
   의존성 없이 동작(기존 동작 유지).
2) 데이터셋 모드(--dataset-uri): 실 데이터셋(s3:// 또는 file://)에 대해
   레이크성능_실험설계.md의 A(전송 마이크로벤치)·B(연속 재생 stall) 계층을 측정.

A 계층 (전송):
  - manifest_load_ms / chunk GET p50·p99 — 첫 요청(TLS 핸드셰이크 혼입)은 분리 집계
  - decode_ms_per_MB (csv vs parquet)
  - cold seek p50·p99 (프리페치 OFF, 청크 첫 방문) / warm seek p50·p99 (활성 청크 내부)

B 계층 (연속 재생, wall-clock 페이싱):
  - 시나리오: forward 1x / forward 5x / random seek N회 / backward 1x
  - stall(웜업 제외 sync_loads) / cache_hit_rate / prefetch_lead_s(청크 소진 전
    프리페치 완료 마진 — 음수 임박이면 stall 직전)
  - 웜업(로드 직후·역재생 진입 seek의 첫 동기 로드)은 콜드스타트로 별도 집계
    (판정 규약: 레이크성능_실험설계.md §4)

직접 실행 (EXT_ROOT에서, s3://는 minio 있는 환경 — Windows Kit python):
  python -m gist.netai.time_travel_summarization.tests.lake_benchmark              # 합성
  python -m gist.netai.time_travel_summarization.tests.lake_benchmark \
      --dataset-uri s3://time-travel-summarization/trajectory/aigrad_bev_10hz_30min_v1_c60 \
      --note "office LAN / i7 / 64GB"                                              # 실측
"""

import argparse
import csv
import datetime
import io
import json
import platform
import random
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

from gist.netai.time_travel_summarization.playback.lake_common import (
    dataset_uri_from_manifest,
    ingest_synthetic,
    join_uri,
    manifest_uri,
)
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


# ======================================================================== #
# 합성 모드 (기존 동작 유지)
# ======================================================================== #

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


# ======================================================================== #
# 데이터셋 모드 — A 계층: 전송 마이크로벤치 (repository 미경유, 어댑터 직결)
# ======================================================================== #

def _decode_rows(raw: bytes, ext: str):
    """청크 bytes -> rows. _read_rows의 디코드부만 분리(GET과 분리 계측용)."""
    if ext == "csv":
        return list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))
    import pyarrow.parquet as pq  # parquet일 때만 (선택 의존성)

    # read_table() 대신 ParquetFile.read() — Kit 임베디드 파이썬의 _dataset.pyd
    # 세그폴트 회피(trajectory_repository._read_rows와 동일 사유).
    return pq.ParquetFile(io.BytesIO(raw)).read().to_pylist()


def transport_bench(uri: str, max_chunks: int = 0) -> dict:
    """A 계층: manifest GET + 청크별 GET/디코드 분리 계측.

    첫 청크 GET은 TLS 핸드셰이크·커넥션 수립이 혼입되므로 분리 집계하고,
    p50/p99는 2번째 요청부터 계산한다(레이크성능_실험설계 §0-1).
    """
    from gist.netai.time_travel_summarization.storage import from_uri

    muri = uri if uri.lower().endswith("manifest.json") else manifest_uri(uri)
    dataset_uri = dataset_uri_from_manifest(muri)

    t0 = time.perf_counter()
    adapter = from_uri(muri)
    with adapter.open_read(muri) as fh:
        manifest = json.loads(fh.read().decode("utf-8"))
    manifest_load_ms = (time.perf_counter() - t0) * 1000

    ext = manifest.get("format", "csv")
    chunks = manifest.get("chunks", [])
    if max_chunks > 0:
        chunks = chunks[:max_chunks]

    get_ms, decode_ms, sizes = [], [], []
    for ch in chunks:
        curi = join_uri(dataset_uri, ch["key"])
        t0 = time.perf_counter()
        with from_uri(curi).open_read(curi) as fh:
            raw = fh.read()
        get_ms.append((time.perf_counter() - t0) * 1000)
        sizes.append(len(raw))
        t0 = time.perf_counter()
        _decode_rows(raw, ext)
        decode_ms.append((time.perf_counter() - t0) * 1000)

    rest = get_ms[1:] if len(get_ms) > 1 else get_ms
    total_mb = sum(sizes) / 1e6
    return {
        "format": ext,
        "chunk_seconds": manifest.get("chunk_seconds"),
        "hz": manifest.get("hz"),
        "n_chunks_measured": len(chunks),
        "total_mb": round(total_mb, 3),
        "manifest_load_ms": round(manifest_load_ms, 2),
        "first_get_ms": round(get_ms[0], 2) if get_ms else None,  # TLS 혼입
        "get_p50_ms": round(_percentile(rest, 0.50), 2),
        "get_p99_ms": round(_percentile(rest, 0.99), 2),
        "get_mean_ms": round(statistics.mean(rest), 2) if rest else 0.0,
        "decode_p50_ms": round(_percentile(decode_ms, 0.50), 2),
        "decode_ms_per_mb": round(sum(decode_ms) / max(total_mb, 1e-9), 2),
        "chunk_mean_mb": round(total_mb / max(len(sizes), 1), 3),
    }


def seek_bench(uri: str, max_chunks: int = 0, warm_queries: int = 2000) -> dict:
    """A 계층: cold seek(프리페치 OFF 청크 첫 방문) / warm seek(활성 청크 내부)."""
    lake = LakeTrajectoryRepository(cache_chunks=4096, prefetch_ahead=0)
    lake.load_from_uri(uri)
    lake._stop_prefetch()
    lake.set_lookup_mode("bisect")
    starts = list(lake._chunk_starts)
    if max_chunks > 0:
        starts = starts[:max_chunks]

    cold_ms = []
    for st in starts[1:]:  # 청크0은 load 시 이미 활성
        t = st + datetime.timedelta(seconds=0.5)
        t0 = time.perf_counter()
        lake.get_data_at_time(t)
        cold_ms.append((time.perf_counter() - t0) * 1000)

    # warm: 청크0 내부 반복 조회 (메모리 lookup — 네트워크 무관)
    c0_start = lake._chunk_starts[0]
    c0_end = lake.parse_timestamp(lake._chunks[0]["end"])
    c0_span = max((c0_end - c0_start).total_seconds(), 0.001)
    lake.get_data_at_time(c0_start)
    rng = random.Random(7)
    warm_us = []
    for _ in range(warm_queries):
        t = c0_start + datetime.timedelta(seconds=rng.uniform(0, c0_span))
        t0 = time.perf_counter()
        lake.get_data_at_time(t)
        warm_us.append((time.perf_counter() - t0) * 1e6)
    lake.clear()
    return {
        "cold_seek_p50_ms": round(_percentile(cold_ms, 0.50), 2),
        "cold_seek_p99_ms": round(_percentile(cold_ms, 0.99), 2),
        "cold_seek_n": len(cold_ms),
        "warm_seek_p50_us": round(_percentile(warm_us, 0.50), 1),
        "warm_seek_p99_us": round(_percentile(warm_us, 0.99), 1),
    }


# ======================================================================== #
# 데이터셋 모드 — B 계층: 연속 재생 stall (wall-clock 페이싱)
# ======================================================================== #

class _InstrumentedLake(LakeTrajectoryRepository):
    """청크 로드 완료·활성화 시각을 기록해 prefetch_lead_s를 산출하는 계측 서브클래스.

    prefetch_lead_s = (청크가 활성화된 시각) - (프리페치 로드가 끝난 시각).
    양수 = 소진 전에 준비 완료(마진), 0 근접 = stall 임박. sync 로드로 활성화된
    청크는 lead가 정의상 없고 그 자체가 stall이다.
    """

    def __init__(self, *args, **kwargs):
        self.probe_load_done = {}   # idx -> (wall_ts, src, load_ms)
        self.probe_activate = {}    # idx -> 최초 활성화 wall_ts
        super().__init__(*args, **kwargs)

    def _load_chunk(self, idx):
        t0 = time.perf_counter()
        chunk = super()._load_chunk(idx)
        src = "prefetch" if threading.current_thread().name == "lake-prefetch" else "sync"
        self.probe_load_done[idx] = (time.perf_counter(), src, (time.perf_counter() - t0) * 1000)
        return chunk

    def _activate(self, idx):
        self.probe_activate.setdefault(idx, time.perf_counter())
        super()._activate(idx)


def _sleep_until(deadline: float):
    """Windows sleep 해상도(~15ms) 보정: 굵은 sleep 후 잔여는 spin."""
    while True:
        remain = deadline - time.perf_counter()
        if remain <= 0:
            return
        if remain > 0.004:
            time.sleep(remain - 0.003)


def run_scenario(
    uri: str,
    name: str,
    *,
    speed: float = 1.0,
    lookup_hz: float = 10.0,
    play_wall_s: float = 180.0,
    seeks: int = 0,
    cache_chunks: int = 4,
    prefetch_ahead: int = 2,
    seed: int = 42,
) -> dict:
    """B 계층 시나리오 1회 실행. seeks>0이면 랜덤 seek 모드(연속 재생 대신).

    웜업 규약(설계 §4): load 직후 청크0 동기 로드 + (backward면) 진입 seek의 동기
    로드는 콜드스타트로 별도 집계하고 stall 판정에서 제외한다.
    """
    lake = _InstrumentedLake(cache_chunks=cache_chunks, prefetch_ahead=prefetch_ahead)
    t0 = time.perf_counter()
    lake.load_from_uri(uri)
    load_s = time.perf_counter() - t0
    lake.set_lookup_mode("bisect")
    start, end = lake.data_start_time, lake.data_end_time
    span_s = (end - start).total_seconds()

    # backward는 끝점에서 시작 — 진입 seek도 웜업으로 분류
    if speed < 0:
        lake.get_data_at_time(end)
    warmup_syncs = lake.stats["sync_loads"]
    base = {k: v for k, v in lake.stats.items()}

    seek_ms, seek_cold = [], 0
    lookups = 0
    origin = end if speed < 0 else start
    wall0 = time.perf_counter()

    if seeks > 0:
        rng = random.Random(seed)
        for _ in range(seeks):
            t = start + datetime.timedelta(seconds=rng.uniform(0, span_s))
            before = lake.stats["sync_loads"]
            t1 = time.perf_counter()
            lake.get_data_at_time(t)
            seek_ms.append((time.perf_counter() - t1) * 1000)
            if lake.stats["sync_loads"] > before:
                seek_cold += 1
            lookups += 1
            time.sleep(0.2)  # 사람의 연속 seek 간격 근사
    else:
        period = 1.0 / lookup_hz
        next_tick = wall0
        while True:
            elapsed = time.perf_counter() - wall0
            if elapsed >= play_wall_s:
                break
            twin = origin + datetime.timedelta(seconds=speed * elapsed)
            if twin < start or twin > end:
                break  # 데이터 범위 소진
            lake.get_data_at_time(twin)
            lookups += 1
            next_tick += period
            _sleep_until(next_tick)

    wall_s = time.perf_counter() - wall0
    s = {k: lake.stats[k] - base.get(k, 0) for k in lake.stats}
    stalls = max(0, lake.stats["sync_loads"] - warmup_syncs) - (seek_cold if seeks > 0 else 0)
    # hit_rate 분모 = 청크 활성화 횟수(경계 통과·seek) — 한 번도 없으면 None(N/A)
    n_lookups_chunk = s["cache_hits"] + s["cache_misses"]
    hit_rate = s["cache_hits"] / n_lookups_chunk if n_lookups_chunk else None

    # prefetch_lead: 프리페치로 준비된 청크가 활성화까지 가진 시간 마진
    leads = []
    for idx, wall_act in lake.probe_activate.items():
        done = lake.probe_load_done.get(idx)
        if done and done[1] == "prefetch" and wall_act >= done[0]:
            leads.append(wall_act - done[0])
    stall_load_ms = [v[2] for v in lake.probe_load_done.values() if v[1] == "sync"]
    lake.clear()

    out = {
        "scenario": name,
        "speed": speed,
        "lookup_hz": lookup_hz,
        "wall_s": round(wall_s, 1),
        "load_s": round(load_s, 2),
        "lookups": lookups,
        "warmup_cold_loads": warmup_syncs,
        "stalls": stalls,
        "hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
        "prefetch_loads": s["prefetch_loads"],
        "prefetch_lead_min_s": round(min(leads), 2) if leads else None,
        "prefetch_lead_p50_s": round(_percentile(leads, 0.5), 2) if leads else None,
        "sync_load_p50_ms": round(_percentile(stall_load_ms, 0.5), 1) if stall_load_ms else None,
    }
    if seeks > 0:
        out.update({
            "seek_n": seeks,
            "seek_cold_n": seek_cold,
            "seek_p50_ms": round(_percentile(seek_ms, 0.50), 1),
            "seek_p99_ms": round(_percentile(seek_ms, 0.99), 1),
        })
    return out


SCENARIOS = {
    "1x": dict(speed=1.0),
    "5x": dict(speed=5.0),
    "seek": dict(seeks=10),
    "backward": dict(speed=-1.0),
}


def run_dataset_mode(args) -> dict:
    """--dataset-uri에 대해 A(전송)·B(시나리오) 계층 전체 실행 + 결과 저장."""
    uri = args.dataset_uri
    label = uri.rstrip("/").rsplit("/", 1)[-1]
    print(f"[lake_benchmark] dataset mode: {uri}")
    print(f"[lake_benchmark] env: {platform.platform()} / {platform.processor()}")
    if args.note:
        print(f"[lake_benchmark] note: {args.note}")

    print("[lake_benchmark] A: transport bench ...")
    transport = transport_bench(uri, max_chunks=args.max_chunks)
    print(f"  manifest={transport['manifest_load_ms']}ms first_get={transport['first_get_ms']}ms "
          f"get p50/p99={transport['get_p50_ms']}/{transport['get_p99_ms']}ms "
          f"decode={transport['decode_ms_per_mb']}ms/MB ({transport['n_chunks_measured']} chunks)")

    print("[lake_benchmark] A: cold/warm seek ...")
    seeks = seek_bench(uri, max_chunks=args.max_chunks, warm_queries=args.warm_queries)
    print(f"  cold p50/p99={seeks['cold_seek_p50_ms']}/{seeks['cold_seek_p99_ms']}ms "
          f"warm p50/p99={seeks['warm_seek_p50_us']}/{seeks['warm_seek_p99_us']}us")

    scenario_rows = []
    for key in [s.strip() for s in args.scenarios.split(",") if s.strip()]:
        if key not in SCENARIOS:
            print(f"  skip unknown scenario: {key}")
            continue
        kw = dict(SCENARIOS[key])
        if key == "seek":
            kw["seeks"] = args.seeks
        print(f"[lake_benchmark] B: scenario {key} ...")
        r = run_scenario(
            uri, key,
            lookup_hz=args.lookup_hz,
            play_wall_s=args.play_wall_s,
            cache_chunks=args.cache_chunks,
            prefetch_ahead=args.prefetch_ahead,
            seed=args.seed,
            **kw,
        )
        hit_txt = f"{r['hit_rate']*100:.1f}%" if r["hit_rate"] is not None else "N/A"
        print(f"  stalls={r['stalls']} warmup={r['warmup_cold_loads']} hit={hit_txt} "
              f"lead_min={r['prefetch_lead_min_s']}s lookups={r['lookups']}")
        scenario_rows.append(r)

    result = {
        "dataset_uri": uri,
        "measured_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "machine": f"{platform.platform()} / {platform.processor()}",
        "note": args.note,
        "params": {
            "lookup_hz": args.lookup_hz, "play_wall_s": args.play_wall_s,
            "cache_chunks": args.cache_chunks, "prefetch_ahead": args.prefetch_ahead,
            "seeks": args.seeks, "seed": args.seed, "max_chunks": args.max_chunks,
        },
        "transport": transport,
        "seek": seeks,
        "scenarios": scenario_rows,
    }

    out_dir = Path(args.out_dir) if args.out_dir else \
        Path(__file__).resolve().parents[4] / "artifacts" / "benchmarks"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    jpath = out_dir / f"lake_bench_{label}_{stamp}.json"
    jpath.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    mpath = out_dir / f"lake_bench_{label}_{stamp}.md"
    mpath.write_text(format_dataset_report(result), encoding="utf-8")
    print(f"[lake_benchmark] saved {jpath}")
    print(f"[lake_benchmark] saved {mpath}")
    return result


def format_dataset_report(res: dict) -> str:
    t, sk = res["transport"], res["seek"]
    lines = [
        f"# Lake benchmark — {res['dataset_uri']}",
        "",
        f"- measured_at: {res['measured_at']}",
        f"- machine: {res['machine']}",
        f"- note(위치·회선·사양): {res['note'] or '(미기입 — 판정 규약상 기입 필요)'}",
        f"- params: {json.dumps(res['params'])}",
        "",
        "## A. 전송 (transport)",
        "",
        "| metric | value |",
        "|--------|-------|",
        f"| format / chunk_seconds / hz | {t['format']} / {t['chunk_seconds']} / {t['hz']} |",
        f"| chunks measured / total MB | {t['n_chunks_measured']} / {t['total_mb']} |",
        f"| manifest_load_ms | {t['manifest_load_ms']} |",
        f"| first GET ms (TLS 혼입) | {t['first_get_ms']} |",
        f"| chunk GET p50 / p99 / mean ms | {t['get_p50_ms']} / {t['get_p99_ms']} / {t['get_mean_ms']} |",
        f"| decode ms/MB (p50 chunk {t['decode_p50_ms']}ms) | {t['decode_ms_per_mb']} |",
        f"| cold seek p50 / p99 ms (n={sk['cold_seek_n']}) | {sk['cold_seek_p50_ms']} / {sk['cold_seek_p99_ms']} |",
        f"| warm seek p50 / p99 us | {sk['warm_seek_p50_us']} / {sk['warm_seek_p99_us']} |",
        "",
        "## B. 연속 재생 (wall-clock 페이싱, 웜업 제외)",
        "",
        "| scenario | speed | lookups | stalls | warmup | hit_rate | lead_min/p50 (s) | sync p50 (ms) | seek p50/p99 (ms) |",
        "|----------|-------|---------|--------|--------|----------|------------------|----------------|--------------------|",
    ]
    for r in res["scenarios"]:
        seekcol = f"{r.get('seek_p50_ms', '-')} / {r.get('seek_p99_ms', '-')}" if "seek_p50_ms" in r else "-"
        hit_txt = f"{r['hit_rate']*100:.1f}%" if r["hit_rate"] is not None else "N/A"
        lines.append(
            f"| {r['scenario']} | {r['speed']} | {r['lookups']} | {r['stalls']} | {r['warmup_cold_loads']} | "
            f"{hit_txt} | {r['prefetch_lead_min_s']} / {r['prefetch_lead_p50_s']} | "
            f"{r['sync_load_p50_ms']} | {seekcol} |"
        )
    lines += [
        "",
        "판정(설계 §4): 1x·5x stall=0 = 끊김없음 합격. warm seek p99 < 5ms = 빠름 합격.",
        "cold seek 목표치는 실측 후 설정(WAN 전제).",
        "",
    ]
    return "\n".join(lines)


# ======================================================================== #

def main(argv=None):
    ap = argparse.ArgumentParser(description="lake playback benchmark (합성 / 실 데이터셋)")
    ap.add_argument("scales", nargs="*",
                    help="합성 모드 스케일 토큰 (예: 100x600 10x3600). 기본 3종")
    ap.add_argument("--dataset-uri", default=None,
                    help="실 데이터셋(s3:// 또는 file://) — 지정 시 A/B 계층 측정 모드")
    ap.add_argument("--lookup-hz", type=float, default=10.0,
                    help="B 계층 lookup 밀도(초당 조회 수, 기본 10)")
    ap.add_argument("--play-wall-s", type=float, default=180.0,
                    help="시나리오당 벽시계 재생 시간(기본 180s)")
    ap.add_argument("--cache-chunks", type=int, default=4, help="LRU 캐시 청크 수(현행 4)")
    ap.add_argument("--prefetch-ahead", type=int, default=2, help="선로드 청크 수(현행 2)")
    ap.add_argument("--seeks", type=int, default=10, help="seek 시나리오 횟수(기본 10)")
    ap.add_argument("--scenarios", default="1x,5x,seek,backward",
                    help="실행 시나리오 (쉼표 구분: 1x,5x,seek,backward)")
    ap.add_argument("--max-chunks", type=int, default=0,
                    help="A 계층에서 측정할 최대 청크 수(0=전부)")
    ap.add_argument("--warm-queries", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--note", default="",
                    help="측정 위치·회선·클라 사양 (판정 규약 §4 — 결과에 병기)")
    ap.add_argument("--out-dir", default=None,
                    help="결과 저장 디렉터리(기본 EXT_ROOT/artifacts/benchmarks)")
    args = ap.parse_args(argv)

    if args.dataset_uri:
        run_dataset_mode(args)
        return

    # 합성 모드(기존 동작)
    scales = [(10, 300), (100, 300), (10, 3600)]
    if args.scales:
        scales = []
        for tok in args.scales:
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
