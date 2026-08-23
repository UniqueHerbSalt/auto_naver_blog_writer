"""브라우저 접근 직렬화와 베어러 토큰 인증.

이 서버는 크롬 하나와 네이버 세션 하나를 공유한다. 두 요청이 동시에 들어오면
같은 에디터를 덮어써서 작성 중인 글이 통째로 날아간다. 그래서 브라우저를
건드리는 모든 작업은 여기 있는 락을 거쳐 한 번에 하나씩만 돈다.
"""

from __future__ import annotations

import hmac
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


class BearerAuth:
    """`Authorization: Bearer <token>` 을 검사하는 ASGI 미들웨어.

    MCP SDK 의 token_verifier 는 OAuth 발급자 설정까지 요구해 개인용으로는
    과하다. 이 서버는 발행 권한을 그대로 들고 있으므로 최소한의 문지기는
    반드시 필요하다 — 네트워크에 열어 두고 토큰이 없으면 접근 가능한
    누구나 블로그에 글을 올릴 수 있다.

    토큰이 비어 있으면 인증을 걸지 않는다(로컬 stdio 용). 네트워크로 열
    때는 반드시 설정해야 하며, 그러지 않으면 서버가 기동 시 경고한다.
    """

    def __init__(self, app, token: str, *, exempt_paths: tuple[str, ...] = ("/health",)):
        self._app = app
        self._token = token
        self._exempt = exempt_paths

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in self._exempt:
            await self._app(scope, receive, send)
            return

        if not self._authorized(scope):
            await _unauthorized(send)
            return
        await self._app(scope, receive, send)

    def _authorized(self, scope) -> bool:
        for name, value in scope.get("headers") or []:
            if name.lower() != b"authorization":
                continue
            raw = value.decode("latin-1", "replace").strip()
            if not raw.lower().startswith("bearer "):
                return False
            # 상수 시간 비교 — 토큰을 한 글자씩 맞춰 보는 공격을 막는다.
            return hmac.compare_digest(raw[7:].strip(), self._token)
        return False


async def _unauthorized(send) -> None:
    body = b'{"error":"unauthorized"}'
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            (b"content-type", b"application/json"),
            (b"www-authenticate", b"Bearer"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})
