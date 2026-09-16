"""객체 수 스윕 후처리 — lake_bench_*.json 여러 개 → N별 표 + 안전계수 + 한계 판정.

레이크성능_실험설계.md §3-1(안전계수)·§4-4(객체 수 축의 판정)를 코드로 옮긴 것.
`tests/lake_benchmark.py --dataset-uri`가 남긴 JSON을 글롭으로 받아, 데이터셋마다
(객체 수 N, chunk_seconds)를 키로 묶고 다음을 낸다.

  A 표   N × chunk: 청크 rows·MB, GET p50/p99/꼬리배율, 디코드 p50·rows당 μs,
         cold seek p50/p99/꼬리배율
  B 표   N × chunk × 시나리오: stalls, lead p50/min, 프리페치 로드 p50, 안전계수
  판정   §4-4의 한계 종류별로 "처음 걸리는 N" 또는 "> 최대 N(격자 내 미도달)"

안전계수(§3-1) = 여유(lead, 초) ÷ 청크 로드 시간 L(초). 분모는
  1순위  같은 런의 prefetch_load_p50_ms(분자와 같은 조건에서 백그라운드가 실제로
         받아 푼 시간, 표본 = 그 런에서 프리페치된 청크 수)
  2순위  같은 데이터셋 A 계층의 cold_seek_p50_ms(정의 동일, 표본 = 청크 수)
  참고   sync_load_p50_ms(웜업 1~2회 표본 — 불안정, 판정에 쓰지 않음)
여유는 p50(정상 상태)과 min(시작 직후 첫 청크)을 각각 쓴다 — 설계 §0-2.

사용:
  python -m gist.netai.time_travel_summarization.utils.lake_scale_report \
      "artifacts/benchmarks/lake_bench_synth_bev_*obj_*.json" \
      "artifacts/benchmarks/lake_bench_aigrad_bev_*.json" [--sf-threshold 10] [--seek-ms 100]

키는 (N, chunk_seconds, 데이터셋 이름)이라 실궤적 N=4와 합성 N=4는 별개 행으로 나란히
놓인다(§1-1 이음매 검사). 같은 키에 결과 파일이 여럿이면 measured_at이 가장 늦은 것을 쓰고
개수를 표시한다. §4-4의 한계 판정은 합성(`{N}obj`) 행만으로 한다. 실용 한계 문턱은
`max(--sf-threshold, 이 세션의 관측 꼬리 배율)` — §3-1 "고정값 10과 세션별 관측 배율 병기".
순수 stdlib.
"""
from __future__ import annotations

import argparse
import glob
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCENARIO_ORDER = ("1x", "5x", "backward", "seek")
_OBJ_RE = re.compile(r"(\d+)obj")


def load_results(patterns: List[str]) -> List[dict]:
    out = []
    for p in patterns:
        hits = sorted(glob.glob(p)) or [p]
        for h in hits:
            path = Path(h)
            if not path.exists() or path.suffix != ".json":
                continue
            d = json.loads(path.read_text(encoding="utf-8"))
            if "transport" not in d or "scenarios" not in d:
                continue  # gui_probe 등 다른 JSON
            d["_file"] = path.name
            out.append(d)
    return out


def dataset_label(res: dict) -> str:
    return res.get("dataset_uri", "").rstrip("/").rsplit("/", 1)[-1]


def is_synth(label: str) -> bool:
    return bool(_OBJ_RE.search(label))


def dataset_key(res: dict) -> Tuple[Optional[int], Optional[int], str]:
    """(객체 수, chunk_seconds, 데이터셋 이름). 객체 수는 결과 헤더 → transport → 이름 순.

    0은 "objids가 없는 레거시 manifest"라 값이 아니므로 이름의 `{N}obj`로 폴백한다.
    """
    n = res.get("n_objects") or res.get("transport", {}).get("n_objects")
    label = dataset_label(res)
    if not n:
        m = _OBJ_RE.search(label)
        n = int(m.group(1)) if m else None
    return (int(n) if n else None, res.get("transport", {}).get("chunk_seconds"), label)


def pick_latest(results: List[dict]) -> Dict[Tuple[int, int, str], Tuple[dict, int]]:
    """키별 최신 결과와 그 키의 파일 수."""
    by: Dict[Tuple[int, int, str], List[dict]] = {}
    for r in results:
        k = dataset_key(r)
        if k[0] is None or k[1] is None:
            continue
        by.setdefault(k, []).append(r)
    return {k: (max(v, key=lambda r: r.get("measured_at", "")), len(v)) for k, v in by.items()}


