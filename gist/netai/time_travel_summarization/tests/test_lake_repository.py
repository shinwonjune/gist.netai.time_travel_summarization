"""LakeTrajectoryRepository 정확성 검증.

윈도우(청크) 단위 로딩 결과가 전체 적재(oracle)와 동일한지, 청크 경계/off-grid
시점에서도 일치하는지 확인한다. 캐시 크기 제한과 백그라운드 프리페치 동작도 검증.

의존성 없이 file:// + CSV로 동작. 직접 실행( python test_lake_repository.py ) 또는 pytest 모두 가능.
"""

import datetime
import tempfile
import time
from pathlib import Path

from gist.netai.time_travel_summarization.playback.lake_common import generate_synthetic_rows, ingest_rows
from gist.netai.time_travel_summarization.playback.lake_repository import LakeTrajectoryRepository
from gist.netai.time_travel_summarization.playback.trajectory_repository import TrajectoryRepository

HZ = 5.0
DURATION = 60.0
CHUNK_SECONDS = 5  # -> 12 chunks


def _build(tmp: Path):
    rows = list(generate_synthetic_rows(n_objects=4, duration_s=DURATION, hz=HZ, seed=7))
    dataset_uri = (tmp / "ds").resolve().as_uri()
    manifest = ingest_rows(rows, dataset_uri, chunk_seconds=CHUNK_SECONDS, fmt="csv", hz=HZ)
    oracle = TrajectoryRepository()
    oracle._data, oracle._timestamps = TrajectoryRepository._rows_to_data(rows)
    return manifest, dataset_uri, oracle


def _query_times(manifest):
    start = TrajectoryRepository.parse_timestamp(manifest["start"])
    times = []
    # grid + off-grid across full span
    for i in range(0, int(DURATION * HZ)):
        times.append(start + datetime.timedelta(seconds=i / HZ))
        times.append(start + datetime.timedelta(seconds=i / HZ + 0.07))  # off-grid
    # exact chunk boundaries
    for c in manifest["chunks"]:
        times.append(TrajectoryRepository.parse_timestamp(c["start"]))
    # before start / after end
    times.append(start - datetime.timedelta(seconds=3))
    times.append(TrajectoryRepository.parse_timestamp(manifest["end"]) + datetime.timedelta(seconds=3))
    return times


def test_lake_matches_full_load():
    with tempfile.TemporaryDirectory() as d:
        manifest, dataset_uri, oracle = _build(Path(d))
        for mode in ("linear", "bisect"):
            lake = LakeTrajectoryRepository(cache_chunks=3, prefetch_ahead=1)
            assert lake.load_from_uri(dataset_uri) is True
            lake.set_lookup_mode(mode)
            oracle.set_lookup_mode(mode)
            assert lake.data_start_time == oracle.parse_timestamp(manifest["start"])
            for t in _query_times(manifest):
                assert lake.get_data_at_time(t) == oracle.get_data_at_time(t), f"mismatch mode={mode} t={t}"
            lake.clear()


def test_cache_is_bounded():
    with tempfile.TemporaryDirectory() as d:
        manifest, dataset_uri, _ = _build(Path(d))
        lake = LakeTrajectoryRepository(cache_chunks=3, prefetch_ahead=1)
        lake.load_from_uri(dataset_uri)
        start = lake.data_start_time
        for i in range(int(DURATION * HZ)):  # 전체 구간 forward 스캔
            lake.get_data_at_time(start + datetime.timedelta(seconds=i / HZ))
        assert len(lake._cache) <= lake._cache_chunks, f"cache grew to {len(lake._cache)}"
        lake.clear()


def test_prefetch_loads_neighbor():
    with tempfile.TemporaryDirectory() as d:
        manifest, dataset_uri, _ = _build(Path(d))
        lake = LakeTrajectoryRepository(cache_chunks=4, prefetch_ahead=1)
        lake.load_from_uri(dataset_uri)
        start = lake.data_start_time
        lake.get_data_at_time(start)          # active chunk 0, schedules prefetch of 1
        for _ in range(50):                    # 워커가 이웃 청크 로드할 시간
            if 1 in lake._cache:
                break
            time.sleep(0.02)
        assert lake.stats["prefetch_loads"] >= 1, lake.stats
        assert 1 in lake._cache
        lake.clear()


if __name__ == "__main__":
    test_lake_matches_full_load()
    print("PASS test_lake_matches_full_load")
    test_cache_is_bounded()
    print("PASS test_cache_is_bounded")
    test_prefetch_loads_neighbor()
    print("PASS test_prefetch_loads_neighbor")
    print("ALL PASS")
