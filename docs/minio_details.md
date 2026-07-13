# MinIO 데이터레이크 연동 상세 정리

이 문서는 Time Travel Summarization extension에서 MinIO를 연결하고, 궤적 데이터를 넣고, 다시 가져오고, 큰 데이터를 청크 단위로 캐싱하는 전체 과정을 정리한다. 취업 준비와 면접 설명을 위해 코드 흐름, 설정 방법, 실제 명령 예시, 용어 설명을 함께 기록한다.

## 0. 현재 프로젝트 버전 기준 보완 사항

이 문서는 현재 코드 기준으로 다음 내용을 반영한다.

```text
1. Data Lake 읽기는 manifest + parquet chunk 방식이 기본이다.
2. lake.direct_data_uri가 있으면 테스트용으로 단일 CSV/parquet URI를 직접 읽을 수 있다.
3. Local 모드에서는 산출물을 extension 내부 artifacts 아래에 저장한다.
4. Data Lake 모드에서는 output_root_uri, event_list_uri, video_output_uri의 s3:// URI를 사용한다.
5. VLM 원본 JSON도 Data Lake 모드에서는 output_root_uri/vlm_outputs/ 아래 MinIO에 저장된다.
6. 실시간 캡처 영상은 Data Lake 모드에서 video_output_uri 아래 mp4로 저장된다.
```

현재 구현상 `config.json`에는 `event_list_uri`도 존재한다. 다만 event processing이 새 event list를 생성할 때는 `output_root_uri/event_list/`를 사용하고, event list를 읽을 때는 UI/Facade에서 `event_list_uri`를 전달할 수 있는 구조다. 즉 `output_root_uri`는 산출물 root, `event_list_uri`는 event list prefix를 명시적으로 읽거나 저장 위치를 분리할 때 쓰는 URI로 보면 된다.

## 1. 전체 구조 요약

이 프로젝트에서 MinIO는 "로컬 파일 대신 원격 Object Storage에 궤적 데이터와 산출물을 저장하는 저장소" 역할을 한다.

작은 CSV 파일은 한 번에 읽어도 되지만, 1시간/6시간처럼 커지는 궤적 데이터는 한 파일 전체를 매번 읽으면 느리고 메모리 사용량도 커진다. 그래서 프로젝트는 다음 구조를 사용한다.

```text
UI Data Lake 버튼
  -> TimeTravelCore.set_data_source("lake")
  -> LakeTrajectoryRepository.load_from_uri(manifest_uri)
  -> MinIO에서 manifest.json 읽기
  -> 현재 재생 시간에 필요한 parquet chunk만 다운로드
  -> chunk cache에 저장
  -> 다음 chunk는 background prefetch
  -> astronaut 위치 업데이트
```

핵심은 "전체 데이터를 한 번에 로드하지 않고, manifest로 인덱스를 먼저 읽은 뒤 필요한 chunk만 읽는다"는 점이다.

테스트 목적의 예외 경로도 있다. `lake.direct_data_uri`를 config에 넣으면 `LakeTrajectoryRepository` 대신 일반 `TrajectoryRepository`가 그 URI를 직접 읽는다. 이 경로는 MinIO CSV 접근 테스트처럼 "manifest/parquet 구조가 아니라 단일 파일을 잘 읽는지" 확인할 때 사용한다.

## 2. 기본 용어

**MinIO**  
AWS S3와 호환되는 Object Storage 서버다. AWS S3가 아니어도 S3 API 규격을 사용하므로 `s3://bucket/key` 같은 URI로 접근할 수 있다.

**S3 API**  
파일 시스템처럼 디렉터리에 파일을 저장하는 방식이 아니라, `bucket` 안에 `object key`라는 이름으로 bytes object를 저장하고 읽는 API다. MinIO는 이 S3 API를 구현한다.

**Bucket**  
Object Storage의 최상위 저장 공간이다. 이 프로젝트에서는 예를 들어 `time-travel-summarization` bucket을 사용한다.

**Object Key**  
Bucket 내부의 객체 이름이다. 예를 들어 아래 URI에서 bucket은 `time-travel-summarization`, object key는 `trajectory/living_trajectory_1h_0_2s_parquet/manifest.json`이다.

```text
s3://time-travel-summarization/trajectory/living_trajectory_1h_0_2s_parquet/manifest.json
```

Object Storage에는 실제 디렉터리가 있는 것이 아니라 `/`가 포함된 key 문자열이 있을 뿐이다. 따라서 `trajectory/...`는 디렉터리처럼 보이지만 실제로는 object key prefix다.

**Endpoint**  
MinIO 서버 주소다. 현재 형태는 다음과 호환된다.

```text
https://api.minio.mobilex.kr
```

코드에서는 `https://`를 제거한 host를 MinIO client에 넘기고, `MINIO_SECURE=true`이면 HTTPS로 접속한다.