def session_tail_ratio(latest) -> Optional[float]:
    """이 세션에서 관측된 로드 시간 꼬리 배율(max÷p50)의 최댓값 — GET과 cold seek 양쪽."""
    vals = []
    for r, _ in latest.values():
        for v in (r.get("transport", {}).get("get_tail_ratio"), r.get("seek", {}).get("cold_seek_tail_ratio")):
            if v is not None:
                vals.append(float(v))
    return max(vals) if vals else None


def _div(a, b_ms):
    if a is None or b_ms is None or b_ms <= 0:
        return None
    return round(a / (b_ms / 1000.0), 1)


def safety_factors(res: dict) -> List[dict]:
    """시나리오별 안전계수 행. 분모 1순위 prefetch_load_p50, 2순위 cold_seek_p50."""
    cold_p50 = res.get("seek", {}).get("cold_seek_p50_ms")
    rows = []
    for sc in res.get("scenarios", []):
        lead_p50, lead_min = sc.get("prefetch_lead_p50_s"), sc.get("prefetch_lead_min_s")
        pf_p50 = sc.get("prefetch_load_p50_ms")
        rows.append({
            "scenario": sc.get("scenario"),
            "stalls": sc.get("stalls"),
            "warmup": sc.get("warmup_cold_loads"),
            "lead_p50_s": lead_p50, "lead_min_s": lead_min,
            "prefetch_load_p50_ms": pf_p50, "prefetch_load_n": sc.get("prefetch_load_n"),
            "sync_load_p50_ms": sc.get("sync_load_p50_ms"),
            "cold_seek_p50_ms": cold_p50,
            "sf_p50_pf": _div(lead_p50, pf_p50), "sf_min_pf": _div(lead_min, pf_p50),
            "sf_p50_cold": _div(lead_p50, cold_p50), "sf_min_cold": _div(lead_min, cold_p50),
            "seek_p50_ms": sc.get("seek_p50_ms"), "seek_p99_ms": sc.get("seek_p99_ms"),
        })
    return rows


def primary_sf(row: dict, which: str = "p50") -> Optional[float]:
    """판정에 쓰는 안전계수 — 1순위 분모가 없으면(프리페치 0회 런) 2순위로 폴백."""
    v = row.get(f"sf_{which}_pf")
    return v if v is not None else row.get(f"sf_{which}_cold")


def judge(latest: Dict[Tuple[int, int, str], Tuple[dict, int]],
          sf_threshold: float = 10.0, seek_ms: float = 100.0) -> List[dict]:
    """§4-4 한계 판정 — 합성(`{N}obj`) 행만 대상. 항목마다 처음 걸리는 N(없으면 None)과
    검사한 최대 N을 돌려준다. 합성 행이 하나도 없으면 전체를 대상으로 한다."""
    synth = {k: v for k, v in latest.items() if is_synth(k[2])} or latest
    by_nc: Dict[Tuple[int, int], dict] = {}
    for (n, cs, label), (r, _) in sorted(synth.items()):
        by_nc.setdefault((n, cs), r)  # 같은 (N, chunk)에 합성이 둘이면 이름순 첫 것
    ns = sorted({k[0] for k in by_nc})
    chunks = sorted({k[1] for k in by_nc})
    out = []
    for cs in chunks:
        # 탐색 응답 한계(A): cold seek p99 > seek_ms
        first = next((n for n in ns if (n, cs) in by_nc and
                      (by_nc[(n, cs)].get("seek", {}).get("cold_seek_p99_ms") or 0) > seek_ms), None)
        out.append({"limit": "탐색 응답 한계(cold seek p99 > %gms)" % seek_ms, "chunk_seconds": cs,
                    "scenario": "-", "first_n": first, "max_n": max(n for n in ns if (n, cs) in by_nc)})
        for scn in SCENARIO_ORDER:
            if scn == "seek":
                continue
            data_n, hard_n, checked = None, None, []
            for n in ns:
                if (n, cs) not in by_nc:
                    continue
                rows = {r["scenario"]: r for r in safety_factors(by_nc[(n, cs)])}
                if scn not in rows:
                    continue
                checked.append(n)
                sf = primary_sf(rows[scn], "p50")
                if data_n is None and sf is not None and sf < sf_threshold:
                    data_n = n
                if hard_n is None and (rows[scn]["stalls"] or 0) > 0:
                    hard_n = n
            if checked:
                out.append({"limit": "데이터 경로 실용 한계(안전계수 p50 < %g)" % sf_threshold,
                            "chunk_seconds": cs, "scenario": scn, "first_n": data_n, "max_n": max(checked)})
                out.append({"limit": "데이터 경로 경성 한계(stall > 0)", "chunk_seconds": cs,
                            "scenario": scn, "first_n": hard_n, "max_n": max(checked)})
    return out


