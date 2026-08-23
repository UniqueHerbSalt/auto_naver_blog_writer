"""브라우저 접근 직렬화.

이 서버는 크롬 하나와 네이버 세션 하나를 공유한다. 두 요청이 동시에 들어오면
같은 에디터를 덮어써서 작성 중인 글이 통째로 날아간다. 그래서 브라우저를
건드리는 모든 작업은 여기 있는 락을 거쳐 한 번에 하나씩만 돈다.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager

log = logging.getLogger(__name__)


class Busy(Exception):
    """다른 작업이 브라우저를 쓰고 있다."""


class BrowserLock:
    """브라우저 작업을 직렬화한다.

    무한정 기다리게 하지 않는다 — 글 한 편 작성은 몇 분씩 걸리는데 그동안
    다른 기기가 조용히 멈춰 있으면 고장난 것처럼 보인다. 짧게 시도해 보고
    실패하면 무엇이 얼마나 돌고 있는지 알려 준다.
    """

    def __init__(self, wait_seconds: float = 2.0):
        self._lock = threading.Lock()
        self._wait = wait_seconds
        self._holder: str | None = None
        self._since: float = 0.0

    @contextmanager
    def hold(self, operation: str, *, clock=time.monotonic):
        if not self._lock.acquire(timeout=self._wait):
            elapsed = int(clock() - self._since)
            raise Busy(
                f"다른 기기에서 '{self._holder}' 작업이 진행 중입니다 "
                f"({elapsed}초 경과). 끝난 뒤 다시 시도하세요."
            )
        self._holder = operation
        self._since = clock()
        try:
            yield
        finally:
            self._holder = None
            self._lock.release()

    @property
    def busy_with(self) -> str | None:
        return self._holder
