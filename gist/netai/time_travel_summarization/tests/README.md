# tests — 유닛 테스트와 레이크 성능 벤치마크 하네스

## 역할과 위치

`tests/`는 이 확장의 순수 로직 유닛 테스트 39개 파일(`test_*.py`)과, pytest
수집 대상이 아닌 벤치마크 도구 1개(`lake_benchmark.py`)를 담는다. 이
확장은 Omniverse Kit 안에서만 완전히 동작하지만, 대부분의 로직(좌표
계산, 게이트 판정, 파서, 상태기계)은 `omni`/`carb`(Kit 런타임 바인딩)에
의존하지 않도록 설계돼 있어 WSL/일반 파이썬 환경에서 pytest로 직접
검증할 수 있다. `omni.ui`/`carb` 같은 모듈을 모듈 최상위에서 import하는
파일(`window.py`, `summary_service.py` 등)을 테스트할 때는 최소 스텁을
심어(`conftest.install_carb_stub`) 순수 로직만 분리해 검증한다.

## 실행 방법

레포 루트(`pyproject.toml`이 있는 위치)에서:

```bash
python3 -m pytest -q
# 또는 pyproject.toml 상단 주석이 권장하는 형태(minio 확장 포함 설치):
uvx --with minio pytest
```

`pyproject.toml`의 `[tool.pytest.ini_options]`가 `testpaths =
["gist/netai/time_travel_summarization/tests"]`로 고정돼 있어, 레포 루트
아무 위치에서 `pytest`만 실행해도 이 디렉터리가 수집된다. 개별 파일은
`python3 -m unittest`로도 돌릴 수 있는 것들이 많다(`unittest.TestCase`
기반 — `test_job_store.py` 주석이 이를 명시).

## conftest.py — 공용 헬퍼

`conftest.py`에는 pytest fixture가 아니라 **일반 함수** 하나만 있다:
`install_carb_stub(with_stage_object_controller=False)`. `carb`는
Omniverse 런타임 밖(WSL pytest)에서 import 불가능한 네이티브 바인딩이라,
`carb.log_info/log_warn/log_error`를 no-op으로 흉내 낸 스텁 모듈을
`sys.modules["carb"]`에 심는다. `with_stage_object_controller=True`면
`playback.stage_object_controller.StageObjectController`도 최소 스텁
(`object`)으로 등록한다(Kit 없이 만들 수 없는 클래스지만, 그 심볼을
import만 하고 실제로는 쓰지 않는 테스트 대상 모듈이 로드 시 죽지 않게
하기 위함).

**autouse fixture로 만들지 않는 이유**가 모듈 docstring에 명시돼 있다:
`sys.modules`를 전역으로 오염시키면 그 오염이 다른 테스트로 새어나간다.
예를 들어 `test_encoder.py`는 "`omni`가 로드되지 않았다"를 단언하는데,
다른 테스트가 import 시점에 전역으로 `carb`/`omni` 스텁을 심어 버리면
이 단언이 깨진다. 그래서 `install_carb_stub`은 필요한 테스트 파일이
**호출 시점에만** 명시적으로 부르고, `test_vlm_window_shorten.py`처럼
`omni.ui`/`omni.kit.app`을 import하는 모듈을 다루는 파일은 스텁을
`setUpClass`/`tearDownClass` 범위로 한정해 되돌린다.

`__init__.py`는 `test_smoke.py`의 심볼을 재노출할 뿐이다(`from .test_smoke
import *`, NVIDIA 라이선스 헤더 포함).

## 외부 의존 가드 (5곳)

전체 스위트는 외부 서비스 없이 돌아가는 것이 기본이지만, 5개 테스트는
관련 의존성/자격증명이 없으면 자동으로 skip된다. 아래 4곳 외에
`test_job_api_validation.py`가 `pytest.importorskip("pydantic")`/`("fastapi")`로
두 패키지가 없으면 파일 전체를 skip한다.

| 파일 | 가드 방식 | 조건 |
|---|---|---|
| `test_encoder.py` | `@unittest.skipUnless` | `imageio` 패키지가 설치돼 있을 때만 그 테스트 실행 |
| `test_repository_uri.py` | `@unittest.skipIf` | `pyarrow`/`pyarrow.parquet` import 실패 시(둘 다 None) 스킵 |
| `test_storage_minio.py` | 모듈 레벨 `pytest.skip(..., allow_module_level=True)` | `.env`를 읽어도 `MINIO_ENDPOINT`/`MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY`/`MINIO_BUCKET` 중 하나라도 없으면 파일 전체 스킵 |
| `test_job_store.py` | `@unittest.skipUnless` | `JOB_STORE_TEST_DSN` 또는 `DATABASE_URL` env var가 설정돼 있을 때만 Postgres 시나리오 실행(SQLite 시나리오는 무조건 실행) |

## lake_benchmark.py — 측정 도구 (pytest 수집 대상 아님)

