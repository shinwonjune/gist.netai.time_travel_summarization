"""교란 조건 일괄 측정 드라이버 (WP1 실행 + WP2 측정).

replay_fidelity가 만든 15쌍(pairs.json·로컬 trace·GT·clean 추론 결과)을 재사용해,
조건별로 교란 trace 생성 -> L40 업로드 -> replay 잡 -> 추론 -> 채점을 완주한다.

조건 매트릭스 (docs/교란기_오류모델_조사.md v0 스코프):
    g10/g25/g50            gaussian σ=10/25/50 (충돌 판정 거리 ~72 대비)
    switch                 첫 GT 이벤트 3s 전, 당사자 2명 ID 스왑
    frag                   첫 이벤트 당사자를 [이벤트-4s, -2s) 가림 후 새 ID로 복귀
    occ-hold/linear/extrap 첫 이벤트 당사자를 이벤트 걸친 3s 결손 + 채움 정책
    ds10/ds5/ds2/ds1       30Hz -> 10/5/2/1Hz

채점은 2기준: ① 원래 GT(사용자에게 전달된 정보의 옳음) ② 화면 기준 GT(교란을
GT에도 적용 — VLM이 화면을 충실히 읽었는가). ①-② 격차 = 데이터 유래 오류.

실행 (fidelity 파이프라인 완료 후):
    python3 -m gist.netai.time_travel_summarization.automation.perturb_eval
전 단계 멱등 — 재실행하면 실패 지점부터 이어한다. 순수 헬퍼: --self-test
"""
from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import urllib.error
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from gist.netai.time_travel_summarization.automation.replay_fidelity import (
    APP_KIT, DEFAULT_REMOTE_EXT, DEFAULT_SSH_HOST, MODEL, PRESET, Tunnel,
    api_get, api_post, f1, make_client, match_events, parse_pred_events, poll_job,
    ssh_tar_fetch,
)
from gist.netai.time_travel_summarization.perturbation import (
    downsample, dump_trace, fragmentation, gaussian, id_switch, load_trace, occlusion,
)

CONDITIONS: List[dict] = [
    {"name": "g10", "kind": "gaussian", "sigma": 10.0},
    {"name": "g25", "kind": "gaussian", "sigma": 25.0},
    {"name": "g50", "kind": "gaussian", "sigma": 50.0},
    {"name": "switch", "kind": "switch"},
    {"name": "frag", "kind": "frag"},
    {"name": "occ-hold", "kind": "occlusion", "policy": "hold", "dur_s": 3.0},
    {"name": "occ-linear", "kind": "occlusion", "policy": "linear", "dur_s": 3.0},
    {"name": "occ-extrap", "kind": "occlusion", "policy": "extrap", "dur_s": 3.0},
    # dsr* = 시간 기반 다운샘플의 '실측' Hz (소스 60Hz 무관). 이전 ds*는 60Hz를
    # 30Hz로 오인해 실측 주기가 라벨의 2배였음 → 구분되는 새 이름으로 재생성.
    {"name": "dsr20", "kind": "downsample", "hz": 20},
    {"name": "dsr10", "kind": "downsample", "hz": 10},
    {"name": "dsr5", "kind": "downsample", "hz": 5},
    {"name": "dsr2", "kind": "downsample", "hz": 2},
    {"name": "dsr1", "kind": "downsample", "hz": 1},
]


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #
def stable_seed(*parts: str) -> int:
    """조건·에피소드 이름에서 결정적 시드 (재실행 재현성)."""
    import zlib
    return zlib.crc32("|".join(parts).encode())


def abs_dt(cap_start: datetime.datetime, sec_of_day: int) -> datetime.datetime:
    """GT의 '자정 기준 초'를 캡처 날짜의 datetime으로."""
    return datetime.datetime.combine(cap_start.date(), datetime.time()) \
        + datetime.timedelta(seconds=sec_of_day)


def apply_remap(gt: Dict[int, Set[int]], remap) -> Dict[int, Set[int]]:
    """교란을 GT 라벨에도 적용한 '화면 기준 정답' 생성. remap 없으면 그대로.

    remap은 단일 op(dict) 또는 op 리스트 — 전 이벤트 교란(frag 다중 rename)이
    여러 op을 순서대로 적용하기 위함. 각 op: swap(pair) 또는 rename(from,to),
    after_s 이후 시각에만 적용.
    """
    if not remap:
        return gt
    ops = remap if isinstance(remap, list) else [remap]
    out: Dict[int, Set[int]] = {}
    for t, ids in gt.items():
        ids = set(ids)
        for op in ops:
            if t >= op["after_s"]:
                if op["type"] == "swap":
                    a, b = op["pair"]
                    ids = {b if i == a else a if i == b else i for i in ids}
                elif op["type"] == "rename":
                    ids = {op["to"] if i == op["from"] else i for i in ids}
        out[t] = ids
    return out


