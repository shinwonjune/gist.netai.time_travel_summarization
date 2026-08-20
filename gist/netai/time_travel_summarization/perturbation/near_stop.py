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
                     post_s: Optional[float] = None,
                     align_seconds: bool = False) -> List[Row]:
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

    # --- 이탈 조각: **자기 접근 구간을 뒤집어 쓴다**(왔던 길을 되돌아 나간다).
    #
    # 왜 원본의 "정지 이후 구간"을 안 쓰는가: 원료가 충돌 에피소드이므로 그 구간에는
    # 충돌 안무가 통째로 들어 있다 — 반동(impact) → pause(완전 정지) → 방향 재추첨.
    # 그걸 away로 회전만 해서 붙이면 우리 이탈도 그 궤적을 따라 꺾여, 멀어지다가
    # 다시 서로에게 되돌아온다(실측: 121 → 125 → 117로 복귀). 접촉이 없는 조건에
    # 접촉의 운동 서명을 심는 셈이라 조건 자체가 오염된다.
    #
    # 접근 구간의 스텝을 **역순으로 뒤집어 부호를 반전**하면, 각 객체가 자기가 들어온
    # 속도 프로파일 그대로 바깥으로 나간다 — 실측 질감(요동·속력 변화)은 유지되고
    # 거리는 단조 증가가 보장된다(두 객체가 각자 들어온 방향의 반대로 가므로).
    # depart_rows를 명시하면 그쪽을 우선한다(외부 조각 주입 경로는 유지).
    steps: Dict[str, List[Tuple[float, float, float]]] = {a: [], b: []}
    if depart_rows is not None:
        for oid in (a, b):
            prev = None
            for _t, objs in frames_by_time(depart_rows):
                if oid not in objs:
                    continue
                cur = objs[oid]
                if prev is not None:
                    steps[oid].append((cur["x"] - prev["x"], cur["y"] - prev["y"],
                                       cur["z"] - prev["z"]))
                prev = cur
    else:
        for oid in (a, b):
            seq = [objs[oid] for _t, objs in approach if oid in objs]
            fwd = [(q["x"] - p["x"], q["y"] - p["y"], q["z"] - p["z"])
                   for p, q in zip(seq, seq[1:])]
            steps[oid] = [(-dx, -dy, -dz) for dx, dy, dz in reversed(fwd)]

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
    # 스텝 뭉치의 평균 진행 방향을 away로 맞추는 회전. 역순 접근 스텝은 이미 바깥을
    # 향하므로 회전각이 0에 가깝고, depart_rows를 외부에서 준 경우에만 실제로 돈다.
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
        sin_t = wx * uz - wz * ux          # u -> w 회전각의 sin (2D 외적, w×u 부호)
        rot[oid] = (cos_t, -sin_t)
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
    if pre_s is not None or post_s is not None:
        # 클립 창만 남긴다 — 렌더 비용을 줄이고, 창 밖 제3자 조우가 GT를 오염시키는 것을
        # 구조적으로 배제한다(창 안 무접촉은 verify가 따로 확인).
        t_stop = t0 + (len(approach)) * step          # 정지 시작 시각
        lo = t_stop - datetime.timedelta(seconds=pre_s if pre_s is not None else 1e9)
        hi = t_stop + datetime.timedelta(seconds=(post_s if post_s is not None else 1e9) + hold_s)
        out = [r for r in out if lo <= r["t"] <= hi]
    if not align_seconds:
        return out
    # 재연 잡은 시각을 **초 단위**로만 받는다(replay_start/end). 시작이 초 경계보다
    # 늦으면 가드가 거부하고, 끝이 초 경계보다 이르면 내림에서 마지막 1초(=이탈
    # 구간)가 통째로 잘린다 — 실측 사고. 그래서 전체를 평행이동해 **시작을 정확히
    # 초 경계에 놓고**, 끝은 다음 초 경계까지 마지막 프레임을 유지해 채운다.
    frames = frames_by_time(out)
    t_first, t_last = frames[0][0], frames[-1][0]
    shift = (t_first.replace(microsecond=0) + datetime.timedelta(seconds=1)) - t_first
    out = [{**r, "t": r["t"] + shift} for r in out]
    t_last += shift
    pad_to = t_last.replace(microsecond=0) + datetime.timedelta(seconds=1)
    last_objs = frames[-1][1]
    k = 1
    while t_last + k * step < pad_to:
        for oid, r in last_objs.items():
            out.append({**r, "t": t_last + k * step})
        k += 1
    # 마지막 프레임을 **정확히 초 경계**에 하나 더 놓는다. 이게 없으면 trace 끝이
    # 04.983 같은 값이 되고, 재연 end가 초 단위 내림이라 마지막 1초(이탈)가 잘린다.
    for oid, r in last_objs.items():
        out.append({**r, "t": pad_to})
    return out


