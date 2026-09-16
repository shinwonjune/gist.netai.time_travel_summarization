# Time Travel Summarization

> 디지털 트윈과 시각 언어 모델(VLM)을 연계해 **사건 중심으로 과거 시공간을 재구성**하는 NVIDIA Omniverse Kit 확장(익스텐션).

GIST NetAI Lab의 연구 결과물 저장소다. 관리자가 긴 로그 영상을 처음부터 끝까지 수동으로
훑지 않아도, 디지털 트윈에 기록된 좌표 궤적과 VLM의 이벤트 판독을 결합해 사건(현재는
'충돌') 발생 시점과 위치로 즉시 이동할 수 있게 한다.

초기 버전(v1 규약)에서는 시각적 추상화 환경에서 Qwen3-VL-8B의 F1을 약 0.2 개선하고,
3배속 가속 시 추론 시간을 50% 이상 단축(최대 12시간 로그까지 적용 가능 확인)하는 결과를
얻었다 — 이 수치는 초기 v1 규약 실험 기준이며, 이후 규약을 다시 정해 재측정했으므로
현행 공표값이 아니다. 현행 실험 결과는 [`experiments/README.md`](experiments/README.md)를
따른다.

프로젝트는 이 폐루프(재현 → 판독) 위에서 **파이프라인 강건성의 측정**으로 범위를
넓혔다. 좌표 궤적에 현실적인 오류(측위 오차, ID 스왑, 가림, 다운샘플링 등)를 주입하고,
그 교란된 좌표로 디지털 트윈을 재연해 영상을 뽑은 뒤, VLM이 그 영상에서 여전히 충돌
이벤트를 검출하는지를 정량으로 측정한다. 이 측정 축을 지원하기 위해 데이터 레이크
(minIO) 연동, 물리 기반 에피소드 대량 생성, 좌표 교란기, VLM 학습(LoRA) 파이프라인이
추가됐다.

---

## 1. 전체 구조

세 실행 환경이 맞물려 하나의 파이프라인을 이룬다.

```
┌────────────────────────────────────────────────────────────────────┐
│  Omniverse Kit 확장 (이 저장소)                                       │
│  ─────────────────────────────────────────────────────────────────  │
│  playback(재생) ↔ physics(물리 시뮬레이션) → video_capture(캡처,     │
│  VLM이 보는 마커도 이 단계에서 합성) → vlm_client(추론 요청)          │
│  → events(후처리). overlay(GUI 전용 프리뷰), perturbation(좌표 교란), │
│  automation(측정 파이프라인 CLI 전체)는 이 흐름 밖의 보조 모듈        │
└───────────────┬───────────────────────────────────┬─────────────────┘
                │ 좌표/영상 I/O (storage/)            │ HTTP(추론) / SSH+REST(잡 제출)
                ▼                                    ▼
┌───────────────────────────┐          ┌──────────────────────────────┐
│  minIO (S3 호환 데이터 레이크) │          │  GPU 서버 (L40/A100)           │
│  궤적 청크 + manifest        │          │  ─────────────────────────    │
│  캡처 영상, VLM 산출물        │◀────────▶│  vLLM(OpenAI 호환 서빙)        │
│                              │  적재/조회 │  job_api.py(생성/학습/재연 큐) │
└───────────────────────────┘          └──────────────────────────────┘
```

- **확장**은 USD 스테이지 위에서 좌표 궤적을 재생하거나 물리로
  시뮬레이션하고, 그 장면을 캡처해 VLM 서버로 보낸다. GUI(사람이 조작)와 headless
  배치(`kit --exec`, 측정 자동화) 두 가지 실행 형태를 모두 지원한다.
- **GPU 서버**는 vLLM이 VLM 추론을 OpenAI 호환 API로 서빙하고, 같은 서버에서
  `job_api.py`(FastAPI)가 데이터 생성/학습/재연 잡을 큐로 관리해 단일 GPU를 여러 잡이
  안전하게 나눠 쓰게 한다.
