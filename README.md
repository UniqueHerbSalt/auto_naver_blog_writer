# auto_naver_blog_writer

네이버 블로그 발행용 MCP 서버.

**Claude가 조사하고 글을 쓴다. 이 서버는 그 글을 네이버 블로그에 올린다.**
서버 안에 LLM 호출은 없다.

```
[Claude]  웹 검색 → 출처 취합·검증 → 완성된 HTML 작성
             ↓  MCP 도구 호출
[MCP]     HTML 파싱 → 블록 변환 → 크로미움 조작 → 발행
```

발행 계층은 [Auto-CooPangPartners](https://github.com/UniqueHerbSalt/Auto-CooPangPartners)
의 `naver.py` 에서 가져온다. 실전에서 다듬어진 코드라 새로 짜지 않는다.

## 문서

| 문서 | 내용 |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | 설계 — 역할 경계, HTML 계약, 재사용 내역 |
| [SOURCES.md](docs/SOURCES.md) | 소식 출처와 **주제 선별 기준** |
| [STYLE.md](docs/STYLE.md) | 글 구조, 제목, 루머 표기 |
| [WRITER_PROMPT.md](docs/WRITER_PROMPT.md) | 글 쓰는 Claude 용 지침 (진입점) |

## 실행 위치

**로컬 PC 에서 돌아야 한다.** 원격 컨테이너에서는 동작하지 않는다.

- Selenium 이 실제 크로미움 창을 띄운다
- 네이버 로그인 세션이 로컬 크롬 프로필에 저장된다
- 최초 로그인(2단계 인증 포함)은 사람이 직접 해야 한다
- 네이버는 headless 탐지가 강해 `headless=False` 가 기본이다

## 설치

```bash
pip install -e .
```

크로미움/크롬은 미리 설치돼 있어야 한다. 드라이버는 Selenium Manager 가
자동으로 맞춰 주므로 따로 받지 않아도 된다.

## 설정

환경변수로 준다 (MCP 클라이언트 설정의 `env` 에 넣으면 된다).

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| `NAVER_BLOG_BLOG_ID` | — | 블로그 주소 아이디. **필수** |
| `NAVER_BLOG_OPEN_TYPE` | `private` | `private`(비공개) \| `public`(전체공개) |
| `NAVER_BLOG_CATEGORY` | — | 기본 카테고리 이름 |
| `NAVER_BLOG_LOGIN_MODE` | `manual` | `manual`(권장) \| `auto` |
| `NAVER_BLOG_HEADLESS` | `false` | 네이버 headless 탐지가 강해 켜지 않는 것을 권장 |

전체 목록은 `src/naver_blog_mcp/config.py` 의 `NaverConfig` 참고.
`~/.config/naverblogmcp/config.toml` 로도 줄 수 있다.

MCP 클라이언트 등록 예:

```json
{
  "mcpServers": {
    "naver-blog": {
      "command": "naver-blog-mcp",
      "env": { "NAVER_BLOG_BLOG_ID": "myblog" }
    }
  }
}
```

## 도구

| 도구 | 브라우저 | 하는 일 |
|---|---|---|
| `check_auth` | 뜸 | 로그인 세션 확인. **제일 먼저 호출** |
| `preview_html` | 안 뜸 | HTML 파싱 결과만 확인 (빠르고 부작용 없음) |
| `list_categories` | 뜸 | 카테고리 목록 |
| `create_draft` | 뜸 | 에디터 작성 + 임시저장. **발행하지 않음** |
| `publish` | 뜸 | 사람이 확인한 뒤 실제 발행 |
| `discard_draft` | — | 초안 정리 + 브라우저 종료 |

`create_draft` 와 `publish` 가 나뉘어 있는 것이 핵심이다. 발행은 되돌리기
어려우므로 사람이 한 번 보고 넘어간다.

## 지원 HTML

`h2` `h3` `p` `figure`+`img`+`figcaption` `blockquote[cite]` `ul` `ol` `li`
`a` `hr`. 나머지는 텍스트만 남기고 버려진다.

`<img src>` 에는 **로컬 파일 경로**를 넣는다. 글을 쓰는 쪽이 원본 사이트에서
미리 내려받아 저장해야 한다 — 자세한 이유는
[docs/WRITER_PROMPT.md](docs/WRITER_PROMPT.md) §3.

## 테스트

```bash
pip install -e ".[dev]"
pytest
```

브라우저 없이 도는 부분(HTML 파싱, 블록 렌더링, 발행 안전장치)을 덮는다.

## 상태

발행 파이프라인 구현 완료, 실제 네이버 계정 검증 전.
글 작성 품질은 실제 발행 결과를 보며 다듬는다.
