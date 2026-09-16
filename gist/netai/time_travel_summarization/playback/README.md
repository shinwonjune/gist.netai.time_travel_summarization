# playback — 재생 헤드 시각을 궤적 좌표로 바꿔 USD 스테이지의 프림에 반영하는 도메인

## 역할과 위치

`gist/netai/time_travel_summarization/playback/`는 시간 축(재생 헤드 진행, 배속, 이벤트 점프)과 좌표 데이터(궤적 CSV/Parquet 단일 파일, 또는 데이터 레이크 — minIO에 시간 분할 청크와 manifest로 적재한 궤적 데이터셋)를 각각 독립적으로 관리하고, 그 둘을 조합해 매 프레임 USD 프림 위치와 가시성을 갱신하는 계층이다. 상위의 `app/facade.py`(`TimeTravelCore`)가 이 계층의 클래스들을 소유하고 조합만 담당하며, 실제 시간 진행 로직과 좌표 조회 로직은 전부 이 디렉터리 안에 있다.

이 계층이 실제로 만들어내는 화면을 먼저 보면 아래 흐름 설명이 구체적으로 와닿는다.

<!-- 이미지 TODO | 파일: images/readme/playback-01-viewport-playback.gif | 촬영: Kit 익스텐션에서 궤적 데이터를 로드하고 재생(Play)한 상태의 뷰포트를 5~10초 화면 녹화. 우주인 프림 여러 개가 각자의 궤적을 따라 이동하는 모습과 좌상단(또는 오버레이 패널)의 현재 재생 시각(Twin Time) 표시가 함께 보여야 함 | 형식: gif -->
![그림 1. 재생 중인 뷰포트 — 우주인 프림들이 궤적을 따라 이동하고 현재 재생 시각(Twin Time)이 표시된 상태](../../../../images/readme/playback-01-viewport-playback.gif)
*그림 1. 재생 헤드가 진행하며 우주인 프림들이 좌표 데이터를 따라 이동하는 화면. 이 프레임 하나하나를 갱신하는 것이 아래 "매 프레임 갱신 경로"에서 설명하는 `TimeTravelCore.update(dt)` 호출이다.*

이 디렉터리의 클래스는 두 그룹으로 나뉜다.

- **Kit(Omniverse) 무의존, 순수 파이썬** — `controller.py`, `trajectory_repository.py`, `lake_repository.py`, `lake_common.py`, `lookup_benchmark.py`, `visibility.py`. `carb`/`omni`/`pxr` 없이 WSL의 표준 파이썬만으로 임포트와 단위 테스트가 가능하다(`lake_repository.py`만 `carb.log_warn` 유무를 `try/except ImportError`로 분기해 Kit 밖에서도 동작하게 해 둔 예외가 있다).
- **Kit 의존** — `stage_object_controller.py`. `omni.usd`, `pxr.UsdGeom`/`Gf` 없이는 임포트조차 되지 않으므로, Kit 밖에서 이 모듈을 쓰는 테스트는 `tests/conftest.py`의 `install_carb_stub(with_stage_object_controller=True)`로 `sys.modules`에 최소 스텁을 심어 우회한다.

## 구성 파일

