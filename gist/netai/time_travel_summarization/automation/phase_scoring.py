"""위상 분해 조건 세트 채점 드라이버 — 클립 단위 VLM 추론 -> 조건별 발화율 표.

설계: docs/위상분해_실험설계.md §6. 추출기(automation/phase_clips.py)가 만든 클립
세트(조건별 디렉터리 + clips_manifest.json)를 vLLM에 **클립 하나당 요청 하나**로
추론시켜, 조건별 "발화율"(= 그 클립에서 충돌 이벤트를 하나라도 보고한 클립의 비율)을
집계한다. 조건 간 발화율 차이가 곧 "모델이 충돌 판정에 실제로 쓰는 단서"의 증거다.

추론 계약은 프로덕션 경로(replay_fidelity/perturb_eval)를 **그대로** 재사용한다 —
train==infer 불변식을 깨면 여기서 잰 발화율이 학습된 모델의 성질이 아니라 프롬프트를
바꾼 새 모델의 성질이 되어 실험 자체가 무의미해지기 때문이다. 구체적으로:

  - 프롬프트: ``vlm_client/prompts.PROMPTS["twin_view"]``
    (replay_fidelity.PRESET). 프롬프트 문자열은 이 모듈에 사본조차 두지 않는다.
  - 전송: ``utils.vllm_client.VLLMClient.analyze_video`` — ffmpeg 재인코딩 슬라이스
    (``-an -c:v libx264 -pix_fmt yuv420p -preset veryfast``) 후 base64 data URI를
    ``video_url``로 실어 ``POST {base}/v1/chat/completions``.
  - 프레임 수: 클립당 20프레임(NFRAMES=20)은 **서버 기동 플래그**
    (``--media-io-kwargs '{"video": {"num_frames": 20}}'``)로 정해지며 클라이언트가
    요청별로 바꿀 수 없다. 즉 이 드라이버는 프레임 예산에 손대지 않는다.
  - 응답 해석: ``replay_fidelity.parse_pred_events`` — 프로덕션 채점이 쓰는 파서와
    동일한 함수를 그대로 호출한다.

**클립 1개 = 청크 1개**로 보내는 방법: 클립은 전부 2초(학습 계약과 동일)라 시간 분할이
필요 없다. 그렇다고 ``analyze_video``를 우회해 요청을 직접 조립하면 메시지 구조가
프로덕션과 갈라질 위험이 있으므로, 대신 **청크 길이를 클립 길이와 같게 지정**한다
(``chunk_duration=probe_duration(clip)``). 그러면 ``chunk_spans(d, d)``가 ``[(0.0, d)]``
하나만 만들어 프로덕션 코드 경로를 한 글자도 바꾸지 않고 "클립 전체 = 청크 1개"가 된다.
고정 2.0초로 넘기지 않는 이유는, 재인코딩된 클립의 실제 길이가 1.98초처럼 2.0에 살짝
못 미치면 ``int(1.98 // 2.0) == 0``이 되어 그 클립이 **요청 없이 조용히 누락**되기
때문이다(무언 누락 금지).

사용 (EXT_ROOT에서):
  python -m gist.netai.time_travel_summarization.automation.phase_scoring \
      --manifest artifacts/phase_ablation_v1/clips_manifest.json \
      --out artifacts/phase_scoring_v1 [--endpoint http://localhost:38011/v1] [--dry-run]

순수 헬퍼 검증: pytest gist/netai/time_travel_summarization/tests/test_phase_scoring.py
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from gist.netai.time_travel_summarization.automation.replay_fidelity import (
    MODEL, PRESET, _http, make_client, parse_pred_events,
)
from gist.netai.time_travel_summarization.utils.vllm_client import VLLMClient
from gist.netai.time_travel_summarization.vlm_client.prompts import PROMPTS

DEFAULT_ENDPOINT = "http://localhost:38011/v1"

# 보고 순서 — 설계 §1 표의 조건 순서(대조 -> 국면 절제 -> 접근만 -> near-miss -> 무관 대조).
CONDITION_ORDER = ["full", "no_approach", "no_contact", "no_aftermath",
                   "approach_only", "near_miss", "control"]

# 실패 게이트: 시도 ABORT_MIN_ATTEMPTS건을 넘긴 뒤 누적 실패율이 이 값을 넘으면 중단한다.
# 서빙이 죽은 채로 수백 건을 마저 돌려 "전부 비발화" 표를 만드는 사고를 막기 위한 것.
ABORT_FAIL_RATE = 0.30
ABORT_MIN_ATTEMPTS = 10    # 초반 1~2건의 산발 실패로 런 전체가 죽지 않게 하는 하한
RETRY_SLEEP_S = 2.0        # 재시도 전 대기 — 일시적 서빙 혼잡이 가라앉을 여유


# ---------------------------------------------------------------- 순수 헬퍼

def normalize_base_url(endpoint: str) -> str:
    """``--endpoint`` -> VLLMClient가 기대하는 base_url.

    VLLMClient는 스스로 ``/v1/chat/completions``를 붙이므로 base_url에 ``/v1``이
    남아 있으면 ``/v1/v1/chat/completions``로 404가 난다. 사용자는 OpenAI 호환
    엔드포인트를 ``.../v1``까지 적는 관례가 있어 양쪽 표기를 모두 받아들인다.
    """
    base = endpoint.strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


def strip_code_fence(text: str) -> str:
    """응답 본문에서 ```json 코드펜스를 벗긴다.

    parse_pred_events가 내부에서 하는 정규화와 같은 규칙이다. 여기서 한 번 더
    하는 이유는 이벤트 추출이 아니라 **상태 분류**(아래 classify_response) 때문이다 —
    "빈 배열을 반환한 정상 비발화"와 "형식을 벗어난 응답"을 구분하려면 원문이
    JSON 배열 꼴인지 따로 봐야 한다. 이벤트 추출 자체는 언제나 프로덕션 파서에
    위임하고 여기서 다시 구현하지 않는다.
    """
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`").lstrip("json").strip()
    return t


