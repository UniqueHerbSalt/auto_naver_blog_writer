"""블록 → 에디터 조작 렌더러 테스트.

실제 브라우저 대신 NaverPublisher 의 저수준 조작 메서드를 가로채, 어떤
순서로 무엇이 쓰였는지만 기록해 검증한다. 셀레늄 없이 돈다.
"""

from pathlib import Path

import pytest

from naver_blog_mcp.blocks import Block, Post
from naver_blog_mcp.config import NaverConfig
from naver_blog_mcp.naver import NaverPublisher


class Recorder(NaverPublisher):
    """본문 작성 중 일어난 일을 순서대로 기록한다."""

    def __init__(self, cfg):
        super().__init__(cfg, profile_dir="/tmp/unused", sleeper=lambda _s: None)
        self.events: list[tuple] = []

    # --- 실제 브라우저를 건드리는 지점들을 전부 막는다 ---
    def _focus_body_end(self, driver, body_el=None):
        pass

    def _apply_base_format(self, driver):
        pass

    def _restore_body_format(self, driver):
        pass

    def _newline(self, driver):
        pass

    def _type_line(self, driver, text):
        self.events.append(("text", text))

    def _write_heading(self, driver, text, level=3):
        self.events.append(("heading", text, level))

    def _insert_divider(self, driver):
        self.events.append(("divider",))

    def _upload_image_file(self, driver, path, what):
        self.events.append(("image", str(path)))
        return True

    def _shrink_image_if_too_big(self, driver):
        pass


@pytest.fixture
def rec():
    return Recorder(NaverConfig(blog_id="test", link_convert_wait_s=0))


def render(rec, blocks):
    rec._write_blocks(None, None, Post(title="t", blocks=blocks))
    return rec.events


def test_blocks_are_written_in_order(rec):
    events = render(rec, [
        Block(kind="heading", text="첫 소제목", level=2),
        Block(kind="paragraph", text="본문입니다."),
        Block(kind="heading", text="둘째 소제목", level=3),
        Block(kind="paragraph", text="이어지는 본문."),
    ])
    assert events == [
        ("heading", "첫 소제목", 2),
        ("text", "본문입니다."),
        ("divider",),                      # 두 번째 소제목 앞에만 구분선
        ("heading", "둘째 소제목", 3),
        ("text", "이어지는 본문."),
    ]


def test_divider_not_inserted_before_first_heading(rec):
    events = render(rec, [Block(kind="heading", text="유일한 소제목", level=2)])
    assert events == [("heading", "유일한 소제목", 2)]


def test_image_uploads_file_and_writes_caption_as_source(tmp_path, rec):
    img = tmp_path / "shot.png"
    img.write_bytes(b"x")
    events = render(rec, [
        Block(kind="image", src=str(img), alt="설정 화면", caption="출처: Apple Newsroom"),
    ])
    assert events == [("image", str(img)), ("text", "출처: Apple Newsroom")]


def test_missing_image_is_skipped_without_killing_the_post(tmp_path, rec):
    """이미지 하나가 없다고 글 전체를 버리지 않는다."""
    events = render(rec, [
        Block(kind="paragraph", text="앞 문단"),
        Block(kind="image", src=str(tmp_path / "없는파일.png")),
        Block(kind="paragraph", text="뒤 문단"),
    ])
    assert events == [("text", "앞 문단"), ("text", "뒤 문단")]


def test_quote_writes_text_then_source_url(rec):
    events = render(rec, [
        Block(kind="quote", text="가장 큰 변화입니다.", url="https://a.com/x"),
    ])
    assert events == [("text", "“가장 큰 변화입니다.”"), ("text", "https://a.com/x")]


def test_bullet_and_numbered_lists(rec):
    events = render(rec, [
        Block(kind="list", items=["가", "나"]),
        Block(kind="list", items=["하나", "둘"], ordered=True),
    ])
    assert events == [
        ("text", "· 가"), ("text", "· 나"),
        ("text", "1. 하나"), ("text", "2. 둘"),
    ]


def test_link_writes_label_then_url_on_its_own_line(rec):
    """URL 이 독립된 줄에 있어야 스마트에디터가 링크 카드로 바꾼다."""
    events = render(rec, [Block(kind="link", text="원문 보기", url="https://a.com/x")])
    assert events == [("text", "원문 보기"), ("text", "https://a.com/x")]


def test_link_without_url_is_ignored(rec):
    assert render(rec, [Block(kind="link", text="문구만")]) == []


def test_full_post_from_html_end_to_end(tmp_path, rec):
    """HTML 파싱부터 에디터 조작까지 한 번에 흐르는지 확인한다."""
    from naver_blog_mcp.htmlparse import parse_html

    img = tmp_path / "01-settings.png"
    img.write_bytes(b"x")
    html = f"""
    <h2>iOS 27 퍼블릭 베타 공개</h2>
    <p>애플이 퍼블릭 베타를 공개했다.</p>
    <figure><img src="{img}" alt="설정"><figcaption>출처: Apple</figcaption></figure>
    <h3>출처</h3>
    <ul><li><a href="https://apple.com/x">Apple Newsroom</a></li></ul>
    """
    events = render(rec, parse_html(html))
    assert events == [
        ("heading", "iOS 27 퍼블릭 베타 공개", 2),
        ("text", "애플이 퍼블릭 베타를 공개했다."),
        ("image", str(img)),
        ("text", "출처: Apple"),
        ("divider",),
        ("heading", "출처", 3),
        ("text", "· Apple Newsroom https://apple.com/x"),
    ]
