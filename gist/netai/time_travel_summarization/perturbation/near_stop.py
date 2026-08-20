"""near-stop 조건 trace 저작기 — 접근 → 무접촉 정지 → 이탈 (v3 계획서 §5-5).

**왜 물리 안무가 아니라 좌표 저작인가.** 재연 렌더러는 받은 좌표를 그대로 그리므로,
"접근하다 목표 거리에서 멈춰 1초 서 있다가 다시 간다"는 시나리오는 엔진에 새 모드를
넣지 않고 **실측 trace 조각의 재배열**로 만들 수 있다. 좌표 수준이라 정지 거리·정지
시간이 정확하고(이산 스텝 오버슈트 없음), 감속 캡이 만들던 경계 떨림("투명 유리에
부딪히는" 인상)도 원천적으로 없다.

**만드는 것.** 한 쌍(A,B)에 대해:
  1. 접근  — 실측 near-miss 에피소드에서 두 객체가 서로 다가가는 구간을 그대로 쓴다
             (운동 질감 = 실측). 중심거리가 목표 정지 거리 이하로 내려가는 첫 샘플에서 절단.
  2. 정지  — 절단 지점 좌표를 30Hz로 hold_s초만큼 반복. 반동이 없다(접촉이 없으므로).
  3. 이탈  — 실측 이탈 조각을 **첫 샘플이 정지 위치와 일치하도록 평행이동**하고,
             진행 방향만 away 방향으로 회전해 붙인다. 위치 공백은 구성상 불가능하다.
             몸 자세(orientation)는 건드리지 않는다 — 이 안무에는 헤딩 회전이 없다.
  4. 시각  — 전체를 균일 30Hz 격자로 재작성한다(결손 없음 → despawn 무관).

**GT**: 접촉이 없으므로 충돌 0건. 발화는 전부 FP로 계상한다(near_miss와 같은 규약).

self-test:  python3 perturbation/near_stop.py
"""
from __future__ import annotations

import datetime
import math
from typing import Dict, List, Optional, Tuple

from .perturb import Row

DEFAULT_HZ = 60.0             # 폴백 — 실제 표집률은 infer_hz()가 trace에서 유도한다
                              # (physics trace 실측 58.8Hz. 30Hz로 가정하면 재격자화에서
                              #  길이가 2배가 된다 — 실측 사고)
DEFAULT_HOLD_S = 1.0          # 충돌 안무의 pause와 동일(impact 0.2s는 반동 = 제외)
CONTACT_DISTANCE = 60.0       # regime3 접촉거리 2r — 정지 거리는 이보다 충분히 커야 한다


