# utils — 데이터셋 구성, 추론 클라이언트, 채점, 레이크 적재 도구

## 역할과 위치

`utils/`는 학습 데이터셋 생성부터 추론, 채점, 레이크 적재, 성능 관측까지
파이프라인의 "가장자리" 단계를 담당하는 독립 CLI/라이브러리 모음이다.
`automation/`이 Kit(시뮬레이션)과 원격 잡 제어를 다루는 반면, 이 디렉터리는
전부 **stdlib 단독 실행이 가능한 순수 Python 스크립트**로 설계됐다(예외:
`ingest_trajectory.py`/`build_lake_perf_dataset.py`는 `minio`/`pyarrow`가
있는 환경에서만 실제 적재가 되고, `vllm_client.py`는 HTTP 전송에
`requests`를 쓴다 — 아래 모듈별 표에 명시). WSL 환경처럼 pip 패키지가 없는
곳에서도 대부분의 로직(파싱, 채점, 청크 계획)은 그대로 돌아간다는 것이
설계 제약이다.

## 파이프라인 단계 다이어그램

```
[데이터셋 구성]                    [추론]                      [채점]
build_dataset.py             →  vllm_client.py           →  compare_results.py
(에피소드 영상 → 2초 클립         (vLLM OpenAI 호환 서버로       (STRICT/RELAXED
 ShareGPT jsonl)                  직접 비디오 추론)              지표 계산)
                                                                      ▲
swift_infer_to_preds.py ─────────────────────────────────────────────┘
(ms-swift `swift infer` 결과 jsonl → compare_results가 기대하는 preds.json)

calculate_average_metrics.py
(여러 compare_results 결과 파일의 평균)

[레이크 적재]                              [관측/진단]
ingest_trajectory.py                  observability.py     (샘플링 레이트별 충돌 관측 가능성)
(trace CSV → parquet/csv 청크          gui_probe_report.py  (GUI 재생/탐색 stall 성능 표)
 데이터레이크 manifest)                inspect_events.py    (VLM 이벤트 JSON → JSONL 후처리)

build_lake_perf_dataset.py
(레이크 성능 실험용 데이터셋 빌드 — trace rebase + 다운샘플 + 청크 적재)
```

## 모듈 표

| 모듈 | 단계 | 입력 → 산출물 | 의존성 |
|---|---|---|---|
| `build_dataset.py` | 데이터셋 구성 | `<episodes-dir>/**/*.meta.json`+영상+collisions CSV → `train/val/test.jsonl` + `test_gt.json` | stdlib + ffmpeg(imageio_ffmpeg 우선) |
| `vllm_client.py` | 추론 | 비디오 파일 → vLLM 응답(`chunk_responses`) | stdlib + ffmpeg + `requests` |
| `compare_results.py` | 채점 | `test_gt.json` + 예측 json → STRICT/RELAXED 지표 | stdlib |
| `calculate_average_metrics.py` | 채점 집계 | 여러 `*comparison_result.json` → 평균 지표 | stdlib |
| `swift_infer_to_preds.py` | 채점 전처리 | `swift infer --result_path` jsonl → `preds.json` | stdlib |
| `ingest_trajectory.py` | 레이크 적재 | trace CSV(로컬/URI) → 데이터 레이크 청크 + manifest | stdlib + `minio`/`pyarrow`(실제 업로드 시) |
| `build_lake_perf_dataset.py` | 레이크 성능 데이터셋 빌드 | 여러 run의 trace → rebase+다운샘플된 성능 측정용 데이터셋 | stdlib + `minio`/`pyarrow` |
| `observability.py` | 관측(측정 도구) | 고주파 trace(+GT) → 샘플링레이트별 관측 가능 비율 | stdlib |
| `gui_probe_report.py` | 관측(후처리) | GUI probe 덤프 json → 재생/탐색/idle 구간별 지표 표 | stdlib |
| `inspect_events.py` | 후처리 CLI | VLM 이벤트 출력 json → JSONL/summary | stdlib(`events.core` 의존) |

