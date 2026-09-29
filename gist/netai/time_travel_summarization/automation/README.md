# automation — BEV 파이프라인 생성, 재연, 강건성 측정 자동화

## 역할과 위치

`automation/`은 이 확장의 실험 파이프라인을 구성하는 모든 배치 스크립트를 모은
디렉터리다. 크게 세 갈래로 나뉜다.

1. **데이터 생성/재연** — Omniverse Kit을 headless로 구동해 물리 시뮬레이션
   에피소드를 만들거나(`generate_episodes.py`), 기존 좌표 데이터를 특정 구간만
   다시 렌더한다(`replay_range.py`). 둘 다 `kit --exec`로 실행되는 Kit 종속
   스크립트다.
2. **원격 잡 제어면** — 로컬 워크스테이션에서 GPU 서버에 생성/학습/서빙/재연
   잡을 제출하고 상태를 조회한다(`remote_generation.py`가 잡 스펙과 전송 계층을
   분리하고, `window.py`가 그 위의 Omniverse UI 패널이다).
3. **측정 드라이버** — 좌표 교란 주입 → 재연 → VLM 추론 → 검출/귀속 지표 분리라는
   이 프로젝트의 핵심 측정 파이프라인을 실행한다(`replay_fidelity.py`,
   `perturb_eval.py`, `rule_baseline.py`, `phase_clips.py`, `phase_scoring.py`,
   `overlay_flags.py`). 전부 로컬 WSL/Windows에서 stdlib(+선택적으로
   `requests`/`numpy`/`PIL`)만으로 실행되는 자기완결 드라이버이며, SSH 터널로
   GPU 서버의 job API(8800)와 vLLM(38011)에 직결한다.

`geom.py`, `scene_profiles.py`, `run_headless.sh`는 위 세 갈래가 공유하는 보조
모듈이다.

## 파이프라인 단계 다이어그램

```
[생성]                    [재연 비용]                  [교란 강건성]
generate_episodes.py  →   replay_fidelity.py       →   perturb_eval.py
(물리 시뮬레이션 촬영)      (physics 원본 vs 좌표 재연     (13개 교란 조건 x 재연
                            렌더 쌍 비교, WP2 clean)      x VLM 추론 x 채점, WP1/WP2)
      │                          │                              │
      │                          ▼                              ▼
      │                    rule_baseline.py              perturb_eval.py --analyze-fp
      │                    (좌표 직결 룰 검출기와           (FP 원인을 좌표 근접으로
      │                     VLM 비교, WP3)                  분류: 환각 vs 데이터 유래)
      ▼
utils/build_dataset.py → training/*.sh (학습) → training/run_eval.sh (평가)

[위상 분해 — 충돌 단서 국면 절제]
phase_clips.py  →  phase_scoring.py  →  overlay_flags.py
(국면별 2초 클립 추출:  (조건별 VLM 발화율      (near_miss/no_contact/
 full/no_approach/       측정, 프로덕션 추론      approach_only/control
 no_aftermath/           경로 그대로 재사용)      클립의 화면 겹침을
 approach_only/                                   픽셀에서 직접 판정)
 no_contact/near_miss/
 control)

[replay_range.py는 위 파이프라인과 독립 — 임의 좌표 구간을 재연만 하는 범용 도구]
[remote_generation.py + window.py는 위 전부가 GPU 서버에 잡을 제출하는 공통 제어면]
```

다이어그램과 아래 표의 `WP1`/`WP2`/`WP3`는 Work Package(측정 계획이 작업을
나누는 단위)의 번호다 — WP1은 교란 주입 실행, WP2는 재연 기반 검출/귀속
측정, WP3는 룰 베이스라인 대조를 가리킨다.

위 다이어그램은 단계 이름만 보여주므로, 각 단계가 디스크에 실제로 무엇을
남기는지는 산출물 디렉터리를 직접 봐야 감이 잡힌다.

<!-- 이미지 TODO | 파일: images/readme/automation-01-artifact-tree.png | 촬영: generate_episodes.py와 perturb_eval.py를 한 번씩 돌린 뒤 `artifacts/episodes/<run>/ep_0000/`(_video.mp4, _trace.csv, collisions.csv, meta.json)와 `artifacts/perturb_eval/<run-tag>/`(조건별 traces/, infer/, report.md) 두 디렉터리를 `tree` 또는 파일 탐색기로 캡처. 터미널이면 `tree -L 3 artifacts/episodes/<run> artifacts/perturb_eval/<run-tag>` 출력 스크린샷 | 형식: png -->
![그림 1. 생성/측정 파이프라인이 남기는 산출물 디렉터리](../../../../images/readme/automation-01-artifact-tree.png)
*그림 1. 위 표의 "입력 → 산출물" 열이 실제 디스크에 어떤 파일 트리로 남는지 보여주는 캡처.*

