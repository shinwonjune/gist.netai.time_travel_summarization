"""Headless batch generator for BEV collision episodes (run via `kit --exec`).

Drives the extension's facade programmatically to produce many episodes without
UI clicks: for each episode it picks 4-6 objects, seed-randomizes their start
positions, runs physics (wander) while capturing offscreen video + a 30Hz trace,
and writes everything into a per-episode folder that `utils.build_dataset` (and
`utils.observability`) consume directly.

Per-episode flow (single Kit session, real-time capture):
    set_active_objects(N) -> apply random positions -> set_physics_mode
    -> start_trace(30Hz) -> start_wander -> run_capture_headless(duration)
    -> stop_wander / stop_trace / set_playback_mode -> organize outputs

Run (headless Kit with the extension enabled):
    kit --no-window --enable <ext> --exec "automation/generate_episodes.py -- \
        --episodes 50 --out artifacts/episodes --duration 40"

near-miss 모드(--near-miss): 짝끼리 접근하다 중심거리 --near-miss-gap 근처에서
흩어지기를 반복 — 접촉이 없으므로 GT 충돌이 0건인 대조 에피소드(VLM이 "가까워졌다"
만으로 충돌을 오탐하는지 보는 시험용). --near-miss-mode로 안무를 고른다: 기본
"swerve"(감속 없이 스침 — GUI 육안 검수에서 "보이지 않는 벽" 인상을 준 v1을 대체),
"stop"은 v1(감속+정지+방향전환) — 감속 단서 vs 근접 단서를 분리해 보는 대조군으로
옵션으로 남긴다. 생성 직후 trace로 자체 검증(어느 모드든 좌표 불변식은 동일).
swerve의 회피 곡선이 얼마나 완만한지는 --near-miss-avoid-frac / --near-miss-turn-radius-frac
/ --near-miss-aim-frac(전부 gap 배수)으로 조정한다 — 미지정 시 환경변수
TTS_NEAR_MISS_AVOID_FRAC 등을 보고, 그것도 없으면 코드 기본값을 쓴다.

조우가 **어디서** 일어나는지의 다양성은 --near-miss-start-jitter(접근 개시 지연) /
--near-miss-speed-min-frac·--near-miss-speed-max-frac(객체별 순항 속도) /
--near-miss-depart-spread(이탈 방향 부채꼴)로 조정한다. 이 셋이 없던 시절에는 짝이
동시에·같은 속도로·서로를 정면 조준해 접근했기 때문에 조우가 두 스폰 위치의 중점
(스폰 구역이 방 중앙 대칭이라 결국 방 중앙)에서 거의 같은 기하로 반복됐고, 그 결과
near-miss 클립들이 강하게 상관되어 유효 표본 수가 명목 개수보다 훨씬 작아졌다.
전부 0(이탈 부채꼴은 0 이하)으로 주면 그 시절 안무로 되돌아간다. 생성된 trace의
조우 분포는 near_miss_diversity(--check-trace 출력에도 같이 찍힌다)로 확인한다.

Pure helpers are importable/testable without Kit:
    python automation/generate_episodes.py --self-test
    python automation/generate_episodes.py --check-trace <trace.csv> --near-miss-gap 95
"""

from __future__ import annotations

import argparse
import datetime
import json
import random
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class EpisodeConfig:
    idx: int
    seed: int
    n_objects: int
    speed: float
    duration: float
    base_time_s: int  # 라벨 시계 t0(자정 기준 초). 오버레이/CSV가 이 시각부터 흐른다.


# --------------------------------------------------------------------------- #
# pure helpers (no Kit dependency -> unit-testable)
# --------------------------------------------------------------------------- #
def episode_configs(n_episodes: int, min_obj: int, max_obj: int,
                    speed_range: Tuple[float, float], duration: float,
                    seed: int) -> List[EpisodeConfig]:
    """Deterministically build per-episode configs from a master seed."""
    rng = random.Random(seed)
    cfgs = []
    # 자정 넘김 금지: t0 + duration이 23:59:59를 넘으면 시분초 라벨이 역전된다.
    base_max = max(0, 86400 - int(duration) - 1)
    for i in range(n_episodes):
        cfgs.append(EpisodeConfig(
            idx=i,
            seed=rng.randint(0, 2**31 - 1),
            n_objects=rng.randint(min_obj, max_obj),
            speed=round(rng.uniform(*speed_range), 2),
            duration=duration,
            base_time_s=rng.randint(0, base_max),
        ))
    return cfgs


def random_positions(bounds: dict, objids: List[str], seed: int,
                     margin_frac: float = 0.1, min_sep_frac: float = 0.15) -> Dict[str, Tuple[float, float, float]]:
    """Seeded random in-bounds start positions, spaced to avoid initial overlap.

    bounds = {center:(cx,cy,cz), size:(sx,sy,sz), is_y_up:bool}. Horizontal axes
    are (x,z) for Y-up else (x,y); the vertical coord is set to the box center.
    """
    cx, cy, cz = bounds["center"]
    sx, sy, sz = bounds["size"]
    if bounds.get("is_y_up", True):
        (h0c, h0s), (h1c, h1s), vert = (cx, sx), (cz, sz), ("y", cy)
        ax = ("x", "z")
    else:
        (h0c, h0s), (h1c, h1s), vert = (cx, sx), (cy, sy), ("z", cz)
        ax = ("x", "y")
    m = margin_frac
    lo0, hi0 = h0c - h0s * (0.5 - m), h0c + h0s * (0.5 - m)
    lo1, hi1 = h1c - h1s * (0.5 - m), h1c + h1s * (0.5 - m)
    min_sep = min_sep_frac * min(h0s, h1s)
    rng = random.Random(seed)
    placed: List[Tuple[float, float]] = []
    out: Dict[str, Tuple[float, float, float]] = {}
    for objid in objids:
        for _ in range(200):
            p0, p1 = rng.uniform(lo0, hi0), rng.uniform(lo1, hi1)
            if all((p0 - q0) ** 2 + (p1 - q1) ** 2 >= min_sep ** 2 for q0, q1 in placed):
                break
        placed.append((p0, p1))
        coord = {ax[0]: p0, ax[1]: p1, vert[0]: vert[1]}
        out[objid] = (coord["x"], coord["y"], coord["z"])
    return out


def sample_floor_positions(bounds: dict, objids: List[str], seed: int, probe_floor,
                           floor_ref: float, spawn_offset: float = 5.0,
                           tol_below: float = 100.0, tol_above: float = 50.0,
                           margin_frac: float = 0.1, min_sep_frac: float = 0.15,
                           tries_per_obj: int = 60) -> Dict[str, Tuple[float, float, float]]:
    """무작위 시작 위치(바닥 검증): 궤적 범위 내 수평 무작위 샘플 중, probe_floor가
    보고한 바닥 높이가 floor_ref 허용창(-tol_below~+tol_above) 안인 지점만 채택하고
    바닥+spawn_offset(cm)에 스폰 — 중력으로 안착. 바닥 없는 지점(무한낙하, 일지 #7-6)과
    타 객체 위 히트를 걸러낸다.

    probe_floor(h0, h1) -> float|None: 수평 좌표 위에서 아래로 쏜 레이 히트의 수직값.
    검증 실패 객체는 결과에서 빠진다(호출부가 데이터 좌표 폴백).
    """
    cx, cy, cz = bounds["center"]
    sx, sy, sz = bounds["size"]
    if bounds.get("is_y_up", True):
        (h0c, h0s), (h1c, h1s) = (cx, sx), (cz, sz)
        make = lambda h0, h1, v: (h0, v, h1)
    else:
        (h0c, h0s), (h1c, h1s) = (cx, sx), (cy, sy)
        make = lambda h0, h1, v: (h0, h1, v)
    m = margin_frac
    lo0, hi0 = h0c - h0s * (0.5 - m), h0c + h0s * (0.5 - m)
    lo1, hi1 = h1c - h1s * (0.5 - m), h1c + h1s * (0.5 - m)
    min_sep = min_sep_frac * min(h0s, h1s)
    rng = random.Random(seed)
    placed: List[Tuple[float, float]] = []
    out: Dict[str, Tuple[float, float, float]] = {}
    for objid in objids:
        for _ in range(tries_per_obj):
            p0, p1 = rng.uniform(lo0, hi0), rng.uniform(lo1, hi1)
            if any((p0 - q0) ** 2 + (p1 - q1) ** 2 < min_sep ** 2 for q0, q1 in placed):
                continue
            hit_v = probe_floor(p0, p1)
            if hit_v is None or not (floor_ref - tol_below <= hit_v <= floor_ref + tol_above):
                continue
            placed.append((p0, p1))
            out[objid] = make(p0, p1, hit_v + spawn_offset)
            break
    return out


