"""레이크 트랙 범위(manifest "tracks") — despawn 정확성 테스트.

배경(레이크성능_실험설계 §0-4): dead-track despawn이 쓰는 get_object_time_ranges를
레이크가 베이스 구현(메모리 _data=활성 청크 전체 스캔)으로 상속하면 트랙 범위가 첫
청크 스팬으로 오계산돼, 재생 헤드가 청크 1을 지나는 순간 전 객체가 숨는다(실측).
검증: ① ingest가 objid별 [first,last]를 manifest에 기록 ② 리더가 그걸로 정확한
범위 반환(청크 활성화와 무관) ③ 무-tracks 레거시 manifest는 전체 범위 폴백
④ append 병합 규칙(있으면 min/max 병합, 레거시는 무-tracks 유지).

의존성 없이 file:// + CSV로 동작(WSL stdlib OK).
"""
import datetime
import json
import tempfile
import unittest
from pathlib import Path

from gist.netai.time_travel_summarization.playback.lake_common import append_rows, ingest_rows
from gist.netai.time_travel_summarization.playback.lake_repository import LakeTrajectoryRepository
from gist.netai.time_travel_summarization.playback.trajectory_repository import TrajectoryRepository

BASE = datetime.datetime(2026, 8, 6, 0, 0, 0)


def _ts(sec: float) -> str:
    return (BASE + datetime.timedelta(seconds=sec)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _rows(spec: dict) -> list:
    """spec = {objid: (first_s, last_s)} → 1Hz rows (timestamp 오름차순)."""
    out = []
    lo = min(s for s, _ in spec.values())
    hi = max(e for _, e in spec.values())
    for s in range(int(lo), int(hi) + 1):
        for oid, (first_s, last_s) in sorted(spec.items()):
            if first_s <= s <= last_s:
                out.append({"timestamp": _ts(s), "objid": oid,
                            "x": float(s), "y": 0.0, "z": float(s)})
    return out


def _manifest_path(dataset_uri: str) -> Path:
    return Path(dataset_uri.replace("file://", "")) / "manifest.json"


class LakeTrackRangesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="lake_tracks_")
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _ingest(self, spec: dict, name: str = "ds", chunk_seconds: int = 10) -> str:
        uri = (self.tmp / name).resolve().as_uri()
        ingest_rows(_rows(spec), uri, chunk_seconds=chunk_seconds, fmt="csv", hz=1.0)
        return uri

    def test_ingest_records_tracks(self):
        uri = self._ingest({"obj001": (0, 60), "obj002": (20, 40)})
        m = json.loads(_manifest_path(uri).read_text(encoding="utf-8"))
        self.assertEqual(m["tracks"]["obj001"], [_ts(0), _ts(60)])
        self.assertEqual(m["tracks"]["obj002"], [_ts(20), _ts(40)])

    def test_ranges_exact_and_not_chunk_local(self):
        # 핵심 회귀: 청크 0을 활성화한 '뒤'에도 범위가 청크 스팬(10s)이 아니라
        # manifest tracks의 실제 트랙 범위여야 한다.
        uri = self._ingest({"obj001": (0, 60), "obj002": (20, 40)})
        repo = LakeTrajectoryRepository(cache_chunks=2, prefetch_ahead=0)
        self.assertTrue(repo.load_from_uri(uri))
        repo.get_data_at_time(BASE + datetime.timedelta(seconds=5))  # 청크 0 활성화
        r = repo.get_object_time_ranges()
        parse = TrajectoryRepository.parse_timestamp
        self.assertEqual(r["obj001"], (parse(_ts(0)), parse(_ts(60))))
        self.assertEqual(r["obj002"], (parse(_ts(20)), parse(_ts(40))))

    def test_legacy_manifest_falls_back_to_full_range(self):
        uri = self._ingest({"obj001": (0, 60), "obj002": (20, 40)})
        p = _manifest_path(uri)
        m = json.loads(p.read_text(encoding="utf-8"))
        del m["tracks"]  # 스키마 도입 전 manifest 재현
        p.write_text(json.dumps(m), encoding="utf-8")
        repo = LakeTrajectoryRepository(cache_chunks=2, prefetch_ahead=0)
        self.assertTrue(repo.load_from_uri(uri))
        r = repo.get_object_time_ranges()
        parse = TrajectoryRepository.parse_timestamp
        full = (parse(m["start"]), parse(m["end"]))
        self.assertEqual(r, {"obj001": full, "obj002": full})

    def test_append_merges_tracks(self):
        uri = self._ingest({"obj001": (0, 60)})
        append_rows(_rows({"obj001": (70, 80), "obj003": (70, 75)}), uri, fmt="csv")
        m = json.loads(_manifest_path(uri).read_text(encoding="utf-8"))
        self.assertEqual(m["tracks"]["obj001"], [_ts(0), _ts(80)])   # min/max 병합
        self.assertEqual(m["tracks"]["obj003"], [_ts(70), _ts(75)])  # 신규 트랙

    def test_append_to_legacy_stays_trackless(self):
        # 레거시에 신규 rows의 부분 범위만 기록하면 기존 트랙이 잘리므로 무-tracks 유지.
        uri = self._ingest({"obj001": (0, 60)})
        p = _manifest_path(uri)
        m = json.loads(p.read_text(encoding="utf-8"))
        del m["tracks"]
        p.write_text(json.dumps(m), encoding="utf-8")
        merged = append_rows(_rows({"obj001": (70, 80)}), uri, fmt="csv")
        self.assertNotIn("tracks", merged)


if __name__ == "__main__":
    unittest.main()
