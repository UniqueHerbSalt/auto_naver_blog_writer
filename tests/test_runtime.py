"""브라우저 락 테스트.

크롬과 네이버 세션이 하나뿐이라, 두 작업이 겹치면 같은 에디터를 덮어써서
작성 중인 글이 날아간다. 로컬 단일 클라이언트라도 안전하지 않다 — Claude 는
한 번에 여러 도구를 병렬로 호출할 수 있다.
"""

import threading

import pytest

from naver_blog_mcp.runtime import BrowserLock, Busy

def test_lock_allows_sequential_work():
    lock = BrowserLock()
    with lock.hold("첫 작업"):
        pass
    with lock.hold("둘째 작업"):
        pass
    assert lock.busy_with is None


def test_lock_rejects_concurrent_work_with_a_helpful_message():
    lock = BrowserLock(wait_seconds=0.05)
    started = threading.Event()
    release = threading.Event()

    def worker():
        with lock.hold("글 작성(iOS 27 공개)"):
            started.set()
            release.wait(timeout=5)

    t = threading.Thread(target=worker)
    t.start()
    started.wait(timeout=5)
    try:
        with pytest.raises(Busy, match="글 작성"):
            with lock.hold("발행"):
                pass
    finally:
        release.set()
        t.join(timeout=5)


def test_lock_is_released_after_a_failure():
    """작업이 예외로 죽어도 락이 남아 있으면 안 된다."""
    lock = BrowserLock(wait_seconds=0.05)
    with pytest.raises(RuntimeError):
        with lock.hold("실패할 작업"):
            raise RuntimeError("boom")
    with lock.hold("다음 작업"):
        pass


def test_busy_message_reports_what_is_running():
    lock = BrowserLock(wait_seconds=0.01)
    lock._lock.acquire()
    lock._holder = "카테고리 조회"
    with pytest.raises(Busy) as e:
        with lock.hold("발행"):
            pass
    assert "카테고리 조회" in str(e.value)