파일명이 `test_*.py` 패턴이 아니므로 pytest가 수집하지 않는다. 데이터레이크
윈도우 재생 성능을 실측하는 CLI 하네스이며, 두 모드를 갖는다.

1. **합성 모드**(기본) — `file://` + CSV 합성 데이터로 ingest/cold-seek/
   warm-seek/continuous 재생을 측정한다. 외부 의존 없이 동작.
2. **데이터셋 모드**(`--dataset-uri`) — 실 데이터셋(`s3://` 또는
   `file://`)에 대해 두 계층을 측정한다: A 계층(전송) —
   `manifest_load_ms`, 청크 GET p50/p99(첫 요청은 TLS 핸드셰이크 혼입
   때문에 분리 집계), `decode_ms_per_MB`(csv vs parquet), cold/warm seek
   p50/p99; B 계층(연속 재생, wall-clock 페이싱) — forward 1x/5x, random
   seek N회, backward 1x 시나리오별 stall(웜업 제외)/cache_hit_rate/
   prefetch_lead_s.

CLI(`main`): `scales`(위치 인자, nargs*) `--dataset-uri` `--lookup-hz`
(10.0) `--play-wall-s`(180.0) `--cache-chunks`(4) `--prefetch-ahead`(2)
`--seeks`(10) `--scenarios`(`1x,5x,seek,backward`) `--max-chunks`(0)
`--warm-queries`(2000) `--seed`(42) `--note` `--out-dir`.

`test_lake_perf_tools.py`가 이 파일의 `transport_bench`/`seek_bench`/
`run_scenario` 함수를 오프라인(`file://` + CSV, 네트워크 무관)으로
검증한다 — 즉 이 벤치마크 하네스 자체의 정확성은 일반 유닛 테스트가
보증하고, 실측값 자체는 `lake_benchmark.py`를 직접 실행해서 얻는다.

사용: `python -m gist.netai.time_travel_summarization.tests.lake_benchmark`
(합성) 또는 `... --dataset-uri s3://.../aigrad_bev_10hz_30min_v1_c60
--note "office LAN / i7 / 64GB"`(실측, minIO 필요).

## 테스트 파일 표