def classify_response(content: str) -> Tuple[str, Dict[int, List[int]]]:
    """응답 원문 -> (상태, 이벤트). 이벤트 추출은 parse_pred_events에 위임.

    상태:
      events    충돌 이벤트를 1건 이상 보고 -> **발화**
      silent    형식은 맞는 JSON 배열인데 이벤트가 없음(``[]``) -> 정상 비발화
      unparsed  JSON 배열 형식을 벗어남(산문·잘린 출력 등) -> 비발화로 세되 별도 계수
      empty     본문이 비어 있음(요청 실패 경로에서만 나온다)

    unparsed를 silent와 합치지 않는 이유: 파서와 모델 출력 형식이 어긋나면 표 전체가
    "비발화"로 눌려 조건 간 차이가 사라지는데, 합쳐 놓으면 그 사고가 겉으로는
    "모델이 조용했다"와 구분되지 않는다. 요약에 따로 찍어 눈에 띄게 한다.
    """
    if not (content or "").strip():
        return "empty", {}
    events = parse_pred_events({"chunk_responses": [{"content": content}]})
    if events:
        return "events", {t: sorted(ids) for t, ids in sorted(events.items())}
    t = strip_code_fence(content)
    if t.startswith("[") and t.endswith("]"):
        return "silent", {}
    return "unparsed", {}


def should_abort(attempted: int, failed: int) -> bool:
    """누적 실패율이 게이트를 넘었는가 — 서빙 이상으로 보고 즉시 중단할 조건."""
    if attempted < ABORT_MIN_ATTEMPTS:
        return False
    return failed / attempted > ABORT_FAIL_RATE


def condition_order_key(condition: str) -> Tuple[int, str]:
    """설계 §1 순서를 우선하고, 모르는 조건은 뒤에 이름순으로 붙인다(무언 누락 금지)."""
    if condition in CONDITION_ORDER:
        return (CONDITION_ORDER.index(condition), "")
    return (len(CONDITION_ORDER), condition)