def organize_outputs(out_root: Path, idx: int, video: Path,
                     collisions: Optional[Path], trace: Optional[Path]) -> Path:
    """Move an episode's video+meta(+collisions+trace) into out_root/ep_XXXX/."""
    ep_dir = out_root / f"ep_{idx:04d}"
    ep_dir.mkdir(parents=True, exist_ok=True)
    moved = {}
    video = Path(video)
    meta = video.with_suffix(".meta.json")
    for label, src in (("video", video), ("meta", meta),
                       ("collisions", collisions), ("trace", trace)):
        if src and Path(src).exists():
            dst = ep_dir / Path(src).name
            shutil.move(str(src), str(dst))
            moved[label] = str(dst)
    return ep_dir


def parse_trace_frames(text: str) -> Dict[str, Dict[str, Tuple[float, float, float]]]:
    """trace CSV(``timestamp,objid,x,y,z``) → ``{타임스탬프: {objid: (x,y,z)}}``.

    dict는 삽입 순서를 지키므로 파일에 적힌 프레임 순서가 그대로 시간 순서가 된다 —
    아래 조우 사건 검출(``near_miss_events``)이 그 순서에 기대어 국소 극소점을 찾는다.
    """
    frames: Dict[str, Dict[str, Tuple[float, float, float]]] = {}
    for line in text.splitlines()[1:]:
        if not line.strip():
            continue
        ts, objid, x, y, z = (c.strip() for c in line.split(","))
        frames.setdefault(ts, {})[objid] = (float(x), float(y), float(z))
    return frames


def near_miss_events(frames: Dict[str, Dict[str, Tuple[float, float, float]]], gap: float,
                     event_frac: float = 2.0, refractory: int = 30) -> List[dict]:
    """trace에서 "조우 사건"을 뽑는다 — 쌍 거리의 국소 극소점 중 충분히 가까운 것.

    한 사건의 정의는 이렇다. 어떤 쌍의 중심거리 시계열에서 앞뒤 프레임보다 작아지는
    지점(국소 극소점)이 생기면 그 순간이 "가장 가까이 붙었다가 다시 멀어지기 시작한"
    순간이고, 그 거리가 ``gap × event_frac`` 안이면 near-miss 안무가 의도한 조우로
    본다. 문턱을 gap 자체가 아니라 그 몇 배로 두는 이유는, swerve가 노리는 통과 간격이
    gap × aim_frac이라 정확히 gap까지 붙지는 않기 때문이고, 또 조우 지점의 **분포**를
    보는 것이 목적이라 "얼마나 붙었나"보다 "어디서 만났나"가 중요하기 때문이다.

    사건의 위치는 그 순간 두 객체의 중점으로 잡는다 — 두 객체 중 하나의 좌표를 쓰면
    같은 조우를 어느 쪽에서 보느냐에 따라 gap의 절반만큼 어긋나므로, 조우 자체의
    위치로는 중점이 자연스럽다.

    양 끝 프레임은 사건 후보에서 제외한다(앞 또는 뒤가 없어 "다시 멀어졌다"를 관측할
    수 없다 — 아직 진행 중인 접근을 조우로 세면 사건 수가 부풀려진다).

    ``refractory``는 같은 쌍의 사건이 최소 이만큼(프레임) 떨어져 있어야 별개로 센다는
    불응기다. 30Hz trace 기준 기본 30프레임 = 1초. 한 번의 스침에서 좌표 흔들림으로
    극소점이 여러 개 잡히는 것을 하나로 합치는 장치이고, 겹치는 후보 중에서는 가장
    깊은(가장 가까웠던) 것을 남긴다.
    """
    keys = list(frames)
    series: Dict[Tuple[str, str], List[Tuple[int, float, Tuple[float, float, float]]]] = {}
    for i, ts in enumerate(keys):
        objs = frames[ts]
        ids = sorted(objs)
        for m in range(len(ids)):
            for n in range(m + 1, len(ids)):
                pa, pb = objs[ids[m]], objs[ids[n]]
                d = sum((pa[k] - pb[k]) ** 2 for k in range(3)) ** 0.5
                mid = tuple((pa[k] + pb[k]) / 2.0 for k in range(3))
                series.setdefault((ids[m], ids[n]), []).append((i, d, mid))
    thresh = gap * event_frac
    cands: List[Tuple[float, Tuple[str, str], int, Tuple[float, float, float]]] = []
    for pair, pts in series.items():
        for j in range(1, len(pts) - 1):
            idx, d, mid = pts[j]
            if d > thresh:
                continue
            # 왼쪽은 <= 로 평평한 구간(같은 값이 이어지는 샘플링 아티팩트)을 흡수하고,
            # 오른쪽은 < 로 두어 그 평평한 구간이 여러 사건으로 중복 검출되지 않게 한다.
            if d <= pts[j - 1][1] and d < pts[j + 1][1]:
                cands.append((d, pair, idx, mid))
    kept: List[Tuple[float, Tuple[str, str], int, Tuple[float, float, float]]] = []
    for d, pair, idx, mid in sorted(cands, key=lambda c: (c[0], c[2])):
        if any(p == pair and abs(idx - i2) < refractory for _, p, i2, _ in kept):
            continue
        kept.append((d, pair, idx, mid))
    kept.sort(key=lambda c: c[2])
    return [{"frame": idx, "pair": pair, "dist": round(d, 3), "pos": mid}
            for d, pair, idx, mid in kept]


def near_miss_diversity(frames: Dict[str, Dict[str, Tuple[float, float, float]]], gap: float,
                        bounds: Optional[Tuple[Tuple[float, float], Tuple[float, float]]] = None,
                        grid: int = 4, axes: Tuple[int, int] = (0, 2),
                        **event_kwargs) -> dict:
    """조우 지점의 **공간적 다양성**을 수치화한다(near-miss 안무 대칭 파괴의 판정 지표).

    배경: v3까지의 near-miss는 짝이 동시에·같은 속도로·서로를 정면 조준해 접근했기
    때문에 조우가 두 스폰 위치의 중점(스폰 구역이 방 중앙 대칭이므로 결국 방 중앙)
    부근에서 거의 같은 기하로 반복됐다. 이 함수는 그 반복을 사람 눈이 아니라 숫자로
    잡기 위한 것이다 — 곡률(틱당 헤딩 변화) 지표가 "조우 한 번의 모양"만 보고 "조우들이
    어디에 흩어져 있는가"는 전혀 못 봤다는 것이 v3의 교훈이었다.

    돌려주는 값 셋(요청 지표 a·b·c에 대응):
      - ``events``: 사건 수. 대칭 파괴가 조우 자체를 없애버리지 않았다는 확인 —
        위치만 흩어지고 조우가 안 일어나면 대조 데이터로 쓸모가 없다.
      - ``std``/``rms_radius``/``spread_frac``: 사건 위치의 흩어짐. ``rms_radius``는
        중심에서의 RMS 거리(=두 축 표준편차의 제곱합 제곱근)이고, ``spread_frac``은
        그것을 방의 짧은 변으로 나눈 무차원 값이다. ``coverage``는 방을
        ``grid × grid`` 칸으로 나눴을 때 사건이 하나라도 들어간 칸의 비율 — 표준편차가
        같아도 두 군데에만 몰려 있는 것과 고루 퍼진 것을 구분한다.
      - ``min_sep``: 사건들 사이의 최소 이격. 서로 다른 조우가 사실상 같은 자리에서
        일어났는지를 잡는다(표준편차는 큰데 min_sep이 0에 가까우면 "몇 군데에 겹쳐서
        반복"이라는 뜻).

    ``coverage``는 사건 수에 딸려 올라가는 값이라(사건이 3개뿐이면 16칸 중 최대 3칸
    = 18.75%가 천장이다) 사건 수가 다른 두 설정을 그대로 비교하면 안 된다. 그래서
    ``coverage_eff``를 같이 준다 — 점유 칸 수를 "그 사건 수로 도달 가능한 최대 칸 수"
    ``min(events, grid²)``로 나눈 값이라, 사건 수와 무관하게 "사건들이 서로 다른 칸에
    떨어졌는가"만 본다(1.0이면 모든 사건이 각자 다른 칸).

    ``bounds``는 방 크기 ``((u0, v0), (u1, v1))``(수평 두 축). 주지 않으면 trace에
    찍힌 모든 객체 좌표의 최소·최대에서 유도한다 — 객체가 방을 돌아다니므로 그 외접
    사각형이 방 크기의 실용적 근사다. 다만 유도한 경계는 궤적에 따라 달라지므로,
    서로 다른 설정을 비교할 때는 같은 ``bounds``를 명시로 넘겨야 ``coverage``와
    ``spread_frac``이 같은 잣대가 된다.
    """
    events = near_miss_events(frames, gap, **event_kwargs)
    au, av = axes
    if bounds is None:
        us = [p[au] for objs in frames.values() for p in objs.values()]
        vs = [p[av] for objs in frames.values() for p in objs.values()]
        bounds = ((min(us), min(vs)), (max(us), max(vs))) if us else ((0.0, 0.0), (1.0, 1.0))
    (u0, v0), (u1, v1) = bounds
    room_u, room_v = max(1e-9, u1 - u0), max(1e-9, v1 - v0)
    pts = [(e["pos"][au], e["pos"][av]) for e in events]
    out: dict = {
        "events": len(pts),
        "room": (round(room_u, 2), round(room_v, 2)),
        "centroid": None, "std": None, "rms_radius": None, "spread_frac": None,
        "span_frac": None, "coverage": None, "coverage_eff": None, "min_sep": None,
    }
    if not pts:
        return out
    cu = sum(p[0] for p in pts) / len(pts)
    cv = sum(p[1] for p in pts) / len(pts)
    su = (sum((p[0] - cu) ** 2 for p in pts) / len(pts)) ** 0.5
    sv = (sum((p[1] - cv) ** 2 for p in pts) / len(pts)) ** 0.5
    cells = {(min(grid - 1, max(0, int((p[0] - u0) / room_u * grid))),
              min(grid - 1, max(0, int((p[1] - v0) / room_v * grid)))) for p in pts}
    seps = [((pts[i][0] - pts[j][0]) ** 2 + (pts[i][1] - pts[j][1]) ** 2) ** 0.5
            for i in range(len(pts)) for j in range(i + 1, len(pts))]
    out.update({
        "centroid": (round(cu, 2), round(cv, 2)),
        "std": (round(su, 2), round(sv, 2)),
        "rms_radius": round((su * su + sv * sv) ** 0.5, 2),
        "spread_frac": round((su * su + sv * sv) ** 0.5 / min(room_u, room_v), 4),
        "span_frac": (round((max(p[0] for p in pts) - min(p[0] for p in pts)) / room_u, 4),
                      round((max(p[1] for p in pts) - min(p[1] for p in pts)) / room_v, 4)),
        "coverage": round(len(cells) / float(grid * grid), 4),
        "coverage_eff": round(len(cells) / float(min(len(pts), grid * grid)), 4),
        "min_sep": round(min(seps), 2) if seps else None,
    })
    return out


