import io
import os
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Optional
from urllib.parse import urlparse

try:
    from minio import Minio
    from minio.error import S3Error
except Exception:  # pragma: no cover - optional dependency; import failure must never
    # kill extension startup (실측: headless에서 pipapi 환경 PermissionError가
    # ImportError가 아니어서 확장 전체가 죽음). minio는 사용 시점에 RuntimeError로 안내.
    Minio = None
    S3Error = None

from .base import ObjectInfo, StorageAdapter


class _MinioStream:
    def __init__(self, response: Any):
        self._response = response

    def read(self, *args: Any, **kwargs: Any) -> bytes:
        return self._response.read(*args, **kwargs)

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._response.release_conn()

    def __enter__(self) -> "_MinioStream":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


class MinioAdapter(StorageAdapter):
    _REQUIRED_KEYS = ("MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY")

    def __init__(self) -> None:
        if Minio is None:
            raise RuntimeError("minio package not installed; pip install minio")
        self._client = None

    def open_read(self, uri: str) -> BinaryIO:
        bucket, key = self._parse_uri(uri)
        self._require_object_key(bucket, key)
        return _MinioStream(self._get_client().get_object(bucket, key))

    def put_bytes(self, uri: str, data: bytes, content_type: Optional[str] = None) -> None:
        bucket, key = self._parse_uri(uri)
        self._require_object_key(bucket, key)
        self._get_client().put_object(
            bucket,
            key,
            io.BytesIO(data),
            length=len(data),
            content_type=content_type or "application/octet-stream",
        )

    def put_file(self, uri: str, local_path: Path, content_type: Optional[str] = None) -> None:
        bucket, key = self._parse_uri(uri)
        self._require_object_key(bucket, key)
        self._get_client().fput_object(bucket, key, str(local_path), content_type=content_type)

    def list_prefix(self, uri_prefix: str, *, recursive: bool = False) -> Iterator[ObjectInfo]:
        bucket, key_prefix = self._parse_uri(uri_prefix)
        objects = self._get_client().list_objects(bucket, prefix=key_prefix, recursive=recursive)
        for obj in objects:
            if obj.size == 0 and obj.object_name.endswith("/"):
                continue
            yield ObjectInfo(
                uri=f"s3://{bucket}/{obj.object_name}",
                size=obj.size,
                last_modified=obj.last_modified.isoformat() if obj.last_modified else None,
                etag=obj.etag,
            )

    def exists(self, uri: str) -> bool:
        bucket, key = self._parse_uri(uri)
        if not key:
            return self._get_client().bucket_exists(bucket)
        try:
            self._get_client().stat_object(bucket, key)
            return True
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                return False
            raise

    def stat(self, uri: str) -> ObjectInfo:
        bucket, key = self._parse_uri(uri)
        self._require_object_key(bucket, key)
        try:
            obj = self._get_client().stat_object(bucket, key)
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                raise FileNotFoundError(uri) from exc
            raise
        return ObjectInfo(
            uri=f"s3://{bucket}/{key}",
            size=obj.size,
            last_modified=obj.last_modified.isoformat() if obj.last_modified else None,
            etag=obj.etag,
        )

    def _get_client(self):
        if self._client is None:
            config = self._load_config()
            endpoint = config["MINIO_ENDPOINT"]
            host = endpoint.split("://", 1)[1] if "://" in endpoint else endpoint
            secure = config.get("MINIO_SECURE", "false").lower() == "true"
            region = config.get("MINIO_REGION", "us-east-1")
            self._client = Minio(
                host,
                access_key=config["MINIO_ACCESS_KEY"],
                secret_key=config["MINIO_SECRET_KEY"],
                secure=secure,
                region=region,
            )
        return self._client

    @classmethod
    def _load_config(cls) -> dict[str, str]:
        env_file = cls._load_env_file(cls._env_path())
        config = {}
        for key in (*cls._REQUIRED_KEYS, "MINIO_SECURE", "MINIO_REGION"):
            value = os.environ.get(key, env_file.get(key))
            if value is not None:
                config[key] = value
        missing = [key for key in cls._REQUIRED_KEYS if not config.get(key)]
        if missing:
            raise RuntimeError(f"Missing MinIO configuration: {', '.join(missing)}")
        return config

    @staticmethod
    def _env_path() -> Path:
        return Path(__file__).resolve().parent.parent / ".env"

    @staticmethod
    def _load_env_file(path: Path) -> dict[str, str]:
        values = {}
        if not path.exists():
            return values
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
        return values

    @staticmethod
    def _parse_uri(uri: str) -> tuple[str, str]:
        parsed = urlparse(uri)
        if parsed.scheme not in ("s3", "minio"):
            raise ValueError(f"Unsupported MinIO URI scheme: {parsed.scheme!r}")
        bucket = parsed.netloc
        if not bucket:
            raise ValueError(f"Missing bucket in URI: {uri!r}")
        return bucket, parsed.path.lstrip("/")

    @staticmethod
    def _require_object_key(bucket: str, key: str) -> None:
        if not key:
            raise ValueError(f"Object key required for bucket {bucket!r}")
