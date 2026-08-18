"""좌표 트랙 교란기 (WP1) — 현실 좌표 수집 시스템의 오류를 trace에 주입한다.

오류 모델 근거는 docs/교란기_오류모델_조사.md (MOT 표준 5대 오류 유형 대조):
    gaussian       측위 오차(deviation) — 행별 iid 노이즈(고주파 최악 케이스)
    id_switch      연관 오류 — t_s 이후 두 objid 영구 스왑
    fragmentation  연관 오류 — 가림 후 재연관 실패로 새 ID 발급
    occlusion      결손 — drop / hold / linear(오프라인 보간) / extrap(온라인 칼만 coasting)
    downsample     수집 주기 하향 — 시간 기반이라 소스 주기(실측 60Hz)와 무관하게
                   목표 Hz(실세계 1~20Hz 대역)를 낸다

원칙: GT는 절대 건드리지 않는다(입력 좌표만 오염). 모든 함수는 순수 변환 —
rows(dict 리스트)를 받아 새 리스트를 반환하고, 난수는 호출자가 시드로 고정한다.

self-test:  python3 perturbation/perturb.py
"""
from __future__ import annotations

import datetime
import random
from typing import Dict, List

Row = Dict[str, object]          # {"t": datetime, "objid": str, "x": float, "y": float, "z": float}
_TS_FMT = "%Y-%m-%d %H:%M:%S.%f"


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #
def load_trace(text: str) -> List[Row]:
    """trace CSV(timestamp,objid,x,y,z) -> Row 리스트. 시각순 정렬 보장."""
    rows: List[Row] = []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for ln in lines[1:]:
        ts, objid, x, y, z = (c.strip() for c in ln.split(","))
        rows.append({"t": datetime.datetime.strptime(ts, _TS_FMT), "objid": objid,
                     "x": float(x), "y": float(y), "z": float(z)})
    rows.sort(key=lambda r: (r["t"], r["objid"]))
    return rows


def dump_trace(rows: List[Row]) -> str:
    """Row 리스트 -> trace CSV 텍스트 (원본과 동일 형식: ms 3자리)."""
    out = ["timestamp,objid,x,y,z"]
    for r in sorted(rows, key=lambda r: (r["t"], r["objid"])):
        ts = r["t"].strftime(_TS_FMT)[:-3]
        out.append(f"{ts},{r['objid']},{r['x']:.3f},{r['y']:.3f},{r['z']:.3f}")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# 교란 변환
# --------------------------------------------------------------------------- #
def gaussian(rows: List[Row], sigma: float, seed: int) -> List[Row]:
    """바닥 평면(x,z)에 N(0,σ²) 노이즈. y(높이)는 고정 — 바닥 위 객체 전제."""
    rng = random.Random(seed)
    return [{**r, "x": r["x"] + rng.gauss(0.0, sigma),
             "z": r["z"] + rng.gauss(0.0, sigma)} for r in rows]


def id_switch(rows: List[Row], t_s: datetime.datetime, a: str, b: str) -> List[Row]:
    """t_s 이후 objid a<->b 영구 스왑 (트래커 identity switch — 좌표는 그대로)."""
    out = []
    for r in rows:
        oid = r["objid"]
        if r["t"] >= t_s:
            oid = b if oid == a else a if oid == b else oid
        out.append({**r, "objid": oid})
    return out


def fragmentation(rows: List[Row], obj: str, t0: datetime.datetime,
                  t1: datetime.datetime, new_id: str) -> List[Row]:
    """가림 후 재연관 실패: obj의 [t0,t1) 행 삭제, t1 이후는 새 ID로 발급.

    원 트랙은 t0에서 끊기고, 재연 화면에서는 **마지막 샘플 이후 사라진다**
    (playback.visibility의 dead-track despawn — 트랙 범위 [first,last] 밖은 숨김,
    허용오차 1s). 즉 결손 구간 이후로 옛 번호는 화면에 없다. 옛 주석의
    "마지막 좌표에 정지"는 despawn 도입 이전 서술이라 폐기한다 — 얼어붙은 분신이
    가짜 충돌을 만들던 문제를 그 모듈이 원천 차단한다.
    """
    out = []
    for r in rows:
        if r["objid"] == obj:
            if t0 <= r["t"] < t1:
                continue
            if r["t"] >= t1:
                r = {**r, "objid": new_id}
        out.append(r)
    return out


