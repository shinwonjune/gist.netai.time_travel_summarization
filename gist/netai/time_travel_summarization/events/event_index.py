"""이벤트 인덱스 — VLM 추론 결과의 시간축 검색 표면 (RAG 직전 받침).

추론 결과(영상별 JSON)는 흩어져 있어 "이 시간대에 무슨 일이 있었나"에 답할 수
없다. 이 모듈은 추론이 끝날 때마다 이벤트를 고정 스키마로 축적하고,
시간창으로 조회하는 최소 표면을 제공한다. 나중의 RAG/agent가 검색할 대상이
바로 이 인덱스다.

설계: 오브젝트 스토리지(minIO)에는 append가 없으므로 **추론 1회 = 인덱스
오브젝트 1개** 규칙을 쓴다(`<root>/vlm_events/<video_stem>.jsonl`).
동시 쓰기 경합이 없고(파일명이 영상별로 유일), 조회는 prefix 목록 + 필터.
규모가 커지면 이 JSONL들을 그대로 DuckDB/parquet으로 컴팩션하는 진화 경로.

레코드(JSONL 1행):
  {"time": "2026-01-01T00:30:15", "time_hms": "00:30:15", "ids": [1, 2],
   "kind": "collision", "video": "capture.mp4", "model": "...", "run": "",
   "indexed_at": iso}
VLM은 픽셀의 오버레이 시계(HH:MM:SS, 날짜 없음)를 읽어 보고하므로, 인덱스
적재 시점에 영상 사이드카의 앵커(capture_start)와 결합해 절대 시각으로 복원해
`time`에 저장한다(자정 넘김은 롤오버 +1일). 앵커를 못 찾으면 `time=None`으로
남기고 `time_hms`만 유지 — 시간창 조회에서는 제외된다(타임라인에 놓을 수 없음).
다일(多日) 범위 조회가 성립하려면 절대 시각이 필수다(01-01과 01-02 양쪽의
00:30 이벤트는 HH:MM:SS만으로는 구분 불가).
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional, Union

_HMS_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")
INDEX_PREFIX = "vlm_events"


def resolve_event_datetime(hms: str, anchor: datetime) -> datetime:
    """오버레이 시각(HH:MM:SS) + 앵커(영상 t0) → 절대 datetime.

    앵커의 날짜를 붙이되, 보고 시각이 앵커의 시각보다 이르면 자정을 넘은
    것이므로 +1일(롤오버). 영상 길이 < 24h 전제(캡처는 분 단위라 항상 성립).
    """
    h, m, s = (int(p) for p in hms.split(":"))
    candidate = anchor.replace(hour=h, minute=m, second=s, microsecond=0)
    if candidate < anchor.replace(microsecond=0):
        candidate += timedelta(days=1)
    return candidate


def parse_events_from_vlm_result(result: dict) -> list[dict]:
    """chunk_responses[].content('[{"HH:MM:SS": [ids]}]')를 이벤트 목록으로.

    VLM 출력은 JSON 앞뒤에 잡문이 붙을 수 있어 첫 '['~끝 ']' 구간만 파싱하고,
    형식이 깨진 청크는 조용히 건너뛴다(부분 실패 허용 — 인덱스는 best-effort).
    같은 (time, ids)는 청크 경계 중복에 대비해 dedupe.
    """
    events: list[dict] = []
    seen: set = set()
    for chunk in result.get("chunk_responses", []) or []:
        content = (chunk or {}).get("content") or ""
        # 청크 avg_logprob → confidence(기하평균 토큰 확률, 0~1) — 랭킹용 신뢰 신호.
        avg_lp = (chunk or {}).get("avg_logprob")
        conf = round(math.exp(avg_lp), 4) if isinstance(avg_lp, (int, float)) else None
        start, end = content.find("["), content.rfind("]")
        if start < 0 or end <= start:
            continue
        try:
            entries = json.loads(content[start:end + 1])
        except Exception:
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for time_str, ids in entry.items():
                if not isinstance(time_str, str) or not _HMS_RE.match(time_str):
                    continue
                if not isinstance(ids, list):
                    ids = [ids]
                try:
                    norm_ids = sorted(int(i) for i in ids)
                except (TypeError, ValueError):
                    continue
                key = (time_str, tuple(norm_ids))
                if key in seen:
                    continue
                seen.add(key)
                events.append({"time": time_str, "ids": norm_ids, "confidence": conf})
    return events


def index_uri_for(index_root_uri: str, video_name: str) -> str:
    stem = Path(str(video_name)).stem or "unknown"
    return f"{index_root_uri.rstrip('/')}/{INDEX_PREFIX}/{stem}.jsonl"


def sidecar_anchor(video_source: str) -> Optional[datetime]:
    """영상 옆의 사이드카(.meta.json)에서 시각 앵커(capture_start)를 읽는다.

    s3://·file://·로컬 경로 모두 storage adapter로 처리. 사이드카가 없거나
    파싱 실패면 None — 호출부는 앵커 미상으로 적재(절대 시각 없이).
    """
    try:
        from ..storage import from_uri

        src = str(video_source).strip()
        if "." not in src.rsplit("/", 1)[-1]:
            return None
        meta_uri = src.rsplit(".", 1)[0] + ".meta.json"
        adapter = from_uri(meta_uri)
        if not adapter.exists(meta_uri):
            return None
        with adapter.open_read(meta_uri) as fh:
            meta = json.loads(fh.read().decode("utf-8"))
        raw = meta.get("capture_start")
        return datetime.fromisoformat(raw) if raw else None
    except Exception:
        return None


def append_index(index_root_uri: str, video_name: str, events: Iterable[dict],
                 model: str = "", run: str = "",
                 anchor: Optional[datetime] = None) -> str:
    """이벤트를 영상별 인덱스 오브젝트로 기록. 이벤트 0건도 기록(검사 증거).

    anchor(영상 t0)가 있으면 HH:MM:SS를 절대 시각으로 복원해 `time`에 저장.
    같은 영상을 재추론하면 같은 오브젝트를 덮어쓴다(최신 추론이 진실).
    """
    from ..storage import from_uri

    uri = index_uri_for(index_root_uri, video_name)
    now = datetime.now().isoformat(timespec="seconds")
    lines = []
    for ev in events:
        hms = ev["time"]
        absolute = (
            resolve_event_datetime(hms, anchor).isoformat(timespec="seconds")
            if anchor is not None else None
        )
        record = {
            "time": absolute,
            "time_hms": hms,
            "ids": ev["ids"],
            "kind": ev.get("kind", "collision"),
            "video": str(video_name),
            "model": model,
            "run": run,
            "indexed_at": now,
        }
        lines.append(json.dumps(record, ensure_ascii=False))
    payload = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    from_uri(uri).put_bytes(uri, payload, content_type="application/x-ndjson")
    return uri


def query_events(index_root_uri: str,
                 start: Union[str, datetime, None] = None,
                 end: Union[str, datetime, None] = None,
                 run: Optional[str] = None) -> list[dict]:
    """[start, end] 시간창(절대 datetime 또는 ISO 문자열)의 이벤트를 시간순으로.

    절대 시각(`time`)이 없는 레코드(앵커 미상 영상)는 타임라인에 놓을 수 없어
    제외된다. 조회는 인덱스 prefix 전체 스캔 — 현 규모(추론 결과 수백 건)에선
    충분하고, 병목이 되면 컴팩션(단일 parquet/DuckDB)으로 승격.
    """
    from ..storage import from_uri

    def _iso(v, default: str) -> str:
        if v is None:
            return default
        if isinstance(v, datetime):
            return v.isoformat(timespec="seconds")
        return str(v)

    start_iso = _iso(start, "0001-01-01T00:00:00")
    end_iso = _iso(end, "9999-12-31T23:59:59")
    # 후행 슬래시 + recursive=True: minIO 비재귀 목록은 'vlm_events/'를 dir 항목
    # 하나로만 반환해 하위 오브젝트가 안 나온다(로컬 dir 목록과 의미가 다름).
    prefix = f"{index_root_uri.rstrip('/')}/{INDEX_PREFIX}/"
    adapter = from_uri(prefix)
    out: list[dict] = []
    for obj in adapter.list_prefix(prefix, recursive=True):
        with adapter.open_read(obj.uri) as fh:
            for line in fh.read().decode("utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                t = record.get("time")
                if not t:  # 앵커 없이 적재된 레코드 — 절대 시각 미상
                    continue
                if not (start_iso <= t <= end_iso):
                    continue
                if run is not None and record.get("run", "") != run:
                    continue
                out.append(record)
    out.sort(key=lambda r: (r.get("time", ""), str(r.get("video", ""))))
    return out


def _self_test() -> None:
    result = {
        "chunk_responses": [
            {"content": 'Sure! [{"00:00:01": [2, 1]}, {"00:00:03": [3]}] done'},
            {"content": '[{"00:00:01": [1, 2]}]'},   # 중복 → dedupe
            {"content": "no events"},                 # 잡문 → 스킵
            {"content": '[{"bad": "x"}]'},            # 형식 위반 → 스킵
        ]
    }
    events = parse_events_from_vlm_result(result)
    assert events == [{"time": "00:00:01", "ids": [1, 2]},
                      {"time": "00:00:03", "ids": [3]}], events
    assert index_uri_for("s3://b/root/", "video_x.mp4").endswith("/vlm_events/video_x.jsonl")

    # 시각 복원: 앵커 날짜 결합 + 자정 롤오버
    anchor = datetime(2026, 1, 1, 23, 50, 0)
    assert resolve_event_datetime("23:55:00", anchor) == datetime(2026, 1, 1, 23, 55)
    assert resolve_event_datetime("00:05:00", anchor) == datetime(2026, 1, 2, 0, 5)

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).as_uri()
        a0 = datetime(2026, 1, 1, 0, 0, 0)
        append_index(root, "a.mp4", events, model="m", anchor=a0)
        append_index(root, "b.mp4", [{"time": "00:00:02", "ids": [4]}], run="r1", anchor=a0)
        allhits = query_events(root)
        assert [e["time"] for e in allhits] == [
            "2026-01-01T00:00:01", "2026-01-01T00:00:02", "2026-01-01T00:00:03"], allhits
        window = query_events(root, datetime(2026, 1, 1, 0, 0, 2), "2026-01-01T00:00:03")
        assert len(window) == 2 and window[0]["ids"] == [4]
        assert query_events(root, run="r1")[0]["video"] == "b.mp4"
        assert query_events(root, run="none") == []
        # 앵커 미상 → time=None → 시간창 조회에서 제외(파일에는 남음)
        append_index(root, "noanchor.mp4", [{"time": "00:00:05", "ids": [9]}])
        assert all(e["video"] != "noanchor.mp4" for e in query_events(root))
        # 재추론 = 덮어쓰기(최신이 진실)
        append_index(root, "a.mp4", [{"time": "00:00:09", "ids": [7]}], anchor=a0)
        assert [e["time_hms"] for e in query_events(root) if e["video"] == "a.mp4"] == ["00:00:09"]
    print("event_index self-test OK")


if __name__ == "__main__":
    _self_test()