- **minIO**는 궤적 데이터셋(시간 분할 청크 + manifest)과 캡처 영상, VLM 산출물, 이벤트
  인덱스를 저장하는 오브젝트 스토리지다. `storage/`가 로컬 파일시스템과 minIO를 같은
  URI 인터페이스(`file://`, `s3://`)로 묶어, 확장의 나머지 코드는 데이터가 어디
  있는지 신경 쓰지 않는다.

측정 국면에서는 이 루프 앞에 좌표 교란 단계가 하나 더 붙는다. 원본 궤적을
`perturbation/`으로 교란한 뒤 같은 루프(재연 → 영상 → VLM)를 다시 태워, 교란 유무에
따른 검출 성능 차이를 비교한다.

---

## 2. 작동 흐름 (end-to-end)

두 갈래로 나뉜다. 하나는 사람이 GUI로 조작하는 **운영 흐름**(한 영상을 만들어 요약을
보는 것)이고, 다른 하나는 headless 배치로 대량 데이터를 만들고 강건성을 재는
**측정 흐름**이다.

### 2-1. 운영 흐름

```
궤적 데이터(CSV 또는 데이터 레이크)
   │  playback/ — TrajectoryRepository, LakeTrajectoryRepository
   ▼
재생(Time Travel) — 재생 헤드 시각을 좌표로 바꿔 USD 프림에 반영
   │  playback/controller.py — PlaybackController
   ▼
캡처(Capture) — 재생 장면을 mp4로 추출하면서 객체 ID 마커와 시각 텍스트를 프레임에 합성
   │  video_capture/realtime_capture.py — RealtimeCaptureRunner.capture()
   │  video_capture/overlay_composer.py — OverlayComposer (VLM이 보는 시각 단서는 여기서 굽는다.
   │  GUI의 View Overlay(overlay/core.py)는 사람이 보는 프리뷰 전용으로 캡처 영상에 들어가지 않는다)
   ▼
VLM 추론(VLM Client) — 영상을 2초 청크로 잘라 vLLM 서버에 요청
   │  vlm_client/core.py — VLMClientCore.generate_captions()
   ▼
이벤트 후처리(Event Post Processing) — VLM JSON → 표준 이벤트 스키마 + 3D 좌표
   │  events/summary_service.py — EventSummaryService.process_event_json()
   ▼
사건 중심 요약 재생(Event-based Summarization Playback)
      events/summary_window.py — EventSummaryWindow
```

캡처 단계가 실제로 만들어내는 프레임은 사람이 보는 3D 뷰포트가 아니라, 객체 ID
마커와 재생 시각이 합성된 평면 영상이다. VLM은 이 화면만 보고 이벤트를 판독하므로,
아래 그림이 곧 VLM의 실제 입력이다.

<!-- 이미지 TODO | 파일: images/readme/root-01-capture-marker-frame.png | 촬영: 확장 GUI로 궤적 데이터를 로드해 재생 중, 두 객체가 서로 접근하거나 접촉한 순간을 Capture 버튼으로 캡처한 mp4에서 한 프레임을 뽑아 저장. 프레임 안에 최소 2개 이상 객체의 ID 마커(숫자 라벨)와 화면의 현재 재생 시각(Timestamp) 오버레이가 함께 보여야 함. 720x480 해상도 그대로 | 형식: png -->
![그림 1. 캡처 단계의 산출 프레임 — 객체 ID 마커와 재생 시각이 합성된 화면](images/readme/root-01-capture-marker-frame.png)
*그림 1. 캡처 단계의 산출 프레임. 객체마다 붙은 숫자 ID 마커와 화면에 새겨진 재생
시각이 VLM이 실제로 근거로 삼는 유일한 정보다.*

흐름의 마지막 단계인 사건 중심 요약 재생은 이벤트 리스트를 따라 뷰포트가 자동으로
사건 발생 시공간으로 이동하며 그 구간만 재생하는 화면으로, 아래 그림처럼 보인다.