def _sec(t: datetime.datetime) -> int:
    return t.hour * 3600 + t.minute * 60 + t.second


def plan_perturbation(cond: dict, pair: dict,
                      label_to_objid: Dict[int, str]) -> Tuple[dict, object]:
    """조건 x 에피소드 -> 실행 계획(plan)과 GT remap.

    - gaussian/downsample: 전역 교란(모든 행) — 표적 개념 없음.
    - switch: 첫 이벤트 3s 전 1회 영구 스왑(당사자x비당사자) — 이후 전 이벤트 파급.
    - occ/frag: **모든 GT 이벤트**를 표적으로(전 이벤트 교란) — 에피소드당 이벤트
      수만큼 교란해 표본을 늘린다. occ은 이벤트마다 참가자 1명을 그 이벤트 구간
      가림, frag는 충돌 참가 객체별로 첫 충돌 전 1회 fragment(새 ID 영구).
    plan은 사이드카에 그대로 저장(계보). remap은 op 리스트(화면GT 생성).
    """
    cap = datetime.datetime.fromisoformat(pair["capture_start"])
    dur = float(pair["duration_s"])
    events = sorted((int(t), sorted(ids)) for t, ids in pair["gt_events"].items())
    kind = cond["kind"]

    if kind == "gaussian":
        return {"kind": "gaussian", "sigma": cond["sigma"]}, None
    if kind == "downsample":
        return {"kind": "downsample", "hz": cond["hz"]}, None

    def clamp(t: datetime.datetime) -> datetime.datetime:
        lo = cap + datetime.timedelta(seconds=1)
        hi = cap + datetime.timedelta(seconds=dur - 1)
        return max(lo, min(t, hi))

    if kind == "switch":
        # 표적 = 첫 이벤트. 스왑 상대는 비당사자(당사자끼리는 집합이라 {a,b}->{b,a} 무효).
        ev_dt = abs_dt(cap, events[0][0]) if events \
            else cap + datetime.timedelta(seconds=dur / 2)
        ev_ids = events[0][1] if events else sorted(label_to_objid)[:1]
        target = ev_ids[0]
        outsider = next((lbl for lbl in sorted(label_to_objid) if lbl not in ev_ids),
                        sorted(label_to_objid)[-1])
        t_s = clamp(ev_dt - datetime.timedelta(seconds=3))
        remap = [{"type": "swap", "after_s": _sec(t_s), "pair": [target, outsider]}]
        return {"kind": "switch", "t_s": t_s.isoformat(),
                "a": label_to_objid[target], "b": label_to_objid[outsider]}, remap

    if kind == "occlusion":
        half = datetime.timedelta(seconds=cond["dur_s"] / 2)
        targets = events or [(int((_sec(cap) + dur / 2)), sorted(label_to_objid)[:1])]
        ops = []
        for ev_t, ev_ids in targets:
            ev_dt = abs_dt(cap, ev_t)
            t0, t1 = clamp(ev_dt - half), clamp(ev_dt + half)
            if t1 > t0:
                ops.append({"obj": label_to_objid[ev_ids[0]], "t0": t0.isoformat(),
                            "t1": t1.isoformat(), "policy": cond["policy"]})
        return {"kind": "occlusion", "ops": ops}, None

    if kind == "frag":
        # 충돌 참가 객체별로 1회 fragment — 그 객체의 첫 충돌 [−4s,−2s) 가림 후 새 ID.
        # 각 원본 라벨은 최대 1회 개명(1:1 remap) → ID 부기 단순. 이후 그 객체의
        # 모든 충돌이 화면GT에서 새 라벨로 이어진다.
        first_ev: Dict[int, int] = {}
        for ev_t, ev_ids in events:
            for lb in ev_ids:
                first_ev.setdefault(lb, ev_t)
        ops, remap = [], []
        next_label = max(label_to_objid) + 1
        for lb in sorted(first_ev):
            ev_dt = abs_dt(cap, first_ev[lb])
            t0 = clamp(ev_dt - datetime.timedelta(seconds=4))
            t1 = clamp(ev_dt - datetime.timedelta(seconds=2))
            if t1 <= t0:
                continue  # 창이 데이터 시작에 눌려 무효 → 이 객체는 건너뜀
            ops.append({"obj": label_to_objid[lb], "t0": t0.isoformat(),
                        "t1": t1.isoformat(), "new_id": f"obj{next_label:03d}"})
            remap.append({"type": "rename", "after_s": _sec(t1),
                          "from": lb, "to": next_label})
            next_label += 1
        return {"kind": "frag", "ops": ops}, remap
    raise ValueError(f"unknown kind {kind}")


