# storage — 로컬 파일시스템과 minIO(S3 호환)를 같은 인터페이스로 여는 계층

## 역할과 위치

`gist/netai/time_travel_summarization/storage/`는 확장의 나머지 코드(재생 궤적 로드, VLM 출력 저장, 이벤트 인덱스 적재, 캡처 사이드카(sidecar — 캡처 영상 옆에 같은 이름으로 남기는 메타 JSON) 기록 등)가 데이터가 로컬 디스크에 있는지 minIO 버킷에 있는지 신경 쓰지 않고 URI 하나로 읽고 쓸 수 있게 하는 계층이다. `StorageAdapter`(`base.py`)가 추상 인터페이스를 정의하고, `LocalAdapter`(`file://` 또는 스킴 없는 경로)와 `MinioAdapter`(`s3://`, `minio://`)가 각각 구현하며, `factory.from_uri(uri)`가 URI 스킴만 보고 어느 어댑터를 반환할지 고른다. 호출부는 언제나 `from_uri(uri)` 하나만 알면 되고, 어댑터 클래스를 직접 임포트하지 않는다.

## 구성 파일

| 파일 | 책임 | 핵심 심볼 |
|---|---|---|
| `__init__.py` | 패키지 공개 표면 — 아래 심볼을 재수출 | `ObjectInfo`, `StorageAdapter`, `LocalAdapter`, `MinioAdapter`, `from_uri`, `normalize_source` |
| `base.py` | 어댑터가 구현해야 하는 추상 인터페이스와 목록 조회 결과 값 객체 | `StorageAdapter`(ABC), `ObjectInfo` |
| `local_adapter.py` | `file://` 또는 스킴 없는 경로를 다루는 어댑터. 쓰기는 임시 파일 경유 원자적 교체 | `LocalAdapter` |
| `minio_adapter.py` | minIO/S3 호환 버킷을 다루는 어댑터. `minio` 파이썬 패키지에 지연 의존 | `MinioAdapter` |
| `factory.py` | URI 스킴으로 어댑터를 고르는 단일 진입점. 어댑터 인스턴스를 스킴별로 1개씩만 유지 | `from_uri` |
| `normalize.py` | 사용자가 minIO 콘솔에서 복사한 `버킷/키` 형태 문자열에 `s3://` 접두를 붙이는 정규화 | `normalize_source` |

## 동작 흐름

### URI로 어댑터를 고르는 경로

호출부는 `from gist.netai.time_travel_summarization.storage import from_uri`로 `from_uri(uri)`를 호출한다(`playback/trajectory_repository.py`, `playback/lake_repository.py`, `playback/lake_common.py`, `events/event_index.py`, `events/summary_service.py`, `app/capture_service.py`, `vlm_client/core.py`, `utils/ingest_trajectory.py`, `utils/build_lake_perf_dataset.py`, `automation/generate_episodes.py`, `automation/replay_range.py`, `video_capture/realtime_capture.py`, `video_capture/movie_capture.py` 등이 이 경로를 쓴다). `factory.from_uri`는 `urlparse(uri).scheme`을 소문자로 비교해 `"s3"`/`"minio"`면 `MinioAdapter`, `"file"`이거나 스킴이 없으면(빈 문자열) `LocalAdapter`를 반환하고, 그 외 스킴은 `ValueError`를 던진다.

어댑터 인스턴스는 프로세스 안에서 스킴별로 하나만 만들어진다 — 모듈 전역 `_LOCAL`/`_MINIO`와 `threading.Lock` `_lock`으로 더블체크 락(double-checked locking) 패턴을 써서, 여러 스레드가 동시에 처음 `from_uri`를 불러도 어댑터가 두 번 생성되지 않는다. `MinioAdapter`는 생성 시점에는 `minio.Minio` 클라이언트를 만들지 않고(`self._client = None`), 실제 I/O 메서드가 처음 호출될 때 `_get_client()`가 별도의 `_client_lock`으로 다시 더블체크 락을 걸어 클라이언트를 지연 생성한다 — 이 지연 때문에 `MINIO_*` 환경변수가 없어도 `MinioAdapter()` 생성 자체는 실패하지 않고, 실제 요청 시점에 `RuntimeError`가 난다.

### `LocalAdapter` — 원자적 쓰기

