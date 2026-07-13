# 프로젝트 설명 (면접용)

> VLM과 디지털 트윈을 연계한 **폐루프 시공간 탐색·요약 프레임워크**.
> 좌표 로그를 시간축 기준으로 디지털 트윈(NVIDIA Omniverse stage)에서 재현하고, 그 화면을 VLM으로 분석해
> 이벤트 목록을 만든 뒤, 다시 디지털 트윈을 이벤트 중심으로 재구성해 요약을 제공한다.

이 문서는 면접에서 자주 묻는 핵심 기능의 **구현 원리**를 코드 링크와 함께 정리한다.

1. [좌표 데이터를 시간의 흐름에 따라 stage에서 재현하는 원리](#1-좌표-데이터의-시간축-재현time-travel-playback)
2. [데이터 레이크(minIO)와 연동하는 원리](#2-데이터-레이크minio-연동)
3. [시각 의미론(twin time)과 이벤트 인덱스](#3-시각-의미론twin-time과-이벤트-인덱스)

코드 링크는 확장(extension) 루트 기준 상대 경로이며 줄 번호 앵커를 포함한다.

---

## 0. 한 장 요약: 핵심 데이터 흐름

```
Kit 앱 프레임 틱(Events 2.0)
   └─ extension._on_update(dt)                      # 매 프레임 호출
        └─ facade.update(dt)
             └─ PlaybackController.update(dt)        # dt를 재생 시계로 누적 → 현재 시각 t 갱신
                  └─(콜백) facade.set_current_time(t)
                        └─ facade.update_stage_objects()
                             ├─ repository.get_data_at_time(t)   # 시각 t → {objid:(x,y,z)} (floor lookup)
                             └─ StageObjectController.update_stage_objects(prim_map, data)
                                   └─ USD xformOp.Set(Gf.Vec3d(x,y,z))   # prim을 실제로 이동
```

- **시계(clock)** 는 벽시계가 아니라 "재생 시각"이다. 프레임 간 경과시간 `dt`에 배속을 곱해 누적한다 → 일시정지·배속·역재생·구간 점프가 모두 같은 메커니즘으로 처리된다.
- **데이터 조회**는 시각 t에 정확히 일치하는 행이 없어도 `t` 이하의 가장 가까운 행을 쓰는 **floor lookup**(계단 보간) 으로 처리한다.
- **재현**은 USD prim의 변환(translate) 속성을 매 프레임 덮어쓰는 방식이다.

---

## 1. 좌표 데이터의 시간축 재현(Time-Travel Playback)

"특정 시각의 객체 위치를 디지털 트윈에 그려 넣고, 시간을 흐르게 해 과거를 재생한다." 이를 4개 책임으로 분리했다:
**데이터(Repository) · 시계(PlaybackController) · 무대(StageObjectController) · 조립(Facade)**.

### 1.1 데이터 모델과 로딩 — `TrajectoryRepository`

좌표 로그의 스키마는 `timestamp, objid, x, y, z` 다섯 컬럼이다. CSV/Parquet 모두 같은 스키마를 쓴다.

로딩 시 행 리스트를 **"시각 → {객체ID: (x,y,z)}"** 중첩 딕셔너리로 바꾸고, **정렬된 timestamp 리스트**를 따로 둔다. 정렬은 뒤에서 이진 탐색(bisect)을 쓰기 위한 전제다.

- 행 → 조회용 구조 변환: [`trajectory_repository.py:205-217`](../gist/netai/time_travel_summarization/playback/trajectory_repository.py#L205-L217)

```python
def _rows_to_data(rows):
    data = {}
    for row in rows:
        data.setdefault(row["timestamp"], {})[row["objid"]] = (
            float(row["x"]), float(row["y"]), float(row["z"]))
    return data, sorted(data.keys())   # ← bisect 위해 정렬 보장
```

- URI 기반 로딩(로컬/레이크 공통 진입점): [`trajectory_repository.py:31-51`](../gist/netai/time_travel_summarization/playback/trajectory_repository.py#L31-L51) — `.csv`/`.parquet`를 확장자로 구분하고, 실제 바이트 읽기는 storage 어댑터(2장)에 위임한다. 즉 **로컬 파일이든 minIO든 같은 코드 경로**다.
- 전체 데이터 시작/끝 시각도 이때 계산해 둔다(슬라이더 범위로 사용).

### 1.2 시간을 흐르게 하는 엔진 — Kit 프레임 틱과 `PlaybackController`

Omniverse Kit은 매 프레임 "update 이벤트"를 발행한다(Events 2.0). 확장은 시작 시 이 스트림을 **구독**해, 프레임마다 콜백을 받는다.

- 구독 등록: [`extension.py:110-116`](../gist/netai/time_travel_summarization/extension.py#L110-L116)
- 프레임 콜백 → 코어로 위임: [`extension.py:122-127`](../gist/netai/time_travel_summarization/extension.py#L122-L127)

```python
def _on_update(self, e):
    dt = e.payload.get("dt", 0)   # 직전 프레임 이후 경과 시간(초)
    self._core.update(dt)         # 재생 로직
    if self._window: self._window.update_ui()
```

`dt`(프레임 간 실제 경과시간)를 재생 시계에 누적한다. 핵심은 **벽시계가 아니라 누적값**이라는 점 — 그래서 배속·역재생·일시정지가 자연스럽다.

- 시계 갱신 로직: [`controller.py:89-127`](../gist/netai/time_travel_summarization/playback/controller.py#L89-L127)

```python
def update(self, dt, parse_timestamp, on_time_changed, on_event_requested):
    if not self._is_playing or not self._current_time: return
    self._accumulated_time += dt * self._playback_speed   # 배속 반영(음수면 역재생)
    if abs(self._accumulated_time) < 0.1: return          # 너무 잦은 갱신 방지(누적 후 일괄 반영)
    seconds_to_add = self._accumulated_time; self._accumulated_time = 0.0
    ...
    self._advance_time(seconds_to_add, on_time_changed)   # 현재 시각 = 현재 시각 + Δ

def _advance_time(self, seconds_to_add, on_time_changed):
    next_time = self._current_time + datetime.timedelta(seconds=seconds_to_add)
    # 범위 끝(또는 시작)에 닿으면 clamp + 정지
    ...
    self._current_time = next_time
    on_time_changed(self._current_time)   # ← 콜백으로 "시각 바뀜"을 facade에 통지
```

설계 포인트(면접에서 강조할 부분):

- `PlaybackController`는 **Omniverse·USD에 의존하지 않는 순수 시간 로직**이다. 콜백(`on_time_changed`, `on_event_requested`)으로 무대와 분리돼 있어 단위 테스트가 쉽다.
- 슬라이더 드래그(progress 0~1) 같은 직접 seek도 같은 시계를 갱신한다: [`controller.py:51-57`](../gist/netai/time_travel_summarization/playback/controller.py#L51-L57).
- 배속은 -10~10으로 clamp, 0은 차단(정지는 재생 토글로): [`controller.py:210-214`](../gist/netai/time_travel_summarization/playback/controller.py#L210-L214). 음수 배속이 곧 역재생이다.

### 1.3 "시각 → 좌표" 매핑 — floor lookup(계단 보간)

시각 `t`가 바뀌면 facade가 `t`에 해당하는 좌표를 조회한다. 콜백 배선은 다음과 같다 — `on_time_changed`로 `facade.set_current_time`을 넘긴다:

- 콜백 배선: [`facade.py:540-541`](../gist/netai/time_travel_summarization/app/facade.py#L540-L541)
- 시각 변경 → 무대 갱신: [`facade.py:303-305`](../gist/netai/time_travel_summarization/app/facade.py#L303-L305), [`facade.py:292-296`](../gist/netai/time_travel_summarization/app/facade.py#L292-L296)

조회의 핵심은 **로그가 t와 정확히 같은 시각을 갖지 않아도 된다**는 것이다. 데이터는 5Hz 같은 이산 주기인데 프레임은 임의 시각이므로, `t` 이하에서 가장 가까운 시각의 값을 쓴다(직전 알려진 값 유지 = 계단 함수).

- 조회 진입점(+벤치마크 계측): [`trajectory_repository.py:97-104`](../gist/netai/time_travel_summarization/playback/trajectory_repository.py#L97-L104)
- 정규화 + 정확히 일치 fast-path + floor: [`trajectory_repository.py:106-115`](../gist/netai/time_travel_summarization/playback/trajectory_repository.py#L106-L115)

```python
def _do_lookup(self, timestamp):
    normalized = timestamp.replace(microsecond=(timestamp.microsecond//1000)*1000)  # ms로 양자화
    key = self.format_timestamp(normalized)
    if key in self._data: return self._data[key]      # 정확히 일치(빠른 경로)
    return self._get_last_known_value(key)            # 아니면 floor(직전 값)
```

floor lookup은 4가지 모드로 구현해 **알고리즘 성능을 실측 비교**할 수 있게 했다(`linear`/`bisect`/`hybrid`/`lkv_cache`).

- 선형 탐색(원본, O(N)): [`trajectory_repository.py:138-148`](../gist/netai/time_travel_summarization/playback/trajectory_repository.py#L138-L148)
- 이진 탐색(O(log N), 정렬 전제): [`trajectory_repository.py:150-154`](../gist/netai/time_travel_summarization/playback/trajectory_repository.py#L150-L154)

```python
def _lookup_bisect(self, timestamp_str):
    idx = bisect.bisect_right(self._timestamps, timestamp_str) - 1   # t 이하 최대 인덱스
    return self._data[self._timestamps[max(idx,0)]]
```

> 시각을 문자열로 비교해도 되는 이유: 포맷이 `YYYY-MM-DD HH:MM:SS.mmm` 고정폭이라 **사전식 정렬 = 시간 정렬**이 성립한다. parse/format은 한 곳에서 관리한다: [`trajectory_repository.py:173-189`](../gist/netai/time_travel_summarization/playback/trajectory_repository.py#L173-L189).

### 1.4 무대에 그리기 — USD prim 변환 갱신 (`StageObjectController`)

조회한 `{objid:(x,y,z)}`를 실제 3D 객체로 옮긴다. 각 객체ID는 `prim_map`을 통해 USD prim 경로에 매핑돼 있고, 그 prim의 **translate xformOp**를 매 프레임 덮어쓴다.

- 위치 적용: [`stage_object_controller.py:53-77`](../gist/netai/time_travel_summarization/playback/stage_object_controller.py#L53-L77)

```python
def update_stage_objects(self, prim_map, data):
    for objid, prim_path in prim_map.items():
        if objid not in data: continue
        prim = stage.GetPrimAtPath(prim_path)
        xformable = UsdGeom.Xformable(prim)
        # 기존 translate op 재사용(없으면 추가) → 매 프레임 op 생성 방지
        translate_op = next((op for op in xformable.GetOrderedXformOps()
                             if op.GetOpType()==UsdGeom.XformOp.TypeTranslate), None) \
                       or xformable.AddTranslateOp()
        x, y, z = data[objid]
        translate_op.Set(Gf.Vec3d(x, y, z))   # ← 객체를 해당 좌표로 이동
```

> `Gf.Vec3d`/`UsdGeom.Xformable`은 Omniverse가 쓰는 USD(Universal Scene Description) API다. `xformOp`는 prim에 붙는 변환(이동/회전/스케일) 연산으로, 여기서는 위치만 매 프레임 갱신해 애니메이션처럼 보이게 한다.

### 1.5 객체 prim 생성(스폰)

데이터가 로드되면 등장하는 객체ID마다 prim을 하나씩 생성한다(케이스 스터디에서는 우주비행사 USD 에셋을 참조로 인스턴싱). `prim_map`(objid→prim 경로)이 이때 만들어진다.

- prim 생성/에셋 참조: [`stage_object_controller.py:108-146`](../gist/netai/time_travel_summarization/playback/stage_object_controller.py#L108-L146)
- 로드된 데이터로 일괄 생성: [`facade.py:975`](../gist/netai/time_travel_summarization/app/facade.py#L975) (`regenerate_astronauts_from_loaded_data`)

각 prim에는 translate/rotate/scale op를 미리 붙여두므로(§1.4), 재생 중에는 translate만 갱신하면 된다.

### 1.6 시간 정합 — `timefmt.py`

재현된 화면에는 타임스탬프가 **번인(burn-in) 오버레이**로 찍히고, 충돌 정답 라벨 CSV도 같은 시각을 쓴다. VLM은 화면의 시각을, 정답은 CSV의 시각을 참조하므로 **둘이 같은 포맷이어야** 추론-채점이 어긋나지 않는다. 그래서 이벤트 시각 포맷을 단일 모듈로 통일했다.

- 단일 포맷 소스: [`timefmt.py:28-32`](../gist/netai/time_travel_summarization/timefmt.py#L28-L32) (`format_event_time`), [`timefmt.py:35-62`](../gist/netai/time_travel_summarization/timefmt.py#L35-L62) (`parse_event_time`, 자정 넘김 보정 포함)

### 1.7 이벤트 중심 비선형 재생(요약 재생)

VLM이 만든 이벤트 목록이 있으면, 일반 재생 대신 "각 이벤트 시점으로 점프 → 일정 시간 재생 → 다음 이벤트" 순회를 돈다. 같은 시계 엔진 위에 모드만 얹은 구조다.

- 이벤트 순회 재생: [`controller.py:129-167`](../gist/netai/time_travel_summarization/playback/controller.py#L129-L167)

---

## 2. 데이터 레이크(minIO) 연동

목표: **특정 시간대를 입력하면, minIO에 분할 저장된 좌표 데이터를 윈도우 단위로 가져와 Omniverse에서 딜레이 없이 재연**한다. 12시간급 대용량 로그도 전체를 메모리에 올리지 않는다.

설계는 두 축이다: **(A) 스토리지 추상화**(로컬 파일 ↔ minIO를 같은 인터페이스로) + **(B) 시간 분할 + 윈도우 로딩**(필요한 청크만, 다음 청크는 미리).

### 2.1 스토리지 추상화 — 같은 코드로 로컬과 minIO를 모두

모든 저장소 접근을 6개 메서드 인터페이스로 추상화했다. 재생 코드는 URI만 넘기고, 백엔드 선택은 URI 스킴으로 자동 결정된다.

- 인터페이스: [`storage/base.py:15-32`](../gist/netai/time_travel_summarization/storage/base.py#L15-L32) (`open_read / put_bytes / put_file / list_prefix / exists / stat`)
- 스킴 기반 디스패치(팩토리): [`storage/factory.py:11-23`](../gist/netai/time_travel_summarization/storage/factory.py#L11-L23)

```python
def from_uri(uri):
    scheme = urlparse(uri).scheme.lower()
    if scheme in ("s3", "minio"): return MinioAdapter()   # 싱글턴 캐시
    if scheme in ("file", ""):    return LocalAdapter()
    raise ValueError(...)
```

이 덕분에 §1.1의 `_read_rows`는 로컬/레이크를 구분하지 않는다 — `from_uri(uri).open_read(uri)` 한 줄이면 끝. **하위호환**도 자연스럽다: 레이크를 끄면 기존 단일 CSV 경로 그대로 동작.

### 2.2 MinIO 어댑터 — S3 호환 객체 스토리지 접근

`minio` 파이썬 클라이언트로 객체 단위 GET/PUT을 한다. 자격증명은 `storage/.env` 또는 OS 환경변수(`MINIO_ENDPOINT/ACCESS_KEY/SECRET_KEY`)에서 읽는다.

- GET/PUT 구현: [`minio_adapter.py:45-64`](../gist/netai/time_travel_summarization/storage/minio_adapter.py#L45-L64) (`get_object`/`put_object`/`fput_object`)
- 클라이언트 lazy 초기화: [`minio_adapter.py:107-121`](../gist/netai/time_travel_summarization/storage/minio_adapter.py#L107-L121)
- `.env`/환경변수 로딩: [`minio_adapter.py:123-151`](../gist/netai/time_travel_summarization/storage/minio_adapter.py#L123-L151)
- `minio` 미설치 시에도 임포트는 실패하지 않게 가드: [`minio_adapter.py:7-12`](../gist/netai/time_travel_summarization/storage/minio_adapter.py#L7-L12) (선택적 의존성)

### 2.3 레이크 레이아웃 — 결정적 청크 키 + manifest 인덱스

대용량 로그를 **시간 단위 청크**로 쪼개 저장한다. 청크 파일명이 **시작 시각(epoch-ms)** 으로 결정되는 게 핵심이다 — 그래서 "어떤 시각이 어느 파일에 있는지"를 **LIST 스캔 없이 계산**할 수 있다.

```
{dataset_uri}/manifest.json
{dataset_uri}/chunk_{startEpochMs:013d}.csv|.parquet
```

- 레이아웃/규약 정의: [`lake_common.py:1-34`](../gist/netai/time_travel_summarization/playback/lake_common.py#L1-L34)
- 결정적 키 생성: [`lake_common.py:37-43`](../gist/netai/time_travel_summarization/playback/lake_common.py#L37-L43) (`to_epoch_ms`, `chunk_object_key`)

`manifest.json`은 1회 GET으로 전체 범위·청크 인덱스를 파악하게 해 주는 메타데이터다: `format`, `chunk_seconds`, `objids`, 전체 `start/end/rows`, `coord_min/max`, 그리고 `chunks[].{key,start,end,rows}`(start 오름차순).

### 2.4 적재(ingest)

행을 시간 버킷으로 나눠 청크로 인코딩(CSV 무의존 / Parquet은 pyarrow snappy)하고 PUT한 뒤, manifest를 마지막에 쓴다.

- 적재 본체: [`lake_common.py:125-196`](../gist/netai/time_travel_summarization/playback/lake_common.py#L125-L196) (`ingest_rows`)
- 청크 인코딩: [`lake_common.py:95-120`](../gist/netai/time_travel_summarization/playback/lake_common.py#L95-L120)
- CLI 래퍼: [`tools/lake_ingest.py`](../tools/lake_ingest.py)

```bash
# 합성 10객체 × 12시간, 60초 청크 → 로컬 file://
python tools/lake_ingest.py --dest /tmp/lake/ds1 --objects 10 --duration 12h
# 기존 CSV를 Parquet 청크로 minIO에 적재
python tools/lake_ingest.py --source data/living.csv --dest s3://bucket/living --format parquet --chunk-seconds 60
```

### 2.5 윈도우 로딩 — `LakeTrajectoryRepository`

면접에서 가장 강조할 부분. `LakeTrajectoryRepository`는 §1의 `TrajectoryRepository`를 **상속**해 floor-lookup·벤치마크·parse/format을 그대로 재사용한다. 단 한 가지 차이: **`self._data/_timestamps`가 전체가 아니라 "활성 청크 1개"만 담는다.**

- 클래스/설계 의도: [`lake_repository.py:38-45`](../gist/netai/time_travel_summarization/playback/lake_repository.py#L38-L45)

**(1) 기동 시 manifest만 로드** — 좌표는 아직 안 읽음(슬라이더 전체 범위만 표시):

- [`lake_repository.py:77-97`](../gist/netai/time_travel_summarization/playback/lake_repository.py#L77-L97) (`load_from_uri`), [`lake_repository.py:99-109`](../gist/netai/time_travel_summarization/playback/lake_repository.py#L99-L109) (`_load_manifest`)

**(2) 조회 시 시각 → 청크 인덱스를 bisect로 계산**, 활성 청크가 바뀌면 교체:

- [`lake_repository.py:113-128`](../gist/netai/time_travel_summarization/playback/lake_repository.py#L113-L128)

```python
def _do_lookup(self, timestamp):           # 부모의 floor lookup을 감싸는 오버라이드
    idx = self._chunk_for_time(timestamp)  # bisect로 시각→청크
    if idx != self._active_idx: self._activate(idx)   # 청크 경계 넘으면 교체
    self._schedule_prefetch(idx)           # 이웃 청크 백그라운드 선로드 예약
    return super()._do_lookup(timestamp)   # ← 여기서부터는 §1.3과 동일한 floor lookup
```

**(3) LRU 캐시로 활성/이웃 청크 보관** — 활성 청크는 절대 evict하지 않음:

- 활성화/동기로드: [`lake_repository.py:130-152`](../gist/netai/time_travel_summarization/playback/lake_repository.py#L130-L152)
- 청크 GET+디코드: [`lake_repository.py:154-162`](../gist/netai/time_travel_summarization/playback/lake_repository.py#L154-L162)
- LRU evict(활성 보호): [`lake_repository.py:164-176`](../gist/netai/time_travel_summarization/playback/lake_repository.py#L164-L176)

**(4) 백그라운드 프리페치** — 재생 헤드가 청크 경계를 넘기 전에 다음 청크를 데몬 스레드가 미리 GET+디코드 → 경계에서의 동기 로드 stall을 0으로:

- 프리페치 워커: [`lake_repository.py:180-219`](../gist/netai/time_travel_summarization/playback/lake_repository.py#L180-L219)
- 선로드 대상 예약(앞 N개 + 역재생 대비 뒤 1개): [`lake_repository.py:221-233`](../gist/netai/time_travel_summarization/playback/lake_repository.py#L221-L233)

> 핵심 아이디어: 조회 1회마다 "지금 청크가 캐시에 있나 → 없으면 동기 로드(stall)"인데, 재생 중에는 다음 청크를 **미리** 올려두면 경계에서도 캐시 히트라 끊김이 없다. 활성 청크를 보호해 방금 프리페치한 청크가 곧바로 쫓겨나지 않게 캐시 최소 크기를 `prefetch_ahead+2`로 잡았다([`lake_repository.py:40-44`](../gist/netai/time_travel_summarization/playback/lake_repository.py#L40-L44)).

### 2.6 재생 코드와의 연결 — 레포지토리만 바꿔 끼움

facade는 설정에 따라 **같은 인터페이스의 레포지토리를 교체**할 뿐, 재생 로직(§1)은 그대로다. 이게 상속 설계의 보상이다.

- 레포 선택(레이크 vs 로컬): [`facade.py:116-192`](../gist/netai/time_travel_summarization/app/facade.py#L116-L192)
- 시간대 입력 → 구간 로드: [`facade.py:250-271`](../gist/netai/time_travel_summarization/app/facade.py#L250-L271) (`load_time_range`)

UI "Load Time Range (minIO)"에 시작/끝 시각을 넣으면 재생 범위가 그 구간으로 좁혀지고 시작점으로 이동한다 — 이때 **해당 청크만** 로드된다.

### 2.7 설정

`config.json`:

```json
"lake": { "enabled": true,
  "manifest_uri": "s3://bucket/trajectory/living/manifest.json",
  "cache_chunks": 4, "prefetch_ahead": 2 }
```

- 설정 파싱(+`${ENV}` 치환): [`config.py:34-94`](../gist/netai/time_travel_summarization/app/config.py#L34-L94)
- `enabled:false`(기본)면 단일 파일 경로(`data_path`)로 동작 → 하위호환.

### 2.8 성능·검증

청크 경계마다 발생하던 동기 로드 **stall을 프리페치가 0으로** 만든다는 것을 벤치마크로 실측했다.

| Scale | Rows | Cold seek(μs) | Warm seek(μs) | Stalls ON/OFF | Hit ON/OFF |
|-------|------|---------------|---------------|---------------|------------|
| 10obj×300s | 15,000 | ~3,955 | ~2.25 | **0 / 4** | 80% / 0% |
| 100obj×300s | 150,000 | ~40,431 | ~2.24 | **0 / 4** | 80% / 0% |

- 벤치마크: [`tests/lake_benchmark.py`](../gist/netai/time_travel_summarization/tests/lake_benchmark.py) — cold/warm seek, 연속 재생 stall·히트율
- 정확성: [`tests/test_lake_repository.py`](../gist/netai/time_travel_summarization/tests/test_lake_repository.py) — 윈도우 로딩 결과가 전체 적재(oracle)와 청크 경계/off-grid 시점에서 동일함을 확인

자세한 운영 가이드는 [`docs/DATA_LAKE.md`](DATA_LAKE.md) 참조.

---

## 3. 시각 의미론(twin time)과 이벤트 인덱스

VLM 추론 결과를 "몇 시부터 몇 시까지 무슨 일이 있었나"로 검색하고, 선택한 이벤트
시점으로 트윈을 재구축하려면 **시각의 의미를 한 번 정리**해야 했다.

### 3.1 twin time — 디지털 트윈 세계의 현재 시각

**twin time** = 트윈 세계가 지금 몇 시인가. 재연(playback) 모드에서는 재생 헤드가
가리키는 **데이터의 시각**이고, physics 생성 모드에서는 **t0 + 시뮬 경과**다.
USD 타임라인의 "stage time"(0초부터 흐르는 재생 초)과는 다른 개념이라 용어를 분리했다.
오버레이(영상 픽셀에 굽는 시계)·충돌 CSV·VLM 보고가 전부 twin time으로 말한다.

### 3.2 사이드카 앵커 — 영상의 상대 시간 ↔ twin time 변환점

영상 파일은 "시작부터 몇 초"라는 상대 시간만 안다. 캡처마다 영상 옆에 남기는
사이드카(`<video>.meta.json`)의 `capture_start`가 **"영상 0초 = twin time 몇 시"**
라는 변환 기준점(앵커)이다.

- 앵커는 **그 모드의 내부 시계** 값이어야 한다. playback 캡처에서 벽시계를 적으면
  영상 픽셀(데이터 시각)과 어긋나 이벤트가 캡처한 날짜에 붙는다 — 그래서 playback
  캡처의 앵커는 재생 헤드의 데이터 시각을 기록하고, 벽시계는 관리용 `wall_clock`
  필드로 분리했다: [`capture_service.py`](../gist/netai/time_travel_summarization/app/capture_service.py) `capture_anchor()`
- playback 캡처는 재연 창(`replay_start/end`)도 함께 남긴다. 사이드카는 로컬뿐
  아니라 s3 경로에도 기록된다(레이크 재연 영상의 시각 복원에 필수).

### 3.3 이벤트 인덱스 — 추론 결과의 시간축 검색 표면

추론 결과(영상별 JSON)는 흩어져 있어 시간대 질문에 답할 수 없다. 추론이 끝날
때마다 이벤트를 고정 스키마로 축적한다: [`event_index.py`](../gist/netai/time_travel_summarization/event_processing/event_index.py)

- **적재**: 추론 1회(영상 1개) = 오브젝트 1개(`vlm_events/<영상>.jsonl`, 이벤트
  1건 = 1행). minIO는 append가 없으므로 영상별 파일이면 동시 쓰기 경합이 원천
  차단되고, 재추론 = 덮어쓰기 = "최신이 진실"이 된다. 0건도 기록(검사 증거).
- **절대 시각 복원**: VLM은 오버레이의 HH:MM:SS만 보고하므로, 적재 시점에
  사이드카 앵커와 결합해 절대 datetime으로 저장한다. 보고 시각이 앵커보다
  이르면 자정을 넘은 것 → +1일(롤오버). 다일(多日) 범위 조회는 절대 시각이
  없으면 성립하지 않는다(01-01과 01-02의 00:30 이벤트를 구분 불가).
- **조회**: `query_events(root, start, end)` — 시간창의 이벤트를 시간순으로.
  Event Post Processing 창의 Event Search가 이걸 호출하고, 항목을 선택하면
  그 시점으로 seek(로드 범위 밖이면 ±5분 자동 로드 후 seek)한다.

### 3.4 재생 공백 점프

재생은 "시계가 주인, 데이터는 조회 대상" 구조라(0장 참조), 데이터 공백 구간에
들어가면 객체는 얼어붙고 시계만 실시간으로 공백을 기어간다(공백 23h = 벽시계
23h). 그래서 진행 틱마다 "다음 데이터까지의 간격 > 임계값(기본 10s)"이면 시계를
다음 데이터 시각으로 순간이동시킨다: [`facade.py`](../gist/netai/time_travel_summarization/app/facade.py) `_maybe_skip_gap()`

- 탐색은 이미 메모리에 있는 인덱스(정렬 timestamp 배열 / 레이크 manifest의 청크
  경계) 위의 이진 탐색 — minIO 조회 없음. 도착 청크 로딩은 기존 프리페치가 처리.
- 역재생은 대칭(직전 데이터로 점프). 임계값 0 = 기능 끔(`set_gap_skip_threshold`).

---

## 4. 면접 예상 Q&A (요약)

- **왜 floor lookup인가?** 데이터는 이산 주기(5Hz), 프레임은 임의 시각. 가장 가까운 직전 값을 유지하면 추가 보간 없이 끊김 없는 재생이 된다. 정확 일치 fast-path로 흔한 경우를 먼저 걸러낸다.
- **시각을 문자열로 비교해도 되나?** 고정폭 포맷이라 사전식 정렬 = 시간 정렬. 그래서 bisect가 성립한다([`trajectory_repository.py:150-154`](../gist/netai/time_travel_summarization/playback/trajectory_repository.py#L150-L154)).
- **레이크와 로컬 코드가 어떻게 같나?** 스토리지는 URI 스킴으로 어댑터를 고르고([`factory.py`](../gist/netai/time_travel_summarization/storage/factory.py)), 레이크 레포는 일반 레포를 상속해 lookup을 재사용한다([`lake_repository.py:38`](../gist/netai/time_travel_summarization/playback/lake_repository.py#L38)). 재생 엔진은 레포가 로컬인지 레이크인지 모른다.
- **12시간 로그를 어떻게 무지연으로?** manifest로 인덱싱 → 결정적 키로 직접 GET → 활성 청크만 메모리 → 다음 청크는 백그라운드 프리페치. 경계 stall이 0이 되는 걸 벤치마크로 증명.
- **시뮬레이션 시계가 벽시계와 다른 이유?** `dt` 누적 기반이라 배속(역재생 포함)·일시정지·구간 점프가 한 메커니즘으로 처리되고, VLM 추론용 영상 3배속 추출 같은 시간 제어가 가능해진다.