`ep_NNNN/` 아래 네 파일(_video, _trace, collisions, meta.json)이 각각
physics 패키지의 산출물과 대응하고, `perturb_eval/<run-tag>/` 아래 조건별
서브디렉터리가 다이어그램의 "13개 교란 조건" 각각의 결과 위치라는 점을
이 트리에서 확인할 수 있다.

## 모듈 표

| 모듈 | 단계 | 입력 → 산출물 | 실행 형태 |
|---|---|---|---|
| `generate_episodes.py` | 생성 | 씬 프로파일/궤적 데이터 → `ep_NNNN/{_video,_trace,collisions,meta.json}` + `_run_manifest.json` | `kit --exec` |
| `replay_range.py` | 재연(범용) | 좌표 구간(시작~끝) → `replay_{start}_{end}.mp4` + 사이드카 | `kit --exec` |
| `scene_profiles.py` | 생성/재연 공용 | `scene_profiles.json` → 아레나 범위/스테이지/카메라 | 순수 stdlib(임포트 라이브러리) |
| `geom.py` | 측정 공용 | 두 점 좌표 → 3D 중심거리 | 순수 stdlib(임포트 라이브러리) |
| `run_headless.sh` | 생성 런처 | env(`KIT_APP`,`EXT_ROOT`,...) → `generate_episodes.py` 호출 | bash |
| `remote_generation.py` | 원격 제어면 | `JobSpec` → 원격 잡 제출/상태 조회 | 순수 stdlib(라이브러리) |
| `window.py` | 원격 제어면 UI | 폼 입력 → `JobSpec` 조립 후 제출 | Omniverse UI(`omni.ui`) |
| `replay_fidelity.py` | 재연 비용 측정(WP2) | 기존 run(episodes) → `pairs.json`, `infer/*.json`, `report.md` | `python3 -m` (WSL stdlib, SSH 터널) |
| `perturb_eval.py` | 교란 강건성 측정(WP1/WP2) | `replay_fidelity`의 `pairs.json` → 조건별 `traces/`, `infer/`, `report.md`, `fp_report.md` | `python3 -m` |
| `rule_baseline.py` | 룰 대조(WP3) | `replay_fidelity`/`perturb_eval` 산출물 → `report.md`(룰 vs VLM F1) | `python3 -m` |
| `phase_clips.py` | 위상 분해 추출 | 충돌/near-miss run → 조건별 2초 클립 + `clips_manifest.json` | `python -m`(ffmpeg 필요) |
| `phase_scoring.py` | 위상 분해 채점 | `clips_manifest.json` → `results.jsonl`, `summary.md` | `python -m` |
| `overlay_flags.py` | 화면 겹침 판정(후처리) | `clips_manifest.json` + 클립 mp4 → `overlay_overlap` 필드 갱신 | `python -m`(ffmpeg 필요) |

## 각 모듈의 동작 요약

### generate_episodes.py — 물리 시뮬레이션 배치 촬영

에피소드마다 4~6개 객체를 무작위 배치하고 물리(wander) 시뮬레이션을 돌리며
오프스크린 영상 + 30Hz 좌표 trace를 동시에 기록한다. 씬 프로파일
(`--scene-profile`, `scene_profiles.json`에 등록)을 지정하면 궤적 데이터 로드
없이 아레나 범위/스테이지/카메라를 명시값으로 받는다(2026-08-15부터 권장 경로).
`--near-miss`를 켜면 짝끼리 접근했다 접촉 없이 흩어지는 대조 데이터(GT 충돌
0건)를 만들며, 안무는 `--near-miss-mode swerve`(기본, 감속 없이 회피)와
`stop`(감속+정지, 구버전 대조군) 두 가지다. 생성 직후 `check_near_miss_trace`/
`check_near_miss_diversity`로 좌표 불변식(접촉 없음, 실제 접근함, 조우 지점의
공간적 다양성)을 자체 검증한다.

핵심 게이트: PhysX 시뮬레이션이 살아 있는 동안(타임라인 재생 중이나 일시정지,
`stop()` 전 — 이 루프는 stop을 호출하지 않으므로 에피소드 사이가 항상 이 상태)
PhysX가 프림 스택에 orient와 scale op를 덧붙여 `XformCommonAPI`가 비호환으로
판정하고, `SetTranslate`는 예외 없이 False만 돌려주며 무시된다(Kit 실기 검증). 그래서
`apply_positions`는 항상 translate op를 직접 찾거나 만들어(find-or-add) 설정한다.