def perturb_rows(plan: dict, rows: List[dict], seed: int) -> List[dict]:
    def _dt(s: str) -> datetime.datetime:
        return datetime.datetime.fromisoformat(s)

    kind = plan["kind"]
    if kind == "gaussian":
        return gaussian(rows, plan["sigma"], seed)
    if kind == "downsample":
        return downsample(rows, plan["hz"])
    if kind == "switch":
        return id_switch(rows, _dt(plan["t_s"]), plan["a"], plan["b"])
    if kind == "occlusion":
        for op in plan["ops"]:
            rows = occlusion(rows, op["obj"], _dt(op["t0"]), _dt(op["t1"]), op["policy"])
        return rows
    if kind == "frag":
        for op in plan["ops"]:
            rows = fragmentation(rows, op["obj"], _dt(op["t0"]), _dt(op["t1"]), op["new_id"])
        return rows
    raise ValueError(kind)


# --------------------------------------------------------------------------- #
# infra
# --------------------------------------------------------------------------- #
def ssh_tar_push(host: str, local_parent: Path, names: List[str],
                 remote_dest: str) -> None:
    """로컬 파일들을 tar 스트림으로 원격 디렉토리에 밀어 넣는다."""
    p1 = subprocess.Popen(["tar", "-cz", "-C", str(local_parent)] + names,
                          stdout=subprocess.PIPE)
    p2 = subprocess.run(["ssh", "-o", "BatchMode=yes", host,
                         f"mkdir -p '{remote_dest}' && tar -xz -C '{remote_dest}'"],
                        stdin=p1.stdout)
    p1.wait()
    if p1.returncode != 0 or p2.returncode != 0:
        raise RuntimeError(f"tar push failed (rc={p1.returncode}/{p2.returncode})")