def check_near_miss_diversity(text: str, gap: float, **kwargs) -> dict:
    """``near_miss_diversity``의 CSV 입력판 — trace 파일 하나를 그대로 받는다."""
    return near_miss_diversity(parse_trace_frames(text), gap, **kwargs)


def check_near_miss_trace(text: str, gap: float, tol: float = 2.0,
                          near_eps: Optional[float] = None) -> dict:
    """near-miss 에피소드의 trace 자체 검증(순수 — Kit·외부 패키지 불필요).

    두 가지를 동시에 봐야 에피소드가 쓸모 있다:
      (a) 모든 쌍의 최소 중심거리 ≥ gap - tol   → 접촉이 없었다(GT 0건과 정합)
      (b) 최소한 한 쌍이 gap + near_eps 안까지  → 접근이 실제로 일어났다
          (안 그러면 "그냥 멀리 떨어져 돌아다닌 영상" = 근접 오탐 시험에 무의미)
    tol은 30Hz 샘플링이 최근접 순간을 놓쳐 생기는 오차 여유, near_eps 기본값은 gap의 15%.
    거리는 rule_baseline·GT 라벨러와 같은 3D 중심거리.

    swerve(v3)는 gap이 아니라 gap × aim_frac(기본 1.05배)을 노리고 비껴가므로 정상
    통과의 최소거리는 gap보다 5% 정도 큰 값으로 나온다 — 기본 near_eps(15%) 안이라
    (b)를 만족한다. 반대로 선회 반경(--near-miss-turn-radius-frac)을 크게 올려 너무
    완만하게 만들면 미리 넓게 벌어져 (b)가 깨지는데, 그때의 FAIL(approached=0)은
    "조향이 과하게 완만하다"는 신호로 읽으면 된다.
    """
    if near_eps is None:
        near_eps = 0.15 * gap
    frames = parse_trace_frames(text)
    pair_min: Dict[Tuple[str, str], float] = {}
    for objs in frames.values():
        ids = sorted(objs)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                pa, pb = objs[ids[i]], objs[ids[j]]
                d = sum((pa[k] - pb[k]) ** 2 for k in range(3)) ** 0.5
                key = (ids[i], ids[j])
                if d < pair_min.get(key, float("inf")):
                    pair_min[key] = d
    min_pair = min(pair_min, key=lambda k: pair_min[k]) if pair_min else None
    min_dist = pair_min[min_pair] if min_pair else float("inf")
    approached = [k for k, d in pair_min.items() if d <= gap + near_eps]
    violations = sorted((round(d, 2), k) for k, d in pair_min.items() if d < gap - tol)
    return {
        "ok": bool(pair_min) and not violations and bool(approached),
        "frames": len(frames), "pairs": len(pair_min),
        "min_dist": round(min_dist, 2) if pair_min else None,
        "min_pair": min_pair, "approached": len(approached),
        "violations": [{"pair": k, "min_dist": d} for d, k in violations[:10]],
    }


def parse_spawn_plan(plan_str: Optional[str], zones: dict, n_objects: int) -> List[Tuple[str, int]]:
    """"zoneA:2,zoneB:2" → [(zone, count)...]. 미지정 시 첫 구역에 전원. 합계는 객체 수와 일치해야."""
    if not plan_str:
        return [(next(iter(zones)), n_objects)]
    out: List[Tuple[str, int]] = []
    for part in plan_str.split(","):
        name, _, cnt = part.strip().partition(":")
        if name not in zones:
            raise ValueError(f"unknown spawn zone {name!r} (defined: {sorted(zones)})")
        out.append((name, int(cnt)))
    total = sum(c for _, c in out)
    if total != n_objects:
        raise ValueError(f"spawn plan total {total} != episode objects {n_objects}")
    return out


def sample_zone_positions(zones: dict, plan: List[Tuple[str, int]], objids: List[str], seed: int,
                          spawn_offset: float = 5.0, margin_frac: float = 0.1,
                          min_sep_frac: float = 0.15, tries_per_obj: int = 60,
                          ) -> Dict[str, Tuple[float, float, float]]:
    """사전 정의 구역에서 순수 수학으로 시작 위치 샘플 — 레이캐스트·Kit API 불필요.

    구역은 "바닥이 존재한다"를 정의자가 보증하는 수평 사각형(y-up 기준 (x,z)) + 바닥 높이:
        zones = {name: {"min": [x0, z0], "max": [x1, z1], "floor": 89.5}}
    plan 순서대로 objids를 앞에서부터 배정, 각 구역 내 가장자리 margin 제외 균등 샘플,
    전 구역 공통으로 객체 간 최소 이격 보장, 바닥+spawn_offset(cm)에 스폰(중력 안착).
    이격 실패 객체는 결과에서 제외(호출부 폴백). 신규 구역의 바닥 검증은 오프라인
    도구(sample_floor_positions 레이캐스트)로 등록 시 1회 수행하는 것을 전제로 한다.
    """
    rng = random.Random(seed)
    placed: List[Tuple[float, float, float]] = []  # (h0, h1, 그 지점의 min_sep)
    out: Dict[str, Tuple[float, float, float]] = {}
    idx = 0
    for name, count in plan:
        z = zones[name]
        (l0, l1), (u0, u1) = z["min"], z["max"]
        s0, s1 = float(u0) - float(l0), float(u1) - float(l1)
        m = margin_frac
        lo0, hi0 = l0 + s0 * m, u0 - s0 * m
        lo1, hi1 = l1 + s1 * m, u1 - s1 * m
        min_sep = min_sep_frac * min(s0, s1)
        floor = float(z["floor"])
        for objid in objids[idx: idx + count]:
            for _ in range(tries_per_obj):
                p0, p1 = rng.uniform(lo0, hi0), rng.uniform(lo1, hi1)
                if all((p0 - q0) ** 2 + (p1 - q1) ** 2 >= min(min_sep, qs) ** 2
                       for q0, q1, qs in placed):
                    placed.append((p0, p1, min_sep))
                    out[objid] = (p0, floor + spawn_offset, p1)
                    break
        idx += count
    return out