| 파일 | 책임 | 핵심 심볼 |
|---|---|---|
| `__init__.py` | 패키지 설명 docstring만 있음(공개 심볼 재수출 없음) | — |
| `controller.py` | 재생 헤드 시간 진행의 상태 기계. 좌표 데이터를 전혀 모른다(콜백으로만 바깥과 통신) | `PlaybackController` |
| `trajectory_repository.py` | 단일 CSV/Parquet 파일 전체를 메모리에 올려 시각별 좌표를 조회하는 기본 리포지토리 | `TrajectoryRepository` |
| `lake_repository.py` | 데이터 레이크(minIO/`s3://` 또는 `file://`)의 시간 분할 청크를 manifest로 인덱싱하고, 재생 헤드 주변 청크만 윈도우로 로드하는 리포지토리 | `LakeTrajectoryRepository(TrajectoryRepository)` |
| `lake_common.py` | 데이터 레이크 파티션 규약(manifest 스키마, 청크 키 포맷) + 합성 궤적 생성 + 적재(ingest) 함수 | `ingest_rows`, `append_rows`, `generate_synthetic_rows`, `ingest_synthetic`, `manifest_uri`, `dataset_uri_from_manifest` |
| `lookup_benchmark.py` | floor-lookup(마지막 표본 조회) 알고리즘 4종의 순수 구현 + 벤치마크 하네스. `pytest` 수집 대상이 아니다(파일명이 `test_*.py`가 아님 — 측정 도구) | `LkvForwardBisectHybrid`, `LkvCache`, `lkv_linear`, `lkv_bisect`, `run_benchmark`, `benchmark_all` |
| `stage_object_controller.py` | USD 프림(우주인 메시, 카메라)의 생성, 이동, 가시성 토글. Kit/USD 필수 | `StageObjectController` |
| `visibility.py` | 재생 헤드 시각으로부터 객체별 보임/숨김을 판정하는 순수 함수(Kit 무의존, 단위 테스트 대상) | `is_track_visible`, `compute_object_visibility` |

## 동작 흐름

### 데이터 로드부터 첫 프레임까지

1. `app/data_service.py`의 `_repo_factory_for(uri, lake_cfg)`(`activate_data_source`가 호출)가 URI **모양**만 보고 리포지토리(repository = 궤적 좌표를 시각별로 조회하는 객체)를 고른다 — URI가 `manifest.json`으로 끝나면 `LakeTrajectoryRepository(cache_chunks=..., prefetch_ahead=...)`, 아니면 `TrajectoryRepository`. 어떤 버튼(로컬/레이크)을 눌렀는지가 아니라 데이터의 모양이 리더를 정하는 것이 의도된 설계다(같은 청크 데이터셋을 저장소만 바꿔 열 수 있어야 "지연이 minIO 경유 때문인가"를 가르는 대조군이 성립하기 때문).
2. `repo.load_from_uri(uri)` — `TrajectoryRepository`는 파일 전체를 `_data: {timestamp: {objid: (x,y,z)}}`로 읽어들이고, `LakeTrajectoryRepository`는 manifest만 읽어 청크 인덱스를 세운 뒤 첫 청크만 동기 로드한다.
3. `core._playback.configure_data_range(repo.data_start_time, repo.data_end_time)`로 `PlaybackController`에 재생 가능 구간을 알린다.
4. `object_service.regenerate_astronauts_from_loaded_data()`가 `repo.get_object_ids()`(전체 데이터에 등장하는 objid 합집합)로 우주인 프림을 만들어 `prim_map: {objid: prim_path}`을 구성한다.

### 매 프레임 갱신 경로

`extension.py`의 Kit 업데이트 콜백 → `TimeTravelCore.update(dt)`(`app/facade.py:307`) → `self._playback.update(dt, parse_timestamp, on_time_changed, on_event_requested)`(`controller.py`)가 다음을 수행한다.