def verify_near_stop(rows: List[Row], a: str, b: str, stop_distance: float,
                     hold_s: float = DEFAULT_HOLD_S,
                     contact_distance: float = CONTACT_DISTANCE,
                     max_step: float = 8.0, hz: Optional[float] = None) -> dict:
    """저작 결과 자동 검증 — 렌더 전에 반드시 통과해야 한다(§5-5).

    ① 전 쌍 최소 수평거리 > contact_distance (GT 무접촉)
    ② 결손 없음 — 표본 간격이 중앙값보다 크게 벌어지는 곳이 없다. "모든 간격이
       완전히 같을 것"을 요구하지는 않는다: 초 경계 정렬(align_seconds)이 마지막
       프레임을 격자보다 이르게 놓아 **짧은** 간격 하나를 남기는데, 그건 결손이
       아니므로 통과시켜야 한다. 검출 대상은 어디까지나 "간격이 커지는" 쪽이다.
    ③ 인접 샘플 이동량 ≤ max_step (순간이동 없음 — 이음새 검출)
    ④ 정지 구간이 실제로 hold_s 동안 정지(대상 쌍 이동량 0)
    ⑤ **이탈이 실제로 멀어진다** — 정지 종료 후 쌍 거리가 단조 증가(허용 오차 이내).
       충돌 에피소드의 사후 구간을 이탈 템플릿으로 쓰면 그 안의 반동·재추첨 때문에
       멀어지다 되돌아오는데(실측 121→125→117), 그건 접촉 없는 조건에 접촉의 운동
       서명을 심는 것이라 반드시 걸러야 한다.
    """
    hz = hz or infer_hz(rows)
    frames = frames_by_time(rows)
    ts = [t for t, _ in frames]
    gap_list = [(ts[i + 1] - ts[i]).total_seconds() for i in range(len(ts) - 1)]
    gaps = {round(g, 4) for g in gap_list}
    med_gap = sorted(gap_list)[len(gap_list) // 2] if gap_list else 0.0
    no_dropout = bool(gap_list) and max(gap_list) <= med_gap * 1.5
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
    still, best_still, best_end = 0, 0, 0
    for k, ((_t1, o1), (_t2, o2)) in enumerate(zip(frames, frames[1:]), start=1):
        moved = max(horiz_dist(o1[o], o2[o]) for o in (a, b) if o in o1 and o in o2)
        still = still + 1 if moved < 1e-6 else 0
        if still > best_still:
            best_still, best_end = still, k
    # 이탈 단조성: 정지 종료 이후 거리가 줄어드는 구간이 있으면 실패로 본다.
    # (표집 잡음 몫으로 0.5까지는 허용)
    depart = [horiz_dist(objs[a], objs[b]) for _t, objs in frames[best_end:]
              if a in objs and b in objs]
    drops = [depart[i] - depart[i + 1] for i in range(len(depart) - 1)]
    max_drop = max(drops) if drops else 0.0
    depart_ok = (len(depart) < 3) or max_drop <= 0.5
    return {
        "ok": (min_pair > contact_distance and no_dropout and max_move <= max_step
               and abs(best_still / hz - hold_s) <= 0.1 and depart_ok),
        "min_pair_distance": round(min_pair, 3),
        "sample_gaps_s": sorted(gaps),
        "no_dropout": no_dropout,
        "max_step_move": round(max_move, 3),
        "hold_measured_s": round(best_still / hz, 3),
        "depart_ok": depart_ok,
        "depart_max_drop": round(max_drop, 3),
        "depart_gain": round(depart[-1] - depart[0], 1) if len(depart) > 1 else 0.0,
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
    # 초 경계 정렬 — 실제 스케일(5초·60Hz) 합성 trace로 창이 온전히 남는지 본다.
    # 접근이 짧은 trace로는 pre_s를 채울 수 없어 창이 줄어드는 것이 정상이므로,
    # 여기서는 접근 2.7초 + 이탈 2.3초를 가진 trace를 쓴다.
    long_rows = []
    for i in range(300):                      # 5초 @60Hz
        long_rows.append(mk(i, "obj001", -200 + 1.0 * i, 0))
        long_rows.append(mk(i, "obj002", 200 - 1.0 * i, 0))
    aligned = author_near_stop(long_rows, "obj001", "obj002", 80.0, hold_s=1.0,
                               pre_s=1.0, post_s=1.0, align_seconds=True)
    fa = frames_by_time(aligned)
    assert fa[0][0].microsecond == 0, fa[0][0]          # 시작 = 초 경계
    span = (fa[-1][0] - fa[0][0]).total_seconds()
    # 접근 1 + 정지 1 + 이탈 1 = 3.0초. 창 경계가 프레임 격자에 떨어지지 않으면
    # 마지막 한 프레임이 빠지므로 1프레임(1/60초) 허용한다.
    assert span >= 3.0 - 2.0 / 60, span
    # 끝이 초 경계까지 패딩됐다 → 재연 end 내림에서 이탈이 잘리지 않는다
    assert fa[-1][0].microsecond == 0, fa[-1][0]
    v_long = verify_near_stop(aligned, "obj001", "obj002", 80.0)
    assert v_long["min_pair_distance"] >= 80.0 - 1e-6, v_long
    # 이탈은 단조 증가여야 한다(되돌아오면 실패)
    assert v_long["depart_ok"] and v_long["depart_gain"] > 0, v_long

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
