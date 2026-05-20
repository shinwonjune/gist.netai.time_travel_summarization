from urllib.parse import urlparse

from .base import StorageAdapter
from .local_adapter import LocalAdapter
from .minio_adapter import MinioAdapter

_LOCAL = None
_MINIO = None


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
