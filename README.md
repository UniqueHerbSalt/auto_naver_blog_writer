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
| [DEPLOY.md](docs/DEPLOY.md) | (선택) NAS Docker 배포 — 기본 구성이 아니다 |

## 실행 위치 — 쓰는 노트북에 깔면 된다

이 서버는 **실제 크롬 창을 띄우는** 서버다. 그래서 글을 쓰는 사람이 앉아
있는 기기에서 도는 게 맞다.

- 크롬 창이 눈앞에 뜬다. **에디터가 무슨 짓을 하는지 그대로 보인다** —
  셀렉터가 깨졌을 때 원인을 바로 안다
- 로그인 세션이 끊기면 그 창에서 그냥 다시 로그인하면 된다
- 일반 데스크톱 크롬이라 네이버 탐지 관점에서 가장 정상적인 지문이다
- 발행 전 검수도 화면에서 바로 한다

서버 없이 **stdio 로 붙는다** — 포트도, 인증서도, 토큰도 필요 없다.

> 원격 컨테이너·CI 에서는 동작하지 않는다. 화면이 없고 사람이 로그인할
> 수 없기 때문이다. 굳이 상시 가동 서버로 돌리려면
> [DEPLOY.md](docs/DEPLOY.md) 를 보되, 위 이점을 전부 포기하게 된다.

## 설치

```bash
git clone <이 저장소> && cd auto_naver_blog_writer
python3 -m venv .venv && source .venv/bin/activate   # 윈도우: .venv\Scripts\activate
pip install -e .
```

크롬은 평소 쓰던 게 깔려 있으면 된다. 드라이버는 Selenium Manager 가
자동으로 맞춰 주므로 따로 받지 않는다.

> **평소 쓰는 크롬과 섞이지 않는다.** 이 서버는 전용 프로필
> (`~/.local/share/naverblogmcp/chrome-profile`, macOS 는
> `~/Library/Application Support/NaverBlogMCP/`)을 쓴다. 그래서 크롬을
> 켜 둔 채로도 돌아가고, 대신 그 프로필에서 네이버 로그인을 **한 번**
> 해 줘야 한다.

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

## 등록

Claude Code:

```bash
claude mcp add naver-blog -e NAVER_BLOG_BLOG_ID=myblog -- /경로/.venv/bin/naver-blog-mcp
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "naver-blog": {
      "command": "/경로/.venv/bin/naver-blog-mcp",
      "env": { "NAVER_BLOG_BLOG_ID": "myblog" }
    }
  }
}
```

가상환경 안의 실행 파일을 **절대경로**로 준다. GUI 앱은 셸의 PATH 를
읽지 않아 이름만 쓰면 못 찾는다.

## 최초 실행

1. `check_auth` 를 호출한다 — 크롬 창이 뜬다
2. 창에서 네이버에 로그인한다 (2단계 인증 포함, 최대 10분 기다린다)
3. **로그인 상태 유지를 켠다.** IP보안은 끄는 편이 좋다 — 켜져 있으면
   접속 IP 가 바뀔 때 세션이 끊긴다
4. 이후 실행부터는 프로필에 세션이 남아 자동으로 통과한다

세션이 만료되면 `check_auth` 가 다시 로그인 창을 띄운다.

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

## 문제가 생기면

크롬 창이 눈앞에 있으니 대부분 보면 안다. 그래도 안 보이는 경우:

```
~/.local/share/naverblogmcp/diagnostics/     # 실패 시점 스크린샷 + HTML
```

에디터 DOM 이 바뀌어 실패하면 여기 스크린샷을 보고 셀렉터를 고친다.
추측으로 고치지 않으려고 남기는 자료다.

## 상태

발행 파이프라인 구현 완료, **실제 네이버 계정 검증 전.**

미검증으로 남은 곳 두 군데 — 첫 실행에서 깨질 가능성이 가장 높다:

- **임시저장 버튼 셀렉터** — 원본 코드에 없던 부분이라 클래스 후보와
  "저장" 문구 폴백으로 추측해 넣었다. 실패해도 예외를 내지 않고 본문을
  에디터에 남겨 둔다
- **카테고리 목록 조회** — 발행 패널 DOM 을 추측했다

둘 다 실패해도 글이 날아가지 않게 만들었다.