def resolve_job(base_id: str) -> Tuple[str, str]:
    """실패 잡은 재제출 불가(409) — -r2.. 접미사로 유효 시도 id와 상태를 찾는다."""
    for attempt in range(1, 10):
        jid = base_id + ("" if attempt == 1 else f"-r{attempt}")
        try:
            st = api_get(f"/jobs/{jid}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return jid, "new"
            raise
        if st.get("state") != "failed":
            return jid, str(st.get("state"))
    raise RuntimeError(f"{base_id}: r9까지 전부 실패 — 잡 로그 확인 필요")


# --------------------------------------------------------------------------- #
# phases (조건 단위 멱등)
# --------------------------------------------------------------------------- #
def load_pairs(fid_out: Path) -> List[dict]:
    pairs = json.loads((fid_out / "pairs.json").read_text(encoding="utf-8"))
    for p in pairs:
        metas = list((fid_out / "episodes" / p["run"] / p["ep"])
                     .glob("_video_*.meta.json"))
        meta = json.loads(metas[0].read_text(encoding="utf-8"))
        p["label_to_objid"] = {int(v): k for k, v in
                               (meta.get("objid_to_label") or {}).items()}
        p["collision_distance"] = float(meta.get("collision_distance", 72.0))
    return pairs


def phase_perturb(cond: dict, pairs: List[dict], fid_out: Path, out: Path) -> None:
    tdir = out / "traces" / cond["name"]
    tdir.mkdir(parents=True, exist_ok=True)
    for p in pairs:
        csv_path = tdir / f"{p['pair_id']}.csv"
        if csv_path.exists():
            continue
        rows = load_trace((fid_out / "episodes" / p["run"] / p["ep"] / p["trace"])
                          .read_text(encoding="utf-8"))
        plan, remap = plan_perturbation(cond, p, p["label_to_objid"])
        seed = stable_seed(cond["name"], p["pair_id"])
        out_rows = perturb_rows(plan, rows, seed)
        (tdir / f"{p['pair_id']}.meta.json").write_text(json.dumps(
            {"condition": cond, "plan": plan, "gt_remap": remap, "seed": seed,
             "rows_in": len(rows), "rows_out": len(out_rows)},
            indent=1, ensure_ascii=False), encoding="utf-8")
        csv_path.write_text(dump_trace(out_rows), encoding="utf-8")
    print(f"[{cond['name']}] perturb ok ({len(pairs)} traces)")


def phase_push(cond: dict, pairs: List[dict], args, out: Path) -> None:
    """조건 trace 전량을 매번 push(tar 덮어씀 — 작은 CSV라 저렴).

    이전엔 조건별 `.pushed` 마커로 스킵했는데, 나중에 pair가 추가되면(에피소드
    증량) 마커 때문에 신규 trace가 영영 업로드 안 돼 렌더가 데이터 없이 전멸했다
    (실측: 증량 15에피소드 전부 스킵). 멱등 재업로드가 안전하다.
    """
    names = [f"{p['pair_id']}.csv" for p in pairs]
    ssh_tar_push(args.ssh_host, out / "traces" / cond["name"], names,
                 f"{args.remote_ext_root}/artifacts/perturbed/{cond['name']}")
    print(f"[{cond['name']}] pushed {len(names)} traces")


def _submit_replay(jid: str, cond: dict, p: dict, args) -> None:
    fmt = "%Y-%m-%d %H:%M:%S"
    start = datetime.datetime.fromisoformat(p["capture_start"])
    end = start + datetime.timedelta(seconds=p["duration_s"])
    try:
        api_post("/jobs", {
            "job_type": "replay", "job_id": jid, "gpu": args.gpu,
            "replay_start": start.strftime(fmt), "replay_end": end.strftime(fmt),
            "data_uri": (f"file://{args.remote_ext_root}/artifacts/"
                         f"perturbed/{cond['name']}/{p['pair_id']}.csv"),
            "render_fps": p["fps"], "app_kit": APP_KIT,
            "camera": p["camera"], "stage": p["stage"]})
        print(f"[{cond['name']}] submitted {jid}", flush=True)
    except urllib.error.HTTPError as e:
        if e.code != 409:
            raise


def phase_replay(cond: dict, pairs: List[dict], args, out: Path) -> Dict[str, str]:
    """반환: pair_id -> job_id (렌더 완료된 쌍만). 실패 잡은 인라인 2회 재시도 후
    그래도 안 되면 스킵 — 한 잡의 산발적 실패(주로 Nucleus 로드 지연 타임아웃)가
    전체 런을 죽이지 않게 한다. 스킵은 로그로 남긴다(무언 누락 금지)."""
    jobs: Dict[str, str] = {}
    todo = []
    tag = getattr(args, "run_tag", "ptb")
    for p in pairs:
        jid, state = resolve_job(f"{tag}-{cond['name']}-{p['pair_id']}")
        jobs[p["pair_id"]] = jid
        if (out / "replays" / jid).is_dir() or state == "done":
            continue
        if state == "new":
            _submit_replay(jid, cond, p, args)
        todo.append((p, jid))
    per_job = (180 + 30 * 8 + 60) * 1.2 + 60
    skipped = []
    for i, (p, jid) in enumerate(todo):
        try:
            poll_job(jid, per_job * (len(todo) - i), label=jid)
            continue
        except RuntimeError as e:
            print(f"[{cond['name']}] {jid} failed ({e}); 재시도", flush=True)
        ok = False
        for _ in range(2):                       # 새 -rN id로 재제출·재폴링
            nxt, _st = resolve_job(f"{tag}-{cond['name']}-{p['pair_id']}")
            if nxt == jid:
                break
            _submit_replay(nxt, cond, p, args)
            jid = nxt
            try:
                poll_job(nxt, per_job, label=nxt)
                jobs[p["pair_id"]] = nxt
                ok = True
                break
            except RuntimeError as e2:
                print(f"[{cond['name']}] {nxt} 재시도 실패 ({e2})", flush=True)
        if not ok:
            skipped.append(p["pair_id"])
            jobs.pop(p["pair_id"], None)
    if skipped:
        print(f"[{cond['name']}] SKIPPED {len(skipped)}: {skipped}", flush=True)
        # 체계적 실패 가드: 시도한 렌더의 대부분이 실패하면 산발 사고가 아니라
        # 데이터 누락/서버 이상(실측: push 버그로 신규 trace 전량 미업로드 → 전멸).
        # 조용히 스킵하고 계속하면 며칠을 낭비하므로 즉시 중단해 알린다.
        if len(todo) >= 4 and len(skipped) >= 0.6 * len(todo):
            raise RuntimeError(
                f"[{cond['name']}] 체계적 렌더 실패: 시도 {len(todo)}건 중 "
                f"{len(skipped)}건 스킵 — 데이터 누락/서버 이상 의심, 중단")
    return jobs


def phase_fetch(cond: dict, jobs: Dict[str, str], args, out: Path) -> None:
    missing = [j for j in jobs.values() if not (out / "replays" / j).is_dir()]
    if missing:
        ssh_tar_fetch(args.ssh_host, f"{args.remote_ext_root}/artifacts/replays",
                      missing, out / "replays")
        print(f"[{cond['name']}] fetched {len(missing)} videos")


def phase_infer(cond: dict, pairs: List[dict], jobs: Dict[str, str],
                out: Path, client) -> None:
    idir = out / "infer"
    idir.mkdir(exist_ok=True)
    for p in pairs:
        if p["pair_id"] not in jobs:      # 렌더 스킵된 쌍 — 추론 대상 아님
            continue
        dst = idir / f"{cond['name']}_{p['pair_id']}.json"
        if dst.exists():
            continue
        vids = list((out / "replays" / jobs[p["pair_id"]]).glob("*.mp4"))
        if len(vids) != 1:
            raise RuntimeError(f"{jobs[p['pair_id']]}: expected 1 mp4, got {len(vids)}")
        result = client.analyze_video(str(vids[0]), model=MODEL, preset_name=PRESET)
        if result["num_errors"]:
            raise RuntimeError(f"{dst.name}: {result['num_errors']} chunk errors")
        client.save_json(result, str(dst))
        print(f"[{cond['name']}] infer {p['pair_id']}")


def score_side(pairs: List[dict], pred_path, out: Path, remap_dir: Optional[Path]) -> dict:
    """한 조건(또는 베이스라인)의 집계: 원GT·화면GT 2기준 det/att F1 + FP."""
    agg = {"orig": {"det_tp": 0, "att_tp": 0, "fn": 0, "fp": 0},
           "screen": {"det_tp": 0, "att_tp": 0, "fn": 0, "fp": 0}}
    scored = 0
    for p in pairs:
        pp = pred_path(p)
        if not pp.exists():        # 렌더 스킵된 쌍 — 추론 파일 없음, 채점에서 제외
            continue
        scored += 1
        gt = {int(t): set(ids) for t, ids in p["gt_events"].items()}
        pred = parse_pred_events(json.loads(pp.read_text(encoding="utf-8")))
        remap = None
        if remap_dir is not None:
            side = json.loads((remap_dir / f"{p['pair_id']}.meta.json")
                              .read_text(encoding="utf-8"))
            remap = side.get("gt_remap")
        for basis, g in (("orig", gt), ("screen", apply_remap(gt, remap))):
            c = match_events(g, pred, tol=1)["counts"]
            for k in agg[basis]:
                agg[basis][k] += c[k]
    agg["n_scored"] = scored
    for basis in ("orig", "screen"):
        c = agg[basis]
        c["f1_det"] = f1(c["det_tp"], c["fp"], c["fn"])
        c["f1_att"] = f1(c["att_tp"], c["fp"] + c["det_tp"] - c["att_tp"], c["fn"])
    return agg


def phase_report(pairs: List[dict], fid_out: Path, out: Path,
                 done_conds: List[dict]) -> None:
    baseline = score_side(
        pairs, lambda p: fid_out / "infer" / f"{p['pair_id']}_replay.json",
        out, None)["orig"]
    results = {"baseline_clean_replay": baseline, "conditions": {}}
    lines = ["# Perturbation robustness - vs clean replay baseline", "",
             f"pairs: {len(pairs)}, baseline(det/att F1, tol1): "
             f"{baseline['f1_det']} / {baseline['f1_att']}", "",
             "| condition | det F1 | d(det) | att F1(orig) | att F1(screen) | FP |",
             "|---|---|---|---|---|---|"]
    for cond in done_conds:
        agg = score_side(pairs,
                         lambda p: out / "infer" / f"{cond['name']}_{p['pair_id']}.json",
                         out, out / "traces" / cond["name"])
        results["conditions"][cond["name"]] = agg
        o, s = agg["orig"], agg["screen"]
        lines.append(f"| {cond['name']} | {o['f1_det']} | "
                     f"{round(o['f1_det'] - baseline['f1_det'], 4):+} | "
                     f"{o['f1_att']} | {s['f1_att']} | {o['fp']} |")
    (out / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False),
                                      encoding="utf-8")
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