1. **모드 체크** — `mode != "playback"`(즉 `"physics"`)이면 즉시 반환. `PlaybackController`는 재생/물리 두 모드의 상호 배타를 자기 상태로 강제한다.
2. **틱 게이트** — `_accumulated_time += dt * playback_speed`를 누적하다가 `abs(_accumulated_time) < _tick_min_s`이면 그 프레임은 좌표 조회를 건너뛴다. `_tick_min_s`는 생성 시점에 env `TTS_TICK_MIN_S`(기본 `0.1`초 = 1배속 기준 최대 10Hz 갱신)를 1회 읽어 고정되며, 익스텐션을 재시작해야 바뀐 값이 반영된다.
3. **이벤트 재생 분기** — `_use_event_summary`가 켜져 있으면 `_update_event_playback`이 `_event_summary`(타임스탬프 문자열 리스트)를 하나씩 순회한다. 각 이벤트마다 `go_to_current_event`로 그 시각으로 점프하고 `on_event_requested(event_timestamp)` 콜백을 호출한 뒤, `_event_playback_duration`(기본 1.0초)만큼 그 자리에서 시간을 흘려보내고 다음 이벤트로 넘어간다. 리스트를 한 바퀴 돌면(`_current_event_index == 0`으로 복귀) 재생을 멈춘다.
4. **일반 재생** — `_advance_time`이 `_current_time`에 `seconds_to_add`를 더하고, 범위 끝/시작에 닿으면 그 경계로 클램프하며 `_is_playing = False`로 정지시킨 뒤 `on_time_changed(self._current_time)` 콜백을 호출한다.
5. `TimeTravelCore._on_playback_tick(t)`가 이 콜백을 받아 `_maybe_skip_gap(t)`로 데이터 공백을 우회한다 — `_gap_skip_s`(기본 10.0초, `set_gap_skip_threshold`로 조정하며 0을 주면 끔) 임계값을 넘는 공백이 앞(정방향) 또는 뒤(역방향)에 있으면 `repo.next_data_time`/`repo.prev_data_time`이 반환한 다음 데이터 시각으로 즉시 점프한다.
6. `TimeTravelCore.update_stage_objects()`가 `repo.get_data_at_time(current_time)`(좌표)과 `_object_visibility_at(current_time)`(가시성, 아래 참조)을 모아 `self._stage_objects.update_stage_objects(prim_map, data, visibility)`를 호출 — 이 한 번의 호출이 그 프레임의 모든 프림 translate와 visibility를 갱신한다.

### 좌표 조회 — floor lookup

`TrajectoryRepository.get_data_at_time(timestamp)`은 정확히 그 시각의 표본이 없으면 **그 시각 이하 가장 최근 표본**(last-known-value, floor lookup)을 반환한다. 구현은 `set_lookup_mode(mode)`로 4가지 중 선택할 수 있다(기본 `"linear"`).

- `"linear"` — `_timestamps`를 처음부터 순회하며 `current <= timestamp_str`인 마지막 항목을 찾는다. `O(N)`이지만 정확하고 상태가 없다.
- `"bisect"` — 정렬된 `_timestamps`에 `bisect.bisect_right`. `O(log N)`, 정확.
- `"hybrid"` — `LkvForwardBisectHybrid`(`lookup_benchmark.py`). 정확히 grid에 맞는 시각(exact hit)이면 `O(1)` 해시 반환, miss(오프그리드 또는 역방향)면 bisect로 폴백. 항상 정확하다.
- `"lkv_cache"` — `LkvCache`(`lookup_benchmark.py`). grid hit일 때만 캐시를 갱신하고 miss면 캐시를 그대로 반환하는 `O(1)` 알고리즘. 순방향 재생에서는 정확한 floor이지만 역방향 재생에서는 한 grid 밀리고, 슬라이더를 빠르게 드래그해 grid를 건너뛰면 stale해질 수 있다(의도된 트레이드오프 — `lookup_benchmark.py` 클래스 docstring 참조).

`get_data_at_time`은 `set_lookup_mode`와 별개로 `start_benchmark(pattern)`/`stop_benchmark()`로 호출 횟수와 누적 시간을 계측할 수 있다(`_bench_active`가 켜져 있을 때만 `time.perf_counter()`를 두 번 더 부른다).

### 가시성 — 죽은 트랙 잔상 차단

`update_stage_objects` 프레임마다 `TimeTravelCore._object_visibility_at(current_time)`이 `repo.get_object_time_ranges()`(objid별 `(first_sample_t, last_sample_t)`)를 `visibility.compute_object_visibility`에 넘겨 objid별 보임/숨김 딕셔너리를 만든다. 판정 규칙은 두 가지다(`visibility.py`).