<!-- 이미지 TODO | 파일: images/readme/root-02-event-summary-playback.png | 촬영: Event Post Processing까지 마친 뒤 Time Travel Window에서 Event based Summary 체크박스를 켠 상태(라벨이 "Event based Summary Mode (N events)"로 바뀐 것이 보이게), Play를 눌러 한 이벤트 구간이 재생되는 도중 뷰포트가 사건 발생 위치를 비추고 있는 순간. summarization_camera로 전환된 뷰포트가 프레임에 포함되어야 함 | 형식: png 또는 5초 내외 gif(이벤트 구간 자동 이동이 보이도록) -->
![그림 2. Event-based Summarization Playback 화면](images/readme/root-02-event-summary-playback.png)
*그림 2. 사건 중심 요약 재생 화면. Time Travel Window의 모드 라벨이 이벤트 개수를
보여주고, 뷰포트는 이벤트 리스트를 따라 사건이 벌어진 시공간으로 자동 이동한다.*

### 2-2. 측정 흐름

```
씬 프로파일/궤적 데이터
   │  physics/ — WanderController(배회 안무), CollisionRecorder(GT 라벨), TraceRecorder(좌표)
   ▼
물리 에피소드 생성 (headless 배치)
   │  automation/generate_episodes.py — kit --exec generate_episodes.py -- <args>
   ▼
학습 데이터셋 빌드
   │  utils/build_dataset.py — python -m ...utils.build_dataset
   ▼
LoRA 학습 (ms-swift)
   │  training/qwen3vl_lora_swift.sh — swift sft 호출
   ▼
재연 비용 측정 (physics 원본 vs 좌표 재연 쌍 비교, clean 조건 겸용)
   │  automation/replay_fidelity.py — python -m ...automation.replay_fidelity
   ▼
좌표 교란 주입 → 재연 → VLM 추론 → 검출/귀속 지표 분리
   │  perturbation/perturb.py(gaussian, id_switch, fragmentation, occlusion, downsample)
   │  automation/perturb_eval.py — python -m ...automation.perturb_eval
   ▼
룰 베이스라인 대조 (좌표 직결 임계값 검출기 vs VLM)
   │  automation/rule_baseline.py — python -m ...automation.rule_baseline
   ▼
위상 분해 (충돌 단서 국면별 절제 클립 → 조건별 발화율)
      automation/phase_clips.py → automation/phase_scoring.py → automation/overlay_flags.py
```

측정 흐름의 첫 산출물인 물리 에피소드는 사람이 조작하는 GUI 화면이 아니라 headless
배치가 만들어내는 BEV(조감) 장면이다. 아래 그림은 그 장면에서 여러 객체가 아레나
안을 배회하는 모습을 보여준다.

<!-- 이미지 TODO | 파일: images/readme/root-03-physics-episode-scene.png | 촬영: automation/generate_episodes.py로 생성한 에피소드 하나의 캡처 mp4(또는 headless 렌더 산출물)에서 여러 객체(3~4개)가 아레나 안에서 각자 다른 위치에 흩어져 배회하는 순간의 프레임. 위에서 내려다보는 BEV 시점이어야 하고, 아레나 경계(씬 프로파일이 정의한 배회 영역)가 프레임 안에 들어오면 좋음. 두 객체가 근접하거나 충돌하기 직전 장면이면 더 좋음 | 형식: png -->
![그림 3. 물리 시뮬레이션이 생성한 배회 에피소드 장면 — BEV 시점](images/readme/root-03-physics-episode-scene.png)
*그림 3. 물리 에피소드 생성이 만들어낸 BEV 장면. 여러 객체가 아레나 경계 안에서
독립적으로 배회하며, 이 궤적이 이후 좌표 교란과 재연 측정의 원료가 된다.*