def load_spawn_zones(args, core) -> dict:
    """--spawn-zones(JSON 파일 경로 또는 인라인 JSON) 로드. 미지정 시 기본 구역 =
    궤적 데이터 좌표 범위(순수 데이터 조회 — physics 불필요) + --spawn-floor."""
    raw = getattr(args, "spawn_zones", None)
    if raw:
        p = Path(raw)
        zones = json.loads(p.read_text(encoding="utf-8")) if p.exists() else json.loads(raw)
        for name, z in zones.items():
            for key in ("min", "max", "floor"):
                if key not in z:
                    raise ValueError(f"zone {name!r}: {key!r} missing")
        return zones
    repo = getattr(core, "_repository", None)
    cr = repo.get_coord_range() if repo is not None and hasattr(repo, "get_coord_range") else None
    if not cr:
        raise RuntimeError("spawn zones: coord range unavailable; provide --spawn-zones")
    mins, maxs = cr
    return {"trajectory_bbox": {"min": [float(mins[0]), float(mins[2])],
                                "max": [float(maxs[0]), float(maxs[2])],
                                "floor": float(getattr(args, "spawn_floor", 89.5))}}


def write_run_manifest(out_root: Path, args_dict: dict, cfgs: List[EpisodeConfig],
                       done_idx: List[int], git_commit: Optional[str] = None,
                       timing: Optional[dict] = None,
                       elapsed_s: Optional[Dict[int, float]] = None) -> Path:
    """배치 재현·역추적용 manifest: 생성 인자, 에피소드별 조건·시드, 성공 여부, 소요 시간."""
    done = set(done_idx)
    elapsed_s = elapsed_s or {}
    manifest = {
        "schema_version": 1,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "git_commit": git_commit,
        # setup_s = 씬 로드+객체 준비(에피소드 밖 고정 비용), total_s = run() 전체.
        # 배치 예상 상한 공식(로드 180s + Σep(D×7.2+30))×1.2 의 실측 검증 근거.
        "timing": timing or {},
        "args": {k: args_dict[k] for k in sorted(args_dict)},
        "episodes": [
            {"idx": c.idx, "dir": f"ep_{c.idx:04d}", "seed": c.seed,
             "n_objects": c.n_objects, "speed": c.speed, "duration": c.duration,
             "base_time_s": c.base_time_s, "ok": c.idx in done,
             "elapsed_s": elapsed_s.get(c.idx)}
            for c in cfgs
        ],
    }
    path = out_root / "_run_manifest.json"
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def upload_episode(ep_dir: Path, upload_uri: str) -> None:
    """에피소드 폴더를 {upload_uri}/{ep명}/으로 업로드. 시간·바이트 로그 = 레이크 처리량 실측."""
    import time as _time

    from gist.netai.time_travel_summarization.storage import from_uri

    base = upload_uri.rstrip("/") + "/" + ep_dir.name
    adapter = from_uri(base)
    ctypes = {".mp4": "video/mp4", ".json": "application/json", ".csv": "text/csv"}
    total = 0
    t0 = _time.time()
    for f in sorted(p for p in ep_dir.iterdir() if p.is_file()):
        adapter.put_file(f"{base}/{f.name}", f,
                         content_type=ctypes.get(f.suffix, "application/octet-stream"))
        total += f.stat().st_size
    dt = max(_time.time() - t0, 1e-6)
    print(f"[gen] upload {ep_dir.name}: {total / 1e6:.1f} MB in {dt:.1f}s "
          f"({total / 1e6 / dt:.1f} MB/s) -> {base}")


def pick_objids(all_objids: List[str], n: int, seed: int) -> List[str]:
    rng = random.Random(seed)
    n = min(n, len(all_objids))
    return sorted(rng.sample(list(all_objids), n))


# --------------------------------------------------------------------------- #
# Kit-driving parts (import omni lazily so the module loads without Kit)
# --------------------------------------------------------------------------- #
def _get_core():
    from gist.netai.time_travel_summarization.extension import get_active_core
    core = get_active_core()
    if core is None:
        raise RuntimeError("extension not started / no active core")
    return core


def apply_positions(core, positions: Dict[str, Tuple[float, float, float]]) -> None:
    """프림 이동 — translate op를 찾아/만들어 직접 설정 (XformCommonAPI 금지).

    XformCommonAPI는 표준 op 스택에서만 동작하고, physics 에피소드를 거친 프림은
    PhysX가 스택을 바꿔 놓아 SetTranslate가 **조용히 무시**된다(2026-08-15 게이트
    실측: ep0만 적용되고 ep1+는 전부 무시 → 매 에피소드 같은 자리에서 시작 + 즉시
    충돌. run19의 "이동 명령 무시 정황"의 정체). 구경로에서는 set_to_earliest_time의
    playback 갱신이 스택을 만져 우연히 가려졌던 결함이라, playback 컨트롤러가 쓰는
    검증된 패턴(TypeTranslate op find-or-add)으로 통일한다.
    """
    import omni.usd
    from pxr import UsdGeom, Gf

    stage = omni.usd.get_context().get_stage()
    prim_map = getattr(core, "_prim_map", {})
    skipped = []
    for objid, xyz in positions.items():
        path = prim_map.get(objid)
        prim = stage.GetPrimAtPath(path) if path else None
        if not (prim and prim.IsValid()):
            skipped.append(objid)
            continue
        xformable = UsdGeom.Xformable(prim)
        translate_op = None
        for op in xformable.GetOrderedXformOps():
            if op.GetOpType() == UsdGeom.XformOp.TypeTranslate:
                translate_op = op
                break
        if translate_op is None:
            translate_op = xformable.AddTranslateOp()
        try:
            translate_op.Set(Gf.Vec3d(float(xyz[0]), float(xyz[1]), float(xyz[2])))
        except Exception:                      # 기존 op가 float 정밀도인 경우
            translate_op.Set(Gf.Vec3f(float(xyz[0]), float(xyz[1]), float(xyz[2])))
    if skipped:
        print(f"[gen] apply_positions: prim 없음/무효 → 미적용 {skipped}")


