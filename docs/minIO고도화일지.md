# minIO 고도화 일지

데이터 레이크 축적 → L40 원격 학습 → 플랫폼 게이트로 이어지는 파이프라인 개선 기록.
문제별로 "기존 구현 → 원인 → 해결(경로·로직)" 순으로 정리.
(2026-05의 데이터 레이크 초기 연동은 `WORKLOG_2026-05_datalake.md`, physics 측 생성 품질은 `physics고도화일지.md` 참조.)

---

## #1. 에피소드 자동 업로드 + run manifest — 체계적 축적 (2026-07-07)

### 문제 정의
headless 배치가 만든 에피소드(video+meta+collisions CSV+trace)가 로컬 `artifacts/episodes/`에만 남아,
학습 서버(L40)가 쓰려면 수동 복사가 필요했고 "이 데이터가 어떤 코드·시드로 만들어졌는지" 추적이 안 됐음.

### 해결 (`automation/generate_episodes.py`)
- `--upload-uri s3://…/episodes/<run>` 옵션 + `upload_episode()`: 에피소드 완료 즉시 minIO에 업로드
  (storage adapter 재사용, 부분 실패는 per-episode `ok` 플래그로 기록하고 배치는 계속).
- `write_run_manifest()`: run 루트에 `_run_manifest.json` — **git commit 해시, 마스터 시드,
  에피소드별 시드·시각·ok 플래그** 기록. "raw는 불변, 계보는 manifest"가 축적 규약.
- 본 생산 실적: `episodes/prod-20260707`(75ep×30s), `prod-20260708`(150ep), `prod-20260709`(225ep).

---

## #2. L40 서버 환경 체계화 + 스테이징 (2026-07-07~08)

### 문제 정의
L40(공용 GPU 서버)에서 repo를 clone해 학습하려는데, 환경 구축 절차·비밀 관리·minIO→로컬 반입이 비정형이었음.

### 해결 (`VLM_server/l40/` 신설)
- `SETUP.md` + `requirements.txt`(minio/fastapi/uvicorn) + `env.example` — 재현 가능한 셋업 문서화.
- **비밀은 서버 측 `~/wonjune/.env.l40`에만** 두고 스크립트가 `set -a; source`로 로드
  (GUI·repo에 자격증명 비노출).
- `stage_episodes.py`: minIO → 로컬 디스크 반입(스테이징). **중단 후 재개 가능**(기존 파일 스킵),
  처리율 로그 출력. 학습 I/O를 오브젝트 스토리지에서 분리하는 표준 패턴.
- `run_smoke.sh`/`run_job.sh`: 데이터 생성 러너. `EXT_ROOT`/`KIT_ROOT` 분리(독립 clone 배치 지원),
  status 파일은 **tmp 쓰기 후 rename(원자적)** — 외부(GUI/API)가 읽는 계약이므로 찢어진 읽기 방지.

---

## #3. build_dataset 크로스플랫폼 버그 2건 (2026-07-08, 6cb4231·dd80d83)

| 문제 | 원인 | 해결 (`utils/build_dataset.py`) |
|---|---|---|
| L40(Linux) 빌드에서 전 클립이 negative 라벨 | meta의 충돌 CSV 경로가 Windows 절대경로(백슬래시) → `Path.name`이 통짜 문자열 반환 → CSV를 못 찾고 조용히 "충돌 없음" 처리 | 구분자 정규화 폴백: `csv_rel.replace("\\", "/").rsplit("/", 1)[-1]` |
| 다중 run 합본 빌드 시 에피소드 ID 충돌 | run마다 `ep001…`이 반복되어 합본에서 라벨이 뒤섞임 | `derive_episode_id(meta_path, episodes_dir)` — episodes 루트 기준 상대경로(=run 접두어 포함)로 ID 생성 |

교훈: 생성(Windows)과 소비(Linux)가 다른 OS인 파이프라인은 **경로·구분자를 데이터로 취급**해 정규화해야 함.
조용한 실패(전부 negative)가 눈에 띈 건 클립 수/positive 비율을 사람이 점검했기 때문 → 빌드 요약 로그 유지.

---

## #4. 원격 생성 제어면 — JobSpec + transport + GUI (2026-07-08, b1778f1·dd80d83)