CLI(`python automation/generate_episodes.py -- --episodes N --out DIR ...`):

| 플래그 | 기본값 / 설명 |
|---|---|
| `--episodes` | 50 |
| `--duration` | 40.0 |
| `--min-objects` | 4 |
| `--max-objects` | 6 |
| `--speed-min` | 200.0 |
| `--speed-max` | 300.0 |
| `--seed` | 42 |
| `--render-fps` | 30 |
| `--scene-profile` | — |
| `--stage` | — |
| `--camera` | — |
| `--keep-positions` | — |
| `--spawn-zones` | — |
| `--spawn-plan` | — |
| `--near-miss` | — |
| `--near-miss-gap` | 95.0 |
| `--near-miss-mode` | `swerve`\|`stop` |
| `--extra-objects` | 0 |
| `--upload-uri` | — |
| `--quit` | — |
| `--self-test` | — |
| `--check-trace` | 오프라인 trace 검증, Kit 불필요 |
| `--out` | 산출 루트 |
| `--spawn-floor` | 89.5 |

near-miss 미세조정 플래그는 비우면 `TTS_NEAR_MISS_*` 환경변수로 폴백한다.

| 플래그 | 대응 환경변수 |
|---|---|
| `--near-miss-avoid-frac` | `TTS_NEAR_MISS_AVOID_FRAC` |
| `--near-miss-turn-radius-frac` | `TTS_NEAR_MISS_TURN_RADIUS_FRAC` |
| `--near-miss-aim-frac` | `TTS_NEAR_MISS_AIM_FRAC` |
| `--near-miss-start-jitter` | `TTS_NEAR_MISS_START_JITTER_S` |
| `--near-miss-speed-min-frac` | `TTS_NEAR_MISS_SPEED_MIN_FRAC` |
| `--near-miss-speed-max-frac` | `TTS_NEAR_MISS_SPEED_MAX_FRAC` |
| `--near-miss-depart-spread` | `TTS_NEAR_MISS_DEPART_SPREAD_DEG` |

### replay_range.py — 임의 좌표 구간 재연

`generate_episodes.py`처럼 새로 물리를 돌리는 대신, 기존 좌표 데이터의 한
구간(`--replay-start`~`--replay-end`)을 재생 헤드로 그대로 재연해 렌더만
한다. GUI 캡처의 투영 오차를 피하는 `camera_params` 정합 경로다.

### remote_generation.py / window.py — 원격 잡 제어면

`JobSpec` dataclass가 잡 파라미터 전체(생성/학습/서빙/재연 공통)를 명시값으로
갖고, `Transport`(Local/SSH/REST) 3종이 "어디서 실행하느냐"만 담당한다.
운영 기본은 `RESTTransport` — job API 데몬(`job_type: generate|train|serve_start
|serve_stop|replay`)에 HTTP로 제출하면 데몬이 GPU 큐에 넣어 같은 GPU의 잡을
직렬화한다. `window.py`는 이 위의 Omniverse UI 패널로, Remote Data Generation/
Training(LoRA)/Serving(vLLM)/Replay Render 네 섹션을 갖는다. "Connect Server"
버튼이 SSH로 데몬을 멱등 기동하고 8800(job API)+38011(vLLM) 포트를 터널링한 뒤
Host를 REST URL로 전환한다.

### replay_fidelity.py — 재연 비용 측정 (WP2 clean 조건 겸용)

같은 에피소드의 physics 원본 영상과 좌표 재연 렌더 영상을 쌍으로 만들어 동일
GT로 VLM 검출 성능을 비교한다. 8단계 멱등 파이프라인(모두 산출물이 있으면
건너뛰므로 중단 후 재실행이 이어달리기가 된다): `generate(선택) → fetch-runs →
plan → replay → fetch-replays → fetch-videos → infer → eval`. `phase_plan`은
sim-clock 게이트(trace 시각이 캡처 앵커와 5초 이상 어긋나거나 스팬이 duration의
1.3배를 넘으면 그 에피소드를 제외)를 적용하고, `stratify_select`로 GT 이벤트
수(0/1/2+) 층에서 라운드로빈으로 쌍을 뽑는다.

**채점 규약**(이 파일이 정의하고 `phase_scoring.py`/`perturb_eval.py`/
`rule_baseline.py`가 그대로 재사용):
- `parse_pred_events(result)` — VLM 응답의 `chunk_responses`에서 `[{"HH:MM:SS":
  [ids]}]` 형태를 파싱해 `{초: 라벨집합}`으로 만든다. 코드펜스 제거, JSON
  배열 형식 검증 포함.