**Access Key / Secret Key**  
MinIO 인증 정보다. Git에 올리면 안 되며 `.env` 또는 OS 환경변수로만 관리한다.

**URI**  
저장 위치를 표현하는 문자열이다. 이 프로젝트는 `s3://`, `minio://`, `file://`, 일반 local path를 구분한다.

**Manifest**  
큰 데이터셋의 목차 파일이다. `manifest.json` 안에 데이터 시간 범위, row 수, object id 목록, chunk 목록이 들어 있다. extension은 먼저 manifest만 읽고 필요한 chunk를 찾아간다.

**Chunk**  
큰 궤적 데이터를 시간 구간별로 나눈 작은 파일이다. 예를 들어 `chunk_seconds=300`이면 5분 단위 chunk가 만들어진다.

**Parquet**  
컬럼 기반 데이터 파일 포맷이다. CSV보다 대용량 분석/저장에 유리하고 압축도 잘 된다. 이 프로젝트에서는 pyarrow로 parquet을 읽고 쓴다.

**pyarrow**  
Python에서 Apache Arrow와 Parquet을 다루는 라이브러리다. Omniverse Kit 안에서 native binary를 로드하므로 Python ABI가 맞는지 검증이 필요하다.

**Cache**  
이미 읽은 chunk를 메모리에 잠시 보관하는 구조다. 같은 시간대로 다시 이동하거나 근처 시간대를 재생할 때 MinIO에서 다시 다운로드하지 않아도 된다.

**Prefetch**  
현재 chunk를 재생하는 동안 다음 chunk를 background thread에서 미리 읽어두는 방식이다. chunk 경계를 넘을 때 지연을 줄이기 위해 사용한다.

**LRU**  
Least Recently Used의 약자다. cache가 꽉 찼을 때 가장 오래 사용하지 않은 chunk를 제거하는 방식이다. 이 프로젝트는 `OrderedDict`로 유사 LRU cache를 구현한다.

## 3. 설정 파일 구조

### 3.1 `.env`

MinIO 접속 정보와 현재 사용할 dataset 이름은 `.env`에서 관리한다. 실제 credential 값은 문서나 Git에 쓰지 않는다.

```bash
MINIO_ENDPOINT=https://api.minio.mobilex.kr
MINIO_ACCESS_KEY=<your-access-key>
MINIO_SECRET_KEY=<your-secret-key>
MINIO_SECURE=true
MINIO_BUCKET=time-travel-summarization

DATA_PATH=./data/living_trajectory_1min_0.2s.csv

# 사용할 lake dataset 하나만 활성화한다.
# LAKE_DATASET=living_trajectory_1min_0_2s_parquet
LAKE_DATASET=living_trajectory_1h_0_2s_parquet
# LAKE_DATASET=living_trajectory_6h_0_2s_parquet
```

`config.json`은 `${MINIO_BUCKET}`, `${LAKE_DATASET}` 같은 값을 읽어서 실제 URI로 확장한다. 이 확장은 [config.py](../gist/netai/time_travel_summarization/app/config.py)에서 처리한다.

```python
def _expand_env(value: str) -> str:
    if not isinstance(value, str):
        return value
    return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
```

### 3.2 `config.json`

현재 MinIO 관련 주요 설정은 [config.json](../gist/netai/time_travel_summarization/config.json)에 있다.

```json
{
  "output_root_uri": "s3://${MINIO_BUCKET}/timetravel",
  "event_list_uri": "s3://${MINIO_BUCKET}/timetravel/event_list",
  "video_output_uri": "s3://${MINIO_BUCKET}/timetravel/video",

  "lake": {
    "enabled": false,
    "direct_data_uri": "",
    "manifest_uri": "s3://${MINIO_BUCKET}/trajectory/${LAKE_DATASET}/manifest.json",
    "cache_chunks": 4,
    "prefetch_ahead": 2
  }
}
```

각 항목의 의미는 다음과 같다.

`output_root_uri`  
Data Lake 모드에서 중간 산출물을 저장할 기본 root URI다.

`event_list_uri`  
event processing 결과인 event list를 저장하거나 읽는 위치다.

`video_output_uri`  
비디오 캡처 결과를 MinIO에 저장할 위치다.

`lake.manifest_uri`  
Data Lake에서 궤적 데이터를 읽기 위한 manifest 위치다. `<dataset>` 전체 경로가 아니라 dataset 이름만 `LAKE_DATASET`에 넣는 것이 안전하다.

`lake.direct_data_uri`  
테스트용 직접 데이터 URI다. 값이 있으면 manifest를 보지 않고 해당 CSV/parquet 파일을 직접 읽는다. 예를 들어 `s3://time-travel-summarization/trajectory/living_trajectory_1min_0.2s.csv`처럼 설정하면 Data Lake 버튼을 눌렀을 때 MinIO CSV를 바로 읽는다. 대용량 운영 경로는 아니며, MinIO 접속/단일 파일 읽기 검증용이다.

