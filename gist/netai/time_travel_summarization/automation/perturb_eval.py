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
    {"name": "ds10", "kind": "downsample", "hz": 10},
    {"name": "ds5", "kind": "downsample", "hz": 5},
    {"name": "ds2", "kind": "downsample", "hz": 2},
    {"name": "ds1", "kind": "downsample", "hz": 1},
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


def apply_remap(gt: Dict[int, Set[int]], remap: Optional[dict]) -> Dict[int, Set[int]]:
    """교란을 GT 라벨에도 적용한 '화면 기준 정답' 생성. remap 없으면 그대로."""
    if not remap:
        return gt
    out: Dict[int, Set[int]] = {}
    for t, ids in gt.items():
        ids = set(ids)
        if t >= remap["after_s"]:
            if remap["type"] == "swap":
                a, b = remap["pair"]
                ids = {b if i == a else a if i == b else i for i in ids}
            elif remap["type"] == "rename":
                ids = {remap["to"] if i == remap["from"] else i for i in ids}
        out[t] = ids
    return out


def plan_perturbation(cond: dict, pair: dict,
                      label_to_objid: Dict[int, str]) -> Tuple[dict, Optional[dict]]:
    """조건 x 에피소드 -> 구체 파라미터(시각·대상 확정)와 GT remap 스펙.

    이벤트가 있는 에피소드는 첫 GT 이벤트를 표적으로(귀속·검출 하락 측정),
    없는 에피소드는 중앙부 기본값으로(가짜 충돌 유발 여부 = FP 프로브).
    """
    cap = datetime.datetime.fromisoformat(pair["capture_start"])
    dur = float(pair["duration_s"])
    events = sorted((int(t), sorted(ids)) for t, ids in pair["gt_events"].items())
    kind = cond["kind"]

    if kind == "gaussian":
        return {"sigma": cond["sigma"]}, None
    if kind == "downsample":
        return {"keep_every": 30 // int(cond["hz"])}, None

    if events:
        ev_t, ev_ids = events[0]
        ev_dt = abs_dt(cap, ev_t)
        target = ev_ids[0]
        # 스왑 상대는 반드시 "비당사자": 당사자끼리 스왑하면 쌍이 집합이라
        # {a,b}->{b,a}로 원GT 기준에서도 안 틀려 교란이 무효가 된다.
        outsider = next((lbl for lbl in sorted(label_to_objid)
                         if lbl not in ev_ids), None)
        pair_labels = [target, outsider] if outsider is not None else ev_ids[:2]
    else:
        ev_dt = cap + datetime.timedelta(seconds=dur / 2)
        target = sorted(label_to_objid)[0]
        pair_labels = sorted(label_to_objid)[:2]

    def clamp(t: datetime.datetime) -> datetime.datetime:
        lo = cap + datetime.timedelta(seconds=1)
        hi = cap + datetime.timedelta(seconds=dur - 1)
        return max(lo, min(t, hi))

    if kind == "switch":
        t_s = clamp(ev_dt - datetime.timedelta(seconds=3))
        a, b = pair_labels
        remap = {"type": "swap", "after_s": _sec(t_s), "pair": [a, b]}
        return {"t_s": t_s.isoformat(), "a": label_to_objid[a],
                "b": label_to_objid[b]}, remap
    if kind == "frag":
        t0 = clamp(ev_dt - datetime.timedelta(seconds=4))
        t1 = clamp(ev_dt - datetime.timedelta(seconds=2))
        new_label = max(label_to_objid) + 1
        remap = {"type": "rename", "after_s": _sec(t1),
                 "from": target, "to": new_label}
        return {"t0": t0.isoformat(), "t1": t1.isoformat(),
                "obj": label_to_objid[target], "new_id": f"obj{new_label:03d}"}, remap
    if kind == "occlusion":
        half = datetime.timedelta(seconds=cond["dur_s"] / 2)
        t0, t1 = clamp(ev_dt - half), clamp(ev_dt + half)
        return {"t0": t0.isoformat(), "t1": t1.isoformat(),
                "obj": label_to_objid[target], "policy": cond["policy"]}, None
    raise ValueError(f"unknown kind {kind}")


def _sec(t: datetime.datetime) -> int:
    return t.hour * 3600 + t.minute * 60 + t.second


def perturb_rows(cond: dict, params: dict, rows: List[dict], seed: int) -> List[dict]:
    kind = cond["kind"]
    if kind == "gaussian":
        return gaussian(rows, params["sigma"], seed)
    if kind == "downsample":
        return downsample(rows, params["keep_every"])
    if kind == "switch":
        return id_switch(rows, datetime.datetime.fromisoformat(params["t_s"]),
                         params["a"], params["b"])
    if kind == "frag":
        return fragmentation(rows, params["obj"],
                             datetime.datetime.fromisoformat(params["t0"]),
                             datetime.datetime.fromisoformat(params["t1"]),
                             params["new_id"])
    if kind == "occlusion":
        return occlusion(rows, params["obj"],
                         datetime.datetime.fromisoformat(params["t0"]),
                         datetime.datetime.fromisoformat(params["t1"]),
                         params["policy"])
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
        params, remap = plan_perturbation(cond, p, p["label_to_objid"])
        seed = stable_seed(cond["name"], p["pair_id"])
        out_rows = perturb_rows(cond, params, rows, seed)
        (tdir / f"{p['pair_id']}.meta.json").write_text(json.dumps(
            {"condition": cond, "params": params, "gt_remap": remap, "seed": seed,
             "rows_in": len(rows), "rows_out": len(out_rows)},
            indent=1, ensure_ascii=False), encoding="utf-8")
        csv_path.write_text(dump_trace(out_rows), encoding="utf-8")
    print(f"[{cond['name']}] perturb ok ({len(pairs)} traces)")


def phase_push(cond: dict, pairs: List[dict], args, out: Path) -> None:
    marker = out / "traces" / cond["name"] / ".pushed"
    if marker.exists():
        return
    names = [f"{p['pair_id']}.csv" for p in pairs]
    ssh_tar_push(args.ssh_host, out / "traces" / cond["name"], names,
                 f"{args.remote_ext_root}/artifacts/perturbed/{cond['name']}")
    marker.write_text("ok", encoding="utf-8")
    print(f"[{cond['name']}] pushed {len(names)} traces")


def phase_replay(cond: dict, pairs: List[dict], args, out: Path) -> Dict[str, str]:
    """반환: pair_id -> job_id (렌더 완료 보장)."""
    jobs: Dict[str, str] = {}
    todo = []
    fmt = "%Y-%m-%d %H:%M:%S"
    for p in pairs:
        jid, state = resolve_job(f"ptb-{cond['name']}-{p['pair_id']}")
        jobs[p["pair_id"]] = jid
        if (out / "replays" / jid).is_dir() or state == "done":
            continue
        if state == "new":
            start = datetime.datetime.fromisoformat(p["capture_start"])
            end = start + datetime.timedelta(seconds=p["duration_s"])
            try:
                api_post("/jobs", {
                    "job_type": "replay", "job_id": jid, "gpu": args.gpu,
                    "replay_start": start.strftime(fmt),
                    "replay_end": end.strftime(fmt),
                    "data_uri": (f"file://{args.remote_ext_root}/artifacts/"
                                 f"perturbed/{cond['name']}/{p['pair_id']}.csv"),
                    "render_fps": p["fps"], "app_kit": APP_KIT,
                    "camera": p["camera"], "stage": p["stage"]})
                print(f"[{cond['name']}] submitted {jid}")
            except urllib.error.HTTPError as e:
                if e.code != 409:
                    raise
        todo.append(jid)
    per_job = (180 + 30 * 8 + 60) * 1.2 + 60
    for i, jid in enumerate(todo):
        poll_job(jid, per_job * (len(todo) - i), label=jid)
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
    for p in pairs:
        gt = {int(t): set(ids) for t, ids in p["gt_events"].items()}
        pred = parse_pred_events(json.loads(pred_path(p).read_text(encoding="utf-8")))
        remap = None
        if remap_dir is not None:
            side = json.loads((remap_dir / f"{p['pair_id']}.meta.json")
                              .read_text(encoding="utf-8"))
            remap = side.get("gt_remap")
        for basis, g in (("orig", gt), ("screen", apply_remap(gt, remap))):
            c = match_events(g, pred, tol=1)["counts"]
            for k in agg[basis]:
                agg[basis][k] += c[k]
    for basis in agg:
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
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fidelity-out", default="artifacts/replay_fidelity",
                    help="replay_fidelity 산출 루트 (pairs.json·clean 추론 재사용)")
    ap.add_argument("--out", default="artifacts/perturb_eval")
    ap.add_argument("--conditions", nargs="*", default=[c["name"] for c in CONDITIONS],
                    help="돌릴 조건 이름 부분집합")
    ap.add_argument("--gpu", type=int, default=1)
    ap.add_argument("--ssh-host", default=DEFAULT_SSH_HOST)
    ap.add_argument("--remote-ext-root", default=DEFAULT_REMOTE_EXT)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        _self_test()
        return

    fid_out = Path(args.fidelity_out).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    pairs = load_pairs(fid_out)
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
    pair = {"pair_id": "x-ep-0000", "capture_start": cap, "duration_s": 30.0,
            "gt_events": {"61610": [1, 3]}}        # 17:06:50 = 61610s
    l2o = {1: "obj001", 2: "obj002", 3: "obj003", 4: "obj004"}
    # switch: 이벤트 3s 전, "당사자 1 x 비당사자 2" 스왑 — 당사자끼리는 집합
    # 불변({1,3}->{3,1})이라 금지. 화면 GT는 {1,3}->{2,3}으로 실제로 틀려진다.
    params, remap = plan_perturbation({"name": "switch", "kind": "switch"}, pair, l2o)
    assert params["a"] == "obj001" and params["b"] == "obj002"
    assert remap == {"type": "swap", "after_s": 61607, "pair": [1, 2]}
    assert apply_remap({61610: {1, 3}}, remap) == {61610: {2, 3}}
    assert apply_remap({61600: {1, 3}}, remap) == {61600: {1, 3}}   # 스위칭 전 불변
    # frag: 새 라벨 5, [ev-4, ev-2) 가림
    params, remap = plan_perturbation({"name": "frag", "kind": "frag"}, pair, l2o)
    assert params["new_id"] == "obj005" and remap["to"] == 5
    assert apply_remap({61610: {1, 3}}, remap) == {61610: {5, 3}}
    # 이벤트 없는 에피소드: 중앙부 기본값
    empty = {**pair, "gt_events": {}}
    params, remap = plan_perturbation(
        {"name": "occ-hold", "kind": "occlusion", "policy": "hold", "dur_s": 3.0},
        empty, l2o)
    assert params["obj"] == "obj001" and remap is None
    # downsample 파라미터
    params, _ = plan_perturbation({"name": "ds5", "kind": "downsample", "hz": 5},
                                  pair, l2o)
    assert params["keep_every"] == 6
    # 시드 결정성
    assert stable_seed("a", "b") == stable_seed("a", "b") != stable_seed("a", "c")
    print("perturb_eval self-test OK")


if __name__ == "__main__":
    main()
