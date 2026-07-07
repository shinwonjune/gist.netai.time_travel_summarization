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

    def push(self, item, timeout: Optional[float] = None) -> bool:
        """Enqueue item. Returns True if enqueued, False if closed or timed out.

        drop_oldest=False(무손실 모드)에서 소비자(인코더 스레드)가 죽으면 큐가 찬 채
        영원히 대기하게 되므로, timeout(초)을 주면 그 시간 안에 자리가 안 나면 False를
        반환한다 — 호출부가 인코더 사망을 감지해 캡처를 중단할 수 있게(무한 동결 방지).
        timeout=None은 기존과 동일하게 무기한 대기.
        """
        import time as _time

        with self._cond:
            if self._closed:
                return False
            if self._drop_oldest:
                if len(self._deque) >= self._maxsize:
                    self._deque.popleft()
                    self._dropped += 1
            else:
                deadline = None if timeout is None else _time.monotonic() + timeout
                while len(self._deque) >= self._maxsize and not self._closed:
                    remaining = None if deadline is None else deadline - _time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return False
                    self._cond.wait(timeout=0.1 if remaining is None else min(0.1, remaining))
                if self._closed:
                    return False
            self._deque.append(item)
            self._cond.notify_all()
            return True

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