두 흐름은 `storage/`(URI 통일 인터페이스)와 `vlm_client/prompts.py`(학습과 추론이 공유하는
프롬프트)를 공유해, 운영 경로에서 쓰는 것과 측정 경로에서 쓰는 것이 같은 VLM 입력
분포를 보게 만든다(train == infer 정합).

---

## 3. 구성 요소

`gist/netai/time_travel_summarization/` 아래 디렉터리별 역할과 각 파트의 상세 문서로
가는 링크다.

| 디렉터리 | 역할 | 핵심 심볼 | 문서 |
|---|---|---|---|
| `app/` | 확장 전체 상태를 한 곳에 모으는 facade `TimeTravelCore`와, 그 상태를 실제로 조작하는 도메인 서비스(데이터 소스, 캡처, 물리, 객체 생성) | `TimeTravelCore`, `physics_service`, `capture_service` | [app/README.md](gist/netai/time_travel_summarization/app/README.md) |
| `playback/` | 재생 헤드 시각을 궤적 좌표로 바꿔 USD 프림에 반영하는 도메인. 단일 파일과 데이터 레이크 두 리포지토리를 지원 | `PlaybackController`, `TrajectoryRepository`, `LakeTrajectoryRepository` | [playback/README.md](gist/netai/time_travel_summarization/playback/README.md) |
| `overlay/` | 뷰포트에 3D 객체 ID 라벨과 시각 HUD를 그리는 GUI 전용 프리뷰 오버레이(학습, 추론 영상의 시각 규약과는 별개) | `ViewOverlay`, `OverlayControlWindow` | [overlay/README.md](gist/netai/time_travel_summarization/overlay/README.md) |
| `vlm_client/` | vLLM(OpenAI 호환) 직결 VLM 추론 클라이언트와 GUI 창 | `VLMClientCore`, `VLMClientWindow`, `PROMPTS` | [vlm_client/README.md](gist/netai/time_travel_summarization/vlm_client/README.md) |
| `events/` | VLM 추론 결과를 파싱, 시간축 인덱스, 3D 좌표로 이어붙이는 후처리 계층 | `EventSummaryService`, `event_index.py` | [events/README.md](gist/netai/time_travel_summarization/events/README.md) |
| `physics/` | PhysX 기반 배회 시뮬레이션과 충돌 GT 라벨링(콜라이더, 충돌거리 규약, near-miss 대조군 안무) | `WanderController`, `TraceRecorder`, `CollisionRecorder`, `collision_proxy.py` | [physics/README.md](gist/netai/time_travel_summarization/physics/README.md) |
| `perturbation/` | 좌표 트랙 교란기 — 강건성 측정용 오류 주입(순수 함수, GT는 건드리지 않음) | `gaussian`, `id_switch`, `fragmentation`, `occlusion`, `downsample`, `author_near_stop` | [perturbation/README.md](gist/netai/time_travel_summarization/perturbation/README.md) |
| `storage/` | 로컬 파일시스템과 minIO(S3 호환)를 같은 URI 인터페이스로 여는 계층 | `from_uri`, `LocalAdapter`, `MinioAdapter`, `normalize_source` | [storage/README.md](gist/netai/time_travel_summarization/storage/README.md) |
| `video_capture/` | 뷰포트를 mp4로 캡처하는 두 경로(A1 baseline / A2 realtime)와 인코딩 파이프라인 | `RealtimeCaptureRunner`, `MovieCaptureRunner`, `OverlayComposer` | [video_capture/README.md](gist/netai/time_travel_summarization/video_capture/README.md) |
| `automation/` | BEV 파이프라인 생성, 재연, 강건성 측정 자동화 CLI 일체 | `generate_episodes.py`, `perturb_eval.py`, `phase_clips.py`, `remote_generation.py` | [automation/README.md](gist/netai/time_travel_summarization/automation/README.md) |
| `training/` | Qwen3-VL-8B LoRA 파인튜닝(ms-swift) 실행 스크립트와 방법론 문서 | `qwen3vl_lora_swift.sh`, `remote_train.sh`, `run_eval.sh` | [training/README.md](gist/netai/time_travel_summarization/training/README.md) |
| `ui/` | 메인 재생 제어 창과 백그라운드 스레드 → 메인 스레드 UI 디스패치 인프라 | `TimeTravelWindow`, `UiTaskDispatcher` | [ui/README.md](gist/netai/time_travel_summarization/ui/README.md) |
| `utils/` | 데이터셋 구성, 추론 클라이언트, 채점, 레이크 적재 도구(stdlib 단독 실행 가능) | `build_dataset.py`, `vllm_client.py`, `compare_results.py`, `ingest_trajectory.py` | [utils/README.md](gist/netai/time_travel_summarization/utils/README.md) |
| `tests/` | 유닛 테스트(39개 파일)와 레이크 성능 벤치마크 하네스 | `conftest.install_carb_stub`, `lake_benchmark.py` | [tests/README.md](gist/netai/time_travel_summarization/tests/README.md) |
| `VLM_server/` | VLM 서버(vLLM) 실행 가이드와 GPU 서버의 잡 API/서빙 운영 | `job_api.py`, `run_qwen3-vl-8b.sh` | [VLM_server/README.md](gist/netai/time_travel_summarization/VLM_server/README.md) |
| `tools/`(레포 루트) | 확장 밖에서 실행하는 합성 궤적 생성과 인프라 진단 스크립트 | `generate_living_trajectory.py`, `lake_ingest.py`, `diagnostics/minio_probe.py` | [tools/README.md](tools/README.md) |

