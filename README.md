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

자세한 설계는 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 참고.

## 실행 위치

**로컬 PC 에서 돌아야 한다.** 원격 컨테이너에서는 동작하지 않는다.

- Selenium 이 실제 크로미움 창을 띄운다
- 네이버 로그인 세션이 로컬 크롬 프로필에 저장된다
- 최초 로그인(2단계 인증 포함)은 사람이 직접 해야 한다
- 네이버는 headless 탐지가 강해 `headless=False` 가 기본이다

## 상태

설계 확정 단계. 구현 전.