### 문제 정의
L40에 데이터 생성을 시키려면 사람이 SSH로 들어가 러너를 손으로 실행해야 했음. extension GUI에서 제출하도록 제어면 구축.

### 해결
- `automation/remote_generation.py`: **JobSpec**(씬·카메라·에피소드 수·duration·객체 수·시드 등 잡 한 벌의 계약).
  같은 스펙이 두 형태로 소비됨 — 셸 실행용 **환경변수 렌더링**(`to_env()`)과 REST용 **JSON 직렬화**.
  전송은 `LocalTransport`/`SSHTransport`(BatchMode, tmux new-session -d로 원격 상주 실행) 추상화 —
  이후 REST로 갈아끼울 수 있는 구조(#5에서 실제로 그렇게 됨).
- `ui/remote_gen_panel.py`: 독립 "Data Generation" 창(메인 창과 분리, 자체 dispatcher).
  Host/Stage/Camera/GPU/Episodes/Duration/Objects/Seed 입력 + 업로드 체크박스.
  **시드는 제출마다 시간 기반 재생성**(`int(time.time())%2e9`) — 같은 시드 재사용으로 인한
  "동일 데이터 중복 축적" 함정 방지(시드가 같으면 위치·방향·시각이 전부 동일).
- `extension.py`: 창 생성/해제 배선.

---

## #5. REST 잡 API + GPU별 순차 큐 + 하이브리드 보안 (2026-07-09, 0cb9941)

### 문제 정의
SSH transport는 "명령을 던질" 뿐 **큐가 없음** — 두 번 제출하면 kit 두 개가 같은 GPU에 떠서
경합(run16 행 사고와 동일 시나리오)이 남. 또한 상태 조회가 "ssh로 status 파일 cat"이라 클라이언트마다 제각각.

### 해결 (`VLM_server/l40/job_api.py` + `run_api.sh`)
- **FastAPI 잡 서버**: `POST /jobs`(202 + 잡 ID 발급) / `GET /jobs`·`/jobs/{id}`(status 파일 반영) /
  `/jobs/{id}/log` / `/health`. 잡 ID는 `_JOB_ID_RE`로 검증(경로 탈출 방지).
- **GPU별 순차 큐**: GPU마다 `threading.Queue` + 워커 스레드 1개 — 같은 GPU에는 잡이 절대 동시 실행되지 않음.
  경합 방지를 규칙(사람 주의)이 아니라 구조로 보장.
- **하이브리드 보안**: uvicorn을 `127.0.0.1:8800`에만 바인딩 + 클라이언트는 SSH 터널(`ssh -L 8800:localhost:8800`)로 접속.
  네트워크 노출 0 + 선택적 `JOB_API_KEY`. VSCode Remote-SSH 사용 시 포트포워딩이 사실상 자동.
- extension 측 `RESTTransport`(urllib, `submit_spec`/`job_status`) 추가 — transport 추상화 덕에 GUI 코드 무변경.

### 아키텍처 정리 (설계 논의 결론, 2026-07-10)
```
[extension GUI] --REST--> [job_api (상주, 접수 계층)] --spawn--> [kit (잡마다 생성/소멸, 실행 계층)]
```
- **접수는 상주, 실행은 일회용** — CI 서버/러너와 같은 표준 패턴. kit을 잡마다 죽이는 이유는
  ① GPU 메모리 반환 ② 상태 오염 격리(타임라인 잠금처럼 프로세스 내 비가역 오염 이력) ③ 시드 재현성의 전제(깨끗한 초기 상태).
- 완료 감지는 두 축을 분리: **작업 완료**(로그 마커 → status 파일 → sentinel 파일로 진화 중, 외부 계약) vs
  **프로세스 종료**(force-exit 15s + 워치독 PID 정리, kit 잔류 대응). sentinel은 전자만 해결하며 후자를 대체하지 않음.
- 상주 kit 워커(씬 로드 3분 상각)는 대화형(agentic) 워크로드 등장 시의 진화 항목 — 그때도 REST 계약은 불변,
  워커 내부(spawn → 전달)만 바뀜.

---

## #6. vlm_client를 vLLM OpenAI 호환 직접 호출로 전환 (2026-07-09, 6298cdb)

### 문제 정의
추론 경로가 VSS(NVIDIA Video Search & Summarization)를 경유했는데, 요약·검색은 자체 구현 예정이라
VSS는 사실상 "영상을 VLM에 전달하는 통로"로만 쓰이고 있었음 → 무거운 의존을 제거하기로 결정.

### 해결
- `utils/vllm_client.py` 신설: ffmpeg로 영상 길이 파악(`parse_duration_s`) → **2초 청크 분할**(`chunk_spans`,
  build_dataset의 slice_clip과 동일 인코딩 파라미터 = **학습·추론 전처리 정합**) → base64 mp4를
  vLLM의 `/v1/chat/completions`(OpenAI 호환)에 직접 POST. 부분 실패는 청크별 error 필드로 보존.
- `vlm_client/core.py`: `VLM_API=openai`(기본, `VLM_BASE_URL` 기본 `http://localhost:38011`) / `vss`(레거시) 분기.
  공개 API 불변 — GUI·상위 로직 무변경.
- 서빙 측 필수 플래그: `--media-io-kwargs '{"video": {"num_frames": 20}}'` — 학습 시 프레임 예산(NFRAMES=20)과
  일치시켜야 train==infer 정합이 성립.

---

## #7. LoRA 데이터 스케일링 실증 — v1 → v2 (2026-07-08~09)

### 설계
- 베이스(Qwen3-VL-8B) 가중치는 동결, LoRA(rank 16/alpha 32, freeze_vit, bf16, grad accum 16)만 학습 →
  base/v1/v2를 **같은 test에서** 3자 비교 가능.
- 지표: **STRICT**(타임스탬프+객체 ID 집합 완전 일치 F1) / **RELAXED**(충돌 유무 이진 F1) / Acc.
- 테스트는 자연 분포(50:50 인위 균형 없음), temperature=0 결정적 출력.

### 결과 (v2 test: 330클립, positive 67 — `docs/compare_outputs/`)
| | base | lora_v1 (pos 136) | lora_v2 (pos ~450) |
|---|---|---|---|
| STRICT F1 | 0.065 | 0.250 | **0.503** |
| RELAXED F1 | 0.338 | 0.548 | **0.784** |
| Acc | 0.203 | 0.800 | **0.903** |

- base는 "전부 충돌" 퇴화(tn=0) — 미세조정 없이는 과제 수행 불가 확인.
- positive 3.3배 증량 → STRICT 2배, fn 27→9. **데이터 증량이 곧 성능**임을 2점으로 실증,
  3점째(v3: prod-20260709 추가 합본, ~900 pos)로 스케일링 커브 확인 진행 중.

### 학습 환경 함정 (L40, ms-swift 3.x)
- `USE_HF=1` 필수(기본 ModelScope 소스라 모델 재다운로드), `HF_HUB_OFFLINE`은 캐시 불완전 시
  FileNotFoundError — 끄고 복구 다운로드가 정답.
- 학습·평가 러너: `training/` (v2 재현 명령 포함).

---

## #8. 정적 게이트 도입 — ruff + mypy + pytest + CI (2026-07-10)

### 목적
플랫폼화의 전제인 "코드가 항상 실행 가능한 상태"를 사람 눈이 아니라 **자동 게이트**로 보장.
(J1 production-readiness 항목. GPU 불필요 작업이라 v3 생산과 병행 진행.)

### 구성
- `pyproject.toml`(신규): `[tool.ruff]` line-length 120, py310, E731/E402 무시(콜백 람다·Kit 지연 import 관례).
  `[tool.mypy]` **6개 신규 모듈만 대상**(remote_generation, vllm_client, build_dataset, types, frame_queue, timefmt),
  follow_imports=silent. `[tool.pytest.ini_options]` `kit` 마커 — Kit 런타임 필요 테스트를 CI에서 제외.
- `.github/workflows/ci.yml`(신규): push/PR마다 ruff check → mypy → `pytest -m "not kit"` + 순수 헬퍼 self-test 3종.
- 결과: ruff clean, mypy clean, pytest 85 passed / 3 skipped.

### mypy를 전 파일에 적용하지 않은 이유 (점진 적용, gradual typing)
- 실측: follow_imports로 훑기만 해도 **22개 파일 76건**. 게이트는 항상 그린이어야 신뢰되는데
  (수 주간 빨간 게이트 = 무시 습관), 일괄 적용은 수 주짜리 대수술.
- **Kit 관례와의 충돌 실예시**: `extension.py`의 shutdown 관례 —
  `self._window = TimeTravelWindow(...)`로 만들고 `on_shutdown`에서 `self._window = None`으로 해제.
  mypy 오류 "Incompatible types in assignment (expression has type None, …)" (extension.py:170/178/182/210 실측).
  고치려면 전 속성을 `Optional[X]`로 재선언해야 하는데 Kit 확장 코드 전반의 관례라
  **facade 분해 리팩터링 때 편입**하는 전략. 신규 코드는 처음부터 타입 대상에 넣어 대상을 점차 확대.

### 게이트가 첫 실행에서 잡은 실버그 3건 (도입 효과의 즉시 증명)
1. **F821**: `Optional` 미임포트 (ruff)
2. **Optional[float] 산술 연산** (mypy — None 가능 값을 그대로 계산)
3. **`storage/minio_adapter.py list_prefix` 폴더 누출** (pytest 최초 실행으로 발견, 실서버 검증):
   minio SDK가 비재귀 목록에서 하위 '폴더'를 dir 항목(size=**None**)으로 반환하는데
   기존 가드가 `size == 0` 비교라 `None == 0`이 False → **폴더가 파일처럼 새어 나옴**.
   `is_dir`/`endswith("/")` 검사로 교체.

### 부수 정리
- `utils/VSS_client.py` 등 IDE 잔재 import(`from unittest import result`) 제거, 사용처 없는 변수 정리.
- `tests/test_vlm_lake_upload.py`: #6의 기본 API 전환(openai)에 맞춰 레거시 VSS 경로 테스트에 `core._api = "vss"` 명시.

---

## #9. 프레임워크 구조 고도화 — facade 분해·잡 타입·GUI 정리 (2026-07-10~12)

RAG/agent 직전 단계까지의 받침 구축. 방향 합의: 추론은 큐 미경유(vLLM 자체 배칭),
서빙과 잡은 GPU 역할 분리.

### (1) facade 분해 — 1,235줄 → 코디네이터 + 서비스 5모듈
`app/facade.py`를 얇은 코디네이터(공개 API 불변)로 두고 동작을 분리:
`capture_service`(캡처·사이드카) / `physics_service`(모드 전환·recorder) /
`data_service`(config·local/lake 활성화) / `object_service`(우주인 생성·활성 선택) /
`benchmark_service`(lookup 벤치마크). **분해 원칙: 상태는 core가 소유, 서비스는
동작만**(테스트가 `__new__`+속성 주입으로 core를 구성하고 extension.py가 `_wander`를
직접 만지므로). 신규 6모듈을 mypy 대상에 편입(6→12파일) — 편입 즉시 기존 코드의
타입 결함 2건(TraceRecorder str/Path, repo_factory 타입) 표면화·수정.

### (2) L40 잡 타입 확장 + GPU 역할 분리
- `JobSpec.job_type`: generate | train | serve_start | serve_stop, 타입별 러너 디스패치
  (`runner_rel_for`). `run_train.sh`(qwen3vl_lora_swift.sh 래핑 — 하이퍼파라미터 단일
  소스, USE_HF=1·venv·status 계약) / `run_serve.sh`(vLLM 127.0.0.1 상주 기동+준비 확인,
  `--served-model-name` 고정으로 GUI 모델명과 정합, 멱등 stop) 신설.
- **GPU 역할 분리**: env `SERVE_GPU`(기본 0)는 서빙 전용 — serve 잡은 강제 배정,
  generate/train이 지정하면 422 거부. 상주(서빙)와 유한 잡의 경합을 큐가 아니라
  역할로 차단. 리뷰 반영: 다른 모델 서빙 중 start는 명시 실패("stop 먼저").

### (3) GUI 정리 — 창 구성 평가에 따른 중복 제거
- **캡처 진입점 3→1**: VLM 창의 A1/A2 버튼 제거(realtime_capture 검증기 잔재 —
  라벨·사이드카 없는 캡처가 섞이는 위험). 메인 Capture 완료 시 VLM 창 Source
  **자동 채움**(`set_capture_complete_callback`)으로 수동 URI 복붙 대체.
- VSS 잔재 정리: 모델 콤보 5종→Qwen3-VL 단일(vLLM model 필드와 일치 필요),
  overlap 노브 제거. "Data Generation" 창 → "Remote Jobs"(Training/Serving 섹션 상설).
- `ui/remote_gen_panel.py` → `automation/window.py` 이동 — "도메인 패키지에 창을
  같이 두는" 지배 규칙으로 통일(ui/에는 공용만: main_window, task_dispatcher).

### (4) 이벤트 인덱스 v1 — 추론 결과의 검색 표면
`event_processing/event_index.py`: 추론 1회 = `vlm_events/<영상>.jsonl` 1개(이벤트
1건=1행). minIO append 부재 → 영상별 오브젝트로 동시 쓰기 경합 원천 차단, 재추론 =
덮어쓰기(최신이 진실). vlm_client 저장 경로에 best-effort 배선. 독립 리뷰(MAJOR)
반영: minIO 비재귀 목록의 폴더 의미 차이로 조회가 빈 결과가 되는 버그 —
후행 슬래시+recursive=True로 수정.

---

## #10. 시각 의미론 정리 — twin time·사이드카 앵커·공백 점프·이벤트 검색 (2026-07-12)

사용자 흐름 확정("range 로드 → 공백 건너뛰며 재연 → range 내 이벤트 목록 → 선택
시 재구축")에서 드러난 시각 체계 결함을 일괄 정리. 용어 확정: **twin time** =
디지털 트윈 세계의 현재 시각(USD 타임라인 stage time과 구분).

| 문제 | 원인 | 해결 (경로) |
|---|---|---|
| playback 캡처의 시각 앵커 오염 | 사이드카 capture_start가 벽시계 — 영상 픽셀(데이터 시각)과 불일치 → 이벤트가 캡처한 날짜에 붙음 | 앵커 = 그 모드의 내부 시계(`capture_anchor()`: playback이면 재생 헤드의 데이터 시각), 벽시계는 `wall_clock` 필드로 분리, 재연 창(`replay_start/end`) 기록 (`app/capture_service.py`) |
| 레이크 캡처는 사이드카 자체가 없음 | 원격 URI면 skip | storage adapter로 s3에도 영상 옆 기록 |
| 인덱스가 HH:MM:SS뿐 → 다일 조회 불성립 | VLM은 오버레이 시계만 보고(날짜 없음) | 적재 시 사이드카 앵커와 결합해 절대 datetime 저장, 자정 롤오버(+1일), `query_events`가 datetime 구간 조회 (`event_index.py: resolve_event_datetime/sidecar_anchor`) |
| 데이터 공백을 시계가 실시간으로 기어감 | 재생 구조 = 시계가 주인, 데이터는 조회 대상 → 공백에서 객체 정지+시계만 진행 | 진행 틱마다 다음 데이터까지 간격>임계값(기본 10s)이면 점프. 탐색은 메모리 인덱스 이진 탐색(타임스탬프 배열/manifest 청크 경계) — minIO 조회 없음 (`facade._maybe_skip_gap`, `trajectory_repository/lake_repository: next/prev_data_time`) |
| 추론→이벤트→재연이 3창 수동 릴레이 | 파일명을 사람이 기억·입력 | Event Post Processing 창에 **Event Search**: twin time 구간 검색 → 목록 → 선택 시 seek(범위 밖이면 ±5분 자동 로드) (`event_processing/window.py`) |

### 검증
- 유닛테스트 100 passed(+10: 롤오버·다일 구분·앵커 미상 제외·공백 점프 전후진·
  임계값 0·사이드카 파싱), ruff/mypy 클린. self-test 2종(remote_generation,
  event_index) 통과.
- **Kit 실기 미검증**: Event Search UI 렌더·선택 seek, playback 캡처 사이드카
  실측, 공백 점프 체감 — 다음 GUI 세션에서 확인 필요.
