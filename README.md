# Time Travel Summarization

> 디지털 트윈과 시각 언어 모델(VLM)을 연계해 **사건 중심으로 과거 시공간을 재구성**하는 NVIDIA Omniverse Kit 익스텐션.

GIST NetAI Lab의 연구 결과물 레포지토리이다.

---

## 연구 요약

- **목표** — 관리자가 긴 로그 영상을 수동으로 훑지 않아도 사건 발생 시점과 위치 중심으로 디지털 트윈을 즉시 재구성할 수 있도록 한다.
- **접근** — 디지털 트윈(시공간 재현) ↔ Omniverse Extension(재구성 로직) ↔ VLM(이벤트 탐지) 세 모듈을 인터페이스 기반으로 연결한 폐루프 프레임워크.
- **검증 결과(초기 v1 규약 실험 기준)** — 시각적 추상화 환경에서 Qwen3-VL-8B의 F1 약 0.2 개선, 3배속 가속 시 추론 시간 50% 이상 단축(최대 12시간 로그까지 적용 가능 확인). 이후 규약을 두 차례 고쳐 재측정했으므로(regime2, regime3) 이 수치는 초기 실험의 것이며 현행 공표값이 아니다.

프로젝트는 이 폐루프 위에서 **파이프라인 강건성의 측정**으로 범위를 넓혔다. 좌표 궤적에 현실적인 오류(측위 오차, ID 스왑, 가림, 다운샘플링 등)를 주입하고, 그 교란된 좌표로 디지털 트윈을 재연해 영상을 뽑은 뒤, VLM이 그 영상에서 여전히 충돌 이벤트를 검출하는지를 정량으로 측정한다. 이 측정 축을 지원하기 위해 데이터 레이크(minIO) 연동, 물리 기반 에피소드 대량 생성, 좌표 교란기, VLM 학습(LoRA) 파이프라인이 추가됐다. 현재 진행 중인 측정 국면과 확정 수치는 로컬 문서(`docs/` — git 비추적)에서 관리한다.

---

## 시스템 구성

```
 ┌─────────────────────┐    overlay+playback    ┌──────────────────┐
 │  Digital Twin (USD) │ ─────────────────────▶ │ Omniverse        │
 │  + trajectory CSV   │                        │ Extension (this) │
 └─────────────────────┘  ◀──── event feedback ─└────────┬─────────┘
                                                         │ video chunks
                                                         ▼
                                                ┌──────────────────┐
                                                │ VLM Server       │
                                                │ (vLLM, OpenAI    │
                                                │  호환 직결)       │
                                                └──────────────────┘
```

측정 국면에서는 이 루프 앞에 좌표 교란 단계가 하나 더 붙는다: 원본 궤적을 `perturbation/`으로 교란한 뒤 같은 루프(재연 → 영상 → VLM)를 다시 태워, 교란 유무에 따른 검출 성능 차이를 비교한다.

---

## 출처