확장 자체의 사용자 워크플로 가이드(궤적 생성 → Config → Extension 활성화 →
단계별 GUI 조작)는 [`gist/netai/time_travel_summarization/Readme.md`](gist/netai/time_travel_summarization/Readme.md)에 있다.

---

## 4. 핵심 사용법

### 4-1. 확장 로드와 GUI 워크플로

USD Composer(또는 다른 Kit 기반 앱)의 Extensions 창에서 이 확장의 로컬 경로를 추가해
활성화한다. 활성화 시 Time Travel, View Overlay, VLM Client, Event Post Processing 등
모든 모듈의 창이 함께 초기화되며, 실제로 뜨는 화면은 아래 그림과 같다.

<!-- 이미지 TODO | 파일: images/readme/root-04-gui-all-windows.png | 촬영: USD Composer에서 Extension을 막 활성화한 직후, Time Travel, View Overlay, VLM Client, Event Post Processing 4개 창이 모두 화면에 떠 있는 상태 전체를 캡처. 창이 서로 겹쳐 안 보이지 않게 배치한 뒤 촬영 | 형식: png -->
![그림 4. 확장 활성화 직후 초기화되는 GUI 창들](images/readme/root-04-gui-all-windows.png)
*그림 4. Extension 활성화 직후 모습. Time Travel, View Overlay, VLM Client, Event
Post Processing 등 여러 창이 동시에 열리며, 이후 단계별 조작은 이 창들을 오가며
진행한다.*

궤적 데이터 생성부터 사건 중심 요약 재생까지 단계별
조작법은 [`Readme.md`](gist/netai/time_travel_summarization/Readme.md)를 따른다.

### 4-2. VLM 서버 준비

GPU 서버에서 vLLM 컨테이너(OpenAI 호환 API)를 띄우고, 필요하면 `job_api.py`로 생성/
학습/재연 잡의 GPU 큐를 관리한다. 절차는 [`VLM_server/README.md`](gist/netai/time_travel_summarization/VLM_server/README.md)와
[`VLM_server/l40/SETUP.md`](gist/netai/time_travel_summarization/VLM_server/l40/SETUP.md)를 따른다.

### 4-3. 측정 파이프라인 실행 예

