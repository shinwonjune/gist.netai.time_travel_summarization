import json
import sys
import types
from datetime import datetime
from pathlib import Path


def _install_carb_stub():
    carb = types.ModuleType("carb")
    carb.log_info = lambda *args, **kwargs: None
    carb.log_warn = lambda *args, **kwargs: None
    carb.log_error = lambda *args, **kwargs: None
    sys.modules["carb"] = carb


_install_carb_stub()

from gist.netai.time_travel_summarization.event_processing.event_index import (  # noqa: E402
    append_index, index_uri_for, parse_events_from_vlm_result,
    query_events, resolve_event_datetime, sidecar_anchor,
)

ANCHOR = datetime(2026, 1, 1, 0, 0, 0)


def test_parse_events_tolerates_junk_and_dedupes():
    result = {
        "chunk_responses": [
            {"content": 'Sure! [{"00:00:01": [2, 1]}, {"00:00:03": [3]}] done'},
            {"content": '[{"00:00:01": [1, 2]}]'},   # 청크 경계 중복 → dedupe
            {"content": "no events"},                 # 잡문 → 스킵
            {"content": '[{"bad": "x"}]'},            # 형식 위반 항목 → 스킵
            {"content": None},                        # 실패 청크 → 스킵
        ]
    }
    assert parse_events_from_vlm_result(result) == [
        {"time": "00:00:01", "ids": [1, 2]},
        {"time": "00:00:03", "ids": [3]},
    ]


def test_resolve_event_datetime_rollover():
    late_anchor = datetime(2026, 1, 1, 23, 50, 0)
    assert resolve_event_datetime("23:55:00", late_anchor) == datetime(2026, 1, 1, 23, 55)
    # 앵커 시각보다 이른 보고 = 자정 넘김 → +1일
    assert resolve_event_datetime("00:05:00", late_anchor) == datetime(2026, 1, 2, 0, 5)


def test_index_uri_is_per_video_object():
    uri = index_uri_for("s3://bucket/root/", "video_x.mp4")
    assert uri == "s3://bucket/root/vlm_events/video_x.jsonl"


def test_append_and_query_absolute_time_window(tmp_path):
    root = Path(tmp_path).as_uri()
    append_index(root, "a.mp4",
                 [{"time": "00:00:01", "ids": [1, 2]}, {"time": "00:00:03", "ids": [3]}],
                 model="m", anchor=ANCHOR)
    append_index(root, "b.mp4", [{"time": "00:00:02", "ids": [4]}], run="r1", anchor=ANCHOR)

    assert [e["time"] for e in query_events(root)] == [
        "2026-01-01T00:00:01", "2026-01-01T00:00:02", "2026-01-01T00:00:03"]
    # datetime 객체와 ISO 문자열 둘 다 허용
    window = query_events(root, datetime(2026, 1, 1, 0, 0, 2), "2026-01-01T00:00:03")
    assert len(window) == 2 and window[0]["ids"] == [4]
    assert query_events(root, run="r1")[0]["video"] == "b.mp4"
    assert query_events(root, run="none") == []


def test_multiday_events_distinguished(tmp_path):
    """같은 HH:MM:SS라도 날짜가 다르면 별개 이벤트 — 다일 range 조회의 근거."""
    root = Path(tmp_path).as_uri()
    append_index(root, "day1.mp4", [{"time": "00:30:00", "ids": [1]}],
                 anchor=datetime(2026, 1, 1, 0, 0, 0))
    append_index(root, "day2.mp4", [{"time": "00:30:00", "ids": [2]}],
                 anchor=datetime(2026, 1, 2, 0, 0, 0))
    day1_only = query_events(root, "2026-01-01T00:00:00", "2026-01-01T23:59:59")
    assert len(day1_only) == 1 and day1_only[0]["ids"] == [1]
    both = query_events(root, "2026-01-01T00:00:00", "2026-01-02T23:59:59")
    assert len(both) == 2


def test_no_anchor_records_kept_but_not_queryable(tmp_path):
    root = Path(tmp_path).as_uri()
    append_index(root, "noanchor.mp4", [{"time": "00:00:05", "ids": [9]}])
    # 파일에는 남는다(time_hms 보존) — 시간창 조회에서만 제외
    raw = (Path(tmp_path) / "vlm_events" / "noanchor.jsonl").read_text(encoding="utf-8")
    record = json.loads(raw.splitlines()[0])
    assert record["time"] is None and record["time_hms"] == "00:00:05"
    assert query_events(root) == []


def test_reindex_overwrites_same_video(tmp_path):
    root = Path(tmp_path).as_uri()
    append_index(root, "a.mp4", [{"time": "00:00:01", "ids": [1]}], anchor=ANCHOR)
    append_index(root, "a.mp4", [{"time": "00:00:09", "ids": [7]}], anchor=ANCHOR)
    assert [e["time_hms"] for e in query_events(root)] == ["00:00:09"]


def test_zero_events_still_writes_evidence(tmp_path):
    root = Path(tmp_path).as_uri()
    append_index(root, "quiet.mp4", [], anchor=ANCHOR)
    assert (Path(tmp_path) / "vlm_events" / "quiet.jsonl").exists()
    assert query_events(root) == []


def test_sidecar_anchor_reads_capture_start(tmp_path):
    video = tmp_path / "cap.mp4"
    video.write_bytes(b"x")
    (tmp_path / "cap.meta.json").write_text(
        json.dumps({"capture_start": "2026-01-01T09:00:00", "mode": "playback"}),
        encoding="utf-8")
    assert sidecar_anchor(str(video)) == datetime(2026, 1, 1, 9, 0, 0)
    assert sidecar_anchor(video.as_uri()) == datetime(2026, 1, 1, 9, 0, 0)
    assert sidecar_anchor(str(tmp_path / "missing.mp4")) is None
