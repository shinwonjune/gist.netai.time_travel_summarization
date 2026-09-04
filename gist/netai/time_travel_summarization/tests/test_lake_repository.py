"""LakeTrajectoryRepository 정확성 검증.

윈도우(청크) 단위 로딩 결과가 전체 적재(oracle)와 동일한지, 청크 경계/off-grid
시점에서도 일치하는지 확인한다. 캐시 크기 제한과 백그라운드 프리페치 동작도 검증.

의존성 없이 file:// + CSV로 동작. 직접 실행( python test_lake_repository.py ) 또는 pytest 모두 가능.
"""

import datetime
import tempfile
import threading
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


def test_object_samples_invalidated_on_chunk_activation():
    """청크 전환 후 get_object_last_sample이 새 활성 청크의 표본 시각을 반환하는지.

    _activate가 self._data/_timestamps는 갈아 끼우면서 부모(TrajectoryRepository)가
    지연 생성해 캐시하는 self._object_samples를 비우지 않으면, 청크 1이 활성인
    상태에서도 청크 0 기준 표본 시각이 그대로 나온다(gap-despawn이 낡은 시각을 읽는
    원인). 수정 후에는 청크 1 활성 상태의 조회 결과가 청크 1 구간 안에 있어야 한다.
    """
    with tempfile.TemporaryDirectory() as d:
        manifest, dataset_uri, _ = _build(Path(d))
        lake = LakeTrajectoryRepository(cache_chunks=4, prefetch_ahead=0)
        assert lake.load_from_uri(dataset_uri) is True

        chunk0_end = TrajectoryRepository.parse_timestamp(manifest["chunks"][0]["end"])
        chunk1_start = TrajectoryRepository.parse_timestamp(manifest["chunks"][1]["start"])
        chunk1_end = TrajectoryRepository.parse_timestamp(manifest["chunks"][1]["end"])

        # 청크 0이 활성인 상태에서 표본 캐시를 먼저 만든다.
        first = lake.get_object_last_sample(chunk0_end)
        assert first
        assert lake._active_idx == 0

        # 청크 1로 재생 헤드를 옮겨 활성 청크를 전환시킨다.
        lake.get_data_at_time(chunk1_start)
        assert lake._active_idx == 1

        second = lake.get_object_last_sample(chunk1_end)
        for objid, ts in second.items():
            assert ts >= chunk1_start, f"{objid}: stale sample {ts} (< chunk1 start {chunk1_start})"
        lake.clear()


def test_stop_prefetch_during_slow_load_leaves_no_stray_exception(monkeypatch):
    """_pf_loop가 stop 이벤트를 지역 인자로 받는지 확인.

    안 그러면 아래 시나리오(슬로우 로드가 _stop_prefetch의 join(timeout=1.0)보다 오래
    걸림)에서 워커가 join 만료 후 None으로 리셋된 self._pf_stop을 참조해
    AttributeError로 죽는다. 백그라운드 스레드 예외라 pytest에 안 잡히므로
    threading.excepthook으로 관측한다.
    """
    exceptions = []
    orig_hook = threading.excepthook
    threading.excepthook = lambda args: exceptions.append(args)
    try:
        with tempfile.TemporaryDirectory() as d:
            manifest, dataset_uri, _ = _build(Path(d))
            lake = LakeTrajectoryRepository(cache_chunks=4, prefetch_ahead=1)
            assert lake.load_from_uri(dataset_uri) is True

            orig_load_chunk = lake._load_chunk

            def _slow_load_chunk(idx):
                time.sleep(1.05)  # _stop_prefetch의 join(timeout=1.0)보다 길게
                return orig_load_chunk(idx)

            monkeypatch.setattr(lake, "_load_chunk", _slow_load_chunk)

            lake._schedule_prefetch(0)  # prefetch_ahead=1 -> 청크 1을 큐에 넣음
            time.sleep(0.05)            # 워커가 idx를 집어 슬로우 로드에 들어갈 시간
            lake.clear()                # _stop_prefetch: join(timeout=1.0) 만료 후 _pf_stop=None
            time.sleep(0.3)             # 워커가 슬로우 로드를 끝내고 루프를 도는 시간
    finally:
        threading.excepthook = orig_hook
    assert exceptions == [], f"prefetch worker thread raised: {exceptions}"


if __name__ == "__main__":
    test_lake_matches_full_load()
    print("PASS test_lake_matches_full_load")
    test_cache_is_bounded()
    print("PASS test_cache_is_bounded")
    test_prefetch_loads_neighbor()
    print("PASS test_prefetch_loads_neighbor")
    test_object_samples_invalidated_on_chunk_activation()
    print("PASS test_object_samples_invalidated_on_chunk_activation")
    print("ALL PASS")