`automation/`의 각 CLI는 절대 임포트를 쓰므로, 레포 루트(`gist/` 디렉터리가 보이는 곳)를
현재 디렉터리 또는 `PYTHONPATH`로 두고 `python -m` 형태로 실행한다.
`generate_episodes.py`/`replay_range.py`만 예외로, Kit 런타임이 필요해 `kit --exec`로
직접 실행하며 패키지 임포트를 쓰지 않는다.

```bash
# 물리 시뮬레이션 배치 촬영 (Kit 실행 파일 필요)
KIT_APP=... EXT_ROOT=... EPISODES=50 OUT=artifacts/episodes DURATION=40 \
  bash gist/netai/time_travel_summarization/automation/run_headless.sh

# 재연 비용 측정 (WSL/Windows에서 순수 stdlib로 SSH 터널을 통해 GPU 서버의 job API/vLLM에 직결)
python3 -m gist.netai.time_travel_summarization.automation.replay_fidelity \
    --generate 14 --runs gen-20260718-153511 --n 15

# 좌표 교란 조건 일괄 측정
python3 -m gist.netai.time_travel_summarization.automation.perturb_eval \
    --conditions g25 switch frag occ-hold dsr5

# 학습 데이터셋 빌드 → LoRA 학습
python -m gist.netai.time_travel_summarization.utils.build_dataset \
    --episodes-dir artifacts/episodes --out-dir artifacts/dataset --preset twin_view --nframes 20
DATA=artifacts/dataset OUTPUT=artifacts/lora_qwen3vl \
    bash gist/netai/time_travel_summarization/training/qwen3vl_lora_swift.sh
```

각 모듈의 정확한 CLI 인자와 산출물 경로는 [automation/README.md](gist/netai/time_travel_summarization/automation/README.md)의
모듈 표, [utils/README.md](gist/netai/time_travel_summarization/utils/README.md)의 모듈 표,
[training/README.md](gist/netai/time_travel_summarization/training/README.md)를 참조한다.

### 4-4. 테스트

레포 루트(`pyproject.toml`이 있는 위치)에서 실행한다. `pyproject.toml`의
`testpaths`가 `gist/netai/time_travel_summarization/tests`로 고정돼 있어 아무 위치에서나
`pytest`만 실행해도 이 디렉터리가 수집된다.

```bash
python3 -m pytest -q          # 단위 테스트(omni/carb 무의존 로직만, Kit 불필요)
ruff check gist/              # 린트
mypy                          # 타입 검사(점진 도입 대상 모듈, pyproject.toml 설정 사용)
```

CI(`.github/workflows/ci.yml`)는 위 세 단계에 이어 `generate_episodes.py --self-test`,
`remote_generation.py`, `utils/vllm_client.py`의 순수 헬퍼 자기 테스트를 순서대로
실행한다. Kit 런타임이 필요한 동작(렌더링, USD 프림 조작, 실제 물리 스텝)은 이 스위트로
검증되지 않는다 — 자세한 테스트 파일 대응표는 [tests/README.md](gist/netai/time_travel_summarization/tests/README.md) 참조.

---

## 5. 설정

### 5-1. 환경 변수

