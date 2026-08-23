"""발행 대상 검증 테스트.

크롬이 하나뿐이라 새 글을 쓰면 이전 글이 에디터에서 사라진다. 그 상태로 옛
draft_id 를 발행하면 **엉뚱한 글이 나간다.** 되돌릴 수 없으므로 막아야 한다.
로컬 단일 사용자에게도 그대로 해당된다 — create_draft 를 두 번 부르면 끝이다.
"""

import pytest

from naver_blog_mcp import server


@pytest.fixture(autouse=True)
def clean_session():
    server._session.drafts.clear()
    server._session.editor_holds = None
    yield
    server._session.drafts.clear()
    server._session.editor_holds = None


def _stage(draft_id: str, *, in_editor: bool):
    """초안을 등록하고, 그것이 에디터에 있는지 여부를 정한다."""
    from naver_blog_mcp.blocks import Block, Post

    post = Post(title=f"글 {draft_id}", blocks=[Block(kind="paragraph", text="본문")])
    server._session.drafts[draft_id] = server._Draft(post=post, saved=True)
    if in_editor:
        server._session.editor_holds = draft_id


def test_publishing_a_draft_no_longer_in_the_editor_is_refused():
    """두 번째 글을 쓴 뒤 첫 번째 draft_id 로 발행하면 막혀야 한다."""
    _stage("aaa", in_editor=False)
    _stage("bbb", in_editor=True)

    result = server.publish("aaa")

    assert "더 이상 에디터에 없습니다" in result
    assert "bbb" in result          # 지금 에디터에 뭐가 있는지 알려 준다
    assert "aaa" in result


def test_publishing_with_an_empty_editor_is_refused():
    _stage("aaa", in_editor=False)
    assert "에디터가 비어 있습니다" in server.publish("aaa")


def test_unknown_draft_id_is_refused():
    result = server.publish("없는아이디")
    assert "찾을 수 없습니다" in result


def test_invalid_open_type_is_refused_before_touching_the_browser():
    _stage("aaa", in_editor=True)
    result = server.publish("aaa", open_type="everyone")
    assert "private 또는 public" in result
    # 거부됐으니 초안은 그대로 남아 있어야 한다
    assert "aaa" in server._session.drafts
