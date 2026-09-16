"""객체 수 스케일 스윕용 합성 레이크 데이터셋 빌드 (레이크성능_실험설계.md §1-1).

객체 수 N만 바꾼 데이터셋 `synth_bev_{N}obj_10hz_30min_v1_c{60,300}`을 만든다. 다른 축
(10Hz·스팬·parquet·chunk_seconds)은 본 데이터셋 `aigrad_bev_10hz_30min_v1`과 같게 두어
관측된 변화를 객체 수 탓으로 돌릴 수 있게 한다.

좌표 규약:
  x·z  씬 프로파일(scene_profiles.json)의 아레나 범위 안 bounded random walk.
  y    --y-source 데이터셋(실궤적)의 객체별 y 시계열을 읽어, 합성 객체 k에
       (k mod 실객체수)번 객체의 y를 무작위 시작 위상으로 회전시켜 붙인다. y의
       분포·시간 상관·압축률이 실데이터와 같아진다. --y-source가 비면 y는 프로파일의
       spawn_floor에 고정.
  스팬 정확히 --span-s(기본 1800.0s, 10Hz면 마지막 표본 1799.9s) — 청크 길이의 정수배로
       두어 꼬리 청크가 생기지 않게 한다(본 데이터셋의 1.0s 꼬리가 역재생 lead min을
       만들었던 문제, 설계 §2-B).

사용 (EXT_ROOT에서, minio·pyarrow 있는 환경 — Windows anaconda/Kit python 또는 L40):
  python -m gist.netai.time_travel_summarization.utils.build_lake_synth_dataset \
    --n-objects 4 10 20 30 40 50 [--chunk-seconds 60 300] [--dry-run]

안전장치: 대상 manifest가 이미 있으면 중단(벤치마크 고정 원칙 — 덮어쓰기 금지).
계보: 각 데이터셋 옆 _build.json 사이드카에 생성기 파라미터를 기록.
"""
from __future__ import annotations

import argparse
import datetime
import json
from typing import Dict, List, Optional, Sequence, Tuple

try:
    from ..automation.scene_profiles import load_profile
    from ..playback.lake_common import (
        generate_synthetic_rows, ingest_rows, join_uri, manifest_uri,
    )
    from ..playback.trajectory_repository import TrajectoryRepository
    from ..storage import from_uri
except ImportError:  # 스크립트 직접 실행 지원
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from gist.netai.time_travel_summarization.automation.scene_profiles import load_profile
    from gist.netai.time_travel_summarization.playback.lake_common import (
        generate_synthetic_rows, ingest_rows, join_uri, manifest_uri,
    )
    from gist.netai.time_travel_summarization.playback.trajectory_repository import (
        TrajectoryRepository,
    )
    from gist.netai.time_travel_summarization.storage import from_uri

DEFAULT_ROOT = "s3://time-travel-summarization/trajectory"
DEFAULT_NAME_TEMPLATE = "synth_bev_{n}obj_10hz_30min_v1"
DEFAULT_Y_SOURCE = f"{DEFAULT_ROOT}/aigrad_bev_10hz_30min_v1_c60"


def load_y_series(dataset_uri: str) -> List[List[float]]:
    """실궤적 데이터셋의 청크를 전부 읽어 objid별 y 시계열(시간 오름차순)을 돌려준다."""
    muri = manifest_uri(dataset_uri)
    with from_uri(muri).open_read(muri) as fh:
        manifest = json.loads(fh.read().decode("utf-8"))
    per_obj: Dict[str, List[Tuple[str, float]]] = {}
    for ch in manifest["chunks"]:
        for r in TrajectoryRepository._read_rows(join_uri(dataset_uri, ch["key"])):
            per_obj.setdefault(str(r["objid"]), []).append((str(r["timestamp"]), float(r["y"])))
    series = []
    for objid in sorted(per_obj):
        rows = sorted(per_obj[objid])
        series.append([y for _, y in rows])
    if not series:
        raise SystemExit(f"[build] y-source에 행이 없음: {dataset_uri}")
    return series


def arena_bounds(profile_name: str, y_floor: Optional[float] = None):
    """씬 프로파일 → 축별 (lo, hi). y는 floor 고정(y_series를 쓰면 무시됨)."""
    prof = load_profile(profile_name)
    mins, maxs = prof["coord_min"], prof["coord_max"]
    floor = float(prof.get("spawn_floor", mins[1])) if y_floor is None else float(y_floor)
    return ((float(mins[0]), float(maxs[0])), (floor, floor), (float(mins[2]), float(maxs[2])))


def target_uris(n_objects_list, root, name_template, chunk_seconds):
    return [(n, cs, f"{root.rstrip('/')}/{name_template.format(n=n)}_c{cs}")
            for n in n_objects_list for cs in chunk_seconds]


def check_targets(targets) -> None:
    """대상 manifest가 하나라도 있으면 중단(벤치마크 고정 원칙). y-source 다운로드 전에 부른다."""
    for _, _, duri in targets:
        muri = manifest_uri(duri)
        if from_uri(muri).exists(muri):
            raise SystemExit(f"[build] 중단: {duri} 에 manifest가 이미 있음 — "
                             f"벤치마크 고정 원칙상 덮어쓰지 않는다(새 버전 이름 사용)")


