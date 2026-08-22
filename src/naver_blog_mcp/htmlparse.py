"""HTML → 블록 변환.

Claude 는 완성된 HTML 을 넘기고, 여기서 블록 배열로 바꾼 뒤 에디터를 조작한다.
HTML 을 에디터에 그대로 주입하지 않는 이유는 docs/ARCHITECTURE.md 참고 —
요약하면 스마트에디터 ONE 에는 HTML 소스 편집 모드가 없고, 클립보드
붙여넣기로는 외부 URL 이미지가 통째로 유실된다.

파서는 **화이트리스트** 방식이다. 모르는 태그는 텍스트만 건지고 버린다.
잘못된 HTML 때문에 발행이 통째로 막히는 것보다, 아는 것만 확실히 옮기는 쪽이 낫다.
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup, NavigableString, Tag

from .blocks import Block
from .errors import HtmlParseError

log = logging.getLogger(__name__)

#: 최상위에서 해석하는 태그. 이 목록에 없으면 안쪽을 다시 훑는다.
_HANDLED = {
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "figure", "img", "blockquote", "ul", "ol", "hr", "a", "pre",
}

#: 통째로 버리는 태그 (텍스트도 건지지 않는다).
_DROPPED = {"script", "style", "head", "meta", "link", "noscript", "iframe"}

_WS = re.compile(r"\s+")


def parse_html(html: str) -> list[Block]:
    """HTML 문자열을 블록 배열로 바꾼다.

    본문이 하나도 나오지 않으면 HtmlParseError 를 던진다 — 빈 글을 발행하는
    사고를 막기 위해서다.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup.find_all(list(_DROPPED)):
        tag.decompose()

    root = soup.body or soup
    blocks: list[Block] = []
    _walk(root, blocks)
    blocks = _tidy(blocks)

    if not blocks:
        raise HtmlParseError(
            "HTML 에서 본문을 찾지 못했습니다. 지원 태그(h2/h3, p, figure/img, "
            "blockquote, ul/ol, a, hr)를 사용했는지 확인하세요."
        )
    return blocks


def _walk(node: Tag, out: list[Block]) -> None:
    """자식들을 순서대로 훑으며 아는 태그를 블록으로 바꾼다."""
    for child in node.children:
        if isinstance(child, NavigableString):
            text = _clean(str(child))
            if text:
                out.append(Block(kind="paragraph", text=text))
            continue
        if not isinstance(child, Tag):
            continue

        name = child.name.lower()
        if name in _HANDLED:
            _emit(child, out)
        else:
            # div/span/section/article 같은 껍데기는 뚫고 들어간다.
            _walk(child, out)


def _emit(el: Tag, out: list[Block]) -> None:
    name = el.name.lower()

    if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        text = _clean(el.get_text())
        if text:
            # 스마트에디터의 문단 서식은 본문/소제목 두 단계뿐이다.
            # h1 은 글 제목과 겹치므로 h2 로 낮춘다.
            level = 3 if name in ("h3", "h4", "h5", "h6") else 2
            out.append(Block(kind="heading", text=text, level=level))
        return

    if name == "figure":
        img = el.find("img")
        cap = el.find("figcaption")
        if img is not None:
            out.append(_image_block(img, caption=_clean(cap.get_text()) if cap else ""))
        elif cap is not None:
            text = _clean(cap.get_text())
            if text:
                out.append(Block(kind="paragraph", text=text))
        return

    if name == "img":
        out.append(_image_block(el))
        return

    if name == "blockquote":
        text = _inline(el, keep_urls=True)
        if text:
            out.append(Block(kind="quote", text=text, url=(el.get("cite") or "").strip()))
        return

    if name in ("ul", "ol"):
        items = [_inline(li, keep_urls=True) for li in el.find_all("li", recursive=False)]
        items = [i for i in items if i]
        if items:
            out.append(Block(kind="list", items=items, ordered=(name == "ol")))
        return

    if name == "hr":
        out.append(Block(kind="divider"))
        return

    if name == "p":
        # 이미지만 담은 문단은 이미지 블록으로 승격한다 (<p><img></p> 패턴).
        imgs = el.find_all("img")
        if imgs and not _clean(el.get_text()):
            for img in imgs:
                out.append(_image_block(img))
            return
        # 문단 안에 이미지가 섞여 있으면 이미지를 따로 떼어낸다.
        for img in imgs:
            out.append(_image_block(img))
            img.extract()
        text = _inline(el)
        if text:
            out.append(Block(kind="paragraph", text=text))
        return

    if name == "pre":
        text = el.get_text().strip("\n")
        if text:
            out.append(Block(kind="paragraph", text=text))
        return

    if name == "a":
        # 문단 밖에 홀로 선 링크만 링크 블록이 된다. 독립된 줄에 URL 을 넣으면
        # 스마트에디터가 링크 카드로 바꿔 준다.
        href = (el.get("href") or "").strip()
        if href:
            out.append(Block(kind="link", text=_clean(el.get_text()), url=href))
        return