# --------------------------------------------------------------------------- #
# FP 원인 분류 — 유령 경로/노이즈 근접이 FP를 만들었는지 좌표로 판정
# --------------------------------------------------------------------------- #
def positions_by_label(rows: List[dict]) -> Dict[int, List[Tuple[float, float, float, float]]]:
    """trace rows -> {라벨: [(초, x, y, z)] 시각순}. 라벨 = objid 끝 3자리."""
    by: Dict[int, List[Tuple[float, float, float, float]]] = {}
    for r in rows:
        t = r["t"]
        sec = t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1e6
        by.setdefault(int(str(r["objid"])[-3:]), []).append(
            (sec, float(r["x"]), float(r["y"]), float(r["z"])))
    for samples in by.values():
        samples.sort()
    return by


def _pos_at(samples: List[Tuple[float, float, float, float]],
            s: float) -> Optional[Tuple[float, float, float]]:
    """시각 s의 '화면상' 위치 — 재연기와 동일한 hold 의미론.

    s 이전 마지막 샘플 위치(없으면 첫 샘플)를 쓴다. 샘플이 끝난 뒤에도 마지막
    위치를 유지한다 — 렌더러가 죽은 트랙(frag의 정지 분신)과 결손 구간을
    마지막 좌표로 계속 그리기 때문에, 화면 기준 근접 판정도 같은 규칙이어야
    분신 유발 FP를 '환각'으로 오분류하지 않는다.
    """
    import bisect
    if not samples:
        return None
    times = [t for t, _x, _y, _z in samples]
    i = bisect.bisect_right(times, s) - 1
    return samples[max(i, 0)][1:]