1. **트랙 범위 밖(항상 적용)** — `now`가 `[first - tol, last + tol]`(기본 `tol_s = DEFAULT_VISIBILITY_TOL_S = 1.0`초) 밖이면 숨긴다. 트랙 파편화(fragmentation)로 죽은 트랙이 마지막 좌표에 얼어붙어 가짜 충돌로 읽히는 문제를 막는다.
2. **결손 인지 despawn(옵션, 기본 비활성)** — env `TTS_DESPAWN_GAP_S`가 설정돼 있으면(`app/facade.py`가 읽어 `gap_s`로 전달) 범위 안이라도 `now - last_sample > gap_s`일 때 숨긴다. `last_sample`은 `repo.get_object_last_sample(current_time)`(objid별 "그 시각 이하 마지막 표본 시각", 이분 탐색 캐시)로 구한다. 기본이 꺼져 있는 이유는, 표본이 드문 조건(다운샘플)에서 정상 객체가 깜빡이기 때문이다 — `frag-sameid` 계열(같은 ID로 중간 행만 지워 죽은 트랙 잔상이 규칙 1을 피해가는 조건)처럼 결손 자체가 소멸을 뜻하는 렌더 조건에서만 조건별로 켠다.

`StageObjectController.update_stage_objects`는 `visibility[objid] is False`인 프림을 `UsdGeom.Imageable.MakeInvisible()`로 숨긴다(삭제가 아니라 토글이라 시간 스크럽으로 왕복해도 다시 보인다). `data`에 없는 objid는 위치를 건드리지 않아(기존 hold 동작) 트랙 내부의 결손 구간은 마지막 좌표를 유지한다. `get_world_positions`(오버레이 라벨용 월드 좌표 조회)도 숨겨진 프림은 결과에서 제외한다 — 지운 죽은 트랙에 라벨만 남는 것을 막기 위함이다.

### 데이터 레이크 청크 활성화와 백그라운드 프리페치

`LakeTrajectoryRepository`는 `_data`/`_timestamps`(부모 `TrajectoryRepository`가 쓰는 필드)에 **활성 청크 하나만** 담는다는 점에서 부모와 다르다. `_do_lookup(timestamp)`이 `_chunk_for_time`(청크 시작 시각 리스트 `_chunk_starts`에 이진 탐색)으로 청크 인덱스를 구하고, 활성 인덱스와 다르면 `_activate(idx)`로 전환한 뒤 부모의 `_do_lookup`을 그대로 재사용한다.

이 청크 인덱싱이 디스크(또는 버킷) 위에서 실제로 어떤 파일 구조로 나타나는지 먼저 보면 아래 캐시와 프리페치 설명을 따라가기 쉽다.

