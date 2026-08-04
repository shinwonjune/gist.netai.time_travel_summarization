"""레이크 성능 실험용 데이터셋 빌드 (레이크성능_실험설계.md §1).

프로드 에피소드 trace(60Hz 물리 기록)를 시간축에 연속 배치(rebase)해 ~30분
스팬을 만들고, 시간 기반 다운샘플러(perturbation.perturb.downsample — 소스
주기 무관)로 목표 Hz를 추출한 뒤, lake_common 적재 경로로 parquet 업로드한다.
chunk_seconds {60, 300} 두 벌을 만들어 청크 교차율(=stall 기회)을 대조한다.

산출 데이터셋 URI (명명 규약 {scene}_{content}_{hz}hz_{span}_v{n} + 청크 접미):
  {root}/{name}_c60   {root}/{name}_c300

계보 기록: 각 데이터셋 옆에 _build.json 사이드카(소스 CSV 목록·rebase 파라미터).

사용 (EXT_ROOT에서, minio·pyarrow 있는 환경 — Windows Kit python 또는 L40 venv):
  python -m gist.netai.time_travel_summarization.utils.build_lake_perf_dataset \
    --run s3://time-travel-summarization/episodes/gen-20260718-153511 \
    [--run s3://.../gen-... ...] [--dry-run]

안전장치:
  - 대상 manifest가 이미 있으면 중단(벤치마크 고정 원칙 — 덮어쓰기 금지).
    재빌드하려면 새 버전 이름(_v2)을 쓰거나 기존 데이터셋을 수동 삭제할 것.
  - wall-clock 시절 trace는 ingest_trajectory.load_trace가 걸러낸다(--force로 강행).
"""
from __future__ import annotations

import argparse
import datetime
import json
from typing import List

try:
    from ..perturbation.perturb import downsample
    from ..playback.lake_common import ingest_rows, join_uri, manifest_uri
    from ..playback.trajectory_repository import TrajectoryRepository
    from ..storage import from_uri
    from .ingest_trajectory import collect_run_csvs, load_trace, normalize_uri
except ImportError:  # 스크립트 직접 실행 지원
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from gist.netai.time_travel_summarization.perturbation.perturb import downsample
    from gist.netai.time_travel_summarization.playback.lake_common import (
        ingest_rows, join_uri, manifest_uri,
    )
    from gist.netai.time_travel_summarization.playback.trajectory_repository import (
        TrajectoryRepository,
    )
    from gist.netai.time_travel_summarization.storage import from_uri
    from gist.netai.time_travel_summarization.utils.ingest_trajectory import (
        collect_run_csvs, load_trace, normalize_uri,
    )

_parse = TrajectoryRepository.parse_timestamp
_format = TrajectoryRepository.format_timestamp


def rebase_episodes(
    episodes: List[List[dict]],
    base_start: datetime.datetime,
    gap_s: float,
    target_span_s: float,
):
    """에피소드별 rows(timestamp 문자열)를 base_start부터 연속 배치.

    각 에피소드의 첫 시각이 커서에 오도록 전체 시프트하고, 커서는
    (에피소드 스팬 + gap_s)만큼 전진한다. 누적 스팬이 target_span_s에
    도달하면 이후 에피소드는 버린다(초과분 포함 에피소드까지는 유지).
    반환 rows의 timestamp는 재배치된 문자열, 원본은 조작하지 않는다.
    """
    cursor = base_start
    out: List[dict] = []
    used = 0
    for rows in episodes:
        if (cursor - base_start).total_seconds() >= target_span_s:
            break
        t0 = _parse(rows[0]["timestamp"])
        t1 = _parse(rows[-1]["timestamp"])
        offset = cursor - t0
        for r in rows:
            nr = dict(r)
            nr["timestamp"] = _format(_parse(r["timestamp"]) + offset)
            out.append(nr)
        cursor += (t1 - t0) + datetime.timedelta(seconds=gap_s)
        used += 1
    return out, used


