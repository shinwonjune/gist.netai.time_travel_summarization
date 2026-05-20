import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator, Optional
from urllib.parse import urlparse
from urllib.request import url2pathname

from .base import ObjectInfo, StorageAdapter


class LocalAdapter(StorageAdapter):
    def open_read(self, uri: str) -> BinaryIO:
        return self._path_from_uri(uri).open("rb")

    def put_bytes(self, uri: str, data: bytes, content_type: Optional[str] = None) -> None:
        target = self._path_from_uri(uri)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._tmp_path(target)
        try:
            tmp_path.write_bytes(data)
            os.replace(tmp_path, target)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def put_file(self, uri: str, local_path: Path, content_type: Optional[str] = None) -> None:
        target = self._path_from_uri(uri)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._tmp_path(target)
        try:
            shutil.copyfile(local_path, tmp_path)
            os.replace(tmp_path, target)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def list_prefix(self, uri_prefix: str, *, recursive: bool = False) -> Iterator[ObjectInfo]:
        base_path = self._path_from_uri(uri_prefix)
        if not base_path.exists():
            return iter(())

        if base_path.is_file():
            files = [base_path]
        elif recursive:
            files = sorted(path for path in base_path.rglob("*") if path.is_file())
        else:
            files = sorted(path for path in base_path.iterdir() if path.is_file())

        return (self._object_info(path) for path in files)

    def exists(self, uri: str) -> bool:
        return self._path_from_uri(uri).exists()

    def stat(self, uri: str) -> ObjectInfo:
        path = self._path_from_uri(uri)
        if not path.exists():
            raise FileNotFoundError(path)
        return self._object_info(path)

    @staticmethod
    def _tmp_path(path: Path) -> Path:
        return path.with_name(f"{path.name}.tmp")

    @staticmethod
    def _path_from_uri(uri: str) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme not in ("", "file"):
            raise ValueError(f"Unsupported local URI scheme: {parsed.scheme!r}")
        if parsed.scheme == "file":
            # url2pathname handles Windows file URIs correctly:
            # "/C:/Users/foo" -> "C:\\Users\\foo", and is a no-op on POSIX.
            path = Path(url2pathname(parsed.path))
        else:
            path = Path(uri)
        return path if path.is_absolute() else path.resolve()

    @staticmethod
    def _object_info(path: Path) -> ObjectInfo:
        file_stat = path.stat()
        return ObjectInfo(
            uri=path.resolve().as_uri(),
            size=file_stat.st_size,
            last_modified=datetime.fromtimestamp(file_stat.st_mtime, timezone.utc).isoformat(),
            etag=None,
        )