def min_pair_distance(by: Dict[int, List], s_lo: float, s_hi: float,
                      labels: Optional[List[int]] = None,
                      step: float = 0.1) -> Optional[Tuple[float, Tuple[int, int]]]:
    """[s_lo,s_hi] 창에서 (지정 라벨들 간) 최소 쌍 거리. 표본 없으면 None."""
    cand = sorted(labels) if labels else sorted(by)
    best: Optional[Tuple[float, Tuple[int, int]]] = None
    s = s_lo
    while s <= s_hi + 1e-9:
        pos = {lb: _pos_at(by[lb], s) for lb in cand if lb in by}
        ok = [(lb, p) for lb, p in pos.items() if p is not None]
        for i in range(len(ok)):
            for j in range(i + 1, len(ok)):
                (la, pa), (lb2, pb) = ok[i], ok[j]
                d = ((pa[0] - pb[0]) ** 2 + (pa[1] - pb[1]) ** 2
                     + (pa[2] - pb[2]) ** 2) ** 0.5
                if best is None or d < best[0]:
                    best = (d, (la, lb2))
        s += step
    return best


def classify_fp(fp_ids: Set[int], fp_sec: int, by: Dict[int, List],
                thr: float) -> dict:
    """FP 한 건의 원인 분류.

    contact     주장된 ID들끼리 창(±1s+해당 초) 안에서 판정 거리 이내 접근
                — "화면상 닿아 보였다"(데이터 유래 FP)
    near        1.5x 거리 이내 접근 — 시각적으로 겹쳐 보였을 개연성
    none        주장 쌍은 멀리 있었음 — 모델 자체 환각
    phantom-id  주장 라벨이 데이터에 아예 없음(존재하지 않는 객체) — 환각
    """
    missing = [i for i in fp_ids if i not in by]
    if missing:
        return {"verdict": "phantom-id", "missing": missing}
    got = min_pair_distance(by, fp_sec - 1, fp_sec + 2, sorted(fp_ids)) \
        if len(fp_ids) >= 2 else None
    if got is None:
        return {"verdict": "none", "min_dist": None}
    d, pair = got
    verdict = "contact" if d <= thr else "near" if d <= 1.5 * thr else "none"
    return {"verdict": verdict, "min_dist": round(d, 1), "pair": list(pair)}