`put_bytes`/`put_file`은 둘 다 같은 패턴을 쓴다. `_make_tmp_path(target)`이 `tempfile.mkstemp(prefix=f"{target.name}.", suffix=".tmp", dir=target.parent)`로 대상과 같은 디렉터리에 유니크한 임시 파일을 만들고, 데이터를 그 임시 파일에 다 쓴 뒤 `os.replace(tmp_path, target)`로 교체한다. 같은 디렉터리에 임시 파일을 두는 이유는 `os.replace`의 원자성이 같은 파일시스템 안에서만 보장되기 때문이고, 파일명에 유니크 접미사를 붙이는 이유는 같은 대상 키에 동시에 여러 쓰기가 들어와도(`tests/test_storage_local.py`의 `test_concurrent_put_bytes_to_same_key_leaves_no_tmp_residue`가 검증) 서로 다른 임시 파일을 써서 경합하지 않게 하기 위함이다 — 마지막에 `os.replace`한 쓰기가 이기고, 나머지는 `finally` 블록에서 자신의 임시 파일만 지운다.

`_path_from_uri`는 `file://` URI를 `urllib.request.url2pathname`으로 변환한다(윈도우 `file:///C:/Users/foo` → `C:\Users\foo`를 올바르게 처리하고, POSIX에서는 no-op). 스킴이 없는 문자열은 그냥 `Path(uri)`로 취급하고, 상대 경로면 `resolve()`로 절대 경로화한다. `list_prefix`는 대상이 파일이면 그 파일 하나만, 디렉터리면 `recursive` 여부에 따라 `iterdir()` 또는 `rglob("*")`로 파일만(디렉터리 제외) 정렬해 나열한다.

### `MinioAdapter` — 설정 로드와 URI 파싱

`_load_config()`는 `MINIO_ENDPOINT`/`MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY`(필수)와 `MINIO_SECURE`/`MINIO_REGION`(선택)을 읽는데, 읽는 순서는 `os.environ.get(key, env_file.get(key))` — 프로세스 환경변수가 있으면 그것을 쓰고, 없을 때만 확장 루트의 `.env` 파일(`storage/minio_adapter.py`의 부모의 부모, 즉 `time_travel_summarization/.env`)에서 채운다. 필수 키 중 하나라도 없으면 `RuntimeError(f"Missing MinIO configuration: ...")`를 던진다. `.env` 파일 형식은 `KEY=value` 한 줄씩(빈 줄과 `#` 주석 무시), 값의 앞뒤 따옴표(`"`/`'`)는 벗겨낸다.

`_parse_uri(uri)`는 `s3://bucket/key/path` 형태만 받는다 — `urlparse`로 `netloc`을 버킷, `path.lstrip("/")`를 키로 쓴다. 버킷이 없으면 `ValueError`, 그리고 `open_read`/`put_bytes`/`put_file`/`stat`처럼 특정 오브젝트를 가리켜야 하는 연산은 `_require_object_key`가 키까지 비어있지 않은지 추가로 검증한다(버킷만 있고 키가 없으면 `ValueError`). `exists(uri)`만 예외다 — 키가 없으면(버킷 루트 URI) `stat_object` 대신 `bucket_exists(bucket)`으로 버킷 자체의 존재를 답한다.

`list_prefix`는 minio SDK의 `list_objects(bucket, prefix=key_prefix, recursive=recursive)`를 그대로 감싸되, 비재귀 목록에서 SDK가 하위 "폴더"를 `size=None`이고 `object_name`이 `/`로 끝나는 항목으로 반환하는 것을 걸러낸다(`obj.is_dir` 또는 `object_name.endswith("/")`인 항목은 건너뜀 — 예전에는 `size == 0` 비교를 썼는데 `None == 0`이 `False`라 폴더가 파일처럼 새어 나온 실측 버그가 있었다).

`open_read`가 반환하는 `_MinioStream`은 minio SDK의 `HTTPResponse`를 감싸 컨텍스트 매니저로 만든다 — `close()`에서 `response.close()`와 `response.release_conn()`을 함께 호출해 커넥션 풀에 반드시 반환한다.

### `normalize_source` — 사용자 입력 정규화