def precompute_floor_positions(core, bounds: dict, cfgs: List[EpisodeConfig],
                               all_objids: List[str]) -> Dict[int, Dict[str, Tuple[float, float, float]]]:
    """startup의 physics 활성 윈도우에서 전 에피소드 시작 위치를 레이캐스트 검증으로 사전 계산.

    시뮬레이션 활성 중 USD 텔레포트는 PhysX에 반영되지 않으므로(run17 실측: 전원 동결·
    trace 0행) 여기서는 좌표 "계산"만 하고, 적용(apply_positions)은 에피소드 루프에서
    physics OFF 상태에 한다(#6의 검증된 순서: 배치 → physics ON).
    바닥 기준(floor_ref) = 데이터 좌표에 서 있는 현재 객체들의 수직 좌표 중앙값.
    """
    import carb
    import omni.kit.app
    from omni.physx import get_physx_scene_query_interface

    app = omni.kit.app.get_app()
    for _ in range(10):  # physics scene/콜라이더 초기화 소진 (scene query 준비)
        app.update()

    is_y_up = bounds.get("is_y_up", True)
    vert = 1 if is_y_up else 2
    cur = core.get_current_object_positions() or {}
    floors = sorted(float(p[vert]) for p in cur.values())
    floor_ref = floors[len(floors) // 2] if floors else float(bounds["center"][vert])
    top = floor_ref + max(float(bounds["size"][vert]), 300.0) + 200.0
    max_dist = (top - floor_ref) + 1000.0

    sq = get_physx_scene_query_interface()

    def probe_floor(h0, h1):
        if is_y_up:
            origin, direction = carb.Float3(h0, top, h1), carb.Float3(0.0, -1.0, 0.0)
        else:
            origin, direction = carb.Float3(h0, h1, top), carb.Float3(0.0, 0.0, -1.0)
        hit = sq.raycast_closest(origin, direction, max_dist)
        if hit and hit.get("hit"):
            return float(hit["position"][vert])
        return None

    out: Dict[int, Dict[str, Tuple[float, float, float]]] = {}
    for cfg in cfgs:
        objids = pick_objids(all_objids, cfg.n_objects, cfg.seed)
        pos = sample_floor_positions(bounds, objids, cfg.seed, probe_floor, floor_ref)
        missing = [o for o in objids if o not in pos]
        if missing:
            print(f"[gen] pre-pos ep{cfg.idx}: no valid floor for {missing} -> data-coord fallback")
        out[cfg.idx] = pos
        print(f"[gen] pre-pos ep{cfg.idx}: {len(pos)}/{len(objids)} floor_ref={floor_ref:.1f} "
              f"{ {k: tuple(round(v, 1) for v in xyz) for k, xyz in pos.items()} }")
    return out


def _ensure_stage(core, stage_url: Optional[str] = None) -> None:
    """Headless bootstrap: open the requested USD (또는 빈 스테이지) + BEV 카메라 재보장.

    stage_url이 주어지면(로컬 경로 또는 omniverse:// Nucleus URL) 그 씬을 연다 —
    GUI에서 쓰던 실제 씬으로 headless 촬영 가능. 없으면 빈 스테이지(바닥/벽은
    set_physics_mode의 create_bounding_box가 생성). summarization 카메라는 확장
    시작 시(스테이지 없음) 생성 실패했을 수 있어 재보장.
    """
    import omni.usd

    ctx = omni.usd.get_context()
    if stage_url:
        ok = ctx.open_stage(stage_url)
        if not ok:
            raise RuntimeError(f"failed to open stage: {stage_url}")
        print(f"[gen] opened stage: {stage_url}")
        # 비동기 페이로드 로딩 완료까지 대기: 끝나기 전에 physics를 켜면 아직 콜라이더가
        # 없는 바닥을 뚫고 무한낙하한다(실측 y=-16594; GUI는 로드 완료 후 조작하므로 정상).
        import time as _time

        import omni.kit.app
        app = omni.kit.app.get_app()
        deadline = _time.time() + 300.0
        settled, loading = 0, None
        while _time.time() < deadline:
            app.update()
            try:
                _msg, _loaded, loading = ctx.get_stage_loading_status()
            except Exception:
                break
            settled = settled + 1 if loading == 0 else 0
            if settled >= 60:  # 로딩 0 상태가 60 update 연속 유지되면 완료로 간주
                break
        print(f"[gen] stage loading settled (loading={loading})")
    elif ctx.get_stage() is None:
        ctx.new_stage()
        print("[gen] no active stage -> created a new empty stage")
    so = getattr(core, "_stage_objects", None)
    if so is not None and hasattr(so, "ensure_summarization_camera"):
        so.ensure_summarization_camera()


def _resolve_camera(camera: Optional[str]) -> Optional[str]:
    """카메라 인자를 프림 경로로 해석. '/'로 시작하면 그대로, 아니면 이름으로 스테이지 검색."""
    if not camera:
        return None
    if camera.startswith("/"):
        return camera
    import omni.usd

    stage = omni.usd.get_context().get_stage()
    if stage is not None:
        for prim in stage.Traverse():
            if prim.GetName() == camera and prim.GetTypeName() == "Camera":
                path = str(prim.GetPath())
                print(f"[gen] camera '{camera}' resolved -> {path}")
                return path
    raise RuntimeError(f"camera named {camera!r} not found in stage")


def run(args, core=None) -> None:
    import omni.kit.app  # noqa: F401  (ensures Kit context)

    core = core or _get_core()
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    import time as _time
    run_t0 = _time.monotonic()          # 소요 시간 실측 → manifest["timing"]
    ep_elapsed: Dict[int, float] = {}

    # 씬 프로파일(명시 파라미터) — 지정 시 데이터 로드 없이 아레나·객체 풀을 만든다.
    # 미지정 시 구경로(암묵 CSV 의존) 폴백 + 경고. keep-positions는 데이터 좌표 배치라
    # 정의상 데이터가 전제이므로 프로파일과 양립 불가.
    profile = None
    prof_name = getattr(args, "scene_profile", None)
    if prof_name:
        # 절대 임포트 — 이 파일은 kit이 스크립트로 직접 실행하므로(부모 패키지 없음)
        # 상대 임포트가 불가하다(파일 내 다른 프로젝트 임포트와 같은 규칙).
        from gist.netai.time_travel_summarization.automation.scene_profiles import (
            coord_range_of, load_profile)
        profile = load_profile(prof_name)
        if getattr(args, "keep_positions", False):
            raise SystemExit("[gen] --scene-profile과 --keep-positions는 양립 불가 "
                             "(keep-positions는 데이터 좌표 배치 = 데이터 로드 전제)")

    stage_uri = getattr(args, "stage", None) or (profile["stage"] if profile else None)
    camera_arg = getattr(args, "camera", None) or (profile["camera"] if profile else None)
    _ensure_stage(core, stage_uri)
    camera_path = _resolve_camera(camera_arg)

    if profile:
        core.set_coord_range_override(*coord_range_of(profile))
        pool = max(int(args.max_objects), int(args.min_objects))
        core.spawn_objects(pool)
        # log_warn 아닌 print — run_job.sh가 stdout을 job.log로 모으므로 계보에 남는다.
        print(f"[gen] scene profile={prof_name} pool={pool} "
              f"coord_min={profile['coord_min']} coord_max={profile['coord_max']} "
              f"(no data load)")
    else:
        print("[gen] deprecated: --scene-profile 미지정 — 암묵 CSV(config data_path) "
              "의존 경로로 폴백 (scene_profiles.json 사용 권장)")
        ok = core.load_data()
        _repo = getattr(core, "_repository", None)
        print(f"[gen] load_data={ok} err={getattr(core, '_last_data_load_error', '')!r} "
              f"repo_start={getattr(_repo, 'data_start_time', None)}")
        # GUI와 동일 경로: regenerate...는 repo를 보존하지만 auto_generate...는 내부에서
        # clear_timetravel_objects()로 _repository까지 지워버린다(facade.py:978) →
        # 좌표 데이터 소실 → 배치 no-op → 전원 (0,0,0) 겹침 폭발의 근원.
        if hasattr(core, "regenerate_astronauts_from_loaded_data"):
            core.regenerate_astronauts_from_loaded_data()
        elif hasattr(core, "auto_generate_astronauts"):
            core.auto_generate_astronauts()

    all_objids = list(getattr(core, "_prim_map_full", None) or getattr(core, "_prim_map", {}))
    if not all_objids:
        raise RuntimeError("no objects available (auto_generate failed?)")
    data_objids = set(all_objids)

    # 합성 객체 추가: physics 모드는 궤적 데이터가 불필요하므로 풀을 늘릴 수 있다.
    # 단 keep-positions와는 양립 불가(합성 객체는 데이터 좌표가 없음).
    extra = int(getattr(args, "extra_objects", 0) or 0)
    if extra > 0 and getattr(args, "keep_positions", False):
        print("[gen] --extra-objects ignored with --keep-positions (no data coords for synthetic)")
        extra = 0
    if extra > 0 and hasattr(core, "add_synthetic_objects"):
        added = core.add_synthetic_objects(extra)
        all_objids = list(getattr(core, "_prim_map_full", None) or getattr(core, "_prim_map", {}))
        print(f"[gen] synthetic objects: +{len(added)} -> pool={sorted(all_objids)}")

    cfgs = episode_configs(args.episodes, args.min_objects, args.max_objects,
                           (args.speed_min, args.speed_max), args.duration, args.seed)
    print(f"[gen] {len(cfgs)} episodes, objects available={len(all_objids)}, out={out_root}")
    done_idx: List[int] = []

    # Bounds depend only on the trajectory range (constant) -> compute once.
    # 주의: 이 physics 창에서 app.update()를 돌리지 말 것 — 캡처의 set_capture_on_play(False)
    # 이전에 "재생 예약 + update"가 만나면 Replicator 자동 모드가 타임라인 자동 전진을
    # 잠근다(run18~20 실측: playing=True인데 update 무전진). 그래서 레이캐스트 사전계산을
    # 버리고 사전 정의 구역(spawn zones, 순수 수학) 방식으로 전환했다.
    core.set_physics_mode()
    bounds = core.get_physics_bounds()
    core.set_playback_mode()
    _repo = getattr(core, "_repository", None)
    print(f"[gen] after playback_mode: repo_start={getattr(_repo, 'data_start_time', None)}")

    spawn_zones = None
    if not getattr(args, "keep_positions", False):
        if getattr(args, "spawn_plan", None) and args.min_objects != args.max_objects:
            raise RuntimeError("--spawn-plan은 고정 객체 수가 전제: --min-objects == --max-objects")
        spawn_zones = load_spawn_zones(args, core)
        print(f"[gen] spawn zones: { {k: v for k, v in spawn_zones.items()} } "
              f"plan={getattr(args, 'spawn_plan', None) or '(첫 구역에 전원)'}")

    setup_s = _time.monotonic() - run_t0   # 씬 로드+객체 준비 (에피소드 밖 고정 비용)
    for cfg in cfgs:
        ep_t0 = _time.monotonic()
        objids = pick_objids(all_objids, cfg.n_objects, cfg.seed)
        core.set_active_objects(objids)
        # 모드 무관 공통: 궤적 데이터 첫 시점 좌표로 벌려놓기. 이걸 안 하면 생성 직후
        # 전원이 (0,0,0)에 완전히 겹친 채 physics가 켜져 PhysX 겹침해소 폭발로
        # 벽을 관통해 낙하한다(실측: step30에 z 4->36, 이후 y -13789).
        # 무작위 배치도 이 안전 좌표에서 physics를 켠 "뒤" 검증된 위치로 텔레포트한다.
        core.set_to_earliest_time()
        synth_active = [o for o in objids if o not in data_objids]
        if synth_active:
            # 합성 객체는 데이터 좌표가 없음 — 구역 샘플이 이격 실패로 놓쳐도 산개는 보장(#6 방지).
            apply_positions(core, random_positions(bounds, synth_active, cfg.seed + 1))
        if spawn_zones is not None:
            plan = parse_spawn_plan(getattr(args, "spawn_plan", None), spawn_zones, len(objids))
            zpos = sample_zone_positions(spawn_zones, plan, objids, cfg.seed)
            missing = [o for o in objids if o not in zpos]
            if missing:
                print(f"[gen] zone-pos ep{cfg.idx}: sep-fail {missing} -> 데이터 좌표/산개 폴백")
            if zpos:
                # physics OFF 상태 적용 → PhysX가 켜질 때 이 좌표를 초기 포즈로 인식(확실 반영).
                apply_positions(core, zpos)
            print(f"[gen] zone-pos ep{cfg.idx}: "
                  f"{ {k: tuple(round(v, 1) for v in xyz) for k, xyz in zpos.items()} }")
            # 적용 검증: 좌표가 실제 프림에 반영됐는지 월드 좌표로 확인(합성 프림 추락
            # 사고의 재발 감지 — run19에서 이동 명령이 조용히 무시된 정황).
            cur = core.get_current_object_positions() or {}
            print(f"[gen] pos-verify ep{cfg.idx}: "
                  f"{ {k: tuple(round(float(v), 1) for v in xyz) for k, xyz in sorted(cur.items())} }")
        if getattr(args, "keep_positions", False):
            # 배치 검증 로그: repository가 비었으면 위 호출이 조용히 no-op이 된다.
            repo = getattr(core, "_repository", None)
            start_t = getattr(repo, "data_start_time", None)
            first_path = next(iter(getattr(core, "_prim_map", {}).values()), None)
            pos = None
            try:
                import omni.usd
                from pxr import UsdGeom
                stage_now = omni.usd.get_context().get_stage()
                prim = stage_now.GetPrimAtPath(first_path) if first_path else None
                if prim and prim.IsValid():
                    pos = tuple(round(v, 1) for v in
                                UsdGeom.XformCache(0).GetLocalToWorldTransform(prim).ExtractTranslation())
            except Exception:
                pass
            print(f"[gen] keep-positions: data_start={start_t} obj1@{pos}")
        core.set_wander_speed(cfg.speed)
        if hasattr(core, "set_wander_seed"):
            core.set_wander_seed(cfg.seed)  # heading 재현성(페이싱 재현과 별개)
        # near-miss 안무는 컨트롤러 생성 시점(set_physics_mode)에 결정된다 → 그 전에 지정.
        if hasattr(core, "set_near_miss_gap"):
            core.set_near_miss_gap(args.near_miss_gap if getattr(args, "near_miss", False) else 0.0)
        if hasattr(core, "set_near_miss_mode"):
            core.set_near_miss_mode(getattr(args, "near_miss_mode", "swerve"))
        if hasattr(core, "set_near_miss_steering"):
            core.set_near_miss_steering(
                avoid_frac=getattr(args, "near_miss_avoid_frac", None),
                turn_radius_frac=getattr(args, "near_miss_turn_radius_frac", None),
                aim_frac=getattr(args, "near_miss_aim_frac", None))
        if hasattr(core, "set_near_miss_diversity"):
            core.set_near_miss_diversity(
                start_jitter_s=getattr(args, "near_miss_start_jitter", None),
                speed_min_frac=getattr(args, "near_miss_speed_min_frac", None),
                speed_max_frac=getattr(args, "near_miss_speed_max_frac", None),
                depart_spread_deg=getattr(args, "near_miss_depart_spread", None))
        core.set_physics_mode()
        trace_path = str((out_root / f"_trace_{cfg.idx:04d}.csv").resolve())
        video_path = str((out_root / f"_video_{cfg.idx:04d}.mp4").resolve())
        core.start_trace(trace_path)
        core.start_wander()
        # 라벨 시각 = 무작위 t0 + sim 경과(실행 시각과 무관). 날짜부는 표기 안 되므로 오늘 날짜 사용.
        anchor = datetime.datetime.combine(
            datetime.date.today(), datetime.time()) + datetime.timedelta(seconds=cfg.base_time_s)
        print(f"[gen] ep {cfg.idx}: base_time={anchor.time()}")
        produced = core.run_capture_headless(cfg.duration, video_path, camera_path=camera_path,
                                             capture_start_dt=anchor,
                                             render_fps=getattr(args, "render_fps", None))
        core.stop_wander()
        core.stop_trace()
        core.set_playback_mode()
        if getattr(args, "near_miss", False):
            # 좌표로 즉시 자체 검증 — 접촉 없음(GT 0의 필요조건) + 접근이 실제로 일어남.
            try:
                nm_text = Path(trace_path).read_text(encoding="utf-8")
                res = check_near_miss_trace(nm_text, args.near_miss_gap)
                print(f"[gen] near-miss check ep {cfg.idx} mode={getattr(args, 'near_miss_mode', 'swerve')}: "
                      f"{'OK' if res['ok'] else 'FAIL'} "
                      f"min_d={res['min_dist']} pair={res['min_pair']} "
                      f"approached={res['approached']}/{res['pairs']} frames={res['frames']} "
                      f"violations={res['violations']}")
                # 조우 다양성(합격 기준 아님 — 배치 전체를 놓고 읽는 관찰 지표).
                # 에피소드마다 events가 1~2에 머물거나 spread가 0에 가까우면 대칭
                # 파괴가 안 먹은 것이므로, 배치 로그에서 바로 눈에 띄게 같이 찍는다.
                div = check_near_miss_diversity(nm_text, args.near_miss_gap)
                print(f"[gen] near-miss diversity ep {cfg.idx}: events={div['events']} "
                      f"spread={div['rms_radius']} ({div['spread_frac']} of short side) "
                      f"coverage={div['coverage']} min_sep={div['min_sep']}")
            except Exception as e:
                print(f"[gen] near-miss check ep {cfg.idx}: FAILED to read trace: {e!r}")
        if not produced:
            ep_elapsed[cfg.idx] = round(_time.monotonic() - ep_t0, 1)
            print(f"[gen] ep {cfg.idx}: capture failed; skipping")
            continue
        # collisions path comes from the sidecar the capture wrote
        meta = Path(produced).with_suffix(".meta.json")
        collisions = None
        if meta.exists():
            cj = json.loads(meta.read_text(encoding="utf-8")).get("collisions_csv")
            collisions = Path(cj) if cj else None
        ep_dir = organize_outputs(out_root, cfg.idx, Path(produced), collisions, Path(trace_path))
        done_idx.append(cfg.idx)
        print(f"[gen] ep {cfg.idx}: objs={objids} speed={cfg.speed} -> {ep_dir}")
        if getattr(args, "upload_uri", None):
            try:
                upload_episode(ep_dir, args.upload_uri)
            except Exception as e:
                print(f"[gen] upload FAILED for ep {cfg.idx}: {e!r} (local files kept)")
        ep_elapsed[cfg.idx] = round(_time.monotonic() - ep_t0, 1)
        print(f"[gen] ep {cfg.idx}: elapsed {ep_elapsed[cfg.idx]}s")

    git_commit = None
    try:
        import subprocess
        git_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(Path(__file__).resolve().parent),
            text=True, timeout=10).strip()
    except Exception:
        pass
    timing = {"setup_s": round(setup_s, 1),
              "total_s": round(_time.monotonic() - run_t0, 1)}
    manifest_path = write_run_manifest(out_root, dict(vars(args)), cfgs, done_idx, git_commit,
                                       timing=timing, elapsed_s=ep_elapsed)
    print(f"[gen] manifest -> {manifest_path} (ok {len(done_idx)}/{len(cfgs)})")
    if getattr(args, "upload_uri", None):
        try:
            from gist.netai.time_travel_summarization.storage import from_uri
            uri = args.upload_uri.rstrip("/") + "/_run_manifest.json"
            from_uri(uri).put_file(uri, manifest_path, content_type="application/json")
            print(f"[gen] manifest uploaded -> {uri}")
        except Exception as e:
            print(f"[gen] manifest upload FAILED: {e!r}")

    print("[gen] done.")