def _image_block(img: Tag, caption: str = "") -> Block:
    return Block(
        kind="image",
        src=(img.get("src") or "").strip(),
        alt=_clean(img.get("alt") or ""),
        caption=caption or _clean(img.get("title") or ""),
    )


def _inline(el: Tag, *, keep_urls: bool = False) -> str:
    """인라인 내용을 평문으로 만든다.

    ⚠️ 현재 `<strong>`/`<em>` 은 **평문으로 눌린다.** 에디터에서 인라인 굵게를
    넣으려면 타이핑 도중 단축키로 서식을 토글해야 하는데, 그러면 이미 검증된
    입력 경로가 불안정해진다. 강조가 필요하면 소제목으로 올리는 편이 낫다.

    링크 처리는 문맥에 따라 다르다:

    - 문단 안(`keep_urls=False`) → **문구만 남기고 URL 은 버린다.** 본문 한가운데
      생 URL 이 박히면 읽기 나쁘고, 한국어 조사가 URL 뒤에 바로 붙으면
      ("...ios27에서") 스마트에디터의 자동 링크가 조사까지 삼켜 링크가 깨진다.
    - 목록·인용문(`keep_urls=True`) → `문구 URL` 로 이어 붙인다. 정보성 글의
      출처 표기가 여기 오므로 URL 이 살아 있어야 한다. 뒤에 공백을 붙여
      조사가 URL 에 달라붙는 것을 막는다.

    출처 추적은 글 말미의 출처 목록이 책임진다(docs/WRITER_PROMPT.md §5).
    """
    parts: list[str] = []

    def visit(node) -> None:
        if isinstance(node, NavigableString):
            parts.append(str(node))
            return
        if not isinstance(node, Tag):
            return
        name = node.name.lower()
        if name == "a":
            href = (node.get("href") or "").strip()
            label = _clean(node.get_text())
            if not keep_urls:
                parts.append(label or href)
            elif href and href != label:
                # URL 뒤 공백은 필수 — 없으면 뒤따르는 조사까지 링크로 먹힌다.
                parts.append(f"{label} {href} " if label else f"{href} ")
            else:
                parts.append(label or href)
            return  # 안쪽 텍스트는 위에서 이미 넣었다
        if name == "br":
            parts.append(" ")
            return
        for child in node.children:
            visit(child)

    for child in el.children:
        visit(child)
    return _clean("".join(parts))


def _clean(text: str) -> str:
    return _WS.sub(" ", text or "").strip()


def _tidy(blocks: list[Block]) -> list[Block]:
    """빈 블록과 연속 구분선을 정리한다."""
    out: list[Block] = []
    for b in blocks:
        if b.kind == "divider":
            # 맨 앞이나 구분선 바로 뒤의 구분선은 버린다.
            if not out or out[-1].kind == "divider":
                continue
        elif b.kind in ("heading", "paragraph", "quote") and not b.text:
            continue
        elif b.kind == "image" and not b.src:
            log.warning("src 가 없는 이미지를 건너뜁니다")
            continue
        elif b.kind == "list" and not b.items:
            continue
        elif b.kind == "link" and not b.url:
            continue
        out.append(b)
    while out and out[-1].kind == "divider":
        out.pop()
    return out
