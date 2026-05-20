import os
import uuid
from pathlib import Path

import pytest

from gist.netai.time_travel_summarization.storage import from_uri

# Try to load .env so MINIO_* env vars are present
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if _ENV_PATH.exists():
    for line in _ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

REQUIRED = ["MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "MINIO_BUCKET"]
if any(k not in os.environ for k in REQUIRED):
    pytest.skip("MinIO env not configured", allow_module_level=True)

BUCKET = os.environ["MINIO_BUCKET"]
PREFIX = f"_smoke/{uuid.uuid4().hex[:8]}"


def _uri(suffix: str) -> str:
    return f"s3://{BUCKET}/{PREFIX}/{suffix}"


@pytest.fixture(scope="module")
def adapter():
    return from_uri(f"s3://{BUCKET}/")


def test_put_bytes_and_read(adapter):
    payload = b"hello minio probe"
    uri = _uri("hello.txt")
    try:
        adapter.put_bytes(uri, payload, content_type="text/plain")
        assert adapter.exists(uri)
        with adapter.open_read(uri) as stream:
            data = stream.read()
        assert data == payload
        info = adapter.stat(uri)
        assert info.size == len(payload)
    finally:
        try:
            adapter._client.remove_object(BUCKET, f"{PREFIX}/hello.txt")
        except Exception:
            pass


def test_put_file(adapter, tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"x" * 1024)
    uri = _uri("a.bin")
    try:
        adapter.put_file(uri, src, content_type="application/octet-stream")
        info = adapter.stat(uri)
        assert info.size == 1024
    finally:
        try:
            adapter._client.remove_object(BUCKET, f"{PREFIX}/a.bin")
        except Exception:
            pass


def test_list_prefix(adapter):
    keys = ["x/1.txt", "x/2.txt", "x/sub/3.txt"]
    try:
        for key in keys:
            adapter.put_bytes(_uri(key), b"x")
        flat = list(adapter.list_prefix(f"s3://{BUCKET}/{PREFIX}/x/", recursive=False))
        deep = list(adapter.list_prefix(f"s3://{BUCKET}/{PREFIX}/x/", recursive=True))
        assert len(deep) >= 3
        assert all("/sub/" not in obj.uri for obj in flat)
    finally:
        for key in keys:
            try:
                adapter._client.remove_object(BUCKET, f"{PREFIX}/{key}")
            except Exception:
                pass


def test_exists_and_stat_missing(adapter):
    uri = _uri("never_existed.txt")
    assert not adapter.exists(uri)
    with pytest.raises(FileNotFoundError):
        adapter.stat(uri)