def infer_hz(rows: List[Row], default: float = DEFAULT_HZ) -> float:
    """trace의 실제 표집률(Hz) — 프레임 시각 간격의 중앙값에서 유도."""
    ts = sorted({r["t"] for r in rows})
    if len(ts) < 3:
        return default
    gaps = sorted((b - a).total_seconds() for a, b in zip(ts, ts[1:]))
    med = gaps[len(gaps) // 2]
    return (1.0 / med) if med > 0 else default


def horiz_dist(a: Row, b: Row) -> float:
    """수평 중심거리(x,z) — 접촉 판정과 같은 축(y는 키 성분이라 제외)."""
    return math.hypot(a["x"] - b["x"], a["z"] - b["z"])


def frames_by_time(rows: List[Row]) -> List[Tuple[datetime.datetime, Dict[str, Row]]]:
    """시각별 프레임 묶음 (시각 오름차순)."""
    acc: Dict[datetime.datetime, Dict[str, Row]] = {}
    for r in rows:
        acc.setdefault(r["t"], {})[r["objid"]] = r
    return sorted(acc.items())


def find_stop_frame(rows: List[Row], a: str, b: str, stop_distance: float
                    ) -> Optional[int]:
    """두 객체의 수평거리가 stop_distance 이하로 내려가는 **첫 프레임 인덱스**.

    없으면 None(그 쌍은 그만큼 가까워지지 않는다 → 저작 대상에서 제외).
    """
    frames = frames_by_time(rows)
    for i, (_t, objs) in enumerate(frames):
        if a in objs and b in objs and horiz_dist(objs[a], objs[b]) <= stop_distance:
            return i
    return None


def _rotate_xz(dx: float, dz: float, cos_t: float, sin_t: float) -> Tuple[float, float]:
    return dx * cos_t - dz * sin_t, dx * sin_t + dz * cos_t


def author_near_stop(rows: List[Row], a: str, b: str, stop_distance: float,
                     hold_s: float = DEFAULT_HOLD_S,
                     depart_rows: Optional[List[Row]] = None,
                     hz: Optional[float] = None,
                     pre_s: Optional[float] = None,
                     post_s: Optional[float] = None) -> List[Row]:
    """접근 → 정지(hold_s) → 이탈 trace를 만든다. 30Hz 균일 격자로 재작성.

    depart_rows가 None이면 원본의 정지 이후 구간을 이탈 조각으로 쓰되, **서로
    멀어지는 방향으로 반사**해 재접근을 막는다. 다른 객체(비대상)는 접근 구간의
    마지막 위치에 그대로 둔다 — 이 조건이 재는 것은 대상 쌍의 운동이므로 제3자가
    창 안에서 새 조우를 만들면 안 된다.
    """
    hz = hz or infer_hz(rows)
    frames = frames_by_time(rows)
    stop_i = find_stop_frame(rows, a, b, stop_distance)
    if stop_i is None:
        raise ValueError(f"{a},{b}: {stop_distance} 이하로 접근하는 프레임이 없다")
    approach = frames[: stop_i + 1]
    stop_objs = approach[-1][1]

    # --- 이탈 방향: 서로 반대(away) 단위벡터 ---
    ax, az = stop_objs[a]["x"], stop_objs[a]["z"]
    bx, bz = stop_objs[b]["x"], stop_objs[b]["z"]
    d = math.hypot(ax - bx, az - bz) or 1.0
    away = {a: ((ax - bx) / d, (az - bz) / d), b: ((bx - ax) / d, (bz - az) / d)}

    # --- 이탈 조각: 원본의 정지 이후 구간에서 각 객체의 프레임별 이동량을 가져와
    #     away 방향으로 회전해 누적(속도 크기·요동 = 실측, 방향만 우리 것) ---
    src = depart_rows if depart_rows is not None else rows
    src_frames = frames_by_time(src)
    tail = src_frames[stop_i + 1:] if depart_rows is None else src_frames
    steps: Dict[str, List[Tuple[float, float, float]]] = {a: [], b: []}
    for oid in (a, b):
        prev = None
        for _t, objs in tail:
            if oid not in objs:
                continue
            cur = objs[oid]
            if prev is not None:
                steps[oid].append((cur["x"] - prev["x"], cur["y"] - prev["y"],
                                   cur["z"] - prev["z"]))
            prev = cur

    out: List[Row] = []
    t0 = approach[0][0]
    step = datetime.timedelta(seconds=1.0 / hz)
    idx = 0

    def others_at(k: int) -> Dict[str, Row]:
        """비대상 객체는 **원본 궤적을 계속 따라간다**(정지·이탈 구간 내내).

        대상 쌍만 멈추고 나머지도 얼어붙으면 화면 전체가 정지해 "정지 형식" 자체가
        새 단서가 된다 — 이 조건이 재려는 것은 대상 쌍의 접근-정지이므로 배경은
        살아 있어야 한다. 원본이 끝나면 마지막 프레임을 유지한다.
        """
        src = frames[min(k, len(frames) - 1)][1]
        return {oid: r for oid, r in src.items() if oid not in (a, b)}

    # 1) 접근 구간 그대로
    for _t, objs in approach:
        for oid, r in objs.items():
            out.append({**r, "t": t0 + idx * step})
        idx += 1
    # 2) 정지 — 대상 쌍만 마지막 좌표 반복, 비대상은 원본 궤적 계속
    n_hold = max(1, int(round(hold_s * hz)))
    for h in range(n_hold):
        for oid in (a, b):
            out.append({**stop_objs[oid], "t": t0 + idx * step})
        for oid, r in others_at(stop_i + 1 + h).items():
            out.append({**r, "t": t0 + idx * step})
        idx += 1
    # 3) 이탈 — 대상 쌍은 away 방향(실측 스텝 크기 재사용), 비대상은 원본 궤적 계속
    pos = {oid: (r["x"], r["y"], r["z"]) for oid, r in stop_objs.items() if oid in (a, b)}
    # away 방향으로의 회전각: 원본 스텝의 평균 진행 방향을 away로 돌린다
    rot: Dict[str, Tuple[float, float]] = {}
    for oid in (a, b):
        mx = sum(s[0] for s in steps[oid]) or 0.0
        mz = sum(s[2] for s in steps[oid]) or 0.0
        m = math.hypot(mx, mz)
        if m < 1e-9:
            rot[oid] = (1.0, 0.0)
            continue
        ux, uz = mx / m, mz / m
        wx, wz = away[oid]
        cos_t = ux * wx + uz * wz
        sin_t = ux * wz - uz * wx          # u -> w 회전 (2D 외적)
        rot[oid] = (cos_t, -sin_t)          # 스텝을 away 쪽으로 돌린다
    n_steps = max(len(steps[a]), len(steps[b]))
    for k in range(n_steps):
        for oid in (a, b):
            if k < len(steps[oid]):
                dx, dy, dz = steps[oid][k]
                rx, rz = _rotate_xz(dx, dz, *rot[oid])
                x, y, z = pos[oid]
                pos[oid] = (x + rx, y + dy, z + rz)
            x, y, z = pos[oid]
            out.append({"t": t0 + idx * step, "objid": oid, "x": x, "y": y, "z": z})
        for oid, r in others_at(stop_i + 1 + n_hold + k).items():
            out.append({**r, "t": t0 + idx * step})
        idx += 1
    if pre_s is None and post_s is None:
        return out
    # 클립 창만 남긴다 — 렌더 비용을 줄이고, 창 밖 제3자 조우가 GT를 오염시키는 것을
    # 구조적으로 배제한다(창 안 무접촉은 verify가 따로 확인).
    t_stop = t0 + (len(approach)) * step          # 정지 시작 시각
    lo = t_stop - datetime.timedelta(seconds=pre_s if pre_s is not None else 1e9)
    hi = t_stop + datetime.timedelta(seconds=(post_s if post_s is not None else 1e9) + hold_s)
    return [r for r in out if lo <= r["t"] <= hi]


def verify_near_stop(rows: List[Row], a: str, b: str, stop_distance: float,
                     hold_s: float = DEFAULT_HOLD_S,
                     contact_distance: float = CONTACT_DISTANCE,
                     max_step: float = 8.0, hz: Optional[float] = None) -> dict:
    """저작 결과 자동 검증 — 렌더 전에 반드시 통과해야 한다(§5-5).

    ① 전 쌍 최소 수평거리 > contact_distance (GT 무접촉)
    ② 표본 간격 균일(결손 없음)
    ③ 인접 샘플 이동량 ≤ max_step (순간이동 없음 — 이음새 검출)
    ④ 정지 구간이 실제로 hold_s 동안 정지(대상 쌍 이동량 0)
    """
    hz = hz or infer_hz(rows)
    frames = frames_by_time(rows)
    ts = [t for t, _ in frames]
    gaps = {round((ts[i + 1] - ts[i]).total_seconds(), 4) for i in range(len(ts) - 1)}
    objs_all = sorted({r["objid"] for r in rows})
    min_pair = float("inf")
    for _t, objs in frames:
        for i in range(len(objs_all)):
            for j in range(i + 1, len(objs_all)):
                oi, oj = objs_all[i], objs_all[j]
                if oi in objs and oj in objs:
                    min_pair = min(min_pair, horiz_dist(objs[oi], objs[oj]))
    max_move = 0.0
    for oid in objs_all:
        seq = [objs[oid] for _t, objs in frames if oid in objs]
        for p, q in zip(seq, seq[1:]):
            max_move = max(max_move, horiz_dist(p, q))
    # 정지 구간 검출: 대상 쌍이 움직이지 않는 최장 연속 구간
    still, best_still = 0, 0
    for (_t1, o1), (_t2, o2) in zip(frames, frames[1:]):
        moved = max(horiz_dist(o1[o], o2[o]) for o in (a, b) if o in o1 and o in o2)
        still = still + 1 if moved < 1e-6 else 0
        best_still = max(best_still, still)
    return {
        "ok": (min_pair > contact_distance and len(gaps) == 1 and max_move <= max_step
               and abs(best_still / hz - hold_s) <= 0.1),
        "min_pair_distance": round(min_pair, 3),
        "sample_gaps_s": sorted(gaps),
        "max_step_move": round(max_move, 3),
        "hold_measured_s": round(best_still / hz, 3),
        "hz": round(hz, 2),
        "frames": len(frames),
    }


# --------------------------------------------------------------------------- #
def _self_test() -> None:
    base = datetime.datetime(2026, 8, 20, 12, 0, 0)

    HZ_T = 60.0

    def mk(i, oid, x, z, y=94.5):
        return {"t": base + datetime.timedelta(seconds=i / HZ_T), "objid": oid,
                "x": float(x), "y": float(y), "z": float(z)}

    # 두 객체가 x축에서 마주 접근(스텝 2씩) → 초기 거리 200, 매 프레임 4씩 감소
    rows = []
    for i in range(40):
        rows.append(mk(i, "obj001", -100 + 2 * i, 0))
        rows.append(mk(i, "obj002", 100 - 2 * i, 0))
    # 정지 거리 80에서 멈춰야 한다
    i_stop = find_stop_frame(rows, "obj001", "obj002", 80.0)
    assert i_stop == 30, i_stop            # 200 - 4*30 = 80
    authored = author_near_stop(rows, "obj001", "obj002", 80.0, hold_s=1.0)
    v = verify_near_stop(authored, "obj001", "obj002", 80.0)
    assert v["ok"], v
    assert v["min_pair_distance"] >= 80.0 - 1e-6, v      # 접촉거리 60 훨씬 위
    assert v["sample_gaps_s"] == [round(1 / HZ_T, 4)], v  # 균일 격자(원본 표집률 유지)
    assert abs(v["hold_measured_s"] - 1.0) < 0.05, v     # 정지 1초
    # 이탈이 실제로 멀어지는가 — 마지막 프레임 거리가 정지 거리보다 크다
    fr = frames_by_time(authored)
    assert horiz_dist(fr[-1][1]["obj001"], fr[-1][1]["obj002"]) > 80.0
    # 이음새: 순간이동 없음(스텝 상한 이내)
    assert v["max_step_move"] <= 8.0, v
    # 접근하지 않는 쌍은 예외
    far = [mk(i, "obj003", 500, 500) for i in range(5)] + [mk(i, "obj004", -500, -500) for i in range(5)]
    try:
        author_near_stop(far, "obj003", "obj004", 80.0)
        raise AssertionError("가까워지지 않는 쌍인데 통과했다")
    except ValueError:
        pass
    print("near_stop self-test OK")


if __name__ == "__main__":
    _self_test()