- `match_events(gt, pred, tol)` — GT-예측을 `±tol`초에서 1:1 그리디 매칭한다.
  **검출**(detected, 시각이 맞음)과 **귀속**(attributed, 시각도 맞고 객체 ID
  집합도 정확히 일치)을 분리해서 판정한다. `tol=0`이 STRICT, `tol=1`이
  RELAXED(±1초)에 해당한다.
- `f1(tp, fp, fn)` — 표준 F1.
- 통계 검정: `sign_test_p(b, c)`(부호 검정 양측 p값, McNemar 비대칭 판정에 사용).

공개 함수(다른 모듈이 그대로 import): `parse_pred_events`, `match_events`,
`f1`, `make_client`(vLLM 클라이언트 팩토리, Windows ffmpeg.exe 경로 변환 포함),
`Tunnel`, `api_get`/`api_post`, `poll_job`, `ssh_tar_fetch`.

모델/프리셋: `MODEL = "Qwen3-VL-8B-Instruct"`, `PRESET = "twin_view"`
(`vlm_client/prompts.py`의 프롬프트 프리셋 이름).

### perturb_eval.py — 교란 조건 일괄 측정 (WP1 실행 + WP2 측정)

`replay_fidelity`가 만든 쌍(pairs.json, 로컬 trace, GT, clean 추론 결과)을
재사용해 조건별로 `phase_perturb → phase_push → phase_replay → phase_fetch →
phase_infer → phase_report`를 완주한다. 채점은 2기준이다 — ① 원래 GT(사용자에게
전달된 정보 자체의 옳음), ② 화면 기준 GT(교란을 GT에도 반영 — 모델이 화면을
충실히 읽었는가). ①과 ②의 격차가 "데이터 유래 오류"의 크기다.

**`CONDITIONS`**(18개 항목, 이름/kind/파라미터)는 아래 표와 같다. 코드와 산출물
디렉터리의 조건명은 `frag*`이지만, 실험 결과 문서(`experiments/README.md`)는
같은 조건을 **new-id**로 부른다. 대조군 측정으로 이 조건의 실패 원인이 "소멸,
재출현"이 아니라 "학습에 없던 새 번호"임이 확정됐기 때문이다. 그 확정 이후
리포트 표기만 `new-id`로 바꾸고, 코드명(`frag*`)은 계보 보존을 위해 그대로
유지했다:

| 조건 | kind | 의미 |
|---|---|---|
| `g10`/`g25`/`g50` | gaussian | 바닥면(x,z)에 σ=10/25/50cm iid 노이즈 |
| `switch` | switch | 첫 GT 이벤트 3초 전, 당사자 1명 x 비당사자 1명 영구 ID 스왑 |
| `frag` | frag | 충돌 참가 객체별로 첫 충돌 [−4s,−2s) 결손 후 새 ID로 복귀 |
| `frag-sameid` | frag | 위와 같되 개명 없음(번호 효과 분리, 결손 인지 despawn 필요) |
| `frag-inclip` / `frag-inclip-sameid` | frag | 결손 창을 [T−1.0,−0.2)로 좁혀 소멸→재출현→충돌이 같은 2초 청크 안에서 완결되게 재연을 정렬 |
| `frag-inclip2` / `frag-inclip2-sameid` | frag | 결손 창 [T−2.5,−0.5)(재출현 0.5초 뒤 충돌), 정렬 없이 표본화 |
| `occ-hold`/`occ-linear`/`occ-extrap` | occlusion | 각 GT 이벤트 참가자 1명을 3초간 결손(정책: 마지막 값 유지/직선 보간/등속 외삽) |
| `dsr20`/`dsr10`/`dsr5`/`dsr2`/`dsr1` | downsample | 시간 기반 다운샘플, 실측 20/10/5/2/1Hz(소스 60Hz 주기 무관) |

FP(오탐) 원인 분류(`--analyze-fp`, `analyze_fp`): 모델이 보고한 시각/객체
쌍을 실제 좌표와 대조해 `contact`(화면상 실제로 접촉 거리 이내 접근 —
데이터 유래), `near`(1.5배 거리 이내 — 시각적으로 겹쳐 보였을 개연성),
`none`(멀리 있었음 — 모델 자체 환각), `phantom-id`(주장한 객체가 데이터에
아예 없음 — 환각) 4종으로 나눈다.

체계적 실패 게이트: 렌더 시도 4건 이상 중 60% 이상이 스킵되면 즉시 중단한다
(데이터 누락/서버 이상을 조용히 넘기지 않기 위함).

CLI:

