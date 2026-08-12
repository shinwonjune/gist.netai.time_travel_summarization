"""GUI probe 덤프(JSON) → 구간별 지표 표 후처리 (레이크성능_실험설계.md §2-C).

한 덤프 안에는 성격이 전혀 다른 세 종류의 프레임이 섞여 있다. 이것을 하나로 뭉쳐
평균을 내면 어느 쪽도 설명하지 못한다. 2026-08-13에 덤프 전체를 세 갈래로 나눠
세어 본 결과가 그 근거다 — 재생 61,008프레임에서 stall 0건, 탐색 3,656프레임에서
stall 1,941건(53.1%), idle 31,494,012프레임에서 stall 0건. 즉 재생과 탐색은 성능
성격이 정반대이고, idle은 두 구간의 비율을 희석시키기만 한다.

구간의 정의(계측 포맷을 바꾸지 않고 후처리만으로 유도된다 — playing 플래그와
twin_time 문자열만 있으면 된다):

  재생(playback)  playing=True인 프레임. 트윈 시계가 실시간에 묶여 흐르는 구간이다.
  탐색(seek)      playing=False인데 직전 프레임과 twin_time이 달라진 프레임.
                  슬라이더 스크럽이나 이벤트 점프처럼 사용자가 시간축을 건너뛴
                  순간이며, 캐시 밖 청크를 새로 부르는 것이 정상 동작이다.
  idle            playing=False이고 twin_time도 그대로인 프레임. 앱이 떠 있을 뿐
                  아무 일도 일어나지 않은 대기 상태라 성능 판정 대상이 아니다.

지표:
  - frame_interval p50/p95/p99: 렌더 루프의 프레임 주기. frame_interval[i]는
    "직전 프레임에서 이 프레임까지 걸린 시간"이므로, 탐색 프레임에서는 곧 그
    탐색 요청이 화면에 반영되기까지의 응답 지연이다.
  - hitch rate: frame_interval > 2×중앙값인 프레임 비율. GUI는 트윈 시계가
    실시간에 묶여 있어 재생 중의 hitch가 곧 사용자 체감 끊김이다.
  - stall frame rate: d_sync>0(레이크 동기 로드가 발생한) 프레임 비율. 구간의 첫
    stall 프레임은 콜드스타트라 웜업으로 따로 센다(판정 규약 §4).
  - stall 프레임의 frame_interval p50/p95/최대: **탐색 구간의 주 판정 지표**.
    탐색에서는 stall이 얼마나 자주 나느냐(빈도)가 아니라 한 번 났을 때 얼마나
    오래 기다리느냐(1회 응답 지연)가 사용자가 겪는 것이기 때문이다.
  - tick_ms p50/p99: controller.update 소요(게이트로 스킵된 프레임 ~0 포함).

사용:
  python -m gist.netai.time_travel_summarization.utils.gui_probe_report \
      artifacts/benchmarks/gui_probe_20260804-*.json [--regime seek]

여러 파일을 주면 (파일 × 구간) 행으로 표를 만들고, 맨 아래에 파일 전체를 합친
구간별 프레임·stall 합계를 덧붙인다. 기본값 --regime all은 세 구간을 모두 보여
준다. 예전의 --all-frames 플래그는 없앴다 — 그 플래그가 하던 일(세 구간을 한
덩어리로 묶어 하나의 비율을 내는 것)이 정확히 이 문서가 틀렸다고 판정한 계산이라,
남겨 두면 잘못된 그림을 만드는 경로만 유지하는 셈이기 때문이다.
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics
from pathlib import Path
from typing import Dict, List

REGIMES = ("playback", "seek", "idle")
JUDGED_REGIMES = ("playback", "seek")  # idle은 판정 대상이 아니다


def _percentile(xs: List[float], q: float) -> float:
    xs = sorted(xs)
    if not xs:
        return 0.0
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def classify_regimes(f: dict) -> Dict[str, List[int]]:
    """프레임 인덱스를 재생/탐색/idle 세 구간으로 나눈다.

    탐색 판정의 기준은 "직전 프레임 대비 twin_time이 바뀌었는가"라서, 버퍼의 첫
    프레임은 비교 대상이 없다. 이때는 판정을 미루고 idle로 둔다. 계측기
    app/lake_probe.py의 record()가 덤프 여부를 정할 때 세는 러닝 카운터도 같은
    규칙을 쓰므로, "덤프된 파일인데 보고에는 아무 구간도 안 잡힌다" 같은 어긋남이
    생기지 않는다.

    twin_time은 None일 수 있어(트윈 시계가 아직 없는 프레임) 값 비교만으로는
    "직전 프레임이 있었는가"를 알 수 없다. 그래서 별도 플래그로 구분한다.
    """
    playing = f["playing"]
    twin = f["twin_time"]
    out: Dict[str, List[int]] = {r: [] for r in REGIMES}
    prev_twin = None
    has_prev = False
    for i in range(len(playing)):
        cur = twin[i]
        if playing[i]:
            out["playback"].append(i)
        elif has_prev and cur != prev_twin:
            out["seek"].append(i)
        else:
            out["idle"].append(i)
        prev_twin, has_prev = cur, True
    return out


def _regime_stats(f: dict, idx: List[int]) -> dict:
    """한 구간(프레임 인덱스 목록)의 지표를 계산한다.

    span_s는 그 구간에 속한 첫 프레임과 마지막 프레임의 벽시계 차이다. 구간이 중간에
    끊겼다 이어질 수 있으므로 "구간에 실제로 머문 시간의 합"이 아니라 "구간이 걸쳐
    있는 범위"임에 주의한다.
    """
    n = len(idx)
    # frame_interval의 첫 프레임 값 0은 직전 프레임이 없어 못 잰 것이라 통계에서 뺀다.
    intervals = [f["frame_interval_ms"][i] for i in idx if f["frame_interval_ms"][i] > 0]
    ticks = [f["tick_ms"][i] for i in idx]
    stall_idx = [i for i in idx if f["d_sync"][i] > 0]
    stall_intervals = [f["frame_interval_ms"][i] for i in stall_idx
                       if f["frame_interval_ms"][i] > 0]

    med = statistics.median(intervals) if intervals else 0.0
    hitches = sum(1 for v in intervals if med > 0 and v > 2 * med)
    warmup = 1 if stall_idx else 0  # 구간 첫 stall = 콜드스타트(불가피)

    return {
        "frames": n,
        "span_s": round(f["wall_ts"][idx[-1]] - f["wall_ts"][idx[0]], 1) if n > 1 else 0.0,
        "interval_p50_ms": round(_percentile(intervals, 0.50), 2),
        "interval_p95_ms": round(_percentile(intervals, 0.95), 2),
        "interval_p99_ms": round(_percentile(intervals, 0.99), 2),
        "hitch_rate_pct": round(100.0 * hitches / max(1, len(intervals)), 2),
        "stall_frames": len(stall_idx),
        "warmup_stall_frames": warmup,
        "stall_frames_post_warmup": max(0, len(stall_idx) - warmup),
        "stall_frame_rate_pct": round(100.0 * len(stall_idx) / max(1, n), 3),
        "stall_interval_p50_ms": round(_percentile(stall_intervals, 0.50), 2),
        "stall_interval_p95_ms": round(_percentile(stall_intervals, 0.95), 2),
        "stall_interval_max_ms": round(max(stall_intervals), 2) if stall_intervals else 0.0,
        "tick_p50_ms": round(_percentile(ticks, 0.50), 3),
        "tick_p99_ms": round(_percentile(ticks, 0.99), 3),
    }


def analyze(path: Path) -> dict:
    """덤프 한 개를 읽어 구간별 지표를 낸다.

    반환값의 "regimes"는 세 구간 모두를 담는다(프레임이 0개인 구간도 자리는 있다).
    """
    d = json.loads(path.read_text(encoding="utf-8"))
    f = d["frames"]
    groups = classify_regimes(f)
    return {
        "file": path.name,
        "reason": d.get("reason"),
        "scenario": d.get("scenario", ""),
        "n_frames": len(f["wall_ts"]),
        "regimes": {r: _regime_stats(f, groups[r]) for r in REGIMES},
    }


_HEAD = ["file", "regime", "frames", "span_s", "interval p50/p95/p99 (ms)", "hitch %",
         "stalls (post-warmup)", "stall %", "stall interval p50/p95/max (ms)",
         "tick p50/p99 (ms)"]


def _row(name: str, regime: str, s: dict) -> str:
    if regime == "idle":
        # idle은 판정 대상이 아니라서 프레임 수만 참고로 싣는다(지표는 빈칸).
        return f"| {name} | idle | {s['frames']} | " + " | ".join("-" for _ in _HEAD[3:]) + " |"
    return (
        f"| {name} | {regime} | {s['frames']} | {s['span_s']} | "
        f"{s['interval_p50_ms']} / {s['interval_p95_ms']} / {s['interval_p99_ms']} | "
        f"{s['hitch_rate_pct']} | "
        f"{s['stall_frames']} ({s['stall_frames_post_warmup']}) | "
        f"{s['stall_frame_rate_pct']} | "
        f"{s['stall_interval_p50_ms']} / {s['stall_interval_p95_ms']} / "
        f"{s['stall_interval_max_ms']} | "
        f"{s['tick_p50_ms']} / {s['tick_p99_ms']} |"
    )


def format_table(reports: List[dict], regimes=REGIMES, show_empty: bool = False) -> str:
    """(파일 × 구간) 표. show_empty=False면 프레임 0개인 구간 행은 생략한다."""
    lines = ["| " + " | ".join(_HEAD) + " |",
             "|" + "|".join("---" for _ in _HEAD) + "|"]
    for rep in reports:
        for r in regimes:
            s = rep["regimes"][r]
            if s["frames"] or show_empty:
                lines.append(_row(rep["file"], r, s))
    return "\n".join(lines)


def format_totals(reports: List[dict], regimes=REGIMES) -> str:
    """파일 전체를 합친 구간별 프레임·stall 합계.

    백분위수는 파일별 값을 평균 낼 수 없으므로(원자료를 다시 모아야 한다) 여기에는
    더하기만으로 정확히 나오는 양 — 프레임 수와 stall 프레임 수 — 만 싣는다.
    """
    out = []
    for r in regimes:
        n = sum(rep["regimes"][r]["frames"] for rep in reports)
        st = sum(rep["regimes"][r]["stall_frames"] for rep in reports)
        pct = round(100.0 * st / n, 1) if n else 0.0
        out.append(f"{r} frames={n} stall={st} ({pct}%)")
    return "합계(파일 전체): " + " | ".join(out)


_VERDICT = """
판정(설계 §4 + 2026-08-13 구간 분리):
  - 재생(playback): stall frame rate = 0(웜업 제외)이고 hitch rate < 1%이면
    "끊김 없음" 합격. 표의 stalls 열 괄호 안 값이 웜업을 뺀 수다.
  - 탐색(seek): stall 빈도는 판정하지 않는다. 시간축을 건너뛰면 캐시 밖 청크를
    새로 부르는 것이 정상 동작이라 stall이 나는 것 자체는 결함이 아니다. 판정
    지표는 stall 프레임의 frame_interval p95 — 한 번의 탐색 요청이 화면에 반영될
    때까지 사용자가 기다린 시간이다.
  - idle(정지 + 트윈 시계 불변): 성능 판정 대상이 아니다. 프레임 수만 참고로 싣는다.
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+", help="gui_probe_*.json (glob 허용)")
    ap.add_argument("--regime", choices=("playback", "seek", "idle", "all"), default="all",
                    help="어느 구간을 표에 실을지. 기본 all은 세 구간을 모두 보여 준다")
    args = ap.parse_args(argv)

    files: List[Path] = []
    for p in args.paths:
        hits = sorted(glob.glob(p))
        files += [Path(h) for h in hits] if hits else [Path(p)]

    reports = []
    for fp in files:
        if not fp.exists():
            print(f"[gui_probe_report] missing: {fp}")
            continue
        reports.append(analyze(fp))

    if not reports:
        raise SystemExit("no probe files")

    regimes = REGIMES if args.regime == "all" else (args.regime,)
    # 구간을 하나로 콕 집어 물었을 때는 그 구간이 0프레임이라는 사실 자체가 답이므로
    # 빈 행도 보여 준다. all일 때는 표가 길어지기만 하므로 생략한다.
    print(format_table(reports, regimes, show_empty=(args.regime != "all")))
    print()
    print(format_totals(reports, regimes))
    print(_VERDICT)


if __name__ == "__main__":
    main()