표의 "데이터 레이크"는 minIO에 시간 분할 청크와 manifest로 적재한 궤적
데이터셋을 가리킨다 — 계층 자체의 설명은 `storage/README.md`와
`playback/README.md`를 참조.

## 각 모듈의 동작 요약

### build_dataset.py — ShareGPT 학습 데이터셋 구성

에피소드(캡처 영상 + `.meta.json` 사이드카(sidecar — 영상 옆에 같은 이름으로
남기는 메타 JSON) + `collisions_*.csv`)를 2초 클립 단위로 잘라 LoRA 학습용
ShareGPT jsonl을 만든다. **2초를 쓰는 이유**가
모듈 docstring에 명시돼 있다 — 추론 시 vLLM 직접 클라이언트
(`vllm_client.py`, `default_chunk_duration=2.0`)가 영상을 2초 청크로 슬라이스
하고 모델은 한 번에 청크 하나만 보므로, 학습 샘플 = 2초 클립 = 추론 시 모델이
보는 것과 정확히 같아야 한다.

라벨 정합: collision은 오버레이가 보여주는 것과 같은 벽시계(`datetime.now()`)
로 찍히므로, `[t0+2i, t0+2i+2)` 창의 클립에 그 창 안의 충돌이 그대로
매핑된다. 초 단위로 묶은 `{HH:MM:SS: [id, id]}` 형태가 채점기가 기대하는
포맷과 같다(`clip_targets`). 프롬프트/시스템 프롬프트는
`vlm_client.prompts.PROMPTS[preset]`에서 그대로 가져와(`build_dataset.py`
자체에는 프롬프트 사본을 두지 않음) 학습 입력과 추론 입력을 일치시킨다.

분할은 **에피소드 단위**로 한다(같은 에피소드의 클립이 train/val/test에
걸쳐 나뉘면 정보 누수). `balance()`가 train/val에서만 음성 클립을
`neg_ratio`(기본 1.0, ~50:50) 비율로 서브샘플링하고, test는 실제 분포
그대로 둔다("honest evaluation").

CLI: `--episodes-dir`(필수) `--out-dir`(필수) `--clip-sec`(2.0) `--neg-ratio`
(1.0) `--preset`(`twin_view`, `PROMPTS` 키 중 선택) `--nframes`(20, 정보용 —
실제 프레임 수는 vLLM 서버 기동 플래그가 결정) `--content-hz`(샘플링레이트
A/B용 다운샘플, 기본 미지정=원본 유지) `--kinds`(`["object"]`, 라벨링할
충돌 종류) `--min-objects`(2, 초당 최소 몇 개 객체가 겹쳐야 사건으로 셀지)
`--val-ratio`(0.1) `--test-ratio`(0.1) `--seed`(42).

산출물: `<out-dir>/{train,val,test}.jsonl`(ShareGPT 형식,
`{"system", "videos", "conversations"}`), `test_gt.json`(클립명 →
`[{"HH:MM:SS": [ids]}]`), `system_prompt.txt`, `dataset_summary.json`
(분할별 클립 수/양성 수).

`train.jsonl`의 한 줄이 실제로 어떤 모습인지는 아래와 같다(`videos`가 이
2초 클립 파일 하나를 가리키고, `conversations`의 `"gpt"` 턴이 그 클립의
정답 라벨이다 — `prompt`는 `vlm_client/prompts.py`의 `PROMPTS[preset]["prompt"]`
원문을 줄였다).

```json
{"system": "You are a vision-language reasoning model ...",
 "videos": ["/abs/path/to/ep0003_clip0014.mp4"],
 "conversations": [
   {"from": "human", "value": "<video>\nAnalyze the provided BEV digital twin video. ..."},
   {"from": "gpt", "value": "[{\"00:00:01\": [3, 5]}]"}
 ]}
```