| 플래그 | 기본값 / 설명 |
|---|---|
| `--fidelity-out` | `artifacts/replay_fidelity` |
| `--out` | `artifacts/perturb_eval` |
| `--conditions` | 기본 전체 |
| `--gpu` | 1 |
| `--run-tag` | `ptb` |
| `--ssh-host` | — |
| `--remote-ext-root` | — |
| `--analyze-fp` | — |
| `--render-only` | — |
| `--infer-only` | GPU 점유 시간을 줄이려 렌더/추론을 분리 실행 |
| `--self-test` | — |

### rule_baseline.py — 좌표 직결 룰 검출기 대조 (WP3)

GT 라벨러와 동일한 규칙(쌍별 중심거리 < τ)을 영상이나 VLM 없이 좌표에 직접
적용하는 검출기다. 상태기계로 쌍마다 접촉 시작(onset) 1건만 이벤트로 세고,
히스테리시스(재무장 거리 = τ × 1.1)로 문턱 근처 떨림의 이중 계수를 막는다.

동결 원칙: τ는 clean trace에서 1회 스윕한 뒤 전 교란 조건에 고정한다(조건별
재튜닝은 oracle 누수). `FROZEN_TAU = 69.0`(현행 regime3 값, `rule_baseline.py`).
`--resweep`을 주지 않으면 항상 이 값을 쓴다.

CLI:

| 플래그 | 기본값 / 설명 |
|---|---|
| `--fidelity-out` | — |
| `--perturb-out` | — |
| `--out` | — |
| `--tau` | `FROZEN_TAU` |
| `--resweep` | — |
| `--sweep MIN MAX STEP` | `[40.0, 110.0, 5.0]` |
| `--self-test` | — |

산출물 `report.md`가 조건별로 룰 검출기와 VLM의 det F1(검출)/att F1(귀속)을
나란히 놓은 표를 만든다.

### phase_clips.py — 위상 분해 조건 세트 추출 (v3.4)

"모델이 충돌을 판정할 때 실제로 어떤 국면(접근/접촉/사후)을 단서로 쓰는가"를
묻기 위해, 한 충돌 사건을 여러 국면으로 절제한 2초 클립 세트를 만드는 도구다.
충돌 사건의 접촉 구간 `[t_touch, t_release]`은 collisions CSV의 초 단위
클러스터(`kind == "object"` 행만, `CONTACT_CLUSTER_S=0.5`로 묶음)를 기준점
삼아, trace 좌표의 지면 거리 시계열에서 문턱(`DEFAULT_TOUCH_THRESHOLD=90.0`cm)
하회/회복 순간을 프레임 단위로 직접 측정한다. 완전 밀착 구간("플라토",
`d_min + PLATEAU_MARGIN_CM(3.0)` 이하)도 함께 측정해 `no_approach`/
`no_aftermath`의 기준점으로 쓴다.

조건 격자(전부 2초, `window_specs()`):

| 조건 | 기준점 | 창(오프셋, 초) | 의미 |
|---|---|---|---|
| `full` | t_touch | [−1.0, +1.0] | 접근+접촉+사후 전부(대조) |
| `no_approach` | t_hold_start(플라토 시작) | [+g, +g+2.0] | 접근 동역학 배제, 접촉+사후만 |
| `no_aftermath` | t_hold_end(플라토 끝) | [−g−2.0, −g] | 사후 신호 배제, 접근+접촉만 |
| `approach_only` | t_touch | 접촉 직전 2초 | 접촉 프레임 원천 배제 |
| `aftermath_only` | t_release | 접촉 직후 2초 | 접촉 프레임 배제, "튕겨나옴" 게이트로 붙어-정지형 배제 |
| `no_contact` | t_touch/t_release | 접촉 전후 각 1초 스플라이스 | 접촉 구간만 도려냄 |
| `near_miss` | 거리 극소점 | [−1.0, +1.0] | 접촉 없는 근접 조우 |
| `control` | 무작위 | 2초 | 모든 이벤트에서 buffer(3초) 이상 떨어진 무관 구간 |

이 표가 정의하는 각 조건의 창 위치를 실제 접촉 사건의 시간축 위에 겹쳐
그리면, 어느 조건이 접근 국면을 남기고 어느 조건이 사후 국면을 남기는지
한눈에 대조된다.

<!-- 이미지 TODO | 파일: images/readme/automation-02-phase-windows.png | 도식: 가로축을 시간(초)으로 두고 t_touch(접촉 시작)와 t_release(접촉 종료)를 세로선으로 표시. 그 위에 full/no_approach/no_aftermath/approach_only/aftermath_only/no_contact 여섯 조건의 2초 창을 각각 다른 색 막대로 배치해 어느 조건이 접근/접촉/사후 중 무엇을 포함하고 무엇을 배제하는지 시각화(위 표의 "기준점"과 "창(오프셋)" 열 그대로 좌표화) | 형식: png -->
![그림 2. phase_clips 조건별 클립 창의 시간축 배치](../../../../images/readme/automation-02-phase-windows.png)
*그림 2. t_touch/t_release를 기준으로 각 조건이 접근/접촉/사후 국면 중 무엇을 담고 무엇을 잘라내는지 보여주는 시간축 도식.*