| 변수 | 용도 | 근거 |
|------|------|------|
| `ASTRONAUT_USD_PATH` | Time Travel 객체로 사용할 USD 경로 (`config.json`의 `${ASTRONAUT_USD_PATH}`로 치환) | `config.json`, `config.example.json` |
| `VLM_BASE_URL` | vLLM 서버 base URL (OpenAI 호환 엔드포인트). 미설정 시 `http://localhost:38011`로 폴백 | `vlm_client/core.py` |
| `VLM_API` | VLM 백엔드 종류. 현재 `openai`(vLLM 직결)만 지원 — VSS 경유 백엔드는 제거됨 | `vlm_client/core.py` |
| `MINIO_ENDPOINT` / `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | minIO 접속 정보(필수 3종) | `storage/minio_adapter.py` |
| `MINIO_SECURE` | minIO TLS 사용 여부(기본 `false`) | `storage/minio_adapter.py` |
| `MINIO_REGION` | minIO 리전(기본 `us-east-1`) | `storage/minio_adapter.py` |
| `MINIO_BUCKET` | `버킷/키` 형태 입력을 `s3://`로 정규화할 때 기본 버킷 | `storage/normalize.py` |
| `TTSUM_REMOTE_HOST` | 원격 GPU 서버 접속 대상(`user@host`) — SSH 기반 잡 제출과 재연 드라이버가 사용 | `automation/replay_fidelity.py`, `automation/perturb_eval.py`, `automation/window.py` |
| `TTSUM_REMOTE_EXT_ROOT` | 원격 서버에서 이 확장이 위치한 루트 경로 | 위와 동일 |
| `TTSUM_DATASET_DIR` | 학습 데이터셋 디렉터리 기본값(GUI 잡 패널) | `automation/window.py` |
| `TTSUM_MERGED_MODEL` | LoRA 병합 모델 경로 기본값(GUI 잡 패널) | `automation/window.py` |
| `TTSUM_HOME` | GPU 서버 스크립트의 홈 디렉터리(기본 `$HOME/ttsum`) — 모델 체크포인트, venv, 데이터셋이 이 아래에 위치 | `VLM_server/README.md`, `VLM_server/l40/run_api.sh`, `VLM_server/run_qwen3-vl-8b.sh` |
| `SERVE_GPU` | vLLM 서빙 전용 GPU 번호(기본 0). `generate`/`train` 잡이 이 GPU를 지정하면 거부됨 | `VLM_server/l40/job_api.py` |
| `JOB_API_KEY` | 설정 시 잡 API에 `X-API-Key` 헤더 인증을 추가 요구(심층 방어, 선택) | `VLM_server/l40/job_api.py` |
| `JOB_STORE_URL` | 잡 스토어 백엔드 URL(기본 SQLite, Postgres 선택 가능) | `VLM_server/l40/job_api.py`, `job_store.py` |
| `USE_CONTAINER` | `1`이면 생성/재연 잡을 Docker 컨테이너로 격리 실행 | `VLM_server/l40/run_job.sh`, `run_replay.sh` |
| `KIT_CONTAINER_IMAGE` | 컨테이너 격리 시 사용할 Kit 이미지 | `VLM_server/l40/*.sh` |
| `TTS_TICK_MIN_S` | 재생 갱신 게이트(초). 누적 재생 시간이 이 값 미만이면 그 프레임은 좌표 조회를 건너뜀(기본 `0.1`) | `playback/controller.py` |
| `TTS_DESPAWN_GAP_S` | 설정 시 결손 인지 despawn(트랙 범위 안이라도 결손이 길면 숨김)을 켬 | `app/facade.py` |

### 5-2. 설정 파일

- **`config.json`** — 데이터 경로, 자동 생성 옵션, 시각 복잡도 그룹, 데이터 레이크
  활성화 여부 등을 담는다. `${VAR}` 형태로 환경 변수를 참조할 수 있고, 같은 디렉터리의
  `.env` 파일이 있으면 JSON 파싱 전에 먼저 로드된다. 샘플은
  [`config.example.json`](gist/netai/time_travel_summarization/config.example.json) 참조.
- **`scene_profiles.json`** — 생성/재연 잡이 궤적 데이터 로드 없이도 씬을 결정할 수
  있도록, 아레나(배회 영역) 좌표 범위와 스테이지, 카메라를 이름으로 등록해 두는
  레지스트리다. `automation/scene_profiles.py`가 로더다.

레포 안의 예시 값(Nucleus 경로, 버킷 이름 등)은 GIST NetAI Lab의 사설 인프라 주소다.
자신의 환경에서 실행하려면 이 경로들을 각자의 Nucleus 서버, minIO 엔드포인트, 버킷
이름으로 교체해야 한다.

---

## 6. 실험 결과