`events/window.py`의 `_resolve_json_input`이 UI에 입력된 파일명/경로 문자열에 `storage.normalize_source`를 거친다. minIO 콘솔에서 오브젝트를 복사하면 `버킷명/키/경로` 형태(스킴 없음)로 붙여넣히는데, 이걸 그대로 `from_uri`에 넘기면 스킴이 없으니 `LocalAdapter`로 잘못 해석된다. `normalize_source(value, bucket=None)`은 다음 순서로 판정한다.

1. 문자열에 `"://"`가 있으면 이미 완전한 URI이므로 그대로 반환.
2. 윈도우 드라이브 문자로 시작(`X:\` 또는 `X:/`, 정규식 `^[A-Za-z]:[\\/]`) 또는 UNC 경로(`\\`로 시작)면 로컬 경로이므로 그대로 반환.
3. `bucket`이 인자로 안 주어졌으면 `os.environ.get("MINIO_BUCKET")`으로 대체(둘 다 없으면 이 규칙은 비활성).
4. 문자열이 `f"{bucket}/"`로 시작하면 `"s3://" + value`를 반환.
5. 그 외(맨 파일명 등)는 그대로 반환.

## 핵심 설계 결정과 규약

- **호출부는 어댑터 클래스를 직접 알지 못한다** — `from_uri`가 유일한 진입점이고, `LocalAdapter`/`MinioAdapter`를 직접 생성하는 코드는 이 디렉터리 밖에 없다(테스트 제외). 같은 청크 데이터셋을 저장소만 바꿔 열 수 있어야 저장소 종류에 따른 지연 차이를 대조군으로 측정할 수 있기 때문이다(`playback/README.md` 참조).
- **minio 패키지 임포트 실패는 확장 시작을 죽이지 않는다** — `minio_adapter.py` 상단의 `try/except`는 임포트 자체가 실패해도(`Minio = None`) 모듈 로드는 통과시키고, 실제로 `MinioAdapter()`를 생성하는 시점에 `RuntimeError`로 안내한다. 주석에 명시된 이유는 headless 환경의 pipapi에서 `PermissionError`가 나는데 이게 `ImportError`가 아니라서 일반적인 `except ImportError`로는 못 잡고 확장 전체가 죽었던 실측 사고 때문이다.
- **어댑터는 프로세스당 스킴별 싱글턴** — `factory.py`의 더블체크 락은 매 호출마다 새 클라이언트/커넥션을 만들지 않기 위한 것이다. `MinioAdapter`도 내부적으로 `Minio` 클라이언트를 지연 생성 후 재사용한다.
- **로컬 쓰기는 항상 임시 파일 경유** — `put_bytes`/`put_file` 둘 다 직접 대상 경로에 쓰지 않고 반드시 `mkstemp` + `os.replace`를 거친다. 쓰다 만 파일이 대상 경로에 남는 상황(그 사이 다른 프로세스가 읽으면 불완전한 데이터를 보게 됨)을 원천 차단한다.
- **`MinioAdapter`는 오브젝트 키가 필수인 연산과 아닌 연산을 구분한다** — `exists`는 키 없이 버킷 자체를 물을 수 있고 `list_prefix`는 키 없이 버킷 전체를 나열할 수 있으며, 나머지(`open_read`/`put_bytes`/`put_file`/`stat`)는 `_require_object_key`로 키 누락을 즉시 `ValueError`로 막는다.

## 사용법

### URI로 어댑터 얻기

```python
from gist.netai.time_travel_summarization.storage import from_uri

adapter = from_uri("file:///abs/path/to/file.csv")   # LocalAdapter
adapter = from_uri("s3://my-bucket/prefix/key.csv")  # MinioAdapter
```

### 읽기/쓰기

```python
adapter.put_bytes(uri, b"payload", content_type="application/json")
with adapter.open_read(uri) as stream:
    data = stream.read()

for info in adapter.list_prefix(f"{prefix}/", recursive=True):
    print(info.uri, info.size, info.last_modified, info.etag)

adapter.exists(uri)
adapter.stat(uri)  # 없으면 FileNotFoundError
```

### 사용자 입력 정규화

```python
from gist.netai.time_travel_summarization.storage import normalize_source

normalize_source("my-bucket/vlm_outputs/x.json")        # -> "s3://my-bucket/vlm_outputs/x.json"
normalize_source("s3://my-bucket/x.json")                 # -> 그대로
normalize_source("C:\\local\\path\\x.json")               # -> 그대로 (윈도우 경로)
```

### 환경 변수

| 변수 | 필수 여부 | 읽는 위치 | 의미 |
|---|---|---|---|
| `MINIO_ENDPOINT` | 필수 | `minio_adapter.py`(`_load_config`) | minIO 엔드포인트. `"scheme://host:port"` 형태면 `_get_client`가 `"://"` 뒤만 잘라 host로 쓴다 |
| `MINIO_ACCESS_KEY` | 필수 | `minio_adapter.py`(`_load_config`) | 액세스 키 |
| `MINIO_SECRET_KEY` | 필수 | `minio_adapter.py`(`_load_config`) | 시크릿 키 |
| `MINIO_SECURE` | 선택(기본 `"false"`) | `minio_adapter.py`(`_get_client`) | `"true"`면 HTTPS로 접속 |
| `MINIO_REGION` | 선택(기본 `"us-east-1"`) | `minio_adapter.py`(`_get_client`) | 클라이언트 리전 |
| `MINIO_BUCKET` | 선택 | `normalize.py`(`normalize_source`) | `bucket` 인자를 안 준 호출에서 "이 버킷 이름으로 시작하는 문자열엔 `s3://`를 붙인다" 판정에 쓰는 기본 버킷 |

값이 프로세스 환경변수에 있으면 그것을 우선하고, 없으면 확장 루트(`time_travel_summarization/.env`)의 `.env` 파일에서 읽는다(`.env`는 저장소에 커밋되지 않는 로컬 설정 파일).

### URI 형식

- 로컬: `file:///abs/path/to/file`(절대 경로) 또는 스킴 없는 경로(상대 경로는 `resolve()`로 절대화됨).
- minIO/S3: `s3://bucket/key/path` 또는 `minio://bucket/key/path`(둘 다 같은 어댑터로 라우팅).

## 테스트

| 테스트 파일 | 검증 대상 |
|---|---|
| `tests/test_storage_local.py` | `LocalAdapter`의 `put_bytes`/`open_read` 왕복, `put_file`+`stat` 크기 일치, `list_prefix`의 비재귀/재귀 목록 범위, `exists`/`stat`(누락 시 `FileNotFoundError`), 같은 키에 대한 동시 `put_bytes`가 임시 파일 잔여물을 남기지 않는지 |
| `tests/test_storage_minio.py` | `MinioAdapter`를 실제 minIO에 대해 검증하는 스모크 테스트 — `MINIO_ENDPOINT`/`MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY`/`MINIO_BUCKET` 중 하나라도 환경에 없으면 모듈 전체를 `pytest.skip`한다. `put_bytes`+`exists`+`open_read`+`stat`, `put_file`, `list_prefix`(비재귀가 하위 폴더를 안 새어 보내는지), `exists`/`stat`의 누락 케이스를 검증하고 각 테스트가 `finally`에서 만든 오브젝트를 정리한다 |

## 한계와 주의

- **`MinioAdapter`는 `minio` 파이썬 패키지가 설치돼 있지 않아도 임포트는 되지만, 인스턴스 생성 시점에 `RuntimeError`가 난다** — 지연 임포트가 아니라 모듈 상단 `try/except ImportError`로 `Minio = None`을 잡아두는 구조라, `minio` 미설치 환경에서 `s3://` URI를 여는 순간에야 에러가 드러난다.
- **`.env` 파일의 위치는 확장 루트 고정** — `MinioAdapter._env_path()`가 `Path(__file__).resolve().parent.parent`(즉 `storage/`의 부모의 부모)로 계산하므로, `storage/` 디렉터리를 다른 위치로 옮기면 `.env` 탐색 경로도 함께 깨진다.
- **`normalize_source`의 버킷 판정은 정확히 `f"{bucket}/"` 접두 일치만 본다** — 버킷 이름이 다른 문자열의 부분 문자열이거나, 사용자가 다른 버킷의 오브젝트를 복사해 붙였을 경우는 정규화되지 않고 스킴 없는 문자열 그대로 남는다(그 다음 `from_uri`가 `LocalAdapter`로 잘못 해석할 수 있음).
