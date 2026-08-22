"""본문 블록 모델.

Claude 가 넘긴 HTML 은 여기 정의된 블록 배열로 변환된 뒤 에디터에 작성된다.
발행 대상이 늘어나도(티스토리 등) 이 모델은 그대로 쓸 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 지원 블록 종류. 파서와 렌더러가 이 목록을 공유한다.
BLOCK_KINDS = ("heading", "paragraph", "image", "quote", "list", "link", "divider")


@dataclass
class Block:
    kind: str

    #: heading/paragraph 의 내용, quote 의 인용문, link 의 표시 문구.
    text: str = ""

    #: heading 의 단계(2 또는 3). 나머지는 0.
    level: int = 0

    #: image 의 이미지 위치. **로컬 절대 경로가 권장 경로**이며, http(s) URL 도
    #: 폴백으로 받는다(원본 사이트가 핫링크를 막으면 실패할 수 있다).
    src: str = ""

    #: image 의 대체 텍스트(무엇이 보이는지).
    alt: str = ""

    #: image 의 캡션 — 정보성 글에서는 출처를 적는다.
    caption: str = ""

    #: quote 의 출처 URL(`cite` 속성), link 의 대상 URL.
    url: str = ""

    #: list 의 항목들.
    items: list[str] = field(default_factory=list)

    #: list 가 번호 목록인지 여부.
    ordered: bool = False


@dataclass
class Post:
    """발행 단위 하나."""

    title: str
    blocks: list[Block] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    #: 블로그 카테고리 이름. 비우면 설정값, 그것도 비면 기본 카테고리.
    category: str = ""

    def summary(self) -> str:
        """사람이 확인용으로 읽을 평문 요약."""
        lines = [f"제목: {self.title}"]
        if self.category:
            lines.append(f"카테고리: {self.category}")
        if self.tags:
            lines.append(f"태그: {', '.join(self.tags)}")
        lines.append("")
        for b in self.blocks:
            if b.kind == "heading":
                lines.append(f"{'#' * b.level} {b.text}")
            elif b.kind == "paragraph":
                lines.append(b.text)
            elif b.kind == "image":
                where = b.src if len(b.src) <= 80 else "..." + b.src[-77:]
                lines.append(f"[이미지] {where}" + (f" — {b.caption}" if b.caption else ""))
            elif b.kind == "quote":
                lines.append(f"> {b.text}" + (f" ({b.url})" if b.url else ""))
            elif b.kind == "list":
                marker = "1." if b.ordered else "-"
                lines.extend(f"  {marker} {item}" for item in b.items)
            elif b.kind == "link":
                lines.append(f"[링크] {b.text or b.url} → {b.url}")
            elif b.kind == "divider":
                lines.append("---")
        return "\n".join(lines)


@dataclass
class PublishResult:
    url: str = ""
    post_id: object = None
    extra: dict = field(default_factory=dict)
