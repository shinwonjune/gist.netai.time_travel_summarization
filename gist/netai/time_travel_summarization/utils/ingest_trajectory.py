"""trace CSV → 데이터레이크 적재 CLI — lake 쓰기 계약(lake_common)의 소스 어댑터.

배포 가정("CCTV 트래커가 좌표를 parquet으로 축적")의 쓰기 경로를 시뮬레이션
소스(trace CSV)로 채우는 참조 구현. 소스가 바뀌어도(트래커 스트림 등) 핵심부
(lake_common.append_rows)는 그대로고, 이 파일의 수집부만 교체 지점이다.

사용 (EXT_ROOT에서, minio·pyarrow 있는 환경 — L40 venv):
  python -m gist.netai.time_travel_summarization.utils.ingest_trajectory \
    --dataset-uri s3://time-travel-summarization/trajectory/living_trajectory_1h_0_2s_parquet \
    --run s3://time-travel-summarization/episodes/gen-20260718-153511
  # 개별 지정: --csv <uri|로컬경로> (반복 가능). --dry-run으로 계획만 확인.

안전장치:
  - 기존 청크와 시간 겹침 거부 (append_rows — 같은 데이터 재적재 방지)
  - 입력 CSV들끼리의 시간 겹침 거부 (동일 objid가 같은 시각에 두 좌표를 갖게 됨)
  - wall-clock 시절 trace(스팬 ≫ 영상 길이) 감지 시 제외 (--force로 강행)
"""
from __future__ import annotations

import argparse
import json
from typing import List, Optional, Tuple

try:
    from ..playback.lake_common import append_rows, dataset_uri_from_manifest
    from ..playback.trajectory_repository import TrajectoryRepository
    from ..storage import from_uri
except ImportError:  # 스크립트 직접 실행(python utils/ingest_trajectory.py) 지원
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from gist.netai.time_travel_summarization.playback.lake_common import (
        append_rows, dataset_uri_from_manifest,
    )
    from gist.netai.time_travel_summarization.playback.trajectory_repository import (
        TrajectoryRepository,
    )
    from gist.netai.time_travel_summarization.storage import from_uri


def normalize_uri(path_or_uri: str) -> str:
    if "://" in path_or_uri:
        return path_or_uri
    from pathlib import Path
    return Path(path_or_uri).resolve().as_uri()


def collect_run_csvs(run_uri: str) -> List[str]:
    """run 루트(episodes/gen-XXX) 아래 ep_*/의 trace CSV들을 수집."""
    prefix = run_uri.rstrip("/") + "/"
    adapter = from_uri(prefix)
    out = [i.uri for i in adapter.list_prefix(prefix, recursive=True)
           if "_trace_" in i.uri.rsplit("/", 1)[-1] and i.uri.endswith(".csv")]
    return sorted(out)


def _sidecar_duration_s(csv_uri: str) -> Optional[float]:
    """trace CSV 옆 영상 사이드카(_video_NNNN.meta.json)의 duration_s. 없으면 None."""
    name = csv_uri.rsplit("/", 1)[-1]
    if "_trace_" not in name:
        return None
    meta_uri = csv_uri.rsplit("/", 1)[0] + "/" + \
        name.replace("_trace_", "_video_").rsplit(".", 1)[0] + ".meta.json"
    try:
        adapter = from_uri(meta_uri)
        if not adapter.exists(meta_uri):
            return None
        with adapter.open_read(meta_uri) as fh:
            return float(json.loads(fh.read().decode("utf-8")).get("duration_s"))
    except Exception:
        return None


def load_trace(csv_uri: str, force: bool = False) -> Tuple[List[dict], str]:
    """CSV 로드 + 스팬 검사. (rows, 상태 메시지) 반환 — 제외 시 rows=[].

    wall-clock 시절 trace(sim-클럭 수정 6e1f3b9 이전)는 스팬이 영상 길이보다
    수 배 늘어져 있어 재연 속도·라벨 정합이 깨진다 → 기본 제외.
    """
    rows = TrajectoryRepository._read_rows(csv_uri)
    if not rows:
        return [], "empty"
    parse = TrajectoryRepository.parse_timestamp
    span = (parse(rows[-1]["timestamp"]) - parse(rows[0]["timestamp"])).total_seconds()
    dur = _sidecar_duration_s(csv_uri)
    if dur and span > dur * 1.3 and not force:
        return [], (f"SKIP wall-clock trace (span {span:.1f}s > 영상 {dur:.0f}s — "
                    f"sim-클럭 수정 이전 산출물, --force로 강행 가능)")
    return rows, f"{len(rows)} rows, span {span:.1f}s"


def check_input_overlaps(spans: List[Tuple[str, object, object]]) -> None:
    """입력 CSV들끼리 시간 교차 검사. spans: (uri, start_dt, end_dt)."""
    s = sorted(spans, key=lambda x: x[1])
    for a, b in zip(s, s[1:]):
        if b[1] <= a[2]:  # 다음 시작 ≤ 이전 끝
            raise SystemExit(
                f"입력 겹침: {a[0].rsplit('/', 1)[-1]} ({a[1]}..{a[2]}) ↔ "
                f"{b[0].rsplit('/', 1)[-1]} ({b[1]}..{b[2]}) — 에피소드를 나눠 적재할 것")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset-uri", required=True,
                    help="적재 대상 데이터셋(또는 manifest.json) URI")
    ap.add_argument("--csv", action="append", default=[],
                    help="trace CSV (uri 또는 로컬 경로, 반복 가능)")
    ap.add_argument("--run", help="episodes run 루트 — ep_*/ trace CSV 자동 수집")
    ap.add_argument("--chunk-seconds", type=int, default=None,
                    help="청크 길이(기본: 기존 manifest 값, 신규면 300)")
    ap.add_argument("--force", action="store_true",
                    help="wall-clock trace 스팬 검사를 무시하고 포함")
    ap.add_argument("--dry-run", action="store_true", help="업로드 없이 계획만 출력")
    args = ap.parse_args()

    csv_uris = [normalize_uri(c) for c in args.csv]
    if args.run:
        found = collect_run_csvs(normalize_uri(args.run))
        print(f"[ingest] run 스캔: {len(found)}개 trace CSV")
        csv_uris += found
    if not csv_uris:
        raise SystemExit("입력 없음 — --csv 또는 --run 지정")

    parse = TrajectoryRepository.parse_timestamp
    all_rows: List[dict] = []
    spans = []
    for uri in csv_uris:
        rows, msg = load_trace(uri, force=args.force)
        print(f"[ingest] {uri.rsplit('/', 1)[-1]}: {msg}")
        if rows:
            spans.append((uri, parse(rows[0]["timestamp"]), parse(rows[-1]["timestamp"])))
            all_rows += rows
    if not all_rows:
        raise SystemExit("적재할 rows 없음")
    check_input_overlaps(spans)

    dataset_uri = dataset_uri_from_manifest(args.dataset_uri)
    if args.dry_run:
        all_rows.sort(key=lambda r: r["timestamp"])
        print(f"[ingest] DRY-RUN: {len(all_rows)} rows "
              f"({all_rows[0]['timestamp']} .. {all_rows[-1]['timestamp']}) -> {dataset_uri}")
        return

    manifest = append_rows(all_rows, dataset_uri, chunk_seconds=args.chunk_seconds)
    print(f"[ingest] done: dataset={dataset_uri}")
    print(f"[ingest] manifest: chunks={len(manifest['chunks'])} rows={manifest['rows']} "
          f"range={manifest['start']} .. {manifest['end']}")


if __name__ == "__main__":
    main()