이 jsonl이 가리키는 클립 자체가 어떻게 생겼는지는 아래 그림 1을 참고한다.

<!-- 이미지 TODO | 파일: images/readme/utils-01-training-clip-frame.png | 촬영: build_dataset.py가 out-dir에 만든 2초 학습 클립 mp4 중 하나에서 프레임 1장 추출(양성 클립 — 충돌 라벨이 있는 클립을 고를 것). video_capture와 동일한 오버레이(ID 마커, 시각 텍스트)가 보여야 함 | 형식: png -->
![그림 1. 학습 클립 프레임 — 2초 단위로 잘린 ShareGPT 학습 샘플의 한 장면](../../../../images/readme/utils-01-training-clip-frame.png)
*그림 1. 이 프레임이 속한 2초 클립 전체가 위 jsonl 한 줄의 `videos` 경로가 가리키는 파일이며, `conversations`의 `"gpt"` 턴이 그 클립의 정답 라벨이다.*

### vllm_client.py — vLLM OpenAI 호환 직접 추론 클라이언트

VSS(NVIDIA VIA)가 서버측에서 하던 업로드/청크분할/VLM호출을 클라이언트가
직접 수행한다: ffmpeg로 2초 청크 슬라이스 → base64 data URI(`video_url`)로
`POST {base_url}/v1/chat/completions` → 응답 수집. 슬라이스 재인코딩 파라미터
(`libx264 -pix_fmt yuv420p -preset veryfast`)는 `build_dataset.slice_clip`과
동일하게 맞춰 학습/추론 클립이 같은 분포가 되게 한다.

**프레임 예산 주의**(모듈 docstring): 학습은 클립당 20프레임 고정
(`NFRAMES=20`)이고, vLLM의 실제 프레임 샘플링은 **서버 기동 플래그**
(`--media-io-kwargs '{"video": {"num_frames": 20}}'`)로 정해진다 —
클라이언트가 요청별로 바꿀 수 없다.

`VLLMClient(base_url, prompt_presets, default_chunk_duration=2.0,
request_timeout=120.0)`의 공개 메서드: `probe_duration(video) -> float`
(ffmpeg stderr 파싱), `analyze_video(video_path, model, preset_name=None,
prompt=None, system_prompt=None, chunk_duration=None, temperature=0.0,
max_tokens=256) -> dict`(청크마다 순회 추론, 부분 실패는 기록하고 계속 —
`{"chunk_responses":[{chunk_idx, start_s, end_s, content, avg_logprob|error}],
"num_chunks", "num_errors", ...}`을 반환해 기존 VSS 응답 형식과 호환),
`save_json`. `chunk_spans(duration_s, chunk_s)`는 꽉 찬 청크만 반환(잔여
버림 — `build_dataset`의 클립 규칙과 동일). `parse_duration_s(stderr)`가
`ffmpeg -i` stderr의 `Duration: HH:MM:SS.xx`를 초로 파싱하는 공개 함수다
(다른 드라이버, 예: `automation/replay_fidelity.py`도 같은 정규식 패턴을
독자적으로 씀).

`automation/replay_fidelity.py`, `automation/perturb_eval.py`,
`automation/phase_scoring.py`가 이 클래스를 상속/재사용해 HTTP 전송만
`requests` → `urllib`로 교체하거나(WSL 환경 대응) 엔드포인트를 바꿔 쓴다.
Kit 무의존이라 오프라인 self-test가 가능하다(`python3 vllm_client.py`).

### compare_results.py — STRICT/RELAXED 채점

`evaluate_clip_level(gt_path, pred_path)`가 클립 단위 평가의 두 지표를
동시에 낸다.

- **STRICT** — 클립 안의 `HH:MM:SS`마다 예측 객체 ID 집합이 GT와
  **완전히 일치**해야 True Positive. 타임스탬프를 `{clip}|{HH:MM:SS}`로
  네임스페이싱해 `calculate_metrics`(원래 정밀도/재현율/F1 계산 함수) 하나로
  전 클립을 한 번에 채점한다.