`no_approach`와 `no_aftermath`는 접촉 구간(t_touch~t_release)을 사이에 두고
서로 반대쪽 국면만 남기도록 창이 배치되며, `approach_only`와
`aftermath_only`는 접촉 구간 자체를 창에서 완전히 비켜간다는 것이 이
도식에서 드러나는 설계 의도다.

(g = `--hold-guard`, 기본 0.05초.) `no_contact`는 절제 반경 계열
(`NO_CONTACT_RADII`: plateau/70/80/100/110/120cm)로도 병행 추출해, "접촉을
빼도 어느 거리까지 보여주면 모델이 여전히 발화하는가"를 용량-반응 곡선으로
잰다.

각 창은 세 겹의 게이트를 통과해야 클립으로 만들어진다: (1) `gate_window` —
조건이 주장하는 국면을 실제로 담고 있는지 기계 검증(예: `no_approach`는 창
첫 프레임이 플라토 안이어야 함), (2) `purity_violation` — 창 안에 기준 사건이
아닌 다른 객체-객체 접촉이 겹치지 않는지(벽 접촉은 순도 검사에서 무시),
(3) `third_object_min_distance` — collisions CSV에 기록되지 않는(반응 없이
스쳐가는) 제3객체 개입을 trace 기하로 직접 검사(v3.3, 기본 90cm 미만이면 폐기).

CLI:

| 플래그 | 기본값 / 설명 |
|---|---|
| `--collision-run` | 반복 가능 |
| `--nearmiss-run` | 반복 가능 |
| `--out` | — |
| `--gap` | 95.0 |
| `--touch-threshold` | 90.0 |
| `--search-back` / `--search-fwd` | 1.5 |
| `--max-contact` | 3.0 |
| `--plateau-margin` | 3.0 |
| `--hold-guard` | 0.05 |
| `--third-object-cm` | 90.0 |
| `--controls-per-episode` | 1 |
| `--seed` | 42 |
| `--dry-run` | — |
| `--nearmiss-reversal-jump` | 400.0 — near-miss 응급 이탈 게이트(최근접 ±0.3초 반경 속도 점프 상한) |
| `--overlay-touch-cm` | 0.0 — legacy 거리 문턱 방식(픽셀 판정 `overlay_flags.py`가 정본) |
| `--reflag-manifest` | 기존 manifest에 `overlay_overlap`만 재부여하는 별도 모드, `--out` 불필요 |

산출물: `{조건}/{조건}_{에피소드}_{NNNN}.mp4` + `clips_manifest.json`
(조건별 게이트 통계, 클립별 anchor/segments/pair/contact_len_s/d_min 등).

### phase_scoring.py — 위상 분해 클립 채점

`phase_clips.py`가 만든 클립 세트를 프로덕션 추론 경로(`replay_fidelity.PRESET`,
`utils.vllm_client.VLLMClient.analyze_video`, `replay_fidelity.parse_pred_events`)
**그대로** 재사용해 클립 1개당 요청 1개로 추론시킨다. train==infer 불변식을
지키기 위해 프롬프트 문자열은 이 모듈에 사본조차 두지 않는다. `chunk_duration`
을 클립의 실측 길이와 같게 넘겨(`analyze_video(..., chunk_duration=duration)`)
"클립 전체 = 청크 1개"를 만든다.

발화율 = 그 조건의 클립 중 충돌 이벤트를 하나라도 보고한 클립의 비율
(`classify_response`가 `events`/`silent`(정상 빈 응답)/`unparsed`(형식 이탈)/
`empty`로 분류). 실패 클립은 분모에서 제외한다. 실패율이 `ABORT_FAIL_RATE`
(30%, 최소 시도 10건 이후)를 넘으면 서빙 이상으로 보고 즉시 중단한다.

CLI:

| 플래그 | 기본값 / 설명 |
|---|---|
| `--manifest` | 필수 |
| `--clips-root` | — |
| `--endpoint` | `http://localhost:38011/v1` |
| `--out` | 필수 |
| `--model` | `MODEL` |
| `--limit` | — |
| `--dry-run` | — |

산출물: `results.jsonl`(멱등 이어하기), `summary.md`, `summary.json`.

### overlay_flags.py — 화면 겹침 픽셀 판정 (후처리)

