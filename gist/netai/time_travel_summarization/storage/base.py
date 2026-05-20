from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, Optional


@dataclass(frozen=True)
class ObjectInfo:
    uri: str
    size: int
    last_modified: Optional[str] = None
    etag: Optional[str] = None


class StorageAdapter(ABC):
    @abstractmethod
    def open_read(self, uri: str) -> BinaryIO: ...

    @abstractmethod
    def put_bytes(self, uri: str, data: bytes, content_type: Optional[str] = None) -> None: ...

    @abstractmethod
    def put_file(self, uri: str, local_path: Path, content_type: Optional[str] = None) -> None: ...

    @abstractmethod
    def list_prefix(self, uri_prefix: str, *, recursive: bool = False) -> Iterator[ObjectInfo]: ...

    @abstractmethod
    def exists(self, uri: str) -> bool: ...

    @abstractmethod
    def stat(self, uri: str) -> ObjectInfo: ...