- **RELAXED** — 클립 단위 이진 판정("이 클립에 충돌이 하나라도 있(었)는가")
  으로, GT/예측 각각의 존재 유무만 비교해 tp/fp/fn/tn과 accuracy까지 낸다.

예측 파싱(`parse_pred_entries`)은 이미 파싱된 리스트와, 코드펜스로 감싸인
raw 모델 문자열(````json [...] ````) 둘 다 받는다.

CLI: `--clips-gt`(필수, `test_gt.json`) `--clips-pred`(필수) `--label`
(`model`). 산출물: `compare_outputs/clips_{label}__comparison_result.json`
(`metrics`, `relaxed_metrics`, `details` 포함 — `calculate_average_metrics.py`
가 `metrics`/`relaxed_metrics` 키를 그대로 소비).

### calculate_average_metrics.py — 여러 채점 결과의 평균

`compare_results.py`가 만든 `*comparison_result.json` 여러 개를 glob 패턴으로
모아 STRICT(`precision/recall/f1_score`)와 RELAXED(있는 파일 전부가 가지고
있을 때만) 지표의 평균을 계산하고 콘솔에 표로 출력한 뒤
`<패턴>average_metrics.json`으로 저장한다. 사용: `python
calculate_average_metrics.py "../compare_outputs/video_18_*.json"`.

### swift_infer_to_preds.py — ms-swift 추론 결과 변환

`swift infer --val_dataset test.jsonl --result_path infer.jsonl`이 만든
jsonl(샘플마다 모델 응답 1건)을 `compare_results.py --clips-pred`가 기대하는
`{clip_id: <응답>}` 형태로 바꾼다. `clip_id`는 각 샘플의 `videos`/`video`/
`images` 필드에서 파일명을 추출해 복구하고(없으면 입력 `test.jsonl`과의
위치 정렬로 폴백), 응답 텍스트는 ms-swift 버전별로 다른 필드명
(`response`/`generated`/`prediction`/`pred`/`output`/`messages`(또는 `conversation`) 안의
assistant 턴)을 순서대로 시도해 뽑는다.

CLI: `--test-jsonl`(필수) `--infer-result`(필수) `--out`(필수).

### ingest_trajectory.py — 데이터 레이크 적재 CLI

"CCTV 트래커가 좌표를 parquet으로 축적한다"는 배포 가정의 쓰기 경로를
시뮬레이션 소스(trace CSV)로 채우는 참조 구현. 핵심 적재 로직은
`playback.lake_common.append_rows`에 있고, 이 파일은 그 앞단의 **수집부**
(어디서 CSV를 모으고, 어떤 CSV를 걸러낼지)만 담당한다.

안전장치 3가지: (1) 기존 청크와 시간 겹침이면 거부(같은 데이터 재적재
방지), (2) 입력 CSV들끼리 시간이 겹치면 거부(`check_input_overlaps`,
같은 objid가 같은 시각에 두 좌표를 갖게 되는 것을 막음), (3) wall-clock
시절 trace(스팬이 사이드카 영상 길이의 1.3배를 넘음 — sim-클럭 수정 이전
산출물)는 기본 제외(`--force`로 강행 가능).

CLI: `--dataset-uri`(필수) `--csv`(반복 가능) `--run`(episodes run 루트 —
`ep_*/`의 trace CSV 자동 수집) `--chunk-seconds` `--force` `--dry-run`.

### build_lake_perf_dataset.py — 레이크 성능 실험용 데이터셋 빌드