<!-- 이미지 TODO | 파일: images/readme/playback-02-lake-manifest-chunks.png | 촬영: `tools/lake_ingest.py`로 만든 데이터셋 디렉터리(예 file:///tmp/lake/ds1)를 `ls -la` 또는 파일 탐색기로 연 화면 캡처, 또는 그 디렉터리의 `manifest.json`을 텍스트 에디터로 연 화면. `chunks` 배열(각 항목의 시작/끝 시각, 파일 경로)과 실제 청크 파일들(.csv 또는 .parquet)이 함께 보여야 함 | 형식: png -->
![그림 2. 데이터 레이크 데이터셋 디렉터리 구조 — manifest.json과 시간 분할 청크 파일들](../../../../images/readme/playback-02-lake-manifest-chunks.png)
*그림 2. `manifest.json`의 `chunks` 배열 각 항목이 `_chunk_starts`가 이진 탐색하는 대상이고, `tracks` 필드가 아래에서 설명할 objid별 범위 폴백 규칙과 직결된다.*

- `_ensure_loaded(idx)` — LRU 캐시(`OrderedDict`, 크기 `cache_chunks`, 기본 최소값은 `prefetch_ahead + 2`)에 있으면 즉시 반환(`cache_hits`), 없으면 `_load_chunk(idx)`로 동기 로드한다(`sync_loads` — 이 경로가 곧 재생 stall 비용이다).
- `_schedule_prefetch(idx)`가 매 조회마다 `[active+1 .. active+prefetch_ahead]`와 역재생 대비용 `active-1`을 백그라운드 워커 큐(`_pf_queue`, 데몬 스레드 `_pf_thread`)에 넣어, 청크 경계를 넘기 전에 다음 청크가 이미 캐시에 있게 만든다(`prefetch_loads`).
- `_activate(idx)`는 청크를 바꿀 때 `_hybrid`/`_lkv_cache`(stateful lookup 캐시)와 `_object_samples`(gap-despawn 표본 시각 캐시)를 함께 초기화한다 — 비우지 않으면 이전 활성 청크의 표본 시각이 새 청크에서도 남아 청크 경계를 넘은 직후 gap-despawn이 낡은 시각으로 오판한다(실측 회귀 사례).
- `get_object_time_ranges()`는 부모의 "메모리 전체 스캔" 구현을 **상속하면 안 된다** — 레이크는 `_data`가 활성 청크뿐이라 그대로 쓰면 트랙 범위가 첫 청크 스팬으로 잘못 계산되고 그렇게 캐시되어, 재생 헤드가 청크 1을 넘는 순간 dead-track despawn이 전 객체를 숨긴다(실측 회귀). 대신 manifest의 `"tracks"` 필드(있으면 objid별 `[first_ts, last_ts]`를 그대로 신뢰)를 쓰고, 그 필드가 없는 레거시 manifest에서는 데이터셋 전체 범위를 전 objid에 부여하는 폴백으로 대체한다(레거시는 전부 연속 생산 궤적이라 사실상 정확함).
- `next_data_time`/`prev_data_time`도 오버라이드된다 — 공백 탐지를 청크 해상도로만 수행한다(manifest의 청크 `[start, end]` 커버리지 안이면 공백 아님, 밖이면 다음/이전 청크 경계). 메모리의 `_chunk_starts` 이진 탐색만으로 끝나 minIO 조회가 없다. 청크 내부의 미세한 공백은 적재기가 연속 데이터만 청크화한다는 전제 아래 무시한다.

## 핵심 설계 결정과 규약

- **시간과 좌표의 분리** — `PlaybackController`는 좌표를, `TrajectoryRepository`/`LakeTrajectoryRepository`는 재생 상태를 전혀 모른다. 콜백(`parse_timestamp`, `on_time_changed`, `on_event_requested`)을 함수 인자로 주입받는 방식으로 결합해, 두 클래스 다 Kit 없이 단위 테스트가 가능하다.
- **역방향 재생을 1급 시민으로 지원** — `set_playback_speed`는 `-10.0 ~ 10.0` 범위의 음수를 허용한다(0은 차단 — 정지에는 `toggle_playback`을 쓴다). 이 결정이 `next_data_time`/`prev_data_time` 쌍, `LkvForwardBisectHybrid`의 backward 감지 분기, gap-skip의 정/역방향 함수 선택(`facade.py`의 `_maybe_skip_gap`)까지 이어진다.
- **`objid` 합집합, 시작 시각 스냅숏이 아님** — `get_object_ids()`는 전체 데이터에 등장하는 objid의 합집합(정렬)을 반환한다. `t=start` 시점의 스냅숏만 보면 중간에 등장하는 트랙(fragmentation의 새 ID 등)을 누락해 프림 자체가 생성되지 않고, 그 결과 "투명 충돌"(라벨은 있는데 눈에 보이는 물체가 없는 상태)이 생긴다. `LakeTrajectoryRepository`도 동일 계약을 유지한다(`_objids`는 manifest가 이미 합집합으로 기록).
- **`XformCommonAPI` 대신 translate op find-or-add 패턴** — `stage_object_controller._get_or_add_translate_op`가 `xformable.GetOrderedXformOps()`에서 translate op를 찾아 재사용하고 없으면 새로 추가한다. physics(리지드바디)를 거친 프림에서 `XformCommonAPI`는 예외 없이 조용히 실패하기 때문에, 이 프로젝트에서는 이 패턴이 프림 이동의 표준이다.
- **`kind=component` 선언** — `create_astronaut_prim`이 생성한 프림에 `Usd.ModelAPI(prim).SetKind("component")`를 명시한다. USD의 `BBoxCache`는 자식 bound를 "가장 가까운 component 조상"의 로컬 공간에서 집계하고, 그런 조상이 없으면 월드 공간으로 폴백하는데, 그 월드 폴백이 콜라이더 반지름 오염(회전에 따라 바운딩 박스가 달라지는 문제)의 전제 조건이었다. 이 선언으로 `ComputeUntransformedBound`가 회전과 무관하게 밀착한 치수를 돌려주게 된다(`physics/collision_proxy.py`의 rest 캐시와는 별개의 이중 안전장치).
- **timestamp 조회 키 포맷은 `timefmt.TIMESTAMP_FMT`로 고정** — `"%Y-%m-%d %H:%M:%S.%f"`(밀리초까지, 날짜 포함)이며 `TrajectoryRepository.format_timestamp`/`parse_timestamp`가 `timefmt.format_timestamp`를 그대로 위임한다. 이는 오버레이/충돌 CSV가 쓰는 `timefmt.format_event_time`(날짜 없음, `PRECISION` 설정에 따라 초 또는 밀리초)과는 **별개의 계약**이다 — 조회 키는 사람이나 VLM에게 보여주는 문자열이 아니라 인메모리 dict를 인덱싱하는 키이므로 `PRECISION`과 무관하게 항상 날짜+밀리초를 유지한다.
- **레이크 매니페스트의 `"tracks"` 필드는 v2 추가, 없으면 전체 범위 폴백** — `lake_common.ingest_rows`/`append_rows`가 `_track_time_ranges`로 objid별 `[first_ts, last_ts]`를 계산해 manifest에 함께 쓴다. `append_rows`가 기존 manifest를 병합할 때, 기존에 `"tracks"`가 없던(v2 이전) manifest는 새 rows를 추가해도 계속 `"tracks"` 없이(무필드) 유지한다 — 부분 범위만 기록하면 기존 트랙 구간이 잘려 despawn을 오판하게 되므로, 레거시는 리더의 "데이터셋 전체 범위" 폴백에 계속 의존시키는 쪽이 안전하다.
- **`ingest_rows`(60초, CSV 기본값)와 `append_rows`(300초, Parquet 기본값)의 기본값이 다르다** — `ingest_rows`는 직접 호출, 테스트, 성능 측정용 저수준 함수라 60초 청크와 CSV가 기본이다. `append_rows`는 manifest가 없을 때 신규 생성 경로로 `ingest_rows`를 호출하되 300초, Parquet을 명시 전달한다 — 실제 배포(프로덕션 궤적 레이크 신규 생성)는 `append_rows` 경로를 쓰는 것이 의도된 기본값이다(`utils/ingest_trajectory.py`의 `--chunk-seconds` 도움말과 일치).
- **적재는 시간 겹침을 거부한다** — `append_rows`는 신규 rows의 `[new_start, new_end]`가 기존 청크 중 하나와라도 겹치면 `ValueError`를 던진다(같은 데이터 재적재 방지). 겹침이 없는 시간 공백은 재생기의 `next_data_time`/공백 점프가 처리하므로 연속 적재일 필요는 없다.

## 사용법

### 단일 파일 리포지토리 로드 (Python API)

```python
from gist.netai.time_travel_summarization.playback.trajectory_repository import TrajectoryRepository

repo = TrajectoryRepository()
repo.load_from_uri("file:///abs/path/to/trajectory.csv")   # 또는 "s3://bucket/key.parquet"
repo.set_lookup_mode("bisect")                              # "linear" | "bisect" | "hybrid" | "lkv_cache"
positions = repo.get_data_at_time(repo.data_start_time)     # {objid: (x, y, z)}
```

`load_from_uri`는 URI 확장자(`.csv` 또는 `.parquet`, 대소문자 무관)로만 포맷을 판별하며, 실제 바이트 읽기는 `storage.from_uri(uri)`가 반환하는 어댑터(`file://` → `LocalAdapter`, `s3://` → `MinioAdapter`)에 위임한다. `.parquet`은 `pyarrow`가 설치돼 있을 때만 동작하는 선택 의존성이다.

### 데이터 레이크 리포지토리 로드

```python
from gist.netai.time_travel_summarization.playback.lake_repository import LakeTrajectoryRepository

repo = LakeTrajectoryRepository(cache_chunks=4, prefetch_ahead=1)
repo.load_from_uri("s3://bucket/trajectory/ds1/manifest.json")  # 또는 데이터셋 디렉터리 URI("manifest.json" 생략 가능)
```

### 합성 데이터 생성과 적재 (`lake_common.py`)

```python
from gist.netai.time_travel_summarization.playback.lake_common import ingest_synthetic

manifest = ingest_synthetic(
    "file:///tmp/lake/ds1", n_objects=10, duration_s=60.0, hz=5.0,
    chunk_seconds=60, fmt="csv",
)
```

실제 트래커 소스(trace CSV)를 데이터 레이크에 적재하는 CLI는 이 디렉터리가 아니라 `utils/ingest_trajectory.py`이며, 내부적으로 `lake_common.append_rows`를 호출한다.

### 환경 변수

| 변수 | 읽는 위치 | 의미 |
|---|---|---|
| `TTS_TICK_MIN_S` | `controller.py`(`PlaybackController.__init__`) | 재생 갱신 게이트(초). 누적 재생 시간이 이 값 미만이면 그 프레임은 좌표 조회를 건너뛴다. 기본 `0.1`(1배속 기준 최대 10Hz). 컨트롤러 생성 시점에 1회만 읽으므로 변경은 익스텐션 재시작 후 반영된다. 파싱 실패 시 기본값으로 폴백. |
| `TTS_DESPAWN_GAP_S` | `app/facade.py`(`_object_visibility_at`) | 설정돼 있으면 `visibility.compute_object_visibility`의 `gap_s`로 전달돼 결손 인지 despawn을 켠다. `playback/` 안의 코드는 이 값을 직접 읽지 않고 인자로만 받는다. |

### URI 형식

- 단일 파일: `file:///abs/path/file.csv`, `file:///abs/path/file.parquet`, `s3://bucket/key.csv`, `s3://bucket/key.parquet`.
- 데이터 레이크: 데이터셋 디렉터리 URI(`s3://bucket/trajectory/ds1` 또는 `file:///path/ds1`) 또는 그 아래의 `manifest.json` URI. `lake_common.manifest_uri`/`dataset_uri_from_manifest`가 둘을 상호 변환한다.

## 테스트

이 디렉터리 자체에는 `tests/` 하위 디렉터리가 없고, 검증은 확장 공용 `tests/`에서 이 패키지를 직접 임포트하는 방식으로 이뤄진다. Kit 의존 없이 WSL 표준 파이썬 `pytest`로 실행 가능하다(`stage_object_controller.py`를 임포트하는 테스트만 `tests/conftest.py`의 `install_carb_stub(with_stage_object_controller=True)`로 스텁을 심는다).

| 테스트 파일 | 검증 대상 |
|---|---|
| `tests/test_tick_gate.py` | `PlaybackController`의 `TTS_TICK_MIN_S` 게이트 — env 미설정/커스텀 값/파싱 실패 폴백, 생성 시점 1회 스냅숏 동작 |
| `tests/test_gap_skip.py` | `TimeTravelCore._maybe_skip_gap`이 `TrajectoryRepository.next_data_time`/`prev_data_time`을 통해 정방향/역방향 공백을 건너뛰는 동작(`PlaybackController`와 함께 통합 검증) |
| `tests/test_repository_uri.py` | `TrajectoryRepository.load_from_uri`의 CSV/Parquet 판별과 `file://` 경로 처리(`pyarrow` 미설치 환경에서는 Parquet 케이스를 건너뜀) |
| `tests/test_lookup_benchmark.py` | `LkvForwardBisectHybrid` 등 `lookup_benchmark.py`의 순수 알고리즘 정확성(순방향/역방향/오프그리드 질의에서 linear oracle과 일치 여부) |
| `tests/test_lake_repository.py` | `LakeTrajectoryRepository`의 청크 단위 조회 결과가 전체 적재(oracle) 대비 청크 경계와 오프그리드 시점에서도 일치하는지, LRU 캐시 크기 제한과 백그라운드 프리페치가 실제로 동작하는지 |
| `tests/test_lake_track_ranges.py` | manifest `"tracks"` 필드의 기록(ingest)과 정확한 반환(레이크 리더, 청크 활성화와 무관), 무-tracks 레거시 manifest의 전체 범위 폴백, `append_rows`의 병합 규칙 |
| `tests/test_lake_ingest_append.py` | `lake_common.append_rows`/`ingest_rows`의 적재 계약(`file://` + CSV, `pyarrow` 불필요) |
| `tests/test_object_regen_ids.py` | `get_object_ids()`가 시작 시각에 없는 늦게 등장한 트랙(objid)까지 합집합에 포함하는지, `object_service._prim_index_for`의 prim 인덱스 규칙 |
| `tests/test_gap_despawn.py`, `tests/test_track_visibility.py` | `visibility.is_track_visible`/`compute_object_visibility` — 트랙 범위 판정, `gap_s` 결손 인지 despawn의 경계값과 다운샘플 오검 방지 |
| `tests/test_lake_perf_tools.py` | `lake_common.ingest_synthetic`을 데이터 소스로 쓰는 레이크 성능 측정 도구(`tests/lake_benchmark.py`, `app/lake_probe.py`) 검증 — `playback/` 자체보다는 그 위 계측 계층 대상 |
| `tests/test_smoke.py` | `TrajectoryRepository`/`PlaybackController` 최소 스모크(로드, `set_progress` 범위 클램프) |

`lookup_benchmark.py`는 파일명이 `test_*.py`가 아니므로 `pytest`가 자동 수집하지 않는다 — 벤치마크 재현이 필요하면 `benchmark_all(...)`을 직접 호출하거나 `format_results_table`/`save_results_csv`로 결과를 뽑아 쓴다.

## 한계와 주의

- **틱 게이트는 배속에 비례해 스케일된다** — `_accumulated_time += dt * playback_speed`이므로 배속을 높이면 게이트를 더 빨리 넘어 사실상 갱신 빈도가 올라간다. 반대로 배속을 낮추면 `TTS_TICK_MIN_S`가 상대적으로 커진 것과 같은 효과를 낸다.
- **`lkv_cache` 모드는 정확도를 보장하지 않는다** — grid를 벗어난 조회(빠른 슬라이더 드래그, 역재생 중 특정 지점)에서 stale한 값을 반환할 수 있다(`lookup_benchmark.LkvCache` docstring에 명시된 의도된 트레이드오프). 정확도가 중요한 경로(측정용 재연)에서는 `"bisect"` 또는 `"hybrid"`를 쓸 것.
- **레이크 리더의 `next_data_time`/`prev_data_time`은 청크 해상도로만 공백을 본다** — 청크 내부의 미세한 공백(예: 개별 표본 몇 개 누락)은 감지하지 못한다. 이는 적재기가 연속 데이터만 청크로 나눈다는 전제에 기대는 것으로, 그 전제가 깨지는 데이터(불연속 구간이 한 청크 안에 섞인 경우)에서는 gap-skip이 발동하지 않는다.
- **`get_coord_range()`의 전체 스캔 비용** — `TrajectoryRepository.get_coord_range()`는 `_data`(로드된 전체 데이터)를 순회해 min/max를 계산한다. `LakeTrajectoryRepository`는 manifest에 미리 기록된 `coord_min`/`coord_max`가 있으면 그것을 우선 반환해 이 비용을 피하지만(오버라이드), manifest에 그 필드가 없는 경우 부모 구현으로 폴백하면서 **활성 청크뿐인 `_data`**만 스캔하게 되므로 전체 범위가 아니라 활성 청크 범위만 반환한다.
- **`stage_object_controller.py`의 카메라 초기값은 하드코딩** — `_camera_start_position`, `_camera_rotation`, `_camera_focal_length` 등은 특정 씬을 기준으로 잡힌 상수다. 다른 스케일의 씬에 재사용하려면 이 값들을 조정해야 한다.
- **`move_camera_to_event`는 world-up이 Y라고 가정한다** — `translate_op.Set(Gf.Vec3d(obj_x, self._camera_height, obj_z))`가 이벤트 위치의 X/Z만 취하고 Y는 고정 카메라 높이로 대체한다.