def downsample_rows(rows: List[dict], hz: float) -> List[dict]:
    """timestamp 문자열 rows -> perturb.downsample(Row: t=datetime) 왕복 어댑터."""
    conv = [{"t": _parse(r["timestamp"]), "objid": r["objid"],
             "x": float(r["x"]), "y": float(r["y"]), "z": float(r["z"])} for r in rows]
    kept = downsample(conv, hz)
    return [{"timestamp": _format(r["t"]), "objid": r["objid"],
             "x": r["x"], "y": r["y"], "z": r["z"]} for r in kept]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", action="append", default=[],
                    help="episodes run 루트(s3://... 또는 로컬) — ep_*/ trace CSV 자동 수집, 반복 가능")
    ap.add_argument("--csv", action="append", default=[], help="개별 trace CSV(반복 가능)")
    ap.add_argument("--root", default="s3://time-travel-summarization/trajectory",
                    help="데이터셋 루트 URI")
    ap.add_argument("--name", default="aigrad_bev_10hz_30min_v1",
                    help="데이터셋 이름({scene}_{content}_{hz}hz_{span}_v{n})")
    ap.add_argument("--hz", type=float, default=10.0, help="목표 좌표 주기(기본 10Hz)")
    ap.add_argument("--target-span-s", type=float, default=1800.0, help="목표 스팬(기본 30분)")
    ap.add_argument("--gap-s", type=float, default=0.1,
                    help="에피소드 사이 간격(기본 0.1s — 동일 시각 충돌 방지, 공백 점프 미발동)")
    ap.add_argument("--repeat", type=int, default=1,
                    help="에피소드 시퀀스 반복 횟수 — 가용 에피소드 총 스팬이 목표에 못 미칠 때 "
                         "타일링으로 채운다(성능 측정엔 rows/s 밀도·청크 수가 중요하지 궤적 "
                         "유일성이 아님). 반복 사실은 _build.json에 기록된다")
    ap.add_argument("--start", default="2026-01-01 00:00:00.000", help="재배치 기준 시각")
    ap.add_argument("--chunk-seconds", type=int, nargs="+", default=[60, 300],
                    help="청크 길이 목록 — 각 값마다 {name}_c{v} 데이터셋 생성")
    ap.add_argument("--force", action="store_true", help="wall-clock trace 스팬 검사 무시")
    ap.add_argument("--dry-run", action="store_true", help="업로드 없이 계획만 출력")
    args = ap.parse_args(argv)

    # 1) 소스 수집
    csv_uris = [normalize_uri(c) for c in args.csv]
    for run in args.run:
        found = collect_run_csvs(normalize_uri(run))
        print(f"[build] run 스캔 {run}: {len(found)}개 trace CSV")
        csv_uris += found
    if not csv_uris:
        raise SystemExit("입력 없음 — --run 또는 --csv 지정")

    episodes: List[List[dict]] = []
    for uri in csv_uris:
        rows, msg = load_trace(uri, force=args.force)
        print(f"[build] {uri.rsplit('/', 1)[-1]}: {msg}")
        if rows:
            episodes.append(sorted(rows, key=lambda r: r["timestamp"]))

    if not episodes:
        raise SystemExit("사용 가능한 에피소드 없음")

    # 2) 시간축 연속 배치 + 다운샘플
    base = _parse(args.start)
    if args.repeat > 1:
        episodes = episodes * args.repeat
        print(f"[build] repeat x{args.repeat}: 배치 후보 {len(episodes)} 에피소드")
    placed, used = rebase_episodes(episodes, base, args.gap_s, args.target_span_s)
    span = (_parse(placed[-1]["timestamp"]) - _parse(placed[0]["timestamp"])).total_seconds()
    print(f"[build] 배치: 에피소드 {used}/{len(episodes)}개, {len(placed)} rows, span {span:.1f}s")
    if span < args.target_span_s * 0.9:
        print(f"[build] WARN: span {span:.0f}s < 목표 {args.target_span_s:.0f}s의 90% — "
              f"에피소드(run)를 더 지정할 것")

    sampled = downsample_rows(placed, args.hz)
    eff_hz = len(sampled) / max(span, 1e-9) / max(len({r['objid'] for r in sampled}), 1)
    print(f"[build] 다운샘플 {args.hz}Hz: {len(placed)} -> {len(sampled)} rows "
          f"(실효 {eff_hz:.2f}Hz/object)")

    # 3) chunk_seconds별 적재
    targets = [(cs, f"{args.root.rstrip('/')}/{args.name}_c{cs}") for cs in args.chunk_seconds]
    for cs, duri in targets:
        muri = manifest_uri(duri)
        if from_uri(muri).exists(muri):
            raise SystemExit(f"[build] 중단: {duri} 에 manifest가 이미 있음 — "
                             f"벤치마크 고정 원칙상 덮어쓰지 않는다(새 버전 이름 사용)")

    if args.dry_run:
        for cs, duri in targets:
            print(f"[build] DRY-RUN: {len(sampled)} rows -> {duri} "
                  f"(parquet, chunk_seconds={cs}, ~{int(span // cs) + 1} chunks)")
        return

    for cs, duri in targets:
        manifest = ingest_rows(sampled, duri, chunk_seconds=cs, fmt="parquet",
                               hz=args.hz, dataset=args.name)
        sidecar = {
            "built_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "sources": csv_uris,
            "repeat": args.repeat,
            "episodes_used": used,
            "base_start": args.start,
            "gap_s": args.gap_s,
            "target_span_s": args.target_span_s,
            "hz": args.hz,
            "chunk_seconds": cs,
            "rows": manifest["rows"],
        }
        suri = join_uri(duri, "_build.json")
        from_uri(suri).put_bytes(
            suri, json.dumps(sidecar, ensure_ascii=False, indent=2).encode("utf-8"),
            content_type="application/json")
        print(f"[build] done: {duri} chunks={len(manifest['chunks'])} rows={manifest['rows']} "
              f"range={manifest['start']} .. {manifest['end']}")


if __name__ == "__main__":
    main()