여러 run의 60Hz 물리 trace를 시간축에 연속 배치(rebase, 에피소드 사이
`--gap-s`만큼 간격)해 목표 스팬(기본 30분)을 만들고, 시간 기반 다운샘플러
(`perturbation.perturb.downsample`)로 목표 Hz(기본 10Hz)를 추출한 뒤
`chunk_seconds`(기본 [60, 300]) 각각으로 parquet 적재한다 — 청크 길이가
다른 두 데이터셋을 만들어 청크 경계 교차율(=stall 기회)을 대조하기
위함이다. 대상 manifest가 이미 있으면 즉시 중단한다(벤치마크 고정 원칙 —
덮어쓰기 금지, 재빌드하려면 새 버전 이름 사용). 데이터셋 옆에 `_build.json`
사이드카(소스 CSV 목록, rebase 파라미터)로 계보를 남긴다.

CLI: `--run`(반복) `--csv`(반복) `--root`
(`s3://time-travel-summarization/trajectory`) `--name`
(`aigrad_bev_10hz_30min_v1`) `--hz`(10.0) `--target-span-s`(1800.0)
`--gap-s`(0.1) `--repeat`(1, 가용 스팬이 부족하면 타일링) `--start`
(`2026-01-01 00:00:00.000`) `--chunk-seconds`(nargs+, [60, 300]) `--force`
`--dry-run`.

### observability.py — 샘플링레이트별 충돌 관측 가능성 측정

"충돌이 실제로 렌더 프레임에 잡히는가"의 이론적 상한을 잰다. 고주파(약
30Hz) 좌표 trace를 근-연속 참값으로 보고, GT 라벨과 같은 접촉 규칙
(수평 중심거리 < `collision_distance`, 사이드카 값 — PhysX contact report가
발화하는 실측 중심거리 ≈ 2r)으로 겹침 구간(τ)을 재구성한 뒤, 후보 샘플레이트
`f`마다 그 구간에 격자점(`k/f`)이 하나라도 들어가는지로 "관측 가능"을
판정한다(`grid_hits_window`). 해석적 교차검증으로 위상평균
`min(1, tau*f)`도 함께 낸다. trace가 없으면 `--collisions-only`로 collisions
CSV의 순간 이벤트에 가정된 창(`--windows`, ms)을 스윕하는 폴백 모드로
동작한다.

CLI: `--trajectory` `--collisions` `--collisions-only` `--meta`
`--collision-distance` `--rates`(nargs+, [5,10,15,30]) `--windows`
(nargs+, [50,100,150,200]) `--axes`(xz|xy, 기본 xz) `--out`.

### gui_probe_report.py — GUI 재생/탐색 성능 후처리

GUI probe 덤프(`playing` 플래그 + `twin_time` 문자열 배열) 하나를 재생
(playback)/탐색(seek)/idle 세 구간으로 후처리만으로 분류한다
(`classify_regimes`) — 계측 포맷을 바꾸지 않고 이 두 필드만으로 나눌 수
있다는 것이 설계 포인트다. 세 구간을 하나로 뭉쳐 평균 내면 어느 쪽도
설명하지 못한다는 것이 이 분류의 근거다(재생 구간은 stall이 없고 탐색 구간에 stall이
몰린다는 실측 — 확정 수치는 레이크 성능 리포트가 정본이며 여기서는 옮기지 않는다).

지표: `frame_interval` p50/p95/p99(재생은 렌더 주기, 탐색은 요청→반영
지연), hitch rate(중앙값의 2배 초과 프레임 비율), stall frame rate
(`d_sync>0`인 프레임 비율, 구간 첫 stall은 웜업으로 별도 집계),
stall 프레임의 `frame_interval` p50/p95/max(탐색 구간의 주 판정 지표 —
빈도가 아니라 1회 응답 지연), `tick_ms` p50/p99.

CLI: `paths`(nargs+, glob 허용) `--regime`(playback|seek|idle|all, 기본
all).

### inspect_events.py — VLM 이벤트 출력 후처리 CLI

`events/core.py`의 `consolidate_events`/`load_json`을 가져다 VLM 출력
JSON(`events/core.py` 형식)을 JSONL(`{timestamp: [[objid,...]]}`)과 선택적
요약 JSON으로 변환한다. 원래 `events/core.py` 안에 있던 CLI 부분이
2026-09 정리로 이 파일로 옮겨졌다(`events/core.py`는 라이브러리 함수만
남음).