def _self_test() -> None:
    """Exercise the pure helpers without Kit (this-session verification)."""
    import tempfile
    cfgs = episode_configs(5, 4, 6, (200.0, 300.0), 40.0, seed=7)
    assert len(cfgs) == 5 and all(4 <= c.n_objects <= 6 for c in cfgs)
    assert all(0 <= c.base_time_s <= 86400 - 41 for c in cfgs)
    assert len({c.base_time_s for c in cfgs}) > 1, "base times should vary"
    assert episode_configs(5, 4, 6, (200, 300), 40, 7) == cfgs, "configs not deterministic"
    bounds = {"center": (0.0, 1.5, 0.0), "size": (100.0, 3.0, 80.0), "is_y_up": True}
    objids = [f"obj{i:03d}" for i in range(1, 6)]
    pos = random_positions(bounds, objids, seed=7)
    assert set(pos) == set(objids)
    for (x, y, z) in pos.values():
        assert -50 <= x <= 50 and -40 <= z <= 40 and y == 1.5
    pts = list((x, z) for x, y, z in pos.values())
    assert all((pts[i][0]-pts[j][0])**2 + (pts[i][1]-pts[j][1])**2 >= (0.15*80)**2
               for i in range(len(pts)) for j in range(i+1, len(pts))), "min-sep violated"
    # spawn zones: plan 파싱, 구역 내 샘플·마진·이격·바닥+5, 다중 구역 배정, 결정성
    zones = {"a": {"min": [0.0, 0.0], "max": [1000.0, 800.0], "floor": 89.5},
             "b": {"min": [2000.0, 0.0], "max": [2600.0, 600.0], "floor": 120.0}}
    assert parse_spawn_plan(None, zones, 4) == [("a", 4)]
    assert parse_spawn_plan("a:2,b:2", zones, 4) == [("a", 2), ("b", 2)]
    for bad in ("c:4", "a:1,b:1"):
        try:
            parse_spawn_plan(bad, zones, 4)
            raise AssertionError(f"plan {bad!r} should fail")
        except ValueError:
            pass
    zp = sample_zone_positions(zones, [("a", 2), ("b", 2)], ["o1", "o2", "o3", "o4"], seed=7)
    assert set(zp) == {"o1", "o2", "o3", "o4"}
    for o in ("o1", "o2"):
        x, y, z = zp[o]
        assert 100.0 <= x <= 900.0 and 80.0 <= z <= 720.0 and abs(y - 94.5) < 1e-9, zp[o]
    for o in ("o3", "o4"):
        x, y, z = zp[o]
        assert 2060.0 <= x <= 2540.0 and 60.0 <= z <= 540.0 and abs(y - 125.0) < 1e-9, zp[o]
    assert zp == sample_zone_positions(zones, [("a", 2), ("b", 2)], ["o1", "o2", "o3", "o4"], seed=7)
    (x1, _, z1), (x2, _, z2) = zp["o1"], zp["o2"]
    assert (x1 - x2) ** 2 + (z1 - z2) ** 2 >= (0.15 * 800.0) ** 2, "zone min-sep violated"

    # sample_floor_positions: 바닥 없는 구역 기각, floor+offset 스폰, 허용창·폴백
    bounds2 = {"center": (0.0, 90.0, 0.0), "size": (1000.0, 300.0, 800.0), "is_y_up": True}
    pos2 = sample_floor_positions(bounds2, ["a", "b", "c"], 7,
                                  lambda h0, h1: 90.0 if h0 >= 0 else None, 90.0)
    assert set(pos2) == {"a", "b", "c"}
    assert all(p[0] >= 0 for p in pos2.values()), "no-floor half must be rejected"
    assert all(abs(p[1] - 95.0) < 1e-9 for p in pos2.values()), "spawn = floor + 5cm"
    assert sample_floor_positions(bounds2, ["a"], 7, lambda h0, h1: None, 90.0) == {}
    assert sample_floor_positions(bounds2, ["a"], 7, lambda h0, h1: 300.0, 90.0) == {}, "tol window"

    # near-miss trace 검증: 접근했고(gap 근처까지) 접촉은 없어야 통과
    def nm_trace(dists) -> str:
        out = ["timestamp,objid,x,y,z"]
        for i, d in enumerate(dists):
            ts = f"2026-07-28 10:00:{i // 10:02d}.{(i % 10) * 100:03d}"
            out.append(f"{ts},obj001,0.000,90.000,0.000")
            out.append(f"{ts},obj002,{d:.3f},90.000,0.000")
        return "\n".join(out) + "\n"
    ok = check_near_miss_trace(nm_trace([400, 300, 200, 100, 96, 100, 200, 400]), gap=95.0)
    assert ok["ok"] and ok["min_dist"] == 96.0 and ok["approached"] == 1, ok
    assert ok["min_pair"] == ("obj001", "obj002") and ok["frames"] == 8, ok
    # 접촉(gap 아래로 붙음) → FAIL + 위반 쌍 보고
    hit = check_near_miss_trace(nm_trace([400, 200, 80, 200]), gap=95.0)
    assert not hit["ok"] and hit["violations"][0]["pair"] == ("obj001", "obj002"), hit
    # 접근 자체가 없으면(멀찍이만) 대조 데이터로 무의미 → FAIL
    far = check_near_miss_trace(nm_trace([400, 380, 360, 400]), gap=95.0)
    assert not far["ok"] and far["approached"] == 0 and not far["violations"], far
    # tol 여유: gap - tol 이상이면 통과(30Hz 샘플링이 최근접 순간을 놓치는 경우)
    assert check_near_miss_trace(nm_trace([400, 94, 400]), gap=95.0, tol=2.0)["ok"]
    assert check_near_miss_trace("timestamp,objid,x,y,z\n", gap=95.0)["ok"] is False

    # 조우 다양성: 쌍 거리의 국소 극소점을 사건으로 뽑아 흩어짐을 잰다(위 검증과
    # 직교하는 축 — 같은 자리에서 같은 조우만 반복해도 gap 불변식은 지켜지므로,
    # 그 반복은 통과/실패가 아니라 이 지표로만 드러난다).
    def nm_trace_at(spots, sep_min=100.0, n=40) -> str:
        out = ["timestamp,objid,x,y,z"]
        f = 0
        for (sx, sz) in spots:
            for i in range(n):
                d = sep_min + abs(i - n // 2) * 30.0
                ts = f"2026-07-28 10:{f // 600:02d}:{(f // 10) % 60:02d}.{(f % 10) * 100:03d}"
                out.append(f"{ts},obj001,{sx:.3f},90.000,{sz:.3f}")
                out.append(f"{ts},obj002,{sx + d:.3f},90.000,{sz:.3f}")
                f += 1
        return "\n".join(out) + "\n"
    room = ((0.0, 0.0), (900.0, 900.0))
    one_spot = check_near_miss_diversity(nm_trace_at([(450.0, 450.0)] * 4), gap=95.0, bounds=room)
    scattered = check_near_miss_diversity(
        nm_trace_at([(150.0, 150.0), (700.0, 200.0), (200.0, 750.0), (750.0, 700.0)]),
        gap=95.0, bounds=room)
    assert one_spot["events"] == scattered["events"] == 4, (one_spot, scattered)
    assert one_spot["rms_radius"] == 0.0 and one_spot["min_sep"] == 0.0, one_spot
    assert scattered["rms_radius"] > 300.0 and scattered["min_sep"] > 300.0, scattered
    assert one_spot["coverage_eff"] == 0.25 and scattered["coverage_eff"] == 1.0, (one_spot, scattered)
    # 문턱(gap × event_frac) 밖에서만 오가면 조우로 세지 않는다
    assert check_near_miss_diversity(nm_trace_at([(450.0, 450.0)], sep_min=400.0),
                                     gap=95.0, bounds=room)["events"] == 0
    assert check_near_miss_diversity("timestamp,objid,x,y,z\n", gap=95.0)["events"] == 0
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        v = root / "_video_0003.mp4"
        v.write_text("v")
        v.with_suffix(".meta.json").write_text("{}")
        col = root / "collisions_x.csv"
        col.write_text("c")
        tr = root / "_trace_0003.csv"
        tr.write_text("t")
        ep = organize_outputs(root, 3, v, col, tr)
        names = sorted(p.name for p in ep.iterdir())
        assert names == ["_trace_0003.csv", "_video_0003.meta.json", "_video_0003.mp4", "collisions_x.csv"], names
        mpath = write_run_manifest(root, {"episodes": 5, "seed": 7}, cfgs, [0, 2], "abc123")
        mj = json.loads(mpath.read_text(encoding="utf-8"))
        assert mj["git_commit"] == "abc123" and len(mj["episodes"]) == 5
        assert [e["ok"] for e in mj["episodes"]] == [True, False, True, False, False]
        assert mj["episodes"][1]["base_time_s"] == cfgs[1].base_time_s
    print("self-test OK:", [(c.idx, c.n_objects, c.speed) for c in cfgs])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episodes", type=int, default=50)
    ap.add_argument("--out", type=str, default="artifacts/episodes")
    ap.add_argument("--duration", type=float, default=40.0)
    ap.add_argument("--min-objects", type=int, default=4)
    ap.add_argument("--max-objects", type=int, default=6)
    ap.add_argument("--speed-min", type=float, default=200.0)
    ap.add_argument("--speed-max", type=float, default=300.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--render-fps", type=int, default=30,
                    help="video/render fps (60의 약수; sim은 항상 60Hz). 60=데시메이션 없음. "
                         "30 선택 근거: 일지 #10 — 2배 단축 + 위상정렬·검수성 유지")
    ap.add_argument("--scene-profile", type=str, default=None,
                    help="scene_profiles.json의 프로파일 이름 — 아레나 범위·스테이지·"
                         "카메라를 명시 지정(데이터 로드 불필요). --stage/--camera를 "
                         "함께 주면 그쪽이 우선.")
    ap.add_argument("--stage", type=str, default=None,
                    help="USD to open (local path or omniverse:// URL); default: new empty stage")
    ap.add_argument("--camera", type=str, default=None,
                    help="capture camera: prim path (/World/..) or prim name to search; "
                         "default: /World/summarization_camera")
    ap.add_argument("--keep-positions", action="store_true",
                    help="skip random start positions; keep data-driven positions (GUI와 동일)")
    ap.add_argument("--spawn-zones", type=str, default=None,
                    help="스폰 구역 정의: JSON 파일 경로 또는 인라인 JSON "
                         '{"name": {"min": [x,z], "max": [x,z], "floor": 89.5}}. '
                         "미지정 시 궤적 범위 전체가 단일 구역")
    ap.add_argument("--spawn-plan", type=str, default=None,
                    help='구역별 객체 수 배정 "zoneA:2,zoneB:2" (합계 = 객체 수; min==max 필요)')
    ap.add_argument("--spawn-floor", type=float, default=89.5,
                    help="기본 구역의 바닥 높이(cm; run18~20 레이캐스트 실측값)")
    ap.add_argument("--near-miss", action="store_true",
                    help="near-miss 안무: 짝끼리 접근했다 gap에서 멈추고 흩어지기를 반복 — "
                         "접촉이 없어 GT 충돌 0건(근접만으로 오탐하는지 보는 대조 데이터셋)")
    ap.add_argument("--near-miss-gap", type=float, default=95.0,
                    help="near-miss 최소 중심거리(cm). 기본 95 = 룰 베이스라인 접촉 임계 "
                         "τ=90(rule_baseline.py) 바깥 → 좌표 직결 검출기도 발화하지 않아야 함")
    ap.add_argument("--near-miss-mode", type=str, default="swerve", choices=["swerve", "stop"],
                    help="near-miss 안무 방식. swerve(기본): 감속 없이 방향만 굽혀 스쳐 "
                         "지나감(gap 불변식은 반경 성분 캡으로 동일 보증). stop(v1): "
                         "감속+정지+방향전환 — GUI 육안 검수에서 '충돌처럼 보인다'는 "
                         "이유로 기각됐으나 감속 단서 대조군으로 옵션 유지")
    # swerve(v3) 회피 곡선의 모양 — 전부 gap 배수. 미지정 시 컨트롤러가 환경변수
    # (TTS_NEAR_MISS_*) → 코드 기본값(3.0 / 1.0 / 1.05) 순으로 해결한다.
    ap.add_argument("--near-miss-avoid-frac", type=float, default=None,
                    help="swerve 회피 개시 반경 = --near-miss-gap × 이 값(기본 3.0). "
                         "크게 하면 더 멀리서부터 완만히 휘기 시작한다")
    ap.add_argument("--near-miss-turn-radius-frac", type=float, default=None,
                    help="swerve 최소 선회 반경 = --near-miss-gap × 이 값(기본 1.0). "
                         "완만함을 좌우하는 값 — 키우면 큰 원을 그리듯 부드러워지지만, "
                         "너무 키우면 제때 못 피해 gap 불변식 안전망이 대신 급선회한다")
    ap.add_argument("--near-miss-aim-frac", type=float, default=None,
                    help="swerve 목표 통과 간격 = --near-miss-gap × 이 값(기본 1.05). "
                         "1.0이면 여유가 없어 안전망 개입이 잦아진다")
    # v4 대칭 파괴 — 조우 지점이 방 중앙에서 같은 기하로 반복되던 문제. 미지정 시
    # 컨트롤러가 환경변수(TTS_NEAR_MISS_START_JITTER_S 등) → 코드 기본값 순으로 해결.
    ap.add_argument("--near-miss-start-jitter", type=float, default=None,
                    help="접근 개시 지연을 객체마다 0~이 초에서 무작위 추출(기본 2.0). "
                         "늦게 도는 쪽으로 조우 지점이 끌려가 대칭이 깨진다. 0이면 끔")
    ap.add_argument("--near-miss-speed-min-frac", type=float, default=None,
                    help="사이클마다 뽑는 객체별 순항 속도의 하한 = --speed × 이 값(기본 0.7). "
                         "상한과 같게 두면 속도 비대칭을 끄는 것과 같다")
    ap.add_argument("--near-miss-speed-max-frac", type=float, default=None,
                    help="같은 순항 속도의 상한 = --speed × 이 값(기본 1.0, 1.0 초과 불가 — "
                         "지시 속도가 천장이라는 성질에 조향률 상한 계산이 기대고 있다)")
    ap.add_argument("--near-miss-depart-spread", type=float, default=None,
                    help="스침 뒤 이탈 방향을 '짝의 반대 방향 ±이 각도(deg)'에서 무작위 "
                         "추출(기본 90). 다음 사이클 시작 배치를 비대칭화한다. 0 이하면 끔")
    ap.add_argument("--check-trace", type=str, default=None,
                    help="오프라인 검증: trace CSV 경로를 --near-miss-gap 기준으로 판정하고 종료 "
                         "(Kit 불필요; 통과 시 종료코드 0)")
    ap.add_argument("--extra-objects", type=int, default=0,
                    help="궤적 데이터 외 합성 우주인 추가 수(physics 전용; keep-positions와 양립 불가)")
    ap.add_argument("--upload-uri", type=str, default=None,
                    help="에피소드·manifest 업로드 대상 (예: s3://time-travel-summarization/episodes/prod-YYYYMMDD)")
    ap.add_argument("--quit", action="store_true", help="quit Kit after finishing (batch/CI)")
    ap.add_argument("--self-test", action="store_true", help="run pure-helper tests without Kit")
    args = ap.parse_args()
    if args.self_test:
        _self_test()
        return
    if args.check_trace:
        text = Path(args.check_trace).read_text(encoding="utf-8")
        res = check_near_miss_trace(text, args.near_miss_gap)
        print(f"[check] {'OK' if res['ok'] else 'FAIL'} gap={args.near_miss_gap} "
              f"min_d={res['min_dist']} pair={res['min_pair']} "
              f"approached={res['approached']}/{res['pairs']} frames={res['frames']}")
        for v in res["violations"]:
            print(f"[check] violation: {v['pair']} min_d={v['min_dist']}")
        # 조우 다양성은 통과/실패를 가르지 않는다(합격 기준이 아니라 관찰 지표) —
        # 같은 자리에서 같은 조우만 반복하는 에피소드도 gap 불변식은 지키기 때문에
        # ok 판정과 분리해 따로 출력한다. 경계는 이 trace의 좌표 범위에서 유도한다.
        div = check_near_miss_diversity(text, args.near_miss_gap)
        print(f"[check] diversity: events={div['events']} room={div['room']} "
              f"spread={div['rms_radius']} ({div['spread_frac']} of short side) "
              f"coverage={div['coverage']} min_sep={div['min_sep']}")
        raise SystemExit(0 if res["ok"] else 1)
    try:
        run(args)
    finally:
        if args.quit:
            try:
                import omni.kit.app
                omni.kit.app.get_app().post_quit(0)
            except Exception:
                pass
            # post_quit 후에도 비데몬 스레드가 프로세스를 잡아 잔류하는 고질 →
            # 15초 내 정상 종료가 안 되면 강제 탈출(출력은 run()에서 이미 완결).
            # 배치 완료 알림이 "프로세스 종료" 이벤트에 의존하므로 종료 보장이 필수.
            import os as _os
            import threading as _threading
            import time as _time

            def _force_exit():
                _time.sleep(15.0)
                print("[gen] force exit (quit did not complete in 15s)")
                _os._exit(0)

            _threading.Thread(target=_force_exit, daemon=True).start()


if __name__ == "__main__":
    main()
