"""lake append 적재 계약 테스트 — file:// 로컬 데이터셋, csv 포맷(pyarrow 불필요)."""
import json

import pytest

from gist.netai.time_travel_summarization.playback.lake_common import (
    append_rows, ingest_rows,
)
from gist.netai.time_travel_summarization.utils.ingest_trajectory import (
    check_input_overlaps, load_trace,
)


def _rows(start: str, n: int, objid: str = "obj001", step_ms: int = 200):
    import datetime
    base = datetime.datetime.strptime(start, "%Y-%m-%d %H:%M:%S.%f")
    out = []
    for i in range(n):
        ts = (base + datetime.timedelta(milliseconds=step_ms * i)
              ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        out.append({"timestamp": ts, "objid": objid, "x": 1.0 * i, "y": 2.0, "z": 3.0})
    return out


def test_append_merges_manifest_with_gap(tmp_path):
    ds = (tmp_path / "ds").as_uri()
    ingest_rows(_rows("2025-01-01 00:00:00.000", 10), ds, chunk_seconds=60, fmt="csv")
    # 1.5년 뒤 데이터를 append — 연속일 필요 없음(공백은 재생기 점프가 처리)
    m = append_rows(_rows("2026-07-18 03:44:14.000", 10, objid="obj002"), ds)

    assert m["start"] == "2025-01-01 00:00:00.000"
    assert m["end"].startswith("2026-07-18 03:44:15")
    assert m["rows"] == 20
    assert m["objids"] == ["obj001", "obj002"]
    starts = [c["start"] for c in m["chunks"]]
    assert starts == sorted(starts)  # 청크 시각순 정렬 (bisect 전제)
    # 이전 manifest 백업
    assert (tmp_path / "ds" / "manifest.json.bak").exists()
    # 디스크의 manifest가 반환값과 일치
    on_disk = json.loads((tmp_path / "ds" / "manifest.json").read_text())
    assert on_disk["rows"] == 20


def test_append_rejects_time_overlap(tmp_path):
    ds = (tmp_path / "ds").as_uri()
    ingest_rows(_rows("2025-01-01 00:00:00.000", 10), ds, chunk_seconds=60, fmt="csv")
    with pytest.raises(ValueError, match="겹침"):
        append_rows(_rows("2025-01-01 00:00:01.000", 5), ds)


def test_append_creates_dataset_when_missing(tmp_path):
    ds = (tmp_path / "new_ds").as_uri()
    m = append_rows(_rows("2026-07-18 03:44:14.000", 5), ds, fmt="csv")
    assert m["rows"] == 5
    assert (tmp_path / "new_ds" / "manifest.json").exists()


def test_input_overlap_check():
    import datetime
    p = lambda s: datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")  # noqa: E731
    ok = [("a", p("2026-07-18 03:00:00"), p("2026-07-18 03:00:30")),
          ("b", p("2026-07-18 04:00:00"), p("2026-07-18 04:00:30"))]
    check_input_overlaps(ok)  # 교차 없음 — 통과
    bad = ok + [("c", p("2026-07-18 04:00:10"), p("2026-07-18 04:00:40"))]
    with pytest.raises(SystemExit, match="겹침"):
        check_input_overlaps(bad)


def test_load_trace_skips_wallclock_stretched(tmp_path):
    # 10초 영상인데 trace 스팬이 30초 → wall-clock 시절 산출물로 판정, 제외
    csv = tmp_path / "_trace_0000.csv"
    rows = _rows("2026-07-18 03:44:14.000", 31, step_ms=1000)  # 스팬 30s
    csv.write_text("timestamp,objid,x,y,z\n" + "\n".join(
        f"{r['timestamp']},{r['objid']},{r['x']},{r['y']},{r['z']}" for r in rows))
    (tmp_path / "_video_0000.meta.json").write_text(json.dumps({"duration_s": 10.0}))

    got, msg = load_trace(csv.resolve().as_uri())
    assert got == [] and "SKIP" in msg
    got_forced, _ = load_trace(csv.resolve().as_uri(), force=True)
    assert len(got_forced) == 31