올바른 예:

```bash
LAKE_DATASET=living_trajectory_1h_0_2s_parquet
```

잘못된 예:

```bash
LAKE_DATASET=living_trajectory_1h_0_2s_parquet/manifest.json
```

위처럼 넣으면 최종 URI가 `.../manifest.json/manifest.json`처럼 잘못 만들어질 수 있다.

## 4. `s3://`가 MinIO로 연결되는 이유

이 프로젝트에서 `s3://`는 반드시 AWS S3를 뜻하지 않는다. `s3://`는 "S3-compatible Object Storage URI"라는 뜻이고, 실제 접속 대상은 `.env`의 `MINIO_ENDPOINT`다.

URI backend 선택은 [storage/factory.py](../gist/netai/time_travel_summarization/storage/factory.py)에서 처리한다.

```python
def from_uri(uri: str) -> StorageAdapter:
    parsed = urlparse(uri)
    scheme = parsed.scheme.lower()
    global _LOCAL, _MINIO
    if scheme in ("s3", "minio"):
        if _MINIO is None:
            _MINIO = MinioAdapter()
        return _MINIO
    if scheme in ("file", ""):
        if _LOCAL is None:
            _LOCAL = LocalAdapter()
        return _LOCAL
    raise ValueError(f"Unsupported URI scheme: {scheme!r} (uri={uri!r})")
```

즉:

```text
s3://... 또는 minio://... -> MinioAdapter
file://... 또는 일반 local path -> LocalAdapter
```

MinIO client 생성은 [storage/minio_adapter.py](../gist/netai/time_travel_summarization/storage/minio_adapter.py)에서 한다.

```python
endpoint = config["MINIO_ENDPOINT"]
host = endpoint.split("://", 1)[1] if "://" in endpoint else endpoint
secure = config.get("MINIO_SECURE", "false").lower() == "true"

self._client = Minio(
    host,
    access_key=config["MINIO_ACCESS_KEY"],
    secret_key=config["MINIO_SECRET_KEY"],
    secure=secure,
    region=region,
)
```

`MINIO_ENDPOINT=https://api.minio.mobilex.kr`, `MINIO_SECURE=true` 조합이면 HTTPS MinIO endpoint와 호환된다.

## 5. MinIO Adapter가 제공하는 기능

[MinioAdapter](../gist/netai/time_travel_summarization/storage/minio_adapter.py)는 extension 내부에서 MinIO를 직접 다루는 계층이다.

```python
class MinioAdapter(StorageAdapter):
    def open_read(self, uri: str) -> BinaryIO:
        bucket, key = self._parse_uri(uri)
        return _MinioStream(self._get_client().get_object(bucket, key))

    def put_bytes(self, uri: str, data: bytes, content_type: Optional[str] = None) -> None:
        bucket, key = self._parse_uri(uri)
        self._get_client().put_object(
            bucket,
            key,
            io.BytesIO(data),
            length=len(data),
            content_type=content_type or "application/octet-stream",
        )

    def stat(self, uri: str) -> ObjectInfo:
        bucket, key = self._parse_uri(uri)
        obj = self._get_client().stat_object(bucket, key)
        return ObjectInfo(
            uri=f"s3://{bucket}/{key}",
            size=obj.size,
            last_modified=obj.last_modified.isoformat() if obj.last_modified else None,
            etag=obj.etag,
        )
```

주요 용도:

`open_read(uri)`  
MinIO object를 bytes stream으로 읽는다. CSV, parquet, manifest 읽기에 사용된다.

`put_bytes(uri, data)`  
메모리의 bytes를 MinIO object로 업로드한다. parquet chunk와 manifest를 올릴 때 사용된다.

`put_file(uri, local_path)`  
local 파일을 그대로 업로드한다.

`list_prefix(uri_prefix)`  
특정 prefix 하위 object 목록을 조회한다.

`exists(uri)` / `stat(uri)`  
object 존재 여부와 크기, 수정 시각, etag를 확인한다.

## 6. Data Lake 레이아웃

Data Lake dataset은 다음 형태로 저장된다.

```text
s3://time-travel-summarization/trajectory/<dataset>/
  manifest.json
  chunk_1735689600000.parquet
  chunk_1735689900000.parquet
  chunk_1735690200000.parquet
  ...
```

레이아웃 규칙은 [playback/lake_common.py](../gist/netai/time_travel_summarization/playback/lake_common.py)에 정의되어 있다.

```python
MANIFEST_NAME = "manifest.json"

def chunk_object_key(start_ms: int, fmt: str) -> str:
    return f"chunk_{start_ms:013d}.{fmt}"

def manifest_uri(dataset_uri: str) -> str:
    return join_uri(dataset_uri, MANIFEST_NAME)
```

`manifest.json` 예시는 다음과 같은 구조다.

```json
{
  "version": 1,
  "dataset": "living_trajectory_1h_0.2s",
  "hz": 5.0,
  "chunk_seconds": 300,
  "format": "parquet",
  "objids": ["obj001", "obj002", "obj003", "obj004"],
  "start": "2025-01-01 00:00:00.000",
  "end": "2025-01-01 01:00:00.000",
  "rows": 72004,
  "coord_min": [206.0, 89.5, -2879.0],
  "coord_max": [1554.0, 200.0, -1258.0],
  "chunks": [
    {
      "key": "chunk_1735689600000.parquet",
      "start": "2025-01-01 00:00:00.000",
      "end": "2025-01-01 00:04:59.800",
      "rows": 6000
    }
  ]
}
```

`hz=5.0`은 1초에 5개 sample, 즉 0.2초 간격이라는 뜻이다. object가 4개면 5분 chunk 하나에는 보통 `300초 * 5Hz * 4객체 = 6000 rows`가 들어간다.

## 7. 데이터 생성

현재 테스트용 1시간/6시간 데이터는 기존 `living_trajectory_1min_0.2s.csv`와 같은 좌표 범위 안에서 움직이도록 [tools/generate_living_trajectory.py](../tools/generate_living_trajectory.py)로 생성했다.

좌표 범위:

```python
X_RANGE = (206.0, 1554.0)
Y_RANGE = (89.5, 200.0)
Z_RANGE = (-2879.0, -1258.0)
```

1시간 CSV 생성:

```bash
python3 tools/generate_living_trajectory.py \
  --duration-hours 1 \
  --output gist/netai/time_travel_summarization/artifacts/trajectory/living_trajectory_1h_0.2s.csv \
  --objects 4 \
  --interval 0.2 \
  --seed 101
```

6시간 CSV 생성:

```bash
python3 tools/generate_living_trajectory.py \
  --duration-hours 6 \
  --output gist/netai/time_travel_summarization/artifacts/trajectory/living_trajectory_6h_0.2s.csv \
  --objects 4 \
  --interval 0.2 \
  --seed 106
```

생성된 local artifact 위치:

```text
gist/netai/time_travel_summarization/artifacts/trajectory/
  living_trajectory_1h_0.2s.csv
  living_trajectory_6h_0.2s.csv
```

프로젝트의 local artifact root는 [app/paths.py](../gist/netai/time_travel_summarization/app/paths.py)에 정의된 다음 위치다.

```text
gist/netai/time_travel_summarization/artifacts
```

## 8. MinIO에 parquet dataset 넣기

CSV를 그대로 MinIO에 올리는 것도 가능하지만, 대용량 재생용으로는 CSV 1개보다 parquet chunk + manifest 방식이 더 적합하다.

적재 CLI는 [tools/lake_ingest.py](../tools/lake_ingest.py)다.

1시간 dataset 업로드:

```bash
conda run -p /tmp/tts-lake-conda python tools/lake_ingest.py \
  --source gist/netai/time_travel_summarization/artifacts/trajectory/living_trajectory_1h_0.2s.csv \
  --dest s3://time-travel-summarization/trajectory/living_trajectory_1h_0_2s_parquet \
  --format parquet \
  --chunk-seconds 300 \
  --hz 5
```

6시간 dataset 업로드:

```bash
conda run -p /tmp/tts-lake-conda python tools/lake_ingest.py \
  --source gist/netai/time_travel_summarization/artifacts/trajectory/living_trajectory_6h_0.2s.csv \
  --dest s3://time-travel-summarization/trajectory/living_trajectory_6h_0_2s_parquet \
  --format parquet \
  --chunk-seconds 300 \
  --hz 5
```

`lake_ingest.py` 내부에서는 CSV rows를 읽고, 시간순으로 정렬하고, `chunk_seconds` 기준으로 나눈 뒤, 각 chunk를 parquet bytes로 인코딩해서 MinIO에 올린다.

핵심 코드:

```python
def ingest_rows(rows, dataset_uri, *, chunk_seconds=60, fmt="csv", hz=None, dataset=""):
    rows = sorted(rows, key=lambda r: r["timestamp"])

    for idx in sorted(buckets):
        crows = buckets[idx]
        start_ms = to_epoch_ms(TrajectoryRepository.parse_timestamp(c_start))
        key = chunk_object_key(start_ms, fmt)
        payload, content_type = _encode_chunk(crows, fmt)
        uri = join_uri(dataset_uri, key)
        from_uri(uri).put_bytes(uri, payload, content_type=content_type)

    muri = manifest_uri(dataset_uri)
    from_uri(muri).put_bytes(
        muri,
        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        content_type="application/json",
    )
```

현재 업로드된 dataset:

```text
s3://time-travel-summarization/trajectory/living_trajectory_1min_0_2s_parquet/manifest.json
s3://time-travel-summarization/trajectory/living_trajectory_1h_0_2s_parquet/manifest.json
s3://time-travel-summarization/trajectory/living_trajectory_6h_0_2s_parquet/manifest.json
```