def build_datasets(
    n_objects_list: Sequence[int],
    *,
    root: str = DEFAULT_ROOT,
    name_template: str = DEFAULT_NAME_TEMPLATE,
    chunk_seconds: Sequence[int] = (60, 300),
    hz: float = 10.0,
    span_s: float = 1800.0,
    start: str = "2026-01-01 00:00:00.000",
    bounds_xyz=None,
    y_series: Optional[List[List[float]]] = None,
    y_source: str = "",
    scene_profile: str = "",
    step_units: float = 12.0,
    seed: int = 42,
    fmt: str = "parquet",
    dry_run: bool = False,
) -> List[dict]:
    """N마다 chunk_seconds 별 데이터셋을 적재하고 manifest 목록을 돌려준다(dry_run이면 빈 목록)."""
    n_objects_list = sorted(set(int(n) for n in n_objects_list))  # 중복 N은 같은 URI 덮어쓰기가 되므로 제거
    targets = target_uris(n_objects_list, root, name_template, chunk_seconds)
    check_targets(targets)
    n_steps = int(round(span_s * hz))
    if dry_run:
        for n, cs, duri in targets:
            print(f"[build] DRY-RUN: {n} obj x {n_steps} steps = {n * n_steps} rows -> {duri} "
                  f"({fmt}, chunk_seconds={cs}, {int(-(-span_s // cs))} chunks)")
        return []

    out = []
    for n in n_objects_list:
        # 같은 N의 c60/c300은 같은 rows여야 한다(청크 길이만 다른 동일 데이터) — 한 번 생성.
        rows = list(generate_synthetic_rows(
            n, span_s, hz=hz, start=start, seed=seed, step_units=step_units,
            bounds_xyz=bounds_xyz, y_series=y_series))
        for cs in chunk_seconds:
            duri = f"{root.rstrip('/')}/{name_template.format(n=n)}_c{cs}"
            manifest = ingest_rows(rows, duri, chunk_seconds=cs, fmt=fmt, hz=hz,
                                   dataset=name_template.format(n=n))
            sidecar = {
                "built_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "generator": "lake_common.generate_synthetic_rows",
                "n_objects": n, "hz": hz, "span_s": span_s, "start": start,
                "chunk_seconds": cs, "format": fmt, "seed": seed, "step_units": step_units,
                "scene_profile": scene_profile, "bounds_xyz": bounds_xyz,
                "y_source": y_source, "y_series_objects": len(y_series) if y_series else 0,
                "rows": manifest["rows"], "chunks": len(manifest["chunks"]),
            }
            suri = join_uri(duri, "_build.json")
            from_uri(suri).put_bytes(
                suri, json.dumps(sidecar, ensure_ascii=False, indent=2).encode("utf-8"),
                content_type="application/json")
            last = manifest["chunks"][-1]
            print(f"[build] done: {duri} chunks={len(manifest['chunks'])} rows={manifest['rows']} "
                  f"last_chunk={last['start']}..{last['end']} ({last['rows']} rows)")
            out.append(manifest)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n-objects", type=int, nargs="+", required=True,
                    help="객체 수 격자(예: 4 10 20 30 40 50)")
    ap.add_argument("--root", default=DEFAULT_ROOT, help="데이터셋 루트 URI")
    ap.add_argument("--name-template", default=DEFAULT_NAME_TEMPLATE,
                    help="데이터셋 이름 템플릿({n}=객체 수, 청크 접미 _c{cs}는 자동)")
    ap.add_argument("--chunk-seconds", type=int, nargs="+", default=[60, 300])
    ap.add_argument("--hz", type=float, default=10.0)
    ap.add_argument("--span-s", type=float, default=1800.0,
                    help="스팬(초). 청크 길이의 정수배로 두어 꼬리 청크를 만들지 않는다")
    ap.add_argument("--start", default="2026-01-01 00:00:00.000")
    ap.add_argument("--scene-profile", default="aigrad_building_v1",
                    help="x·z 아레나 범위를 가져올 씬 프로파일 이름")
    ap.add_argument("--y-source", default=DEFAULT_Y_SOURCE,
                    help="y 시계열을 가져올 실궤적 데이터셋 URI. 빈 문자열이면 spawn_floor 고정")
    ap.add_argument("--step-units", type=float, default=12.0, help="샘플당 최대 이동(스테이지 단위)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--format", default="parquet", choices=("parquet", "csv"))
    ap.add_argument("--dry-run", action="store_true", help="업로드 없이 계획만 출력")
    args = ap.parse_args(argv)

    for cs in args.chunk_seconds:
        if abs(args.span_s / cs - round(args.span_s / cs)) > 1e-9:
            print(f"[build] WARN: span {args.span_s}s 가 chunk_seconds {cs}의 정수배가 아님 — "
                  f"꼬리 청크가 생긴다(설계 §1-1)")

    args.n_objects = sorted(set(args.n_objects))
    check_targets(target_uris(args.n_objects, args.root, args.name_template, args.chunk_seconds))
    bounds = arena_bounds(args.scene_profile)
    print(f"[build] arena({args.scene_profile}): x{bounds[0]} y{bounds[1]} z{bounds[2]}")
    y_series = None
    if args.y_source and not args.dry_run:
        y_series = load_y_series(args.y_source)
        print(f"[build] y-source {args.y_source}: {len(y_series)} objects, "
              f"len={[len(s) for s in y_series]}, y range="
              f"{min(min(s) for s in y_series):.2f}..{max(max(s) for s in y_series):.2f}")

    build_datasets(
        args.n_objects, root=args.root, name_template=args.name_template,
        chunk_seconds=args.chunk_seconds, hz=args.hz, span_s=args.span_s, start=args.start,
        bounds_xyz=bounds, y_series=y_series, y_source=args.y_source,
        scene_profile=args.scene_profile, step_units=args.step_units, seed=args.seed,
        fmt=args.format, dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