def _f(v, nd=None):
    if v is None:
        return "-"
    return f"{v:.{nd}f}" if nd is not None and isinstance(v, (int, float)) else str(v)


def format_a_table(latest) -> str:
    head = ["N", "chunk_s", "dataset", "runs", "rows/chunk", "MB/chunk", "GET p50/p99 (ms)", "GET tail(max÷p50)",
            "decode p50 (ms)", "decode us/row", "cold seek p50/p99 (ms)", "cold tail", "warm seek p50 (us)"]
    lines = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    for (n, cs, label) in sorted(latest):
        r, cnt = latest[(n, cs, label)]
        t, sk = r["transport"], r.get("seek", {})
        lines.append(
            f"| {n} | {cs} | {label} | {cnt} | {_f(t.get('chunk_mean_rows'))} | {_f(t.get('chunk_mean_mb'))} | "
            f"{_f(t.get('get_p50_ms'))} / {_f(t.get('get_p99_ms'))} | {_f(t.get('get_tail_ratio'))} | "
            f"{_f(t.get('decode_p50_ms'))} | {_f(t.get('decode_us_per_row'))} | "
            f"{_f(sk.get('cold_seek_p50_ms'))} / {_f(sk.get('cold_seek_p99_ms'))} | "
            f"{_f(sk.get('cold_seek_tail_ratio'))} | {_f(sk.get('warm_seek_p50_us'))} |")
    return "\n".join(lines)


def format_b_table(latest) -> str:
    head = ["N", "chunk_s", "dataset", "scenario", "stalls", "warmup", "lead p50/min (s)",
            "prefetch load p50 (ms, n)", "cold seek p50 (ms)", "sync p50 (ms, 참고)",
            "SF p50 / min (분모=prefetch)", "SF p50 / min (분모=cold seek)", "seek p50/p99 (ms)"]
    lines = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    for (n, cs, label) in sorted(latest):
        r, _ = latest[(n, cs, label)]
        for row in safety_factors(r):
            seekcol = (f"{_f(row['seek_p50_ms'])} / {_f(row['seek_p99_ms'])}"
                       if row.get("seek_p50_ms") is not None else "-")
            lines.append(
                f"| {n} | {cs} | {label} | {row['scenario']} | {_f(row['stalls'])} | {_f(row['warmup'])} | "
                f"{_f(row['lead_p50_s'])} / {_f(row['lead_min_s'])} | "
                f"{_f(row['prefetch_load_p50_ms'])} ({_f(row['prefetch_load_n'])}) | "
                f"{_f(row['cold_seek_p50_ms'])} | {_f(row['sync_load_p50_ms'])} | "
                f"{_f(row['sf_p50_pf'])} / {_f(row['sf_min_pf'])} | "
                f"{_f(row['sf_p50_cold'])} / {_f(row['sf_min_cold'])} | {seekcol} |")
    return "\n".join(lines)


def format_verdict(judged: List[dict]) -> str:
    lines = ["| 한계 종류 | chunk_s | scenario | 처음 걸리는 N |", "|---|---|---|---|"]
    for j in judged:
        first = j["first_n"] if j["first_n"] is not None else f"> {j['max_n']} (격자 내 미도달)"
        lines.append(f"| {j['limit']} | {j['chunk_seconds']} | {j['scenario']} | {first} |")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+", help="lake_bench_*.json (glob 허용)")
    ap.add_argument("--sf-threshold", type=float, default=10.0,
                    help="실용 한계 안전계수 문턱(기본 10 — 2026-08-04 관측 꼬리 배율 11.6에서 유래)")
    ap.add_argument("--seek-ms", type=float, default=100.0, help="탐색 응답 한계(cold seek p99)")
    args = ap.parse_args(argv)

    results = load_results(args.paths)
    if not results:
        raise SystemExit("no lake_bench results")
    latest = pick_latest(results)
    if not latest:
        raise SystemExit("no results with (n_objects, chunk_seconds)")

    tail = session_tail_ratio(latest)
    threshold = max(args.sf_threshold, tail) if tail is not None else args.sf_threshold
    print("## A. 전송·탐색 (N × chunk × dataset)\n")
    print(format_a_table(latest))
    print("\n## B. 연속 재생·안전계수 (N × chunk × dataset × 시나리오)\n")
    print(format_b_table(latest))
    print(f"\n## 판정 (설계 §4-4 — 합성 행만, 안전계수 문턱 {threshold:g} = max(고정 {args.sf_threshold:g}, "
          f"세션 관측 꼬리 배율 {_f(tail)}), 탐색 {args.seek_ms:g}ms)\n")
    print(format_verdict(judge(latest, threshold, args.seek_ms)))


if __name__ == "__main__":
    main()