검증된 row/chunk 수:

```text
1시간 dataset: rows=72004, chunks=13
6시간 dataset: rows=432004, chunks=73
```

chunk가 12개/72개가 아니라 13개/73개인 이유는 generator가 마지막 endpoint timestamp를 포함하기 때문이다. 예를 들어 1시간 데이터는 `00:00:00.000`부터 `01:00:00.000`까지 포함하므로 마지막 시점만 들어간 작은 chunk가 하나 더 생긴다.

## 9. Data Lake에서 데이터 가져오기

Data Lake 읽기 전용 repository는 [playback/lake_repository.py](../gist/netai/time_travel_summarization/playback/lake_repository.py)의 `LakeTrajectoryRepository`다.

생성:

```python
repo_factory = lambda: LakeTrajectoryRepository(
    cache_chunks=int(lake_cfg.get("cache_chunks", 4)),
    prefetch_ahead=int(lake_cfg.get("prefetch_ahead", 1)),
)
```

manifest 로드:

```python
def load_from_uri(self, uri: str) -> bool:
    self.clear()
    muri = uri if uri.lower().endswith(MANIFEST_NAME) else manifest_uri(uri)
    self._dataset_uri = dataset_uri_from_manifest(muri)
    adapter = from_uri(muri)
    with adapter.open_read(muri) as stream:
        manifest = json.loads(stream.read().decode("utf-8"))
    self._load_manifest(manifest)

    if not self._chunks:
        return False
    self._start_prefetch()
    self._activate(0)
    return True
```

여기서 중요한 점은 `load_from_uri()`가 전체 parquet 파일을 모두 읽지 않는다는 것이다. 먼저 `manifest.json`만 읽고, 첫 화면을 바로 그릴 수 있도록 첫 chunk만 동기 로드한다.

## 10. UI에서 Data Lake 버튼을 누르면 생기는 일

UI 버튼 처리 흐름은 [ui/main_window.py](../gist/netai/time_travel_summarization/ui/main_window.py)와 [app/facade.py](../gist/netai/time_travel_summarization/app/facade.py)에 걸쳐 있다.

```text
Data Lake 버튼 클릭
  -> TimeTravelWindow._request_source_switch("lake")
  -> 다음 update tick에서 _apply_source_switch()
  -> TimeTravelCore.set_data_source("lake")
  -> TimeTravelCore._activate_data_source("lake")
  -> LakeTrajectoryRepository.load_from_uri(manifest_uri)
  -> object id 목록 확인
  -> astronaut 재생성
```

Data Lake 전환을 UI 버튼 draw stack 안에서 바로 처리하지 않고 update tick으로 미루는 이유는 안정성 때문이다. 이전에 crash stack이 `omni.ui Button::_drawContent` 근처에서 발생했기 때문에, MinIO/pyarrow처럼 무거운 I/O는 UI draw 중에 실행하지 않도록 분리했다.

성공 시 log 예:

```text
[TimeTravel] Data Lake button clicked
[TimeTravel] Applying source switch on update tick: lake
[TimeTravel] Lake manifest loaded: format=parquet, rows=72004, chunks=13, objects=4
[TimeTravel] Lake chunk load start: idx=0 uri=s3://...
[TimeTravel] Lake chunk load done: idx=0 timestamps=1500
[TimeTravel] Regenerated 4 astronauts from loaded data
[TimeTravel] Data Lake activation complete
```

`timestamps=1500`은 첫 chunk 안의 unique timestamp 수다. 5분 chunk, 5Hz이면 `300 * 5 = 1500` timestamps가 된다.

## 11. Cache와 Prefetch 구조

`LakeTrajectoryRepository`는 `_cache`에 이미 읽은 chunk를 저장한다.

```python
self._cache: "OrderedDict[int, _Chunk]" = OrderedDict()
self._cache_chunks = max(int(cache_chunks), min_cache)
```

chunk 조회 흐름:

```python
def _do_lookup(self, timestamp: datetime.datetime):
    idx = self._chunk_for_time(timestamp)
    if idx != self._active_idx:
        self._activate(idx)
    self._schedule_prefetch(idx)
    return super()._do_lookup(timestamp)
```

1. 현재 timestamp가 어느 chunk에 속하는지 계산한다.
2. 현재 active chunk와 다르면 해당 chunk를 로드한다.
3. 현재 chunk 기준으로 다음 chunk들을 prefetch queue에 넣는다.
4. 실제 timestamp lookup은 기존 `TrajectoryRepository`의 floor lookup 로직을 재사용한다.

cache hit/miss:

```python
def _ensure_loaded(self, idx: int) -> _Chunk:
    with self._cache_lock:
        if idx in self._cache:
            self._cache.move_to_end(idx)
            self.stats["cache_hits"] += 1
            return self._cache[idx]
        self.stats["cache_misses"] += 1

    chunk = self._load_chunk(idx)
    self._cache_put(idx, chunk)
    return chunk
```

cache eviction:

```python
def _cache_put(self, idx: int, chunk: _Chunk):
    with self._cache_lock:
        self._cache[idx] = chunk
        self._cache.move_to_end(idx)
        while len(self._cache) > self._cache_chunks:
            for k in list(self._cache.keys()):
                if k != self._active_idx:
                    del self._cache[k]
                    break
```

현재 재생 중인 active chunk는 제거하지 않는다. 나머지 중 오래된 chunk부터 제거한다.

prefetch:

```python
def _schedule_prefetch(self, active_idx: int):
    targets = [active_idx + d for d in range(1, self._prefetch_ahead + 1)]
    targets.append(active_idx - 1)
    for t in targets:
        if t not in self._cache and t not in self._pf_inflight:
            self._pf_inflight.add(t)
            self._pf_queue.put(t)
```

`prefetch_ahead=2`이면 현재 chunk 다음 2개를 미리 읽는다. `active_idx - 1`도 넣는 이유는 사용자가 time slider나 Go To Time으로 뒤로 이동할 수 있기 때문이다.

## 12. 일반 로드와 Lake 로드의 차이

일반 CSV/parquet 로드는 [TrajectoryRepository](../gist/netai/time_travel_summarization/playback/trajectory_repository.py)가 담당한다.

```python
def load_from_uri(self, uri: str) -> bool:
    self.clear()
    if not uri.lower().endswith((".csv", ".parquet")):
        return False

    self._data, self._timestamps = self._rows_to_data(self._read_rows(uri))
    return bool(self._timestamps)
```

이 방식은 파일 하나를 통째로 읽는다.

```python
def _read_rows(uri: str) -> List[dict]:
    adapter = from_uri(uri)
    with adapter.open_read(uri) as stream:
        raw = stream.read()
    if uri.lower().endswith(".csv"):
        return list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))
    import pyarrow.parquet as pq
    return pq.read_table(io.BytesIO(raw)).to_pylist()
```

따라서 작은 CSV 테스트에는 좋지만, 1시간/6시간 이상 대용량에는 Lake 방식이 더 낫다.

비교:

```text
Direct CSV/parquet load
  장점: 구조 단순, 테스트 쉬움
  단점: 전체 파일 다운로드 + 전체 parsing 필요

Manifest + parquet chunk lake load
  장점: 필요한 시간대 chunk만 다운로드, cache/prefetch 가능
  단점: ingest 과정과 manifest 관리 필요
```

## 13. Local artifact 위치

local 모드에서 생성되는 산출물은 extension 내부 `artifacts` 아래에 둔다.

```text
gist/netai/time_travel_summarization/artifacts/
  video/
  vlm_outputs/
  intermediate_results/
  event_list/
  trajectory/
  trace/
  benchmarks/
```

이 위치를 사용하는 이유는 프로젝트 산출물이 extension 밖에 흩어지지 않게 하기 위해서다.

Data Lake 모드에서는 config의 S3 URI를 사용한다.

```text
s3://time-travel-summarization/timetravel
s3://time-travel-summarization/timetravel/event_list
s3://time-travel-summarization/timetravel/video
s3://time-travel-summarization/timetravel/vlm_outputs
s3://time-travel-summarization/timetravel/intermediate_results
```

즉 UI에서 Local/Data Lake 선택에 따라 read source뿐 아니라 산출물 저장 위치도 달라진다.

현재 저장 라우팅은 다음과 같다.

```text
Local 모드:
  video capture        -> artifacts/video/
  VLM raw JSON         -> artifacts/vlm_outputs/
  intermediate events  -> artifacts/intermediate_results/
  event list           -> artifacts/event_list/

Data Lake 모드:
  video capture        -> video_output_uri/capture_*.mp4
  VLM raw JSON         -> output_root_uri/vlm_outputs/*.json
  intermediate events  -> output_root_uri/intermediate_results/*_intermediate.jsonl
  event list           -> output_root_uri/event_list/*_eventlist.jsonl
```

`TimeTravelCore.get_output_root_uri_for_active_mode()`와 `get_video_output_uri_for_active_mode()`는 현재 data source가 `lake`일 때만 URI를 반환한다. Local 모드에서는 `None`을 반환하므로 각 기능은 local artifacts 경로로 fallback한다.

## 14. 검증 방법

Omniverse 내부 Python에서 pyarrow/MinIO가 정상 동작하는지 확인하려면 [utils/pyarrow_abi_probe.py](../gist/netai/time_travel_summarization/utils/pyarrow_abi_probe.py)를 사용한다.

1시간 manifest 검증:

```python
SMOKE_LEVEL = 3
TEST_URI = "s3://time-travel-summarization/trajectory/living_trajectory_1h_0_2s_parquet/manifest.json"
```

