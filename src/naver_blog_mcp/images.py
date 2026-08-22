"""이미지 해석 — 블록의 `src` 를 업로드 가능한 로컬 파일 경로로 바꾼다.

스마트에디터는 URL 로 이미지를 삽입할 수 없고 파일 업로드만 받는다. 그래서
본문에 넣을 이미지는 무조건 로컬 파일이어야 한다.

**권장 경로는 로컬 파일이다.** 글을 쓰는 Claude 가 원본 사이트에서 직접
내려받아 저장하고 그 경로를 넘긴다. 뉴스·기업 사이트는 핫링크 차단과
User-Agent 검사를 하는 경우가 많아, 여기서 뒤늦게 URL 을 받으려 하면 403 이
떨어져 이미지가 통째로 빠진다(docs/WRITER_PROMPT.md 참고).

http(s) URL 도 폴백으로 받되, 실패해도 글 전체를 버리지 않고 그 이미지만
건너뛴다.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import unquote, urlparse

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 12 * 1024 * 1024

_EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

#: 핫링크 차단을 피하기 위한 브라우저 흉내 헤더. python-requests 기본
#: User-Agent 로 나가면 많은 CDN 이 봇으로 보고 403 을 준다.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}


def resolve_image(src: str, dest_dir: Path, *, session=None) -> Path | None:
    """블록의 `src` 를 로컬 파일 경로로 해석한다. 실패하면 None.

    - 로컬 경로(`/a/b.png`, `~/x.png`, `file://...`) → 존재 확인 후 그대로
    - http(s) URL → 내려받아 임시 폴더에 저장 (폴백 경로)
    """
    if not src:
        return None

    if src.startswith("file://"):
        local = Path(unquote(urlparse(src).path))
        if local.is_file():
            return local
        log.warning("로컬 이미지가 없습니다: %s", local)
        return None

    if not src.startswith(("http://", "https://")):
        local = Path(src).expanduser()
        if local.is_file():
            return local
        log.warning(
            "이미지 파일을 찾을 수 없습니다: %s — 글을 쓰는 쪽에서 이미지를 먼저 "
            "내려받아 저장했는지 확인하세요.", src[:200],
        )
        return None

    log.info(
        "이미지가 외부 URL 입니다(폴백 경로). 핫링크 차단으로 실패할 수 있으니 "
        "가능하면 미리 내려받아 로컬 경로로 넘기세요: %s", src[:120],
    )
    return download_image(src, dest_dir, session=session)


def download_image(url: str, dest_dir: Path, *, session=None, timeout=(10, 30)) -> Path | None:
    """이미지를 내려받아 로컬 경로를 돌려준다. 실패하면 None."""
    import requests

    session = session or requests
    origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    # Referer 를 이미지 자신의 오리진으로 채워 "그 사이트 안에서 보는 요청"처럼
    # 보이게 한다 — 핫링크 차단을 넘는 흔한 방법.
    headers = {**_BROWSER_HEADERS, "Referer": origin + "/"}

    try:
        response = session.get(url, timeout=timeout, stream=True, headers=headers)
        if response.status_code != 200:
            log.warning("이미지 다운로드 실패 HTTP %s: %s", response.status_code, url[:120])
            return None

        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        ext = _EXT_BY_TYPE.get(content_type)
        if ext is None:
            log.warning("지원하지 않는 이미지 형식(%s): %s", content_type, url[:120])
            return None

        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"img_{abs(hash(url)) % 10**10}{ext}"
        size = 0
        with open(path, "wb") as f:
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    log.warning("이미지가 너무 큽니다(>%dMB): %s",
                                MAX_IMAGE_BYTES // 1024 // 1024, url[:120])
                    f.close()
                    path.unlink(missing_ok=True)
                    return None
                f.write(chunk)
        if size == 0:
            path.unlink(missing_ok=True)
            return None
        return path
    except Exception as e:  # noqa: BLE001 - 이미지 하나 실패가 발행을 막지 않는다
        log.warning("이미지 다운로드 오류(%s): %s", type(e).__name__, url[:120])
        return None