`near_miss`/`no_contact`/`approach_only`/`control` 클립에서 "두 물체가
화면에서 겹쳐 보이는가"를 좌표 재투영이 아니라 **렌더된 픽셀에서 직접**
판정한다(좌표 재투영 경로는 정합이 보장되지 않아 기각됨, 모듈 docstring
참조). 클립당 추론 프레임 20장 전수를 검사한다(최근접 순간 1장 근사는
채택하지 않음 — 화면 겹침이 거리뿐 아니라 화면 위치에도 좌우되기 때문).

판정 5단계: (1) `detect_circles` — 밝은 원판(반지름 9px)+검은 테두리
(반지름 11.5px) 고정 크기 매칭으로 오버레이 원 검출, (2) `background_percentile`
+`motion_mask` — 클립 프레임 전수의 픽셀별 하위 25%값을 배경으로 추정해
차분(프레임 간 직접 차분과 중앙값 배경은 각각 "빈 블롭"과 "배경 흡수" 문제로
기각됨), (3) `attribute_circles` — 원을 자기 블롭에 귀속, (4) `frame_verdict`
— `circle_circle`(원판 교차)/`circle_object`(원판 안에 남의 블롭 픽셀
8px 이상)/`merged_blob`(두 자기 블롭이 같은 연결 성분) 세 사유 중 하나라도
성립하면 겹침, (5) `clip_verdict` — 20장 중 1장이라도 겹치면 클립 겹침.

`--debug-samples`가 저장하는 판정 근거 주석 이미지를 보면 이 5단계 판정이
실제 프레임에서 무엇을 검출하고 어떻게 겹침을 확정하는지 확인할 수 있다.

<!-- 이미지 TODO | 파일: images/readme/automation-03-overlay-verdict.png | 촬영: overlay_flags.py를 `--debug-samples --debug-dir <dir>`로 실행한 뒤 `near_miss` 또는 `no_contact` 조건 클립 하나의 겹침(overlap) 판정 프레임을 선택. 주석 이미지에 오버레이 원(밝은 원판+검은 테두리) 검출 결과와 판정 사유(circle_circle/circle_object/merged_blob)가 표시되어 있어야 함 | 형식: png -->
![그림 3. overlay_flags의 화면 겹침 판정 프레임](../../../../images/readme/automation-03-overlay-verdict.png)
*그림 3. 오버레이 원 검출과 블롭 귀속을 거쳐 두 물체가 화면에서 겹쳐 보인다고 판정된 프레임의 주석 예시.*

이 프레임에서 원판 두 개가 실제로 교차하거나(circle_circle) 한 원판 안에
다른 객체의 블롭 픽셀이 들어와 있는지(circle_object)를 확인하면, 좌표상의
근접이 아니라 화면 픽셀 수준에서 겹침을 판정한다는 이 모듈의 설계가
구체적으로 무엇을 보고 있는지 알 수 있다.

CLI:

| 플래그 | 기본값 / 설명 |
|---|---|
| `--manifest` | 필수 |
| `--clip-root` | — |
| `--conditions` | — |
| `--frames` | 20 |
| `--fps` | 10.0 |
| `--overlay-radius` | 12.0 |
| `--diff-thr` | 25 |
| `--bg-percentile` | 25.0 |
| `--min-blob-px` | 60 |
| `--no-merged-blob` | — |
| `--debug-samples` | 판정 근거 주석 이미지 저장, 육안 오판율 검증용 |
| `--debug-dir` | — |
| `--limit` | — |
| `--dry-run` | — |

`clips_manifest.json`에 `overlay_overlap`/`overlay_overlap_frames`/
`overlay_reasons`를 재절단 없이 덧쓴다.

### geom.py / scene_profiles.py / run_headless.sh

`geom.center_distance_3d(a, b)`는 3D 중심거리 계산의 단일 구현이며
`perturb_eval.py`/`rule_baseline.py`가 공용으로 쓴다(비트 단위로 동일한
결과를 내도록 검증됨, `tests/test_geom.py`). `scene_profiles.py`는
`scene_profiles.json`(추적 파일)에서 이름으로 아레나 범위/스테이지/카메라를
읽어 생성/재연 잡이 궤적 데이터 없이도 씬을 결정할 수 있게 한다.
`run_headless.sh`는 Windows/로컬 Kit 실행 파일로 `generate_episodes.py`를
배치 구동하는 launcher다.

## 실행 예 (레포 루트에서)