def analyze_fp(pairs: List[dict], fid_out: Path, out: Path,
               cond_names: List[str]) -> None:
    """조건별(+clean 베이스라인) FP를 좌표 근접으로 원인 분류 -> fp_report.md."""
    def trace_path(cond: Optional[str], p: dict) -> Path:
        if cond is None:                                  # 베이스라인 = 원본 trace
            return fid_out / "episodes" / p["run"] / p["ep"] / p["trace"]
        return out / "traces" / cond / f"{p['pair_id']}.csv"

    def pred_path(cond: Optional[str], p: dict) -> Path:
        if cond is None:
            return fid_out / "infer" / f"{p['pair_id']}_replay.json"
        return out / "infer" / f"{cond}_{p['pair_id']}.json"

    rows_out: List[str] = ["# FP cause analysis - coordinate proximity",
                           "", "| condition | FP | contact | near | none | phantom-id |",
                           "|---|---|---|---|---|---|"]
    detail: Dict[str, list] = {}
    for cond in [None] + list(cond_names):
        name = cond or "baseline(clean)"
        counts = {"contact": 0, "near": 0, "none": 0, "phantom-id": 0}
        items = []
        for p in pairs:
            tpath, ppath = trace_path(cond, p), pred_path(cond, p)
            if not (tpath.exists() and ppath.exists()):   # 스킵된 쌍 제외
                continue
            by = positions_by_label(load_trace(tpath.read_text(encoding="utf-8")))
            gt = {int(t): set(ids) for t, ids in p["gt_events"].items()}
            pred = parse_pred_events(json.loads(
                pred_path(cond, p).read_text(encoding="utf-8")))
            for sec, ids in match_events(gt, pred, tol=1)["fp"]:
                c = classify_fp(set(ids), sec, by, p["collision_distance"])
                counts[c["verdict"]] += 1
                items.append({"pair_id": p["pair_id"], "sec": sec,
                              "ids": list(ids), **c})
        total = sum(counts.values())
        rows_out.append(f"| {name} | {total} | {counts['contact']} | "
                        f"{counts['near']} | {counts['none']} | {counts['phantom-id']} |")
        detail[name] = items
    (out / "fp_analysis.json").write_text(
        json.dumps(detail, indent=1, ensure_ascii=False), encoding="utf-8")
    (out / "fp_report.md").write_text("\n".join(rows_out) + "\n", encoding="utf-8")
    print("\n".join(rows_out))


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fidelity-out", default="artifacts/replay_fidelity",
                    help="replay_fidelity 산출 루트 (pairs.json·clean 추론 재사용)")
    ap.add_argument("--out", default="artifacts/perturb_eval")
    ap.add_argument("--conditions", nargs="*", default=[c["name"] for c in CONDITIONS],
                    help="돌릴 조건 이름 부분집합")
    ap.add_argument("--gpu", type=int, default=1)
    ap.add_argument("--run-tag", default="ptb",
                    help="잡 ID 접두 — 서버의 옛 동명 잡(다른 의미론) 재사용을 피하려 새 태그 지정")
    ap.add_argument("--ssh-host", default=DEFAULT_SSH_HOST)
    ap.add_argument("--remote-ext-root", default=DEFAULT_REMOTE_EXT)
    ap.add_argument("--analyze-fp", action="store_true",
                    help="FP 원인 분류만 로컬 실행 (터널·렌더 불필요)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        _self_test()
        return

    fid_out = Path(args.fidelity_out).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    pairs = load_pairs(fid_out)
    if args.analyze_fp:
        analyze_fp(pairs, fid_out, out,
                   [c["name"] for c in CONDITIONS if c["name"] in set(args.conditions)])
        return
    conds = [c for c in CONDITIONS if c["name"] in set(args.conditions)]
    n_jobs = len(conds) * len(pairs)
    print(f"[plan] {len(conds)} conditions x {len(pairs)} pairs = {n_jobs} replay jobs "
          f"(~{n_jobs * 3.5 / 60:.1f}h render) + {n_jobs} inferences")

    with Tunnel(args.ssh_host):
        client = make_client(out)
        done: List[dict] = []
        for cond in conds:
            phase_perturb(cond, pairs, fid_out, out)
            phase_push(cond, pairs, args, out)
            jobs = phase_replay(cond, pairs, args, out)
            phase_fetch(cond, jobs, args, out)
            phase_infer(cond, pairs, jobs, out, client)
            done.append(cond)
            print(f"[{cond['name']}] condition complete ({len(done)}/{len(conds)})")
        phase_report(pairs, fid_out, out, done)
    print(f"\n[done] results: {out / 'report.md'}")


def _self_test() -> None:
    cap = "2026-07-22T17:06:35"
    # 두 이벤트(17:06:50={1,3}, 17:06:56={3,4})로 전 이벤트 교란을 검증
    pair = {"pair_id": "x-ep-0000", "capture_start": cap, "duration_s": 30.0,
            "gt_events": {"61610": [1, 3], "61616": [3, 4]}}
    l2o = {1: "obj001", 2: "obj002", 3: "obj003", 4: "obj004"}
    # switch: 첫 이벤트 3s 전, "당사자 1 x 비당사자 2" 스왑(remap은 op 리스트)
    plan, remap = plan_perturbation({"name": "switch", "kind": "switch"}, pair, l2o)
    assert plan["a"] == "obj001" and plan["b"] == "obj002"
    assert remap == [{"type": "swap", "after_s": 61607, "pair": [1, 2]}]
    assert apply_remap({61610: {1, 3}}, remap) == {61610: {2, 3}}
    assert apply_remap({61600: {1, 3}}, remap) == {61600: {1, 3}}   # 스위칭 전 불변
    # frag 전 이벤트: 충돌 참가 객체(1,3,4) 각각 새 ID로 개명 → 다중 rename remap
    plan, remap = plan_perturbation({"name": "frag", "kind": "frag"}, pair, l2o)
    froms = {op["from"]: op["to"] for op in remap}
    assert set(froms) == {1, 3, 4} and set(froms.values()) == {5, 6, 7}
    # 첫 충돌 시각 {1,3} 모두 개명, 라벨 2는 충돌 없어 불변
    remapped = apply_remap({61610: {1, 3}, 61616: {3, 4}}, remap)
    assert 1 not in remapped[61610] and 3 not in remapped[61610]   # 둘 다 새 ID로
    # occ 전 이벤트: 이벤트 수만큼 op(2개), remap 없음
    plan, remap = plan_perturbation(
        {"name": "occ-hold", "kind": "occlusion", "policy": "hold", "dur_s": 3.0},
        pair, l2o)
    assert plan["kind"] == "occlusion" and len(plan["ops"]) == 2 and remap is None
    # downsample: hz 그대로 전달
    plan, _ = plan_perturbation({"name": "dsr5", "kind": "downsample", "hz": 5},
                                pair, l2o)
    assert plan["hz"] == 5
    # 시드 결정성
    assert stable_seed("a", "b") == stable_seed("a", "b") != stable_seed("a", "c")

    # FP 원인 분류: 두 객체가 5초에 접근(거리 50), 나머지 시각은 원거리(300)
    base = datetime.datetime(2026, 7, 22, 17, 0, 0)
    rows = []
    for i in range(0, 100):                      # 10초 x 10Hz
        s = i / 10.0
        x1 = 0.0
        x2 = 300.0 - (250.0 if abs(s - 5.0) < 0.3 else 0.0)   # 5초 부근만 50까지 접근
        rows.append({"t": base + datetime.timedelta(seconds=s), "objid": "obj001",
                     "x": x1, "y": 90.0, "z": 0.0})
        rows.append({"t": base + datetime.timedelta(seconds=s), "objid": "obj002",
                     "x": x2, "y": 90.0, "z": 0.0})
    by = positions_by_label(rows)
    assert set(by) == {1, 2} and len(by[1]) == 100
    got = min_pair_distance(by, 4.0 + 61200, 6.0 + 61200)     # 17:00:04~06 (초는 자정 기준)
    assert got is not None and abs(got[0] - 50.0) < 1e-6 and got[1] == (1, 2)
    # 접근 시각의 FP -> contact, 원거리 시각 -> none, 없는 라벨 -> phantom-id
    assert classify_fp({1, 2}, 61205, by, thr=72.0)["verdict"] == "contact"
    assert classify_fp({1, 2}, 61208, by, thr=72.0)["verdict"] == "none"
    assert classify_fp({1, 6}, 61205, by, thr=72.0)["verdict"] == "phantom-id"
    # near: thr를 40으로 낮추면 50은 1.5x(60) 이내
    assert classify_fp({1, 2}, 61205, by, thr=40.0)["verdict"] == "near"
    # 죽은 트랙 hold 의미론: obj001 샘플이 3초에 끝나도(분신) 마지막 위치에
    # 남아 있는 것으로 본다 — 8초에 obj002가 그 자리를 지나가면 contact.
    dead = [{"t": base + datetime.timedelta(seconds=i / 10.0), "objid": "obj001",
             "x": 0.0, "y": 90.0, "z": 0.0} for i in range(0, 30)]         # 0~3s에서 사망
    dead += [{"t": base + datetime.timedelta(seconds=i / 10.0), "objid": "obj002",
              "x": 500.0 - (500.0 if abs(i / 10.0 - 8.0) < 0.3 else 0.0),
              "y": 90.0, "z": 0.0} for i in range(0, 100)]
    by2 = positions_by_label(dead)
    assert classify_fp({1, 2}, 61208, by2, thr=72.0)["verdict"] == "contact"
    print("perturb_eval self-test OK")


if __name__ == "__main__":
    main()
