"""예외 계층."""

from __future__ import annotations


class BlogWriterError(Exception):
    """모든 자체 예외의 루트."""


class ConfigError(BlogWriterError):
    """설정 누락/형식 오류/필수값 누락."""


class CredentialError(BlogWriterError):
    """로그인 실패. 재시도하지 않고 즉시 중단한다."""



class HtmlParseError(BlogWriterError):
    """넘겨받은 HTML 을 블록으로 바꿀 수 없다."""