def occlusion(rows: List[Row], obj: str, t0: datetime.datetime,
              t1: datetime.datetime, policy: str) -> List[Row]:
    """obj의 [t0,t1) 구간 결손 + 채움 정책.

    drop    행 삭제만 (재연은 마지막 좌표 유지)
    hold    마지막 관측 좌표를 복사한 행으로 명시적 채움
    linear  결손 양끝을 직선으로 잇는 보간(오프라인 후처리 시나리오)
    extrap  직전 속도로 등속 연장(온라인 트래커 칼만 coasting) — 복귀 시 점프
    """
    if policy not in ("drop", "hold", "linear", "extrap"):
        raise ValueError(f"unknown occlusion policy: {policy}")
    kept = [r for r in rows if not (r["objid"] == obj and t0 <= r["t"] < t1)]
    if policy == "drop":
        return kept

    mine = sorted((r for r in rows if r["objid"] == obj), key=lambda r: r["t"])
    gap = [r for r in mine if t0 <= r["t"] < t1]          # 원래 그 시각들에 채워 넣는다
    before = [r for r in mine if r["t"] < t0]
    after = [r for r in mine if r["t"] >= t1]
    if not gap or not before:
        return kept                                        # 채울 기준이 없으면 결손 유지

    last = before[-1]
    fills: List[Row] = []
    for g in gap:
        dt = (g["t"] - last["t"]).total_seconds()
        if policy == "hold" or (policy == "linear" and not after) \
                or (policy == "extrap" and len(before) < 2):
            x, y, z = last["x"], last["y"], last["z"]      # 폴백은 전부 hold
        elif policy == "linear":
            nxt = after[0]
            span = (nxt["t"] - last["t"]).total_seconds() or 1.0
            f = dt / span
            x = last["x"] + (nxt["x"] - last["x"]) * f
            y = last["y"] + (nxt["y"] - last["y"]) * f
            z = last["z"] + (nxt["z"] - last["z"]) * f
        else:                                              # extrap: 등속 연장
            prev = before[-2]
            vdt = (last["t"] - prev["t"]).total_seconds() or 1.0
            x = last["x"] + (last["x"] - prev["x"]) / vdt * dt
            y = last["y"]
            z = last["z"] + (last["z"] - prev["z"]) / vdt * dt
        fills.append({"t": g["t"], "objid": obj, "x": x, "y": y, "z": z})
    return sorted(kept + fills, key=lambda r: (r["t"], r["objid"]))


def downsample(rows: List[Row], hz: float) -> List[Row]:
    """시간 기반 다운샘플 — 객체별로 직전 채택 후 1/hz초 경과 시 다음 샘플 채택.

    소스 기록 주기(실측 60Hz)와 무관하게 정확히 목표 Hz를 낸다. 이전의
    'keep_every개마다' 방식은 소스 주기 가정(30Hz)이 틀리면(실측 60Hz) 산출
    주기가 2배로 어긋났다(일지 #24). 시각은 조작하지 않는다.
    """
    interval = 1.0 / float(hz)
    last: Dict[str, datetime.datetime] = {}
    out = []
    for r in sorted(rows, key=lambda r: (str(r["objid"]), r["t"])):
        oid = str(r["objid"])
        prev = last.get(oid)
        t = r["t"]
        assert isinstance(t, datetime.datetime)
        if prev is None or (t - prev).total_seconds() >= interval - 1e-9:
            last[oid] = t
            out.append(r)
    return sorted(out, key=lambda r: (r["t"], r["objid"]))