이 저장소의 측정 파이프라인은 두 가지를 묻는다. 좌표 수집 장비가 만들어내는 전형적인
오류(위치 노이즈, 낮은 수집 주기, ID 오배정, 가림)가 재연 경로에 섞여도 VLM 판독이
실제로 벌어진 충돌을 되찾아내는가라는 **좌표 교란 강건성**, 그리고 모델이 "충돌이
일어났다"고 말할 때 화면의 어떤 국면(접근, 접촉, 사후)과 어떤 거리를 근거로 그렇게
말하는가라는 **충돌 단서의 위상분해**다. 같은 오염된 좌표를 좌표 직결 규칙(룰)과
비교해, 영상을 거치는 것이 언제 이득이고 언제 손해인지도 함께 잰다.

측정 규약(채점 기준, 표본 구성, 검정 방법)과 확정된 수치, 판정은 규약 세대가 바뀔
때마다 갱신되므로 이 문서에는 옮기지 않는다 — 현행 결과는
[`experiments/README.md`](experiments/README.md)에서 관리한다.

---

## 7. 설치 및 요구사항

이 프로젝트는 세 가지 실행 환경이 맞물려 동작한다.

1. **Omniverse Kit SDK** — 이 확장 자체는 [`kit-app-template`](https://github.com/NVIDIA-Omniverse/kit-app-template)로
   빌드한 USD Composer(또는 다른 Kit 기반 앱) 위에서 로드된다. kit-app-template
   저장소의 빌드 절차를 먼저 따라야 한다.
2. **GPU 서버(vLLM)** — VLM 추론은 별도 GPU 서버에서 vLLM이 OpenAI 호환 API로
   서빙한다. 측정 파이프라인(생성/재연/학습) 잡도 같은 서버에서
   `VLM_server/l40/job_api.py`(FastAPI)가 큐를 관리하며 실행된다.
3. **minIO** — 궤적 데이터셋과 캡처 영상은 minIO(S3 호환) 버킷에 적재해 데이터
   레이크로 재생한다. `storage/minio_adapter.py`가 `s3://` URI를 이 minIO에 매핑한다.

`config/extension.toml`의 `[python.pipapi]`가 `minio`, `pyarrow`, `imageio`,
`imageio-ffmpeg`를 확장 기동 시 런타임 패키지로 설치한다 — 데이터 레이크 연동과 캡처
인코딩(시스템 ffmpeg가 없는 머신 대응)에 필요하다.

---

## 8. 출처

이 확장은 [NVIDIA `kit-app-template`](https://github.com/NVIDIA-Omniverse/kit-app-template)의
확장 템플릿으로 스캐폴딩했다. `source/extensions/` 아래에 이 확장
(`gist.netai.time_travel_summarization`)을 추가하는 형태로 구성되어 있다.

---

## 9. 프로젝트 규약

- **문서는 README만 git으로 추적한다.** 실험 계획서, 작업 일지 같은 진행 중 문서는
  로컬 파일로만 관리하며 `docs/` 아래에 있다.
- **규약 세대가 다른 실험 수치는 섞어 비교하지 않는다.** 접촉거리, 탄성, trace 좌표
  정의 같은 데이터 생성 규약이 바뀌면 학습에 쓰인 데이터와 새로 재는 데이터의 분포가
  어긋나므로(train ≠ infer), 규약이 바뀔 때마다 재생성, 재학습, 재측정을 함께
  진행한다. 현행 공표값은 [`experiments/README.md`](experiments/README.md)가 가리키는
  최신 세대 리포트다.
- **정적 품질 게이트** — `ruff check gist/`(린트), `mypy`(신규, 순수 모듈부터 점진
  도입), `pytest -q`(단위 테스트)를 push/PR마다 CI(`.github/workflows/ci.yml`)로
  강제한다. 규칙 근거는 [`pyproject.toml`](pyproject.toml) 인라인 주석을 참조.
