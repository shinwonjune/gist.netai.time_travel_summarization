import threading
from collections import deque
from typing import Optional


class FrameQueue:
    """Bounded queue holding (frame_index, rgba_bytes, width, height).

    When full, push() drops the oldest entry and increments dropped count.
    """

    def __init__(self, maxsize: int = 8, drop_oldest: bool = True):
        self._maxsize = maxsize
        self._drop_oldest = bool(drop_oldest)
        self._deque: deque = deque()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._dropped = 0
        self._closed = False

    def push(self, item) -> None:
        with self._cond:
            if self._closed:
                return
            if self._drop_oldest:
                if len(self._deque) >= self._maxsize:
                    self._deque.popleft()
                    self._dropped += 1
            else:
                while len(self._deque) >= self._maxsize and not self._closed:
                    self._cond.wait(timeout=0.1)
                if self._closed:
                    return
            self._deque.append(item)
            self._cond.notify_all()

    def pop(self, timeout: Optional[float] = None):
        with self._cond:
            while not self._deque and not self._closed:
                if not self._cond.wait(timeout=timeout):
                    return None
            if self._deque:
                item = self._deque.popleft()
                self._cond.notify_all()
                return item
            return None

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed
