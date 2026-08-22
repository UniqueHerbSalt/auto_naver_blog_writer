"""HTML → 블록 변환 테스트. 브라우저 없이 도는 부분이다."""

import pytest

from naver_blog_mcp.blocks import Block
from naver_blog_mcp.errors import HtmlParseError
from naver_blog_mcp.htmlparse import parse_html


def kinds(html: str) -> list[str]:
    return [b.kind for b in parse_html(html)]


def test_heading_levels():
    blocks = parse_html("<h2>대제목</h2><h3>소제목</h3>")
    assert [(b.level, b.text) for b in blocks] == [(2, "대제목"), (3, "소제목")]


def test_h1_becomes_h2():
    """h1 은 글 제목과 겹치므로 h2 로 낮춘다."""
    assert parse_html("<h1>제목</h1>")[0].level == 2


def test_figure_captures_caption_as_source():
    block = parse_html(
        '<figure><img src="/tmp/a.png" alt="설명"><figcaption>출처: Apple</figcaption></figure>'
    )[0]
    assert (block.kind, block.src, block.alt, block.caption) == (
        "image", "/tmp/a.png", "설명", "출처: Apple",
    )


def test_image_only_paragraph_becomes_image_block():
    assert kinds('<p><img src="/tmp/a.png"></p>') == ["image"]


def test_image_inside_paragraph_is_split_out():
    assert kinds('<p>글 <img src="/tmp/a.png"> 이어짐</p>') == ["image", "paragraph"]


def test_wrapper_tags_are_pierced():
    assert kinds("<div><section><p>본문</p></section></div>") == ["paragraph"]


def test_script_and_style_are_dropped_entirely():
    with pytest.raises(HtmlParseError):
        parse_html("<script>alert(1)</script><style>p{}</style>")


def test_blockquote_keeps_cite_url():
    block = parse_html('<blockquote cite="https://a.com/x">인용문</blockquote>')[0]
    assert (block.text, block.url) == ("인용문", "https://a.com/x")


def test_list_items_keep_urls_for_sources():
    """출처 목록의 URL 은 살아 있어야 한다."""
    block = parse_html('<ul><li><a href="https://a.com/x">Apple</a> (확인)</li></ul>')[0]
    assert block.items == ["Apple https://a.com/x (확인)"]
    assert not block.ordered


def test_ordered_list():
    assert parse_html("<ol><li>하나</li></ol>")[0].ordered


def test_paragraph_link_drops_url_to_protect_autolink():
    """문단 속 URL 뒤에 한국어 조사가 붙으면 자동 링크가 깨진다 — 문구만 남긴다."""
    block = parse_html('<p><a href="https://a.com/x">뉴스룸</a>에서 확인</p>')[0]
    assert block.text == "뉴스룸에서 확인"
    assert "https" not in block.text


def test_standalone_link_becomes_link_block():
    block = parse_html('<a href="https://a.com/x">링크</a>')[0]
    assert (block.kind, block.url, block.text) == ("link", "https://a.com/x", "링크")


def test_inline_emphasis_is_flattened():
    assert parse_html("<p>아주 <strong>중요</strong>하다</p>")[0].text == "아주 중요하다"


def test_leading_and_duplicate_dividers_are_dropped():
    assert kinds("<hr><p>가</p><hr><hr><p>나</p><hr>") == ["paragraph", "divider", "paragraph"]


def test_empty_blocks_are_dropped():
    assert kinds("<p>  </p><p>내용</p><ul></ul>") == ["paragraph"]


def test_image_without_src_is_dropped():
    assert kinds('<p>글</p><img alt="빈것">') == ["paragraph"]


@pytest.mark.parametrize("html", ["", "   ", "<div><span> </span></div>"])
def test_empty_html_raises_rather_than_publishing_nothing(html):
    with pytest.raises(HtmlParseError):
        parse_html(html)


def test_whitespace_is_normalised():
    assert parse_html("<p>여러\n   줄\t띄어쓰기</p>")[0].text == "여러 줄 띄어쓰기"
