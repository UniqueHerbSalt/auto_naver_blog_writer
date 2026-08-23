"""브라우저 락과 베어러 인증 테스트.

여러 기기에서 붙어 쓰는 구성이라 여기가 깨지면 작성 중인 글이 날아가거나
엉뚱한 글이 발행된다.
"""

import threading

import pytest

from naver_blog_mcp.runtime import BearerAuth, BrowserLock, Busy


# --- 락 ---

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


# --- 인증 ---

class Spy:
    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True


async def call(app, headers, path="/mcp"):
    sent = []

    async def send(msg):
        sent.append(msg)

    await app({"type": "http", "path": path, "headers": headers}, None, send)
    return sent


def status_of(sent):
    return next((m["status"] for m in sent if m["type"] == "http.response.start"), None)


@pytest.mark.anyio
async def test_valid_token_passes_through():
    spy = Spy()
    app = BearerAuth(spy, "secret")
    await call(app, [(b"authorization", b"Bearer secret")])
    assert spy.called


@pytest.mark.anyio
@pytest.mark.parametrize("headers", [
    [],                                          # 헤더 없음
    [(b"authorization", b"Bearer wrong")],       # 틀린 토큰
    [(b"authorization", b"secret")],             # Bearer 접두어 없음
    [(b"authorization", b"Basic secret")],       # 다른 방식
    [(b"authorization", b"Bearer ")],            # 빈 토큰
])
async def test_bad_credentials_are_rejected(headers):
    spy = Spy()
    sent = await call(BearerAuth(spy, "secret"), headers)
    assert not spy.called
    assert status_of(sent) == 401


@pytest.mark.anyio
async def test_health_path_is_exempt():
    spy = Spy()
    await call(BearerAuth(spy, "secret"), [], path="/health")
    assert spy.called


@pytest.fixture
def anyio_backend():
    return "asyncio"
