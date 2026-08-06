"""데이터레이크 파티션 규약 + 합성 데이터 생성 + 적재(ingest).

레이크 레이아웃 (dataset_uri 하위):
    {dataset_uri}/manifest.json          # 청크 인덱스 + 데이터 범위
    {dataset_uri}/chunk_{startMs:013d}.csv|.parquet

manifest.json:
    {
      "version": 1, "dataset": str, "hz": float, "chunk_seconds": int,
      "format": "csv"|"parquet", "objids": [...],
      "start": ts, "end": ts, "rows": int,
      "coord_min": [x,y,z], "coord_max": [x,y,z],
      "chunks": [{"key", "start", "end", "rows"}, ...]   # start 오름차순
      "tracks": {objid: [first_ts, last_ts], ...}        # v2 추가(despawn용) — 없으면
                                                          # 리더가 전체 범위로 폴백
    }

청크 키가 결정적(시작 epoch-ms 기반)이라 시간대 -> 키를 LIST 스캔 없이 계산/조회한다.
CSV는 의존성 없이 동작하고, Parquet은 pyarrow가 있을 때만 사용한다(선택).
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import math
import random
from typing import Dict, Iterable, List, Optional, Tuple

from .trajectory_repository import TrajectoryRepository

MANIFEST_NAME = "manifest.json"
_EPOCH = datetime.datetime(1970, 1, 1)
_FIELDS = ("timestamp", "objid", "x", "y", "z")


def to_epoch_ms(dt: datetime.datetime) -> int:
    """tz-naive datetime -> epoch milliseconds (머신 로컬 tz에 의존하지 않도록 고정 기준 사용)."""
    return int(round((dt - _EPOCH).total_seconds() * 1000))


def chunk_object_key(start_ms: int, fmt: str) -> str:
    return f"chunk_{start_ms:013d}.{fmt}"


def join_uri(base: str, name: str) -> str:
    return base.rstrip("/") + "/" + name


def manifest_uri(dataset_uri: str) -> str:
    return join_uri(dataset_uri, MANIFEST_NAME)


def dataset_uri_from_manifest(uri: str) -> str:
    """manifest.json URI -> dataset_uri (디렉터리). manifest가 아니면 그대로 반환."""
    if uri.lower().endswith(MANIFEST_NAME):
        return uri[: -(len(MANIFEST_NAME) + 1)]  # +1 = 구분자 '/'
    return uri.rstrip("/")


# ---------- 합성 데이터 ----------

def generate_synthetic_rows(
    n_objects: int,
    duration_s: float,
    hz: float = 5.0,
    start: str = "2025-01-01 00:00:00.000",
    seed: int = 42,
    bounds: Tuple[float, float] = (0.0, 1000.0),
    step_units: float = 12.0,
) -> Iterable[dict]:
    """경계 박스 내 random-walk 궤적을 hz 간격으로 생성. 시간 오름차순 yield.

    성능 측정용. n_objects/duration_s로 규모를 조절한다(예: 100객체×12시간).
    """
    rng = random.Random(seed)
    fmt = "%Y-%m-%d %H:%M:%S.%f"
    base = datetime.datetime.strptime(start, fmt)
    step = datetime.timedelta(seconds=1.0 / hz)
    n_steps = int(round(duration_s * hz))
    lo, hi = bounds
    objids = [f"obj{idx:03d}" for idx in range(1, n_objects + 1)]
    pos = {oid: [rng.uniform(lo, hi), rng.uniform(lo, hi), rng.uniform(lo, hi)] for oid in objids}
    for i in range(n_steps):
        ts = (base + step * i).strftime(fmt)[:-3]
        for oid in objids:
            p = pos[oid]
            for a in range(3):
                p[a] = min(hi, max(lo, p[a] + rng.uniform(-step_units, step_units)))
            yield {"timestamp": ts, "objid": oid, "x": p[0], "y": p[1], "z": p[2]}


def _track_time_ranges(rows_sorted: List[dict]) -> dict:
    """objid별 [first_ts, last_ts] (rows는 timestamp 오름차순 전제) — manifest "tracks".

    레이크 소스 재생/재연의 dead-track despawn이 트랙 범위를 알 수 있게 한다.
    이게 없으면 리더는 '메모리 _data = 활성 청크뿐' 제약 탓에 범위를 알 수 없어
    데이터셋 전체 범위로 폴백한다(연속 궤적엔 정확, frag 적재·입퇴장형엔 부정확 —
    lake_repository.get_object_time_ranges 참조)."""
    out: dict = {}
    for r in rows_sorted:
        span = out.get(r["objid"])
        if span is None:
            out[r["objid"]] = [r["timestamp"], r["timestamp"]]
        else:
            span[1] = r["timestamp"]
    return out


# ---------- 청크 인코딩 ----------

def _encode_chunk(rows: List[dict], fmt: str) -> Tuple[bytes, str]:
    """청크 rows -> (bytes, content_type). fmt: 'csv' | 'parquet'."""
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r[k] for k in _FIELDS})
        return buf.getvalue().encode("utf-8"), "text/csv"
    if fmt == "parquet":
        import pyarrow as pa
        import pyarrow.parquet as pq

        table = pa.table(
            {
                "timestamp": [str(r["timestamp"]) for r in rows],
                "objid": [str(r["objid"]) for r in rows],
                "x": [float(r["x"]) for r in rows],
                "y": [float(r["y"]) for r in rows],
                "z": [float(r["z"]) for r in rows],
            }
        )
        out = io.BytesIO()
        pq.write_table(table, out, compression="snappy")
        return out.getvalue(), "application/octet-stream"
    raise ValueError(f"unknown format: {fmt!r}")


# ---------- 적재 ----------

def _bucket_and_upload(
    rows_sorted: List[dict], dataset_uri: str, chunk_seconds: int, fmt: str,
) -> Tuple[List[dict], int, List[str], List[float], List[float]]:
    """정렬된 rows를 청크로 나눠 업로드. (chunks, total, objids, mins, maxs) 반환.

    ingest_rows(신규 생성)와 append_rows(기존에 추가)가 공유하는 몸통.
    """
    from ..storage import from_uri

    base = TrajectoryRepository.parse_timestamp(rows_sorted[0]["timestamp"])
    buckets: Dict[int, List[dict]] = {}
    objids = set()
    mins = [math.inf] * 3
    maxs = [-math.inf] * 3
    for r in rows_sorted:
        objids.add(r["objid"])
        xyz = (float(r["x"]), float(r["y"]), float(r["z"]))
        for a in range(3):
            if xyz[a] < mins[a]:
                mins[a] = xyz[a]
            if xyz[a] > maxs[a]:
                maxs[a] = xyz[a]
        t = TrajectoryRepository.parse_timestamp(r["timestamp"])
        idx = int((t - base).total_seconds() // chunk_seconds)
        buckets.setdefault(idx, []).append(r)

    manifest_chunks: List[dict] = []
    total = 0
    for idx in sorted(buckets):
        crows = buckets[idx]
        c_start = crows[0]["timestamp"]
        c_end = crows[-1]["timestamp"]
        start_ms = to_epoch_ms(TrajectoryRepository.parse_timestamp(c_start))
        key = chunk_object_key(start_ms, fmt)
        payload, content_type = _encode_chunk(crows, fmt)
        uri = join_uri(dataset_uri, key)
        from_uri(uri).put_bytes(uri, payload, content_type=content_type)
        manifest_chunks.append({"key": key, "start": c_start, "end": c_end, "rows": len(crows)})
        total += len(crows)
    return manifest_chunks, total, sorted(objids), mins, maxs


def append_rows(
    rows: Iterable[dict],
    dataset_uri: str,
    *,
    chunk_seconds: Optional[int] = None,
    fmt: Optional[str] = None,
) -> dict:
    """기존 데이터셋에 rows를 **추가** 적재하고 manifest를 병합 갱신한다.

    - 시각은 조작 없이 그대로 보존 — 이벤트 인덱스(vlm_events)의 절대 시각과
      궤적이 일치해야 검색→점프→재연이 성립한다. 기존 청크와의 공백은 재생기의
      공백 점프(next_data_time)가 처리하므로 연속일 필요 없음.
    - 기존 청크와 시간이 겹치면 거부(같은 데이터 재적재 방지).
    - manifest가 없으면 ingest_rows로 새 데이터셋 생성.
    - 교체 전 이전 manifest를 manifest.json.bak으로 백업.
    """
    from ..storage import from_uri

    dataset_uri = dataset_uri.rstrip("/")
    muri = manifest_uri(dataset_uri)
    adapter = from_uri(muri)
    if not adapter.exists(muri):
        return ingest_rows(rows, dataset_uri,
                           chunk_seconds=chunk_seconds or 300, fmt=fmt or "parquet")
    with adapter.open_read(muri) as fh:
        old = json.loads(fh.read().decode("utf-8"))

    chunk_seconds = int(chunk_seconds or old.get("chunk_seconds") or 300)
    fmt = (fmt or old.get("format") or "parquet").lower()

    rows = sorted(rows, key=lambda r: r["timestamp"])
    if not rows:
        raise ValueError("append_rows: no rows to append")

    parse = TrajectoryRepository.parse_timestamp
    new_start, new_end = parse(rows[0]["timestamp"]), parse(rows[-1]["timestamp"])
    for c in old.get("chunks", []):
        if parse(c["start"]) <= new_end and new_start <= parse(c["end"]):
            raise ValueError(
                f"append_rows: 시간 겹침 — 기존 청크 {c['key']} ({c['start']}..{c['end']}) 와 "
                f"신규 rows ({rows[0]['timestamp']}..{rows[-1]['timestamp']}) 교차. "
                "같은 데이터 재적재인지 확인할 것.")

    new_chunks, total, objids, mins, maxs = _bucket_and_upload(
        rows, dataset_uri, chunk_seconds, fmt)

    chunks = sorted(old.get("chunks", []) + new_chunks, key=lambda c: parse(c["start"]))
    old_min, old_max = old.get("coord_min"), old.get("coord_max")
    manifest = dict(old)
    manifest.update({
        "objids": sorted(set(old.get("objids", [])) | set(objids)),
        "start": chunks[0]["start"],
        "end": max(chunks, key=lambda c: parse(c["end"]))["end"],
        "rows": int(old.get("rows", 0)) + total,
        "coord_min": [min(a, b) for a, b in zip(old_min, mins)] if old_min else mins,
        "coord_max": [max(a, b) for a, b in zip(old_max, maxs)] if old_max else maxs,
        "chunks": chunks,
    })
    # 트랙 범위 병합 — old에 tracks가 있을 때만. 레거시(무-tracks) manifest에 신규
    # rows의 부분 범위만 기록하면 기존 트랙 구간이 잘려 despawn 오판을 만들므로,
    # 레거시는 병합 후에도 무-tracks(리더의 전체 범위 폴백) 상태를 유지한다.
    # 시각 문자열은 고정 폭 포맷이라 사전순 비교 = 시간순 비교로 안전.
    old_tracks = old.get("tracks")
    if old_tracks is not None:
        merged = {k: list(v) for k, v in old_tracks.items()}
        for oid, span in _track_time_ranges(rows).items():
            cur = merged.get(oid)
            merged[oid] = span if cur is None else [min(cur[0], span[0]), max(cur[1], span[1])]
        manifest["tracks"] = merged
    else:
        manifest.pop("tracks", None)

    bak_uri = muri + ".bak"
    from_uri(bak_uri).put_bytes(
        bak_uri, json.dumps(old, ensure_ascii=False, indent=2).encode("utf-8"),
        content_type="application/json")
    from_uri(muri).put_bytes(
        muri, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        content_type="application/json")
    return manifest


def ingest_rows(
    rows: Iterable[dict],
    dataset_uri: str,
    *,
    chunk_seconds: int = 60,
    fmt: str = "csv",
    hz: Optional[float] = None,
    dataset: str = "",
) -> dict:
    """rows를 시간 단위 청크로 분할해 dataset_uri 하위에 업로드하고 manifest를 쓴다.

    dataset_uri: 's3://bucket/trajectory/ds1' 또는 'file:///tmp/lake/ds1'.
    반환: 작성한 manifest dict.
    """
    from ..storage import from_uri

    fmt = fmt.lower()
    dataset_uri = dataset_uri.rstrip("/")
    rows = sorted(rows, key=lambda r: r["timestamp"])
    if not rows:
        raise ValueError("ingest_rows: no rows to ingest")

    manifest_chunks, total, objids, mins, maxs = _bucket_and_upload(
        rows, dataset_uri, chunk_seconds, fmt)

    manifest = {
        "version": 1,
        "dataset": dataset,
        "hz": hz,
        "chunk_seconds": chunk_seconds,
        "format": fmt,
        "objids": sorted(objids),
        "start": rows[0]["timestamp"],
        "end": rows[-1]["timestamp"],
        "rows": total,
        "coord_min": mins,
        "coord_max": maxs,
        "tracks": _track_time_ranges(rows),
        "chunks": manifest_chunks,
    }
    muri = manifest_uri(dataset_uri)
    from_uri(muri).put_bytes(
        muri, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"), content_type="application/json"
    )
    return manifest


def ingest_synthetic(
    dataset_uri: str,
    *,
    n_objects: int = 10,
    duration_s: float = 60.0,
    hz: float = 5.0,
    chunk_seconds: int = 60,
    fmt: str = "csv",
    seed: int = 42,
) -> dict:
    rows = generate_synthetic_rows(n_objects, duration_s, hz=hz, seed=seed)
    return ingest_rows(rows, dataset_uri, chunk_seconds=chunk_seconds, fmt=fmt, hz=hz, dataset="synthetic")
