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

---

## #11. LoRA v3 완결 — 병합·vLLM Docker 서빙 (2026-07-14~18)

### 결과 (v3: prod-20260709 추가 합본, ~900 pos — 스케일링 커브 3점 완성)
| | base | v1 | v2 | **v3** |
|---|---|---|---|---|
| STRICT F1 | 0.065 | 0.250 | 0.503 | **0.699** |
| RELAXED F1 | 0.338 | 0.548 | 0.784 | **0.837** |

- 오차 해부: FN의 57.7%가 **정확히 1초 차이**(같은 클립·같은 객체) — ±1s 허용 시 STRICT 0.699→0.791.
  검출 능력은 학습됐고 초 경계 정렬이 지배 오차라는 뜻(향후 클립 위상 정렬 여지).
- 체크포인트: epoch1 `checkpoint-133`(eval_loss 최저 0.0759) 채택, epoch2는 경미한 과적합.

### 병합·서빙 (`VLM_server/l40/run_serve.sh` 전면 개정 — Docker)
- 병합: `USE_HF=1 swift export --merge_lora` — USE_HF 없으면 ModelScope에서 베이스를 재다운로드하는
  함정(#7과 동일 계보). 산출 = `.../checkpoint-133-merged`(17GB 자기완결).
- 서빙을 **vLLM 공식 이미지(vllm/vllm-openai) 컨테이너**로 전환: `--gpus device=$GPU`(역할 분리 유지),
  포트는 `127.0.0.1`에만 바인딩(SSH 터널 보안 모델), `--served-model-name` 고정(클라이언트 404 방지),
  `num_frames 20`(train==infer 정합), 멱등 start/stop(다른 모델 서빙 중이면 명시 실패).
- **KV 캐시 함정**: 병합본 기본 컨텍스트 262144는 KV 36.0GiB 필요 > 가용 23.72GiB → 기동 거부.
  `bytes/token = 2×L×H_kv×D_head×dtype = 144KiB`(Qwen3-8B) → `MAX_MODEL_LEN=32768`(KV 4.5GiB)로 확정.
  계산 공식·실측표는 `docs/vllm_serving.md`.

## #12. 원격 잡 제출 무음 실패 — 3중 원인 해부 (2026-07-18, 005f551·1e46c2c)

### 증상
GUI serve/generate 제출 → "submitted" 표시, 그러나 L40 무반응: GPU 변화 없음, job dir·status·log **흔적 0**.

### 원인 체인 (셋 다 걸려 있었음)
1. **틸드 인용 버그** (`automation/remote_generation.py`): 러너 경로가 `~/...`면 shlex.quote가
   통째로 홑따옴표로 감싸 틸드 확장이 막힘 → 원격에서 `bash '~/.../run_serve.sh'` = 리터럴 틸드
   파일 없음 → **러너가 아예 실행 안 됨**. tmux는 세션 생성만으로 rc 0 → GUI는 submitted로 오판.
2. **조기 실패 무음** (`VLM_server/l40/run_job.sh`): status/log 파일 생성이 kit/앱 해석 **뒤**라,
   해석 단계에서 죽으면 에러가 tmux 세션과 함께 증발.
3. **APP_KIT 미지정**: L40 apps에 .kit이 6개라 자동 발견 불가(`ERROR: APP_KIT 지정 필요`) —
   GUI에 입력 칸이 없어 항상 빈 값.

### 해결
- `_tilde_safe()`: `~/` → `$HOME/`+quote (원격 sh -c가 확장) — **submit과 status 조회 양쪽** 적용.
- run_job.sh: job dir·status·log 생성을 최상단으로 이동 + `fail()` — 조기 실패도
  `state=failed` + `note=사유`로 기록. GUI Check Status가 note 병기.
- GUI **App kit 필드** 신설(기본 `my_company.my_usd_composer` — prod-20260709 job.log의 실사용 앱).
- 교훈: "제출 성공"과 "러너 실행 성공"은 다른 축 — 전자는 tmux rc, 후자는 status 파일이 진실.
  status 파일이 생기기 전 구간을 없애는 것이 관측성의 핵심.

## #13. trace CSV 시계 불일치 — sim 클럭 통일 (2026-07-18, 6e1f3b9)

### 증상 (사용자 발견)
30초 에피소드의 `_trace_*.csv`가 **99초 스팬** — 재연하면 ~3.3배 슬로모션 + 충돌 CSV(GT)와 시각 불일치.

### 원인
physics일지 #6에서 오버레이·충돌 CSV를 sim 클럭(capture_start+sim경과)으로 옮길 때
**`facade.update()`의 `trace.tick()` 호출부만 누락** — 인자 없이 호출되어 TraceRecorder가
wall clock(`datetime.now()`)으로 스탬프. 렌더가 sim보다 느린 만큼(30s sim ≈ 99s wall) trace가 늘어짐.

### 해결 (`app/facade.py`, `physics/trace_recorder.py`)
- 충돌 CSV와 동일 조건(`_use_sim_clock`)으로 `get_sim_clock_datetime()`을 tick에 전달.
- TraceRecorder에 동일 타임스탬프 재기록 가드(렌더 대기 펌프 틱은 sim 클럭 정지 상태로 들어옴).
- 재생성 실측: 스팬 29.98s ≈ 30s 정합. 교훈은 #6과 동일 — **시계는 한 곳에서만 읽는다**;
  기록기가 여럿이면 "시계 주입"을 계약으로 강제해야 누락이 재발하지 않는다.

## #14. 운영 관측성·재연→추론 UX 일괄 (2026-07-18, f17fdb8·bd4b809)

| 항목 | 내용 (경로) |
|---|---|
| manifest 소요 시간 | `timing.setup_s/total_s` + 에피소드별 `elapsed_s`(실패도 기록) — 배치 예상 상한 공식의 실측 검증 근거 (`automation/generate_episodes.py`) |
| 캡처 자동 재생 | playback 모드에서 Capture 시작 시 자동 play — "play→capture" 수동 순서의 정지 화면 구간 제거 (`app/capture_service.py`) |
| serving 상태 분리 | serve 잡의 submit/status를 Serving 섹션 전용 라벨로 라우팅 — 생성 잡 상태와 혼재 해소 (`automation/window.py`) |
| Check Serving | 잡 status 파일이 아니라 **실체**를 직접 확인: `docker ps` + `/v1/models` → `container=Up 55 minutes  api=ready` (`build_serve_check_command`) |

## #15. Connect Server — 데몬·터널 통합 연결 버튼 (2026-07-18)

### 배경
REST(잡 큐) 경로를 쓰려면 ① 서버에서 데몬 기동 ② 로컬에서 SSH 터널 ③ Host 칸 교체를
사람이 순서대로 해야 했음. 데몬은 자기 자신을 못 띄우므로(부트스트랩) 기동만은 SSH가 필수 —
"확장이 터널을 코드로 소유한다면 기동까지 한 버튼에 묶는 게 맞다"는 검토 결론을 구현.

### 구현
- `run_api.sh`: **멱등 가드**(health 응답하면 통과 — 재실행 시 데몬 2개 포트 경합 방지) +
  venv 활성화. 검증 중 실버그 발견: 비대화형(tmux) 셸에서 `uvicorn: not found` **즉사** —
  venv PATH 부재. 데몬 원격 기동이 애초에 불가능했던 상태를 함께 수정.
- `remote_generation.py`: `build_daemon_start_command`(클라이언트측 선확인 + tmux 상주 기동),
  `SSHTunnel`(ssh -N -L 8800/38011 상주 프로세스 소유, ExitOnForwardFailure — 기존 터널과
  중복이어도 health 통과로 무해), `connect_server`(기동→터널→로컬 health 시퀀스).
- `window.py`: **Connect Server** 버튼 — 성공 시 Host를 `http://localhost:8800`으로 전환
  (이후 제출은 잡 큐 경유, vLLM도 localhost:38011 직결), 토글로 Disconnect(호스트 원복),
  창 파괴 시 터널 정리.

### 검증 (L40 실측)
1차 기동 `started` → health `{"ok":true,...}` → 2차 `running`(멱등) 3단 통과.
후속(future work, 보완사항 §6-2): tmux→systemd user 서비스(`Restart=on-failure`,
선행: linger 권한), 터널 생존 감시·자동 재연결.

## #16. 병합 체크포인트 손상 — `2222…` 퇴화 출력의 계층 진단 (2026-07-19)

### 증상
e2e 첫 실전(재연 캡처 → vLLM 추론)에서 전 청크가 `"2222222…"` 반복. v3 평가(STRICT 0.699)와
정면 모순. twin_view 프리셋으로 바꿔도, 학습 분포 그대로인 physics 원본 영상(ep_0001)을 넣어도 동일.

### 계층 진단 (용의자를 바깥쪽부터 벗겨냄)
| 실험 | 결과 | 기각된 가설 |
|---|---|---|
| 캡처 프레임 추출 육안 확인 | BEV·숫자 라벨·타임스탬프 정상 | 영상 분포 이탈 |
| twin_view 재시도 | 동일 퇴화 | 프리셋 불일치 |
| physics 원본(ep_0001) 추론 | 동일 퇴화 | 재연 캡처 품질 |
| serve.info 확인 | checkpoint-133-merged 서빙 중 | 엉뚱한 모델 |
| **텍스트만 질문**("2+2?") — vLLM 경유 | `!!!!!!!!` | 클라이언트·청킹·프롬프트 전부 |
| **transformers 직접 로드**(vLLM 미경유, GPU 1) | 역시 `!!!!!!!!!!` | vLLM 로딩 |
| **베이스 모델**(HF 캐시) 동일 질문 | `'Four'` 정상 | 베이스 캐시 손상 |

→ **`swift export --merge_lora` 산출물 자체가 손상**. v3 평가가 좋았던 건 어댑터 직접 로드
(`swift infer --adapters`)였기 때문 — **병합 경로는 이번이 첫 검증**이었다. 손상 시점 정황:
최초 병합 시 ModelScope 재다운로드 → IncompleteRead 중단 → 재시도 이력.

### 해결
깨진 병합본을 `.broken`으로 격리 후 **`HF_HUB_OFFLINE=1`**(검증된 캐시만 사용, 재다운로드 차단)
로 재병합 → transformers 텍스트 sanity `'Four'` 통과 → 컨테이너 재기동 → curl 정상 →
GUI 추론 정상 JSON 확인.

### 교훈
1. **모델 교체·병합 후에는 텍스트 1문항 sanity를 서빙 게이트로**: "2+2 → Four" 한 줄이
   전체 파이프라인 디버깅 몇 시간을 대체한다. 무거운 평가 이전의 최소 관문.
2. 퇴화 반복 출력은 성능 문제가 아니라 가중치/입력 규약 손상 신호 — 지표 하락과 구분할 것.
3. 진단은 바깥(입력)에서 안(가중치)으로 한 겹씩: 각 실험이 용의자 하나를 확정 기각하도록 설계.

## #17. 이벤트 시각 앵커 정합 — eventlist base_date 하드코드 제거 (2026-07-19)

### 증상 두 건
1. Lake 모드로 Process Events 후 **Event Search에 아무것도 안 걸림**.
2. eventlist가 **2025-01-01 기준**으로 생성됨(데이터는 2026-07-18인데).

### 원인
1. Event Search가 읽는 건 `vlm_events/` 인덱스(Lake 모드 **Generate**가 적재)인데, Generate를
   Local 모드로 돌림 → 인덱스 미적재. Process Events의 산출물(intermediate/eventlist)은 검색
   대상이 아님. + 기존 인덱스 1건은 병합 손상 시절 추론분이라 0 이벤트(빈 파일).
2. `summary_service.py`의 **`base_date="2025-01-01"` 하드코드**. VLM 출력엔 오버레이 시계의
   HH:MM:SS만 있어 날짜를 공급해야 하는데, 5월 리빙랩 데이터셋(실제로 2025-01-01 시각대)과
   우연히 일치해 잠복해 있던 결함. 날짜만이 아니라 **이벤트 위치 조회도 그 시각으로 하므로
   position까지 오염**됨.

### 해결
- `vlm_client/core.py`: 결과 JSON에 **`video_source`**(원본 영상 URI) 기록 — `video` 필드는
  스테이징 임시명이라 사이드카 역추적 불가였던 구멍.
- `summary_service.py`: base_date **3단 복원** — ① video_source 사이드카 앵커(capture_start)
  날짜 ② 로드된 궤적 데이터 시작 날짜 ③ 레거시 고정값(최후 폴백). #10에서 vlm_events 인덱스에
  적용한 "사이드카 앵커" 원리를 Process Events 경로에도 통일.
- 검증: 3단 폴백 케이스 테스트 + 기존 eventlist 테스트 4/4 통과.

### 남은 격차 (future work — 이벤트 클릭 재연)
검색된 이벤트를 선택해도 재연이 안 되는 건 별개 격차: 활성 lake 데이터셋
(`living_trajectory_1h_0_2s_parquet`)의 커버리지가 2025-01-01 00:00~01:00뿐이라 7/18 좌표가
없음. **episodes/ raw 존의 trace CSV를 trajectory/ lake 존(parquet+manifest)으로 인제스트하는
단계가 미구현** — 이게 생기면 검색→±5분 자동 로드→재연이 Lake 모드에서 자기완결된다.

## #18. CI 첫 그린 + public repo 비밀 감사 (2026-07-19, 7f2ac2a·6d56512)

### 배경
"52b1e94 커밋의 lint-and-test 실패" 문의로 조사 → GitHub Actions 이력상 **첫 run(7/14)부터
전 커밋이 실패**해 온 상태였음(52b1e94는 docs 14줄 커밋 — 무고). CI 동일 환경
(py3.12 + ruff/mypy/pytest/minio, `.env` 없는 클린 체크아웃)을 로컬 venv로 재현해 특정.

### 원인 → 해결
| 실패 | 원인 | 해결 |
|---|---|---|
| `test_extension_config_data_uri_local` | repo 실제 config.json의 `${DATA_PATH}`가 **로컬 .env에 의존** — .env는 gitignore라 CI엔 없음 → 빈 값 | 테스트를 임시 config 파일로 격리(검증 대상은 상대경로→file:// 해석 로직) (7f2ac2a) |
| `test_generate_captions_saves_…` | #17의 "lake 반환값=전체 URI" 계약 변경이 낸 회귀 | 테스트를 새 계약에 맞게 갱신 (190fe86) |
| (재현 중 발견) mypy 2건 | SSHTunnel._proc이 Optional 선언 없이 None 초기화 | Optional 선언 + stop() 지역변수 내로잉 (6d56512) |

재현 환경 최종: ruff 클린 · mypy 12파일 클린 · **pytest 97 passed, 0 failed** → push 시 첫 그린.

### 부수: public repo 비밀 감사
repo가 public임을 확인하고 **전체 git 히스토리**를 스캔 — 추적 파일의 키는 전부
`nvapi-***`/`sk-proj-***` 마스킹 자리표시였고, 히스토리에도 실키 패턴(nvapi-/sk-/ghp_/AKIA)·
MINIO/OMNI 자격증명 실값 **0건**. 노출된 저위험 정보(사설 IP, minIO 도메인, VSS 기본
패스워드)만 인지 대상으로 기록.

### 교훈
- 테스트가 repo의 **실제 설정 파일 + 로컬 .env**를 읽으면 "내 자리에서만 통과"하는
  환경 의존이 생긴다 — 단위 테스트는 자기 입력을 스스로 만들 것.
- CI 실패는 "그 커밋의 diff"가 아니라 **이력 전체의 추이**부터 볼 것(전부 빨강이면
  회귀가 아니라 환경 문제).