6시간 manifest 검증:

```python
SMOKE_LEVEL = 3
TEST_URI = "s3://time-travel-summarization/trajectory/living_trajectory_6h_0_2s_parquet/manifest.json"
```

검증에서 확인해야 할 핵심:

```text
pyarrow import: OK
ABI CHECK: no obvious mismatch from filename
manifest format: parquet
manifest rows: 72004 또는 432004
manifest chunks: 13 또는 73
first chunk uri: s3://...
parquet rows: 6000
sample rows: [...]
SMOKE_LEVEL=3 complete.
```

실제 Data Lake 버튼 검증에서는 다음 log가 중요하다.

```text
Lake manifest loaded
Lake chunk load done
Regenerated N astronauts from loaded data
Data Lake activation complete
Data Lake loaded: objects=N, astronauts=N
```

특히 `Regenerated N astronauts`가 없으면 데이터는 읽었지만 scene object 생성까지 이어지지 않은 상태일 수 있다.

## 15. 자주 발생하는 문제와 원인

### 15.1 `minio package not installed`

증상:

```text
minio package not installed; pip install minio
```

원인:

Omniverse Kit Python 환경에 `minio` package가 없다.

대응:

extension dependency 또는 Omniverse Python 환경에 `minio`가 설치되어야 한다. 일반 시스템 Python에 설치해도 Kit 내장 Python에서 보이지 않으면 효과가 없다.

### 15.2 `NoSuchKey`

원인 후보:

```text
1. LAKE_DATASET 이름이 잘못됨
2. manifest.json이 실제로 업로드되지 않음
3. bucket 또는 object key 오타
4. LAKE_DATASET에 manifest.json까지 넣어서 URI가 중복됨
```

확인할 URI 예:

```text
s3://time-travel-summarization/trajectory/living_trajectory_1h_0_2s_parquet/manifest.json
```

### 15.3 Data Lake 버튼을 눌러도 astronaut가 안 생김

확인할 log:

```text
Lake manifest loaded
Lake chunk load done
Regenerated ... astronauts
Data Lake activation complete
```

`Lake manifest loaded`만 있고 `Regenerated ... astronauts`가 없다면 `TimeTravelCore._activate_data_source()` 이후 object id 처리나 scene 생성 경로를 봐야 한다.

### 15.4 Data Lake에서 직접 CSV 테스트와 manifest 테스트가 헷갈림

`lake.direct_data_uri`가 설정되어 있으면 Data Lake 버튼을 눌러도 manifest/parquet 경로가 아니라 직접 CSV/parquet 파일을 읽는다. 이때 log에는 다음처럼 직접 URI가 표시된다.

```text
[TimeTravel] Lake mode test direct data URI: s3://...
```

대용량 parquet manifest 경로를 검증하려면 `direct_data_uri`를 비우고 `lake.manifest_uri`를 사용해야 한다.

### 15.5 pyarrow crash

이전에 의심했던 가설은 "Omniverse Python 3.10이 pyarrow cp312 native module을 로드한다"는 ABI mismatch였다. 그러나 실제 probe 결과는 Omniverse Kit Python이 `3.12.12`, pyarrow native file도 `cp312`였고, smoke level 1/2/3가 통과했다.

따라서 현재 확인된 상태에서는 ABI mismatch가 1차 원인은 아니다. 그래도 pyarrow 관련 crash가 다시 생기면 다음 순서로 확인한다.

```text
1. SMOKE_LEVEL=0: Python runtime, sys.path, pyarrow native file 확인
2. SMOKE_LEVEL=1: Arrow table 생성
3. SMOKE_LEVEL=2: parquet write/read
4. SMOKE_LEVEL=3: 실제 MinIO manifest와 parquet chunk 읽기
```

### 15.6 재생 중 버벅임

원인 후보:

```text
1. chunk_seconds가 너무 작아 chunk 전환이 너무 자주 발생
2. cache_chunks가 너무 작아 prefetch한 chunk가 바로 제거됨
3. 네트워크 지연
4. pyarrow decode 시간이 UI update보다 길어짐
```

현재 설정:

```json
{
  "cache_chunks": 4,
  "prefetch_ahead": 2
}
```

큰 데이터에서 더 안정적으로 만들려면 `cache_chunks`를 6 또는 8로 늘려볼 수 있다. 단, 메모리 사용량은 증가한다.

## 16. 면접에서 설명할 수 있는 포인트

이 프로젝트에서 구현한 데이터레이크 연동은 단순히 "MinIO에 파일을 올렸다"가 아니라 다음 요소들을 포함한다.

1. **Storage abstraction**

   `from_uri()`로 `file://`, local path, `s3://`, `minio://`를 같은 인터페이스로 다룬다. 덕분에 상위 로직은 저장소가 local인지 MinIO인지 덜 신경 쓴다.