이 익스텐션은 [NVIDIA `kit-app-template`](https://github.com/NVIDIA-Omniverse/kit-app-template)의 확장 템플릿으로 스캐폴딩했다. `source/extensions/` 아래에 이 확장(`gist.netai.time_travel_summarization`)을 추가하는 형태로 구성되어 있다.

---

## 디렉토리 구조

```
gist/netai/time_travel_summarization/
├── extension.py                # Kit 진입점
├── config.json                 # 사용자 환경 설정 (env 치환 지원)
├── config.example.json         # 설정 샘플
├── Readme.md                   # 사용자 워크플로 가이드 (정본)
├── scene_profiles.json         # 씬 프로파일 레지스트리(생성 잡의 아레나, 스테이지, 카메라)
│
├── app/                        # 익스텐션 컴포지션
│   ├── facade.py               # TimeTravelCore (외부 API, 상태는 여기 단일 소유)
│   ├── config.py                # ExtensionConfig
│   └── paths.py                 # ExtensionPaths (artifacts 정책)
│
├── playback/                   # Time Travel 재생 도메인
│   ├── controller.py
│   ├── trajectory_repository.py
│   └── stage_object_controller.py
│
├── overlay/                    # Viewport overlay (VLM에 시간과 객체 ID를 보여주는 시각 프롬프팅)
│   ├── core.py
│   ├── components.py
│   └── window.py
│
├── vlm_client/                 # VLM 서버 통신 (vLLM OpenAI 호환 직결, 2초 청크 단위 요청)
│   ├── core.py
│   └── window.py
│
├── events/                     # VLM 결과 후처리 — 파싱, 이벤트 인덱스, 요약 재생
│   ├── core.py                  # VLM output JSON → 표준 이벤트 스키마 변환
│   ├── event_index.py           # 시간창 기반 이벤트 검색 인덱스 (RAG/agent 대비)
│   ├── summary_service.py
│   └── window.py
│
├── physics/                    # 콜라이더 프록시, 객체 배회(wander) 안무, trace 기록
│   ├── collision_proxy.py       # 시각 메시와 정합된 물리 콜라이더 부착/해제
│   ├── wander_controller.py     # 객체 자율 이동(배회) 컨트롤러
│   ├── trace_recorder.py        # 월드 좌표를 궤적 CSV로 스트리밍 기록(collider-trace-v1)
│   ├── collision_recorder.py    # 물리 충돌 이벤트를 GT 라벨로 기록
│   └── physics_scene.py, walls.py
│
├── perturbation/                # 좌표 트랙 교란기 — 강건성 측정용 오류 주입
│   └── perturb.py               # gaussian / id_switch / fragmentation / occlusion / downsample
│
├── storage/                     # 로컬/minIO 스토리지 어댑터 (s3://, file:// 통일 인터페이스)
│   ├── base.py, factory.py
│   ├── local_adapter.py, minio_adapter.py
│   └── normalize.py             # 사용자 입력 경로 정규화(버킷/키 → s3:// 등)
│
├── video_capture/                # 오프스크린/실시간 캡처, 인코더, 오버레이 합성
│   ├── movie_capture.py, realtime_capture.py
│   ├── encoder.py                 # FrameEncoder (imageio/ffmpeg 백엔드)
│   └── overlay_composer.py        # 캡처 경로 전용 오버레이 마커 합성
│
├── automation/                    # 측정 파이프라인 CLI 일체 (생성/교란/재연/채점/제출)
│   ├── generate_episodes.py       # headless 배치 에피소드 생성 (kit --exec)
│   ├── replay_range.py            # 좌표 구간 headless 재연 렌더 (kit --exec)
│   ├── perturb_eval.py            # 교란 조건 일괄 측정 드라이버
│   ├── replay_fidelity.py         # 원본 vs 재연 페어 비교 드라이버
│   ├── phase_clips.py             # 위상 분해 조건별 클립 추출기
│   ├── phase_scoring.py           # 위상 분해 조건별 VLM 채점 드라이버
│   ├── rule_baseline.py           # 좌표 직결 룰 베이스라인 검출기
│   ├── overlay_flags.py           # 화면 겹침(overlay_overlap) 픽셀 판정기
│   ├── remote_generation.py       # 잡 스펙/전송 분리 원격/로컬 잡 제출기
│   ├── scene_profiles.py          # 씬 프로파일 로더
│   └── window.py                  # 원격 잡 패널 GUI
│
├── training/                     # Qwen3-VL-8B LoRA 파인튜닝 (ms-swift)
│   ├── qwen3vl_lora_swift.sh       # 실행 소스 오브 트루스
│   ├── qwen3vl_lora_swift.yaml     # 같은 설정의 선언적 기록본
│   └── METHODOLOGY.md, HF_MODEL_CARD.md, VERIFICATION_CHECKLIST.md
│
├── assets/                        # Astronaut.usd 등 씬 자산
├── ui/                            # UI 인프라
│   ├── main_window.py
│   └── task_dispatcher.py         # main thread dispatcher
│
├── utils/                         # 운영 스크립트 (런타임 비의존)
├── tests/                         # 단위 테스트 + 측정 도구(lake_benchmark.py 등)
├── data/                          # 입력 trajectory CSV
├── VLM_server/                    # VLM 서버 운영 가이드 + L40 GPU 서버 잡 API/vLLM 서빙
│   └── l40/                        # job_api.py(FastAPI 잡 제어면), run_*.sh 러너
└── artifacts/                     # 런타임 산출물 (gitignored)
    ├── video/
    ├── vlm_outputs/
    ├── intermediate_results/
    └── event_list/

tools/                              # 레포 루트 CLI (확장 밖, 데이터 준비 도구)
├── generate_living_trajectory.py    # 거실 좌표 범위 궤적 CSV 생성
├── generate_overlay_calibration_grid.py  # 실시간 오버레이 투영 보정용 5x5 격자 CSV
└── lake_ingest.py                   # 합성/CSV 궤적 → 시간 분할 청크 → minIO(또는 file://) 적재
```

---

## 설치 및 요구사항

이 프로젝트는 세 가지 실행 환경이 맞물려 동작한다.

1. **Omniverse Kit SDK** — 이 확장 자체는 [`kit-app-template`](https://github.com/NVIDIA-Omniverse/kit-app-template)로 빌드한 USD Composer(또는 다른 Kit 기반 앱) 위에서 로드된다. kit-app-template 레포의 빌드 절차를 먼저 따라야 한다.
2. **GPU 서버(vLLM)** — VLM 추론은 별도 GPU 서버에서 vLLM이 OpenAI 호환 API로 서빙한다. 측정 파이프라인(생성/재연/학습) 잡도 같은 서버에서 `VLM_server/l40/job_api.py`(FastAPI)가 큐를 관리하며 실행된다. 서버 준비 절차는 [`gist/netai/time_travel_summarization/VLM_server/README.md`](gist/netai/time_travel_summarization/VLM_server/README.md)와 [`VLM_server/l40/SETUP.md`](gist/netai/time_travel_summarization/VLM_server/l40/SETUP.md)를 따른다.
3. **minIO** — 궤적 데이터셋과 캡처 영상은 minIO(S3 호환) 버킷에 적재해 데이터 레이크로 재생한다. `storage/minio_adapter.py`가 `s3://` URI를 이 minIO에 매핑한다.

레포 안의 예시 값(`omniverse://10.38.38.32/...` 같은 Nucleus 경로, `time-travel-summarization` 같은 버킷 이름)은 GIST NetAI Lab의 사설 인프라 주소다. 자신의 환경에서 실행하려면 이 경로들을 각자의 Nucleus 서버, minIO 엔드포인트, 버킷 이름으로 교체해야 한다.

---

## 빠른 시작

1. **VLM 서버 준비** — [`gist/netai/time_travel_summarization/VLM_server/README.md`](gist/netai/time_travel_summarization/VLM_server/README.md)
2. **환경 변수 설정**
   ```bash
   export VLM_BASE_URL="http://<vlm-server-host>:38011/v1"
   export ASTRONAUT_USD_PATH="omniverse://<your-nucleus-host>/.../Astronaut.usd"
   ```
3. **익스텐션 로드** — USD Composer → Extensions → Local Path 추가 후 활성화.
4. **사용자 워크플로** — [`gist/netai/time_travel_summarization/Readme.md`](gist/netai/time_travel_summarization/Readme.md) (궤적 데이터 생성 → Time Travel → View Overlay → 동영상 추출 → VLM 추론 → Event 기반 요약 재생까지 단계별).

---

## 환경 변수

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

설정값을 직접 `config.json`에 적어 두는 것도 가능하다. 샘플은 [`config.example.json`](gist/netai/time_travel_summarization/config.example.json) 참조.

---

## 측정 도구 실행 경로

`automation/`의 각 CLI는 `gist.netai.time_travel_summarization...` 형태의 절대 임포트를 쓰므로, 이 레포 루트(`gist/` 디렉터리가 보이는 곳)를 현재 디렉터리 또는 `PYTHONPATH`로 두고 `python -m` 형태로 실행한다. `generate_episodes.py`/`replay_range.py`는 예외로, Kit 런타임(omni.\*)이 필요해 `kit --exec`로 직접 실행하며 패키지 임포트를 쓰지 않는다.

| 모듈 | 실행 형태 | 설명(근거: 모듈 docstring 첫 문장) |
|------|-----------|-------------------------------------|
| `automation/generate_episodes.py` | `kit --exec generate_episodes.py -- <args>` (`automation/run_headless.sh` 참조) | headless 배치로 BEV 충돌 에피소드를 다수 생성 |
| `automation/replay_range.py` | `kit --exec replay_range.py -- <args>` | 좌표 구간을 headless로 재연 렌더 |
| `automation/perturb_eval.py` | `python -m gist.netai.time_travel_summarization.automation.perturb_eval --help` | 교란 조건 일괄 측정 드라이버(WP1 생성 + WP2 측정) |
| `automation/replay_fidelity.py` | `python -m gist.netai.time_travel_summarization.automation.replay_fidelity --help` | 원본 vs 재연 페어 비교 드라이버(clean 조건 겸용) |
| `automation/phase_clips.py` | `python -m gist.netai.time_travel_summarization.automation.phase_clips --help` | 위상 분해 조건 세트 추출기(국면별 클립 절제) |
| `automation/phase_scoring.py` | `python -m gist.netai.time_travel_summarization.automation.phase_scoring --help` | 위상 분해 조건 세트 채점 드라이버(클립 단위 VLM 추론) |
| `automation/rule_baseline.py` | `python -m gist.netai.time_travel_summarization.automation.rule_baseline --help` | 좌표 직결 룰 베이스라인 vs VLM 강건성 비교 |
| `automation/overlay_flags.py` | `python -m gist.netai.time_travel_summarization.automation.overlay_flags --help` | 렌더된 프레임의 화면 겹침(overlay_overlap) 픽셀 판정 |
| `automation/remote_generation.py` | `python -m gist.netai.time_travel_summarization.automation.remote_generation` | 원격/로컬 headless 데이터 생성 잡 제출기(자기 테스트 포함) |
| `automation/scene_profiles.py` | `python -m gist.netai.time_travel_summarization.automation.scene_profiles` | 씬 프로파일(아레나, 스테이지, 카메라) 이름 기반 로더 |
| `utils/build_dataset.py` | `python -m gist.netai.time_travel_summarization.utils.build_dataset --help` | 캡처된 BEV 에피소드로 Qwen3-VL LoRA 학습 데이터셋 구성 |
| `tests/lake_benchmark.py` | `python gist/netai/time_travel_summarization/tests/lake_benchmark.py --help` | 데이터레이크 윈도우 재생 성능 측정(합성 모드/실 데이터셋 모드). 파일명이 `test_*.py`가 아니라 pytest에 수집되지 않는 독립 CLI로, 레이크 성능 리포트의 측정 도구 |
| `tools/generate_living_trajectory.py` | `python tools/generate_living_trajectory.py --help` | 거실 좌표 범위 안에서 궤적 CSV 생성 |
| `tools/generate_overlay_calibration_grid.py` | `python tools/generate_overlay_calibration_grid.py --help` | 실시간 오버레이 투영 보정용 5x5 궤적 CSV 생성 |
| `tools/lake_ingest.py` | `python tools/lake_ingest.py --help` | 합성/CSV 궤적을 시간 분할 청크 + manifest로 minIO(또는 file://)에 적재 |

CI(`.github/workflows/ci.yml`)는 `ruff` 린트, `mypy`(점진 도입 대상 모듈), `pytest -m "not kit"`(Kit 런타임이 필요 없는 단위 테스트), 그리고 `generate_episodes.py --self-test`, `remote_generation.py`, `utils/vllm_client.py`의 순수 헬퍼 자기 테스트를 순서대로 실행한다.

---