```bash
# Kit --exec로 실행되는 파일(생성/재연) — Kit 실행 파일이 필요
KIT_APP=... EXT_ROOT=... EPISODES=50 OUT=artifacts/episodes DURATION=40 \
  bash gist/netai/time_travel_summarization/automation/run_headless.sh

# 순수 stdlib 드라이버 — WSL/Windows에서 python -m으로 직접 실행
python3 -m gist.netai.time_travel_summarization.automation.replay_fidelity \
    --generate 14 --runs gen-20260718-153511 --n 15
python3 -m gist.netai.time_travel_summarization.automation.perturb_eval \
    --conditions g25 switch frag occ-hold dsr5
python3 -m gist.netai.time_travel_summarization.automation.rule_baseline
python -m gist.netai.time_travel_summarization.automation.phase_clips \
    --collision-run artifacts/episodes/gen-... --nearmiss-run artifacts/episodes/gen-... \
    --out artifacts/phase_ablation_v1
python -m gist.netai.time_travel_summarization.automation.phase_scoring \
    --manifest artifacts/phase_ablation_v1/clips_manifest.json --out artifacts/phase_scoring_v1
python -m gist.netai.time_travel_summarization.automation.overlay_flags \
    --manifest artifacts/phase_ablation_v1/clips_manifest.json --clip-root artifacts/phase_ablation_v1

# 순수 헬퍼 self-test (Kit/네트워크 불필요) — 레포 루트에서 실행
python3 gist/netai/time_travel_summarization/automation/generate_episodes.py --self-test
python3 gist/netai/time_travel_summarization/automation/replay_range.py --self-test
python3 -m gist.netai.time_travel_summarization.automation.replay_fidelity --self-test
python3 -m gist.netai.time_travel_summarization.automation.perturb_eval --self-test
python3 -m gist.netai.time_travel_summarization.automation.rule_baseline --self-test
```

## 환경변수

- `TTSUM_REMOTE_HOST` — `replay_fidelity.py`/`perturb_eval.py`/`window.py`의
  기본 SSH 대상(GPU 서버).
- `TTSUM_REMOTE_EXT_ROOT` — 위 원격 호스트의 확장 루트 경로.
- `TTS_NEAR_MISS_AVOID_FRAC` / `TTS_NEAR_MISS_TURN_RADIUS_FRAC` /
  `TTS_NEAR_MISS_AIM_FRAC` / `TTS_NEAR_MISS_START_JITTER_S` /
  `TTS_NEAR_MISS_SPEED_MIN_FRAC` / `TTS_NEAR_MISS_SPEED_MAX_FRAC` /
  `TTS_NEAR_MISS_DEPART_SPREAD_DEG` —
  `generate_episodes.py`의 near-miss CLI 플래그가 비어 있을 때 물리 쪽
  컨트롤러가 폴백으로 읽는 값(코드 기본값이 최종 폴백).
- `USE_CONTAINER` / `KIT_CONTAINER_IMAGE` / `L40_ENV_FILE` —
  `remote_generation.py`가 SSH/local 직결 제출 시 러너로 그대로 전달하는
  인프라 스위치(REST 경로는 job API 데몬의 환경이 대신 적용).

## 테스트

전용 유닛 테스트는 `gist/netai/time_travel_summarization/tests/`에 있다
(`test_near_miss.py`, `test_geom.py`, `test_phase_clips.py`,
`test_phase_scoring.py`, `test_overlay_flags.py`, `test_scene_profiles.py`,
`test_replay_job.py` 등 — 자세한 대응표는 `tests/README.md` 참조). 이 외에
`generate_episodes.py`, `replay_range.py`, `replay_fidelity.py`,
`perturb_eval.py`, `rule_baseline.py`는 각각 `--self-test`(또는 파일 직접
실행)로 Kit/네트워크 없이 순수 헬퍼를 자체 검증한다.

## 한계

- `generate_episodes.py`/`replay_range.py`는 kit이 스크립트로 직접 실행하므로
  절대 임포트만 가능하다(부모 패키지가 없어 상대 임포트가 실패한다).
- `replay_fidelity.py`/`perturb_eval.py`는 자체 SSH 터널로 GPU 서버의
  job API/vLLM에 직결하므로 원격 서버가 떠 있어야 동작한다(로컬 self-test는
  예외).
- `phase_clips.py`/`overlay_flags.py`는 ffmpeg를 이름으로 직접 부르므로 PATH에 있어야 한다. VLM 추론을 도는 드라이버(`replay_fidelity.py`, `perturb_eval.py`, `phase_scoring.py`)도 `utils/vllm_client.py`의 청크 재인코딩 경로에서 ffmpeg(PATH 또는 imageio_ffmpeg 폴백)에 의존한다.
- near-miss 조우의 응급 이탈 게이트(`NEARMISS_REVERSAL_JUMP` 등)는 특정 안무
  세대(swerve v3 조향 이전/이후)에 맞춰 튜닝된 값이라 원료 데이터 세대가
  바뀌면 재검토가 필요하다.