# --------------------------------------------------------------------------- #
def _self_test() -> None:
    base = datetime.datetime(2026, 7, 22, 12, 0, 0)

    def mk(sec_ms: float, oid: str, x: float) -> Row:
        return {"t": base + datetime.timedelta(seconds=sec_ms), "objid": oid,
                "x": x, "y": 90.0, "z": 0.0}

    # 2객체 x 10샘플(0.1s 간격), obj001은 x가 1.0/s로 등속 증가
    rows = [mk(i * 0.1, "obj001", i * 0.1) for i in range(10)] \
         + [mk(i * 0.1, "obj002", 100.0) for i in range(10)]

    # I/O 왕복 보존
    assert load_trace(dump_trace(rows)) == sorted(
        [{**r, "x": round(float(r["x"]), 3)} for r in rows],
        key=lambda r: (r["t"], r["objid"]))

    # gaussian: 시드 결정성 + y 불변 + 행 수 보존
    g1, g2 = gaussian(rows, 5.0, 42), gaussian(rows, 5.0, 42)
    assert g1 == g2 and len(g1) == len(rows)
    assert all(r["y"] == 90.0 for r in g1)
    assert any(r["x"] != o["x"] for r, o in zip(g1, rows))

    # id_switch: t_s 이후만 스왑, 좌표는 자리 유지
    t_s = base + datetime.timedelta(seconds=0.5)
    sw = id_switch(rows, t_s, "obj001", "obj002")
    for r, o in zip(sorted(sw, key=lambda r: (r["t"], r["x"])),
                    sorted(rows, key=lambda r: (r["t"], r["x"]))):
        assert r["x"] == o["x"]
        expect = o["objid"] if o["t"] < t_s else \
            ("obj002" if o["objid"] == "obj001" else "obj001")
        assert r["objid"] == expect

    # fragmentation: 결손 3샘플 + 이후 새 ID, 타 객체 무영향
    t0 = base + datetime.timedelta(seconds=0.3)
    t1 = base + datetime.timedelta(seconds=0.6)
    fr = fragmentation(rows, "obj001", t0, t1, "obj003")
    ids = {str(r["objid"]) for r in fr}
    assert ids == {"obj001", "obj002", "obj003"}
    assert sum(str(r["objid"]) == "obj001" for r in fr) == 3      # t<0.3만
    assert sum(str(r["objid"]) == "obj003" for r in fr) == 4      # t>=0.6
    assert sum(str(r["objid"]) == "obj002" for r in fr) == 10

    # occlusion 4정책: 행 수·채움 값
    oc_drop = occlusion(rows, "obj001", t0, t1, "drop")
    assert sum(str(r["objid"]) == "obj001" for r in oc_drop) == 7
    oc_hold = occlusion(rows, "obj001", t0, t1, "hold")
    assert sum(str(r["objid"]) == "obj001" for r in oc_hold) == 10
    filled = [r for r in oc_hold if str(r["objid"]) == "obj001"
              and t0 <= r["t"] < t1]                              # type: ignore[operator]
    assert all(abs(float(r["x"]) - 0.2) < 1e-9 for r in filled)   # 마지막 관측 0.2 유지
    oc_lin = occlusion(rows, "obj001", t0, t1, "linear")
    lin = sorted((r for r in oc_lin if str(r["objid"]) == "obj001"
                  and t0 <= r["t"] < t1), key=lambda r: r["t"])   # type: ignore[operator]
    # 0.2(t=0.2)와 0.6(t=0.6) 사이 직선 -> t=0.3,0.4,0.5에서 0.3,0.4,0.5
    assert [round(float(r["x"]), 3) for r in lin] == [0.3, 0.4, 0.5]
    oc_ex = occlusion(rows, "obj001", t0, t1, "extrap")
    ex = sorted((r for r in oc_ex if str(r["objid"]) == "obj001"
                 and t0 <= r["t"] < t1), key=lambda r: r["t"])    # type: ignore[operator]
    # 등속 1.0/s 연장 -> 직선과 동일 값(등속 궤적이므로) — 폴백 아님을 값으로 확인
    assert [round(float(r["x"]), 3) for r in ex] == [0.3, 0.4, 0.5]
    try:
        occlusion(rows, "obj001", t0, t1, "cubic")
        raise AssertionError("unknown policy should raise")
    except ValueError:
        pass

    # downsample 시간 기반: 소스 10Hz(0.1s 간격) → 5Hz(0.2s)면 0/0.2/0.4/0.6/0.8 = 5개
    ds5 = downsample(rows, 5)
    assert sum(str(r["objid"]) == "obj001" for r in ds5) == 5
    assert sum(str(r["objid"]) == "obj002" for r in ds5) == 5
    # 2Hz(0.5s)면 0/0.5 = 2개. 소스 주기가 바뀌어도(120Hz) 같은 결과여야 한다
    dense = [mk(i * 0.05, "obj001", 0.0) for i in range(20)]   # 20Hz 소스
    assert sum(1 for _ in downsample(dense, 2)) == 2           # 0/0.5s = 2개 (주기 무관)
    # 30->10Hz 상당(3개 중 1개), 객체별 독립
    ds = downsample(rows, 100)  # 10Hz 소스에 100Hz 요청 → 전부 유지(10개)
    assert sum(str(r["objid"]) == "obj001" for r in ds) == 10
    assert sum(str(r["objid"]) == "obj002" for r in ds) == 10
    print("perturb self-test OK")


if __name__ == "__main__":
    _self_test()