2. **S3-compatible MinIO adapter**

   MinIO endpoint, secure flag, access key, secret key를 `.env`에서 읽고, `open_read`, `put_bytes`, `stat`, `list_prefix` 같은 공통 기능을 제공한다.

3. **Manifest 기반 대용량 데이터 구조**

   큰 궤적 데이터를 시간 단위 parquet chunk로 나누고, manifest에 index를 저장한다. 전체 데이터 scan 없이 시간 범위와 chunk 목록을 알 수 있다.

4. **Lazy loading**

   Data Lake activation 시 전체 dataset이 아니라 manifest와 첫 chunk만 읽는다. 실제 재생 시간이 이동하면 필요한 chunk만 추가로 읽는다.

5. **Cache / Prefetch**

   최근 chunk를 메모리에 보관하고, 다음 chunk를 background로 미리 읽어서 대용량 원격 데이터 재생의 지연을 줄인다.

6. **UI 안정성 개선**

   MinIO/pyarrow 로드 작업을 UI draw callback에서 바로 실행하지 않고 update tick으로 넘겨 crash 가능성을 낮췄다.

7. **검증 도구**

   Omniverse Kit 내부 Python에서 pyarrow ABI, parquet read/write, 실제 MinIO object read를 단계별 smoke test로 확인했다.

면접용 한 문장 요약:

```text
로컬 CSV 기반 재생 구조를 S3-compatible MinIO 데이터레이크 구조로 확장했고,
manifest + parquet chunk + lazy loading + cache/prefetch를 적용해
1시간/6시간 규모의 trajectory 데이터를 전체 로드 없이 시간대별로 가져오도록 만들었습니다.
```

## 17. 현재 테스트 데이터셋 목록

```text
1분 원본 기반 parquet:
  s3://time-travel-summarization/trajectory/living_trajectory_1min_0_2s_parquet/manifest.json

1시간 parquet:
  s3://time-travel-summarization/trajectory/living_trajectory_1h_0_2s_parquet/manifest.json
  rows=72004, chunks=13

6시간 parquet:
  s3://time-travel-summarization/trajectory/living_trajectory_6h_0_2s_parquet/manifest.json
  rows=432004, chunks=73
```

`.env`에서 사용할 dataset만 하나 활성화하면 된다.

```bash
LAKE_DATASET=living_trajectory_6h_0_2s_parquet
```

그러면 `config.json`의 다음 template이 실제 manifest URI로 확장된다.

```json
"manifest_uri": "s3://${MINIO_BUCKET}/trajectory/${LAKE_DATASET}/manifest.json"
```

최종 URI:

```text
s3://time-travel-summarization/trajectory/living_trajectory_6h_0_2s_parquet/manifest.json
```

## 18. 앞으로 개선할 수 있는 점

1. **chunk metadata 강화**  
   각 chunk의 byte size, checksum, row group 수를 manifest에 넣으면 검증과 장애 진단이 쉬워진다.

2. **시간 범위 부분 로드 최적화**  
   UI의 Load Time Range와 manifest chunk index를 연결해 선택한 시간 범위에 해당하는 chunk만 준비하도록 더 명확히 만들 수 있다.

3. **Parquet row group 활용**  
   현재는 chunk parquet 파일 하나를 통째로 읽는다. 더 커지면 parquet 내부 row group과 predicate pushdown을 활용할 수 있다.

4. **MinIO 연결 상태 UI 표시**  
   Data Lake 버튼 근처에 endpoint, dataset, manifest load status, cache hit/miss를 표시하면 운영성이 좋아진다.

5. **자동 dataset 목록 조회**  
   `list_prefix()`를 사용하면 bucket 안의 `trajectory/` prefix를 조회해서 UI에서 dataset을 선택하게 만들 수 있다.

> **갱신 이력 (2026-07)** — 위 개선안 중 일부는 이후 구현되었다.
> - **2번(시간 범위 부분 로드)**: `LakeTrajectoryRepository`의 manifest 청크 인덱스 +
>   윈도우 로딩 + 프리페치로 구현 완료(§ 데이터 레이크 재연, `playback/lake_repository.py`).
> - **5번(list_prefix)**: 이후 실사용 중 **폴더 누출 버그** 발견·수정 — minio SDK는 비재귀
>   목록에서 하위 '폴더'를 dir 항목(`size=None`)으로 반환하는데, 과거 가드가 `size == 0`
>   비교라 `None == 0`이 False가 되어 폴더가 파일처럼 새어 나왔다. `is_dir`/`endswith("/")`
>   검사로 교체(`storage/minio_adapter.py` `list_prefix`, minIO고도화일지 #8). 정적 게이트의
>   pytest 최초 실행으로 잡힌 실버그.
> - 이후 이벤트 인덱스가 `vlm_events/` prefix에 append하며 이 목록 조회를 실제로 활용한다.
