"""MCP 서버 — 글 내용에는 관여하지 않고 발행만 담당한다.

도구는 발행을 되돌리기 어렵다는 전제로 나뉘어 있다:

    check_auth      로그인 세션 확인 (제일 먼저)
    preview_html    브라우저 없이 HTML 파싱 결과만 확인 (빠름, 부작용 없음)
    list_categories 블로그 카테고리 목록
    create_draft    에디터에 작성 + 임시저장까지. **발행하지 않는다**
    publish         사람이 확인한 뒤 실제 발행
    discard_draft   작성해 둔 초안을 버리고 브라우저를 닫는다

create_draft 와 publish 는 같은 브라우저 세션을 공유한다. create_draft 가
돌려준 draft_id 를 publish 에 넘긴다.
"""

from __future__ import annotations

import logging
import sys
import uuid
from dataclasses import dataclass, field

try:  # MCP SDK 2.x
    from mcp.server.mcpserver import MCPServer as _McpServer
except ImportError:  # MCP SDK 1.x — FastMCP 가 같은 역할을 한다
    from mcp.server.fastmcp import FastMCP as _McpServer

from .blocks import Post
from .config import NaverConfig, load_config
from .errors import BlogWriterError
from .htmlparse import parse_html
from .naver import NaverPublisher
from .paths import data_dir, ensure_dirs

log = logging.getLogger(__name__)

mcp = _McpServer("naver-blog")


@dataclass
class _Draft:
    post: Post
    saved: bool = False
    open_type: str = "private"


@dataclass
class _Session:
    """브라우저와 작성 중인 초안을 들고 있는 프로세스 전역 상태."""

    config: NaverConfig | None = None
    publisher: NaverPublisher | None = None
    drafts: dict[str, _Draft] = field(default_factory=dict)

    def cfg(self) -> NaverConfig:
        if self.config is None:
            self.config = load_config()
        return self.config

    def pub(self) -> NaverPublisher:
        if self.publisher is None:
            ensure_dirs()
            self.publisher = NaverPublisher(
                self.cfg(), profile_dir=str(data_dir() / "chrome-profile")
            )
        return self.publisher

    def close(self) -> None:
        if self.publisher is not None:
            self.publisher.close()
            self.publisher = None
        self.drafts.clear()


_session = _Session()


@mcp.tool()
def check_auth() -> str:
    """네이버 로그인 세션을 확인한다. 다른 도구를 쓰기 전에 먼저 호출할 것.

    세션이 없으면 브라우저를 열고 사람이 로그인을 마칠 때까지 기다린다
    (2단계 인증 때문에 최대 10분). 글을 다 쓴 뒤에 로그인 문제를 발견하면
    작업이 통째로 날아가므로 반드시 먼저 부른다.
    """
    try:
        cfg = _session.cfg()
        _session.pub().verify()
        return (
            f"로그인 확인됨.\n"
            f"블로그: {cfg.blog_id or cfg.naver_id}\n"
            f"기본 공개범위: {cfg.open_type}\n"
            f"기본 카테고리: {cfg.category or '(블로그 기본값)'}"
        )
    except BlogWriterError as e:
        return f"로그인 실패: {e}"


@mcp.tool()
def preview_html(html: str) -> str:
    """HTML 을 블록으로 파싱한 결과만 보여준다. 브라우저를 띄우지 않는다.

    글을 실제로 올리기 전에 형식이 제대로 해석되는지 확인하는 용도다. 빠르고
    부작용이 없으니 create_draft 전에 한 번 확인하는 것을 권한다.

    지원 태그: h2/h3, p, figure+img+figcaption, blockquote[cite], ul/ol/li,
    a, hr. 나머지는 텍스트만 남기고 버려진다.
    """
    try:
        blocks = parse_html(html)
    except BlogWriterError as e:
        return f"파싱 실패: {e}"

    post = Post(title="(제목은 create_draft 인자로 넘깁니다)", blocks=blocks)
    kinds: dict[str, int] = {}
    for b in blocks:
        kinds[b.kind] = kinds.get(b.kind, 0) + 1
    counts = ", ".join(f"{k} {v}" for k, v in kinds.items())
    return f"블록 {len(blocks)}개 ({counts})\n\n{post.summary()}"