def summarize(rows: List[dict]) -> dict:
    """채점 행 목록 -> 조건별 집계.

    발화율의 분모(n)는 **추론에 성공한 클립 수**다. 실패한 클립을 분모에 넣으면
    서빙 사고가 발화율 하락으로 위장되므로, 실패는 분모 밖에서 따로 센다.
    """
    by_cond: Dict[str, dict] = {}
    for r in rows:
        cond = r.get("condition", "?")
        c = by_cond.setdefault(cond, {"n": 0, "spoke": 0, "unparsed": 0, "failed": 0})
        if not r.get("ok"):
            c["failed"] += 1
            continue
        c["n"] += 1
        if r.get("spoke"):
            c["spoke"] += 1
        if r.get("status") == "unparsed":
            c["unparsed"] += 1
    for c in by_cond.values():
        c["rate"] = round(c["spoke"] / c["n"], 4) if c["n"] else None
    conditions = {k: by_cond[k] for k in sorted(by_cond, key=condition_order_key)}
    return {
        "conditions": conditions,
        "total_scored": sum(c["n"] for c in conditions.values()),
        "total_failed": sum(c["failed"] for c in conditions.values()),
        "total_unparsed": sum(c["unparsed"] for c in conditions.values()),
    }


def render_summary(summary: dict, meta: Optional[dict] = None) -> str:
    """집계 -> summary.md 본문."""
    meta = meta or {}
    conds = summary["conditions"]
    base = conds.get("full", {}).get("rate")
    lines = ["# Phase ablation - utterance rate by condition", ""]
    for key in ("manifest", "endpoint", "model", "preset"):
        if meta.get(key):
            lines.append(f"- {key}: `{meta[key]}`")
    lines += ["",
              f"scored: {summary['total_scored']}, failed: {summary['total_failed']}, "
              f"unparsed: {summary['total_unparsed']}", "",
              "| condition | n | spoke | rate | vs full | unparsed | failed |",
              "|---|---|---|---|---|---|---|"]
    for name, c in conds.items():
        rate = "-" if c["rate"] is None else f"{c['rate']:.3f}"
        if c["rate"] is None or base is None or name == "full":
            delta = "-"
        else:
            delta = f"{c['rate'] - base:+.3f}"
        lines.append(f"| {name} | {c['n']} | {c['spoke']} | {rate} | {delta} | "
                     f"{c['unparsed']} | {c['failed']} |")
    lines.append("")

    # 설계 §6의 보류 규약: 무관 대조(control)에서 발화가 나오면 조건 간 비교의 전제가
    # 깨진 것이므로 표를 해석하기 전에 원인(프롬프트·인코딩)을 먼저 조사한다.
    ctrl = conds.get("control")
    if ctrl and ctrl["n"] and ctrl["spoke"]:
        lines += [f"> WARNING: control(무관 구간) 발화 {ctrl['spoke']}/{ctrl['n']} "
                  f"(rate {ctrl['rate']}). 설계 §6에 따라 결과 해석을 보류하고 "
                  "프롬프트·인코딩 원인을 먼저 조사할 것.", ""]
    if summary["total_unparsed"]:
        lines += [f"> NOTE: 형식 이탈 응답 {summary['total_unparsed']}건 — 비발화로 계수했다. "
                  "비율이 크면 파서·출력 형식 정합을 먼저 확인할 것.", ""]
    if summary["total_failed"]:
        lines += [f"> NOTE: 추론 실패 {summary['total_failed']}건은 발화율 분모에서 제외했다. "
                  "results.jsonl의 `ok=false` 행에 원인이 기록되어 있다.", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- manifest

def load_manifest(manifest_path: Path, clips_root: Path) -> Tuple[List[dict], List[dict]]:
    """clips_manifest.json -> (채점 대상, 제외 대상).

    제외 사유는 버리지 않고 이유와 함께 돌려준다 — 추출 단계에서 ffmpeg가 실패한
    클립(manifest의 ``error``)이나 파일이 없는 클립을 조용히 빠뜨리면, 조건별 n이
    줄어든 이유를 나중에 알 수 없다.
    """
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = data.get("clips", data) if isinstance(data, dict) else data
    usable, excluded = [], []
    for e in entries:
        item = dict(e)
        item["clip_path"] = str((clips_root / e["clip"]).resolve())
        if e.get("error"):
            item["exclude_reason"] = f"manifest error: {e['error']}"
            excluded.append(item)
        elif not Path(item["clip_path"]).exists():
            item["exclude_reason"] = "clip file missing"
            excluded.append(item)
        else:
            usable.append(item)
    return usable, excluded


def load_done(results_path: Path) -> Dict[str, dict]:
    """기존 results.jsonl -> {clip: row}. 중단 후 재실행을 이어하기로 만든다."""
    if not results_path.exists():
        return {}
    done: Dict[str, dict] = {}
    for line in results_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue          # 중단 시점의 잘린 마지막 행 — 그 클립은 다시 채점한다
        if row.get("ok"):     # 실패 행은 재실행에서 다시 시도한다
            done[row["clip"]] = row
    return done


# ---------------------------------------------------------------- 추론

class _StdlibClient(VLLMClient):
    """전송 계약은 그대로 두고 HTTP만 stdlib urllib으로 바꾼 클라이언트.

    기반 VLLMClient는 ``requests``를 쓰는데, 이 드라이버가 도는 환경(WSL·Windows
    로컬)에 requests가 없을 수 있어 replay_fidelity와 같은 방식으로 대체한다.
    프롬프트·인코딩·메시지 구조는 건드리지 않는다.
    """

    def _post(self, payload: dict) -> dict:
        return _http("POST", f"{self.base_url}/v1/chat/completions", payload,
                     timeout=self.request_timeout)


def make_scoring_client(work_dir: Path, endpoint: str):
    """채점용 클라이언트 — 프로덕션과 같은 전송 계약, 엔드포인트만 인자로 교체.

    WSL/리눅스에서는 replay_fidelity.make_client를 그대로 쓴다(Windows ffmpeg.exe를
    wslpath로 넘기는 처리까지 검증된 경로). 네이티브 Windows에서는 그 경로가
    wslpath에 의존해 쓸 수 없으므로, ffmpeg 호출은 VLLMClient 기본 구현(경로 변환
    불필요)을 쓰고 HTTP만 바꾼 _StdlibClient를 쓴다.
    """
    base = normalize_base_url(endpoint)
    if os.name == "nt":
        return _StdlibClient(base, PROMPTS)
    client = make_client(work_dir)
    client.base_url = base       # make_client는 SSH 터널 포트 고정 — 여기선 인자 우선
    return client


def score_clip(client, clip_path: Path, model: str) -> dict:
    """클립 1개 추론 -> {content, avg_logprob, error}.

    클립 전체를 청크 1개로 보내기 위해 chunk_duration을 실측 길이와 같게 준다
    (모듈 독스트링의 "클립 1개 = 청크 1개" 참고).
    """
    duration = client.probe_duration(clip_path)
    if not duration or duration <= 0:
        raise RuntimeError(f"clip duration invalid: {clip_path} ({duration})")
    result = client.analyze_video(str(clip_path), model=model, preset_name=PRESET,
                                  chunk_duration=duration)
    chunks = result.get("chunk_responses") or []
    if len(chunks) != 1:
        raise RuntimeError(f"expected 1 chunk, got {len(chunks)} ({clip_path.name}, "
                           f"duration {duration:.3f}s)")
    ch = chunks[0]
    return {"content": ch.get("content") or "", "avg_logprob": ch.get("avg_logprob"),
            "error": ch.get("error"), "duration_s": round(duration, 3)}


def score_entry(client, entry: dict, model: str) -> dict:
    """클립 1개 채점 -> JSONL 1행. 실패 시 1회 재시도 후 실패로 기록한다."""
    clip_path = Path(entry["clip_path"])
    attempts, last_err = 0, None
    for attempts in (1, 2):
        try:
            got = score_clip(client, clip_path, model)
        except Exception as exc:                      # ffmpeg·HTTP·형식 오류 전부
            last_err = repr(exc)
        else:
            if got["error"]:                          # analyze_video 내부에서 삼킨 실패
                last_err = got["error"]
            else:
                status, events = classify_response(got["content"])
                return {"condition": entry["condition"], "clip": entry["clip"],
                        "ok": True, "spoke": status == "events", "status": status,
                        "events": {str(t): ids for t, ids in events.items()},
                        "content": got["content"], "avg_logprob": got["avg_logprob"],
                        "duration_s": got["duration_s"], "attempts": attempts,
                        "source_video": entry.get("source_video"),
                        "t_ref": entry.get("t_ref")}
        if attempts == 1:
            time.sleep(RETRY_SLEEP_S)
    return {"condition": entry["condition"], "clip": entry["clip"], "ok": False,
            "spoke": False, "status": "failed", "error": last_err,
            "attempts": attempts, "source_video": entry.get("source_video"),
            "t_ref": entry.get("t_ref")}


# ---------------------------------------------------------------- CLI

def _counts_by_condition(entries: List[dict]) -> str:
    counts: Dict[str, int] = {}
    for e in entries:
        counts[e["condition"]] = counts.get(e["condition"], 0) + 1
    return ", ".join(f"{k}={counts[k]}" for k in sorted(counts, key=condition_order_key))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", required=True, help="clips_manifest.json 경로")
    ap.add_argument("--clips-root", default=None,
                    help="클립 루트(기본: manifest가 있는 디렉터리)")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT,
                    help=f"vLLM OpenAI 호환 base url (기본 {DEFAULT_ENDPOINT})")
    ap.add_argument("--out", required=True, help="결과 디렉터리(results.jsonl·summary.md)")
    ap.add_argument("--model", default=MODEL, help="서빙 모델 이름")
    ap.add_argument("--limit", type=int, default=0, help="디버그용 — 앞에서 N개만 채점")
    ap.add_argument("--dry-run", action="store_true",
                    help="manifest 검증만 하고 요청은 보내지 않는다")
    args = ap.parse_args(argv)

    manifest_path = Path(args.manifest).resolve()
    clips_root = Path(args.clips_root).resolve() if args.clips_root else manifest_path.parent
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    usable, excluded = load_manifest(manifest_path, clips_root)
    print(f"[phase_scoring] manifest {manifest_path}: {len(usable)} clips "
          f"({_counts_by_condition(usable)})")
    for e in excluded:
        print(f"[phase_scoring] EXCLUDE {e.get('clip')}: {e['exclude_reason']}")
    if not usable:
        raise SystemExit("채점할 클립이 없다 — 추출기(phase_clips) 산출물을 확인할 것")

    results_path = out / "results.jsonl"
    done = load_done(results_path)
    pending = [e for e in usable if e["clip"] not in done]
    if done:
        print(f"[phase_scoring] resume: {len(done)} already scored, {len(pending)} pending")
    if args.limit:
        pending = pending[: args.limit]
        print(f"[phase_scoring] --limit {args.limit} -> {len(pending)} clips this run")

    if args.dry_run:
        print(f"[phase_scoring] dry-run: {len(pending)} clips would be sent to "
              f"{normalize_base_url(args.endpoint)} (model {args.model}, preset {PRESET})")
        for e in pending[:10]:
            print(f"  {e['condition']}: {e['clip']}")
        if len(pending) > 10:
            print(f"  ... ({len(pending)} total)")
        return

    client = make_scoring_client(out, args.endpoint)
    rows = list(done.values())
    attempted = failed = 0
    aborted = False
    with open(results_path, "a", encoding="utf-8") as fh:
        for i, entry in enumerate(pending, 1):
            row = score_entry(client, entry, args.model)
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()          # 중단되어도 여기까지는 남는다(이어하기 전제)
            rows.append(row)
            attempted += 1
            if not row["ok"]:
                failed += 1
                print(f"[phase_scoring] FAIL {row['clip']}: {row.get('error')}", flush=True)
            elif i % 10 == 0 or i == len(pending):
                print(f"[phase_scoring] {i}/{len(pending)} scored "
                      f"(fail {failed})", flush=True)
            if should_abort(attempted, failed):
                aborted = True
                print(f"[phase_scoring] ABORT: 실패 {failed}/{attempted} "
                      f"(> {ABORT_FAIL_RATE:.0%}) — 서빙 이상으로 보고 중단한다", flush=True)
                break

    summary = summarize(rows)
    meta = {"manifest": str(manifest_path), "endpoint": normalize_base_url(args.endpoint),
            "model": args.model, "preset": PRESET}
    (out / "summary.md").write_text(render_summary(summary, meta), encoding="utf-8")
    (out / "summary.json").write_text(
        json.dumps({"meta": meta, **summary,
                    "excluded": [{"clip": e.get("clip"), "reason": e["exclude_reason"]}
                                 for e in excluded]}, indent=1, ensure_ascii=False),
        encoding="utf-8")
    print(render_summary(summary, meta))
    print(f"[phase_scoring] results -> {results_path}, summary -> {out / 'summary.md'}")
    if aborted:
        raise SystemExit("중단됨 — 서빙 복구 후 같은 명령으로 재실행하면 이어한다")


if __name__ == "__main__":
    main()
