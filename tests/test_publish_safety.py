"""발행 안전장치 테스트.

의도치 않은 전체공개는 되돌릴 수 없는 사고다. 공개범위를 확실히 지정하지
못하면 발행을 중단해야 한다.
"""

import pytest

from naver_blog_mcp.blocks import Block, Post
from naver_blog_mcp.config import NaverConfig
from naver_blog_mcp.naver import NaverError, NaverPublisher


class FakeDriver:
    current_url = "https://blog.naver.com/test/123"

    def __init__(self):
        self.switched = []

    class _Switch:
        def default_content(self):
            pass

    switch_to = _Switch()


class FakeWait:
    """wait.until(...).click() 을 받아 넘기는 최소 구현."""

    def __init__(self):
        self.clicks = 0

    def until(self, _condition):
        return self

    def click(self):
        self.clicks += 1


class Publisher(NaverPublisher):
    def __init__(self, cfg, *, open_type_selectable: bool):
        # ⚠️ _sleep 은 __init__ 에서 인스턴스 속성으로 잡히므로 메서드 오버라이드로는
        # 못 막는다. 반드시 sleeper 로 주입해야 테스트가 실제로 잠들지 않는다.
        super().__init__(cfg, profile_dir="/tmp/unused", sleeper=lambda _s: None)
        self._selectable = open_type_selectable
        self.selected_category = None

    def _select_open_type(self, driver, label):
        return self._selectable

    def _select_category(self, driver, name):
        self.selected_category = name


def test_publish_aborts_when_private_cannot_be_selected():
    """비공개를 못 고르면 전체공개로 나가느니 중단한다."""
    pub = Publisher(NaverConfig(blog_id="t", open_type="private"), open_type_selectable=False)
    with pytest.raises(NaverError, match="전체공개"):
        pub._do_publish(FakeDriver(), FakeWait(), open_type="private", category="")


def test_publish_proceeds_when_public_selection_fails():
    """이미 전체공개가 의도라면 선택 실패해도 진행한다(사고가 아니다)."""
    pub = Publisher(NaverConfig(blog_id="t"), open_type_selectable=False)
    url = pub._do_publish(FakeDriver(), FakeWait(), open_type="public", category="")
    assert url == "https://blog.naver.com/test/123"


def test_publish_succeeds_when_scope_is_selectable():
    pub = Publisher(NaverConfig(blog_id="t"), open_type_selectable=True)
    url = pub._do_publish(FakeDriver(), FakeWait(), open_type="private", category="IT")
    assert url == "https://blog.naver.com/test/123"
    assert pub.selected_category == "IT"


def test_open_type_argument_overrides_config():
    """설정이 private 여도 호출자가 public 을 주면 그 의도를 따른다."""
    pub = Publisher(NaverConfig(blog_id="t", open_type="private"), open_type_selectable=False)
    pub._do_publish(FakeDriver(), FakeWait(), open_type="public", category="")


def test_default_open_type_is_private():
    """기본값이 비공개여야 실수로 공개 발행되지 않는다."""
    assert NaverConfig().open_type == "private"