CLI: `input_file`(위치 인자) `-o/--output` `--summary` `--date`
(`2025-01-01`, 타임스탬프 변환 기준 날짜).

## 실행 예 (레포 루트에서)

```bash
python -m gist.netai.time_travel_summarization.utils.build_dataset \
    --episodes-dir artifacts/episodes --out-dir artifacts/dataset \
    --clip-sec 2 --neg-ratio 1.0 --preset twin_view --nframes 20

python3 gist/netai/time_travel_summarization/utils/vllm_client.py   # self-test

python -m gist.netai.time_travel_summarization.utils.compare_results \
    --clips-gt artifacts/dataset/test_gt.json --clips-pred preds.json --label lora

python -m gist.netai.time_travel_summarization.utils.swift_infer_to_preds \
    --test-jsonl artifacts/dataset/test.jsonl --infer-result infer.jsonl --out preds.json

python -m gist.netai.time_travel_summarization.utils.ingest_trajectory \
    --dataset-uri s3://time-travel-summarization/trajectory/my_dataset \
    --run s3://time-travel-summarization/episodes/gen-20260718-153511

python -m gist.netai.time_travel_summarization.utils.observability \
    --trajectory trace.csv --meta video.meta.json --rates 5 10 15 30

python -m gist.netai.time_travel_summarization.utils.gui_probe_report \
    artifacts/benchmarks/gui_probe_*.json --regime seek
```

## 환경변수

레이크 적재 계열(`ingest_trajectory.py`, `build_lake_perf_dataset.py`)은
`storage.from_uri`를 통해 minIO 자격증명(`MINIO_ENDPOINT`,
`MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, `MINIO_BUCKET` 등, `.env`에서
읽음)에 의존한다. 확인 못함: 이 env var들의 정확한 소비처는 `storage/`
패키지이며, 이 디렉터리의 스크립트는 URI만 넘긴다.

## stdlib 단독 실행 제약

이 디렉터리는 pip 패키지가 없는 환경(WSL)에서도 대부분 동작해야 한다는
설계 제약을 갖는다. `build_dataset.py`, `compare_results.py`,
`calculate_average_metrics.py`, `swift_infer_to_preds.py`,
`observability.py`, `gui_probe_report.py`, `inspect_events.py`는 stdlib
(+ `build_dataset.py`는 ffmpeg 바이너리)만으로 동작한다. `vllm_client.py`
는 HTTP 전송에 `requests`가 필요하다(`automation/` 쪽 드라이버들은 이를
`urllib`로 교체한 서브클래스를 쓴다). `ingest_trajectory.py`/
`build_lake_perf_dataset.py`는 실제 minIO/parquet 적재에 `minio`/`pyarrow`
가 필요하다(로컬 `file://` + CSV 포맷은 이 없이도 동작).

## 테스트

전용 유닛 테스트는 `gist/netai/time_travel_summarization/tests/`에 있다
(자세한 대응표는 `tests/README.md` 참조). `vllm_client.py`는
`python3 vllm_client.py`로 직접 실행하는 self-test를 갖는다.

## 한계

- `build_dataset.py`의 `videos` 필드는 클립의 절대경로를 jsonl에 그대로
  써넣는다 — 데이터셋을 다른 머신으로 옮기면 경로가 깨진다(`training/
  remote_train.sh`가 이 때문에 데이터셋을 반드시 그 머신에서 빌드하는
  이유).
- `swift_infer_to_preds.py`의 필드명 폴백은 ms-swift 버전이 크게 바뀌면
  깨질 수 있다(코드 주석이 이 취약성을 명시한다).
- `ingest_trajectory.py`/`build_lake_perf_dataset.py`의 실제 업로드 경로는
  minio/pyarrow가 설치된 환경(L40 venv 또는 Windows Kit python)에서만
  검증 가능하다.