@mcp.tool()
def list_categories() -> str:
    """블로그 카테고리 이름 목록을 읽는다.

    발행 패널을 열어 목록을 읽고 바로 닫는다. **글이 발행되지 않는다.**
    """
    try:
        names = _session.pub().list_categories()
    except BlogWriterError as e:
        return f"카테고리를 읽지 못했습니다: {e}"
    if not names:
        return "카테고리를 찾지 못했습니다. 기본 카테고리로 발행됩니다."
    return "카테고리:\n" + "\n".join(f"- {n}" for n in names)


@mcp.tool()
def create_draft(
    title: str,
    html: str,
    tags: list[str] | None = None,
    category: str = "",
) -> str:
    """HTML 로 된 글을 에디터에 작성하고 임시저장한다. **발행하지 않는다.**

    이미지는 `<img src>` 에 **로컬 파일 경로**를 넣는다. 원본 사이트에서 미리
    내려받아 저장해 둘 것 — 외부 URL 은 핫링크 차단으로 실패하는 경우가 많다.

    작성이 끝나면 draft_id 를 돌려준다. 사람이 확인한 뒤 publish(draft_id) 를
    호출해 실제로 발행한다.

    Args:
        title: 글 제목. HTML 안에 넣지 말고 여기로 넘긴다.
        html: 본문 HTML. 지원 태그는 preview_html 설명 참고.
        tags: 블로그 태그 목록.
        category: 카테고리 이름. 비우면 설정 기본값.
    """
    try:
        blocks = parse_html(html)
    except BlogWriterError as e:
        return f"파싱 실패 — 아무것도 작성하지 않았습니다: {e}"

    if not title.strip():
        return "제목이 비어 있습니다."

    post = Post(
        title=title.strip(),
        blocks=blocks,
        tags=list(tags or []),
        category=category or _session.cfg().category,
    )

    try:
        publisher = _session.pub()
        publisher.verify()
        publisher.write_post(post)
        saved = publisher.save_draft()
    except BlogWriterError as e:
        return f"작성 실패: {e}"

    draft_id = uuid.uuid4().hex[:8]
    _session.drafts[draft_id] = _Draft(post=post, saved=saved)

    where = (
        "네이버 임시저장함에 저장했습니다."
        if saved
        else "임시저장 버튼을 찾지 못했습니다 — 본문은 에디터에 그대로 있습니다."
    )
    return (
        f"작성 완료 (draft_id={draft_id}). 아직 발행되지 않았습니다.\n"
        f"{where}\n"
        f"브라우저에서 내용을 확인한 뒤 publish(\"{draft_id}\") 를 호출하세요.\n\n"
        f"{post.summary()}"
    )


@mcp.tool()
def publish(draft_id: str, open_type: str = "") -> str:
    """create_draft 로 작성해 둔 글을 실제로 발행한다.

    되돌리기 어려운 동작이다. 사람이 내용을 확인했는지 먼저 물어보고 호출한다.

    Args:
        draft_id: create_draft 가 돌려준 값.
        open_type: "private"(비공개) 또는 "public"(전체공개).
            비우면 설정 기본값(보통 private).
    """
    draft = _session.drafts.get(draft_id)
    if draft is None:
        known = ", ".join(_session.drafts) or "(없음)"
        return f"draft_id 를 찾을 수 없습니다: {draft_id}. 현재 초안: {known}"

    if open_type and open_type not in ("private", "public"):
        return f"open_type 은 private 또는 public 이어야 합니다: {open_type!r}"

    try:
        result = _session.pub().finish_publish(
            open_type=open_type, category=draft.post.category
        )
    except BlogWriterError as e:
        return f"발행 실패: {e}"

    _session.drafts.pop(draft_id, None)
    scope = result.extra.get("open_type", "")
    return f"발행 완료 (공개범위={scope}): {result.url}"


@mcp.tool()
def discard_draft(draft_id: str = "") -> str:
    """작성해 둔 초안을 버리고 브라우저를 닫는다.

    네이버 임시저장함에 이미 들어간 글은 지우지 않는다 — 블로그에서 직접
    지워야 한다. 이 도구는 이 서버가 들고 있던 상태만 정리한다.
    """
    if draft_id:
        _session.drafts.pop(draft_id, None)
    _session.close()
    return "초안 상태를 정리하고 브라우저를 닫았습니다."


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,  # stdout 은 MCP 프로토콜 전용이다
    )
    try:
        mcp.run()
    finally:
        _session.close()


if __name__ == "__main__":
    main()