| 파일 | 검증 대상 모듈 | 무엇을 검증하는가 |
|---|---|---|
| `test_encoder.py` | `video_capture.encoder` | FrameEncoder 백엔드 선택(imageio 있음/없음), omni 미로드 상태에서 임포트 가능 |
| `test_event_index.py` | `events.event_index` | 이벤트 인덱스 로직(carb 스텁 사용) |
| `test_event_summary_window.py` | `events.summary_window` | EventSummaryWindow의 순수 로직(omni.ui/carb는 메서드 내부 지연 import라 모듈 최상위는 무의존) |
| `test_eventlist_lake.py` | `events.summary_service` | 레이크 소스에서의 이벤트 리스트 구성(carb 스텁 사용) |
| `test_frame_queue.py` | `video_capture.FrameQueue` | 프레임 큐 동작 |
| `test_gap_despawn.py` | `playback.trajectory_repository`, `playback.visibility` | 결손 인지 despawn 판정(frag-sameid 계열이 트랙 생존 창을 덮어 despawn이 안 걸리는 문제의 회귀 방지) |
| `test_gap_skip.py` | `app.facade.TimeTravelCore`, `playback.controller` | 결손 구간 스킵 동작(carb + stage_object_controller 스텁) |
| `test_geom.py` | `automation.geom` | `center_distance_3d`가 perturb_eval/rule_baseline의 원본 수식과 비트 단위로 동일한지 |
| `test_job_api_validation.py` | job_api `JobRequest` | 자유 문자열 필드 최소 검증(공백류, `-` 시작 금지). FastAPI TestClient 없이 pydantic 모델 직접 생성 |
| `test_job_store.py` | `VLM_server.l40.job_store`, `automation.remote_generation.JobSpec` | 잡 스토어(SQLite 항상 + Postgres는 DSN 있을 때) |
| `test_lake_ingest_append.py` | `playback.lake_common`, `utils.ingest_trajectory` | lake append 적재 계약(file://, csv 포맷, pyarrow 불필요) |
| `test_lake_perf_tools.py` | `app.lake_probe`, `playback.lake_common`, `tests.lake_benchmark` | 레이크 성능 실험 도구(transport_bench/seek_bench/run_scenario) 전부 오프라인 검증 |
| `test_lake_repository.py` | `playback.lake_repository.LakeTrajectoryRepository` | 청크 단위 로딩 결과가 전체 적재(oracle)와 동일한지, 청크 경계/캐시/프리페치 |
| `test_lake_track_ranges.py` | `playback.lake_repository`, `playback.lake_common` | 레이크 트랙 범위(manifest "tracks") — despawn 정확성 |
| `test_lookup_benchmark.py` | `playback.lookup_benchmark` | 좌표 조회 알고리즘(선형/이분/하이브리드) 벤치마크 정확성 |
| `test_near_miss.py` | `automation.generate_episodes`, `physics.WanderController` | near-miss 안무의 GT 0건 근거(속도 제어, gap 불변식, 트레이스 검증 3층) |
| `test_normalize_source.py` | `storage.normalize.normalize_source` | minIO 콘솔 형식 등 소스 문자열 정규화(omni 무의존) |
| `test_object_regen_ids.py` | `app.object_service._prim_index_for`, `playback.trajectory_repository` | 객체 재생성의 objid 합집합/prim 인덱스 규칙(frag "투명 충돌" 회귀 방지) |
| `test_output_routing.py` | `app.data_service`, `app.facade.TimeTravelCore` | 산출물 라우팅(carb + stage_object_controller 스텁) |
| `test_overlay_composer.py` | `video_capture.overlay_composer` | 오버레이 합성기 headless 임포트/동작 |
| `test_overlay_flags.py` | `automation.overlay_flags` | overlay_overlap 픽셀 판정기 순수 로직(합성 그레이스케일 배열, ffmpeg/이미지 파일 불필요) |
| `test_phase_clips.py` | `automation.phase_clips` | 위상 분해 추출기 v3.x 순수 로직(합성 trace로 kind 필터, 창 경계, 게이트 검증, ffmpeg 불필요) |
| `test_phase_scoring.py` | `automation.phase_scoring` | 위상 분해 채점 드라이버 순수 로직(엔드포인트 정규화, 응답 분류, 네트워크 불필요) |
| `test_physics_imports.py` | `physics` 패키지 | omni 없이 물리 패키지가 임포트되는지 |
| `test_realtime_projection.py` | `video_capture.realtime_capture` | 실시간 오버레이 투영 스케일 계산 |
| `test_replay_job.py` | `automation.replay_range`, `automation.remote_generation` | 재연 잡 순수 헬퍼(구간 파싱/검증/파일명, replay 잡 env 렌더링/러너 디스패치, omni 무의존) |
| `test_repository_uri.py` | `app.config.ExtensionConfig`, `playback.trajectory_repository` | URI 기반 저장소 해석(pyarrow 없으면 관련 테스트 스킵) |
| `test_scene_profiles.py` | `automation.scene_profiles` | 프로파일 로더 + 스폰 구역이 프로파일 아레나를 따르는지 |
| `test_smoke.py` | `app.config`, `app.paths`, `playback.controller`, `playback.trajectory_repository` | 기본 스모크(설정/경로/컨트롤러/저장소 최소 동작) |
| `test_storage_local.py` | `storage.from_uri`(file://) | 로컬 파일 스토리지 어댑터 |
| `test_storage_minio.py` | `storage.from_uri`(s3://) | minIO 스토리지 어댑터(env 미설정 시 모듈 전체 스킵) |
| `test_tick_gate.py` | `playback.controller.PlaybackController` | 데이터 갱신 게이트(`TTS_TICK_MIN_S`) — 재생 누적 시간이 문턱 미만이면 lookup 스킵 |
| `test_timefmt.py` | `timefmt` | `format_timestamp`가 기존 각지 복제 표현식과 바이트 단위로 동일한 문자열을 내는지 |
| `test_trace_recorder.py` | `physics.TraceRecorder` | 고주파 좌표 trace 기록기 |
| `test_track_visibility.py` | `playback.trajectory_repository`, `playback.visibility` | 트랙 가시성(생존 창) 판정 |
| `test_video_capture_types.py` | `video_capture.CaptureRequest`, `CaptureResult` | 캡처 요청/결과 타입 기본값 |
| `test_vlm_lake_upload.py` | `vlm_client.core.VLMClientCore` | VLM 클라이언트의 레이크 업로드 경로(carb 스텁) |
| `test_vlm_window_shorten.py` | `vlm_client` 관련 window | omni.ui/carb/omni.kit.app을 setUpClass/tearDownClass 범위로 스텁해 순수 헬퍼만 검증 |
| `test_wander_stuck.py` | `physics.CollisionRecorder`, `physics.WanderController` | wander 시뮬레이션의 끼임(stuck) 감지/재배치 |

## 한계

- Kit 런타임이 실제로 필요한 동작(렌더링, USD 프림 조작, 실제 물리 스텝)
  은 이 스위트로 검증되지 않는다 — carb/omni 스텁은 "import가 실패하지
  않게"만 해줄 뿐 실제 Kit 동작을 흉내 내지 않는다.
- `test_job_store.py`의 Postgres 시나리오, `test_storage_minio.py`
  전체, `test_repository_uri.py`의 pyarrow 관련 항목은 CI 환경에 해당
  의존성/자격증명이 없으면 조용히 스킵된다 — 스킵 사유는 pytest 출력의
  "skipped" 목록에서 확인해야 한다.
