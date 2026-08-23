# NAS 배포 (선택)

> **기본 구성이 아니다.** 이 서버는 글 쓰는 사람의 노트북에서 도는 게 맞다
> ([README](../README.md) 참고) — 크롬 창이 눈앞에 뜨니 에디터가 깨졌을 때
> 바로 보이고, 세션이 끊기면 그 자리에서 다시 로그인하면 되고, 일반 데스크톱
> 크롬이라 네이버 탐지에도 가장 유리하다.
>
> 이 문서는 그 이점을 포기하고서라도 **상시 가동 서버**가 필요할 때를 위한
> 것이다. 예약 발행처럼 사람이 없는 시간에 돌려야 하는 경우 정도다.
> 포기하는 것: 화면으로 보는 디버깅, 손쉬운 재로그인, 가장 정상적인 브라우저
> 지문. 얻는 것: 상시 가동, 여러 기기 접속.

Synology DS723+ (x86_64) 기준. Docker + 자체서명 HTTPS 구성이다.

컨테이너 안에 **로그인된 실제 크롬**이 상주한다. 일반 API 서버와 다른 점이
여기서 나오니 §5(최초 로그인)와 §7(운영)을 특히 유의한다.

---

## 1. 구조

```
NAS (Docker, 컨테이너 1개)
├── Xvfb :99              가상 디스플레이 1920x1080
├── Chrome + /data/chrome-profile   ← 네이버 세션이 사는 곳
├── x11vnc → noVNC :6080  최초 로그인 / 세션 복구 통로 (HTTPS)
└── MCP :8443/mcp         streamable-http + 베어러 토큰 (HTTPS)
```

크롬이 하나뿐이므로 **브라우저 작업은 한 번에 하나씩만** 돈다. 다른 기기가
글을 쓰는 중에 요청하면 "다른 기기에서 작업 중"이라는 응답을 받는다.

---

## 2. 준비

```bash
git clone <이 저장소> && cd auto_naver_blog_writer
cp .env.example .env
```

`.env` 를 채운다. 토큰은 길게 만든다.

```bash
openssl rand -hex 32   # 이 값을 NAVER_BLOG_TOKEN 에
```

---

## 3. 인증서

접속에 쓸 **이름과 IP 를 전부** 인자로 넘긴다. SAN 에 없는 주소로 붙으면
클라이언트가 거부한다.

```bash
./scripts/gen-certs.sh nas.local 192.168.0.10
```

`certs/` 에 세 파일이 생긴다.

| 파일 | 용도 |
|---|---|
| `ca.crt` | **접속할 기기마다 신뢰시킨다** (§6) |
| `server.crt` `server.key` | 컨테이너로 마운트 |

CA 를 따로 두는 이유: 서버 인증서를 갱신할 때 CA 를 신뢰해 뒀으면 기기를
다시 손댈 필요가 없다. `ca.key` 는 이 CA 로 아무 인증서나 발급할 수 있으니
NAS 밖으로 내보내지 않는다.

---

## 4. 기동

```bash
docker compose up -d --build
docker compose logs -f
```

`Uvicorn running on https://0.0.0.0:8443` 이 보이면 뜬 것이다.

---

## 5. 최초 네이버 로그인 — 반드시 사람이 한다

2단계 인증과 캡차는 자동화할 수 없다. noVNC 로 컨테이너 안 크롬 화면에
직접 들어가 로그인한다.

1. 브라우저로 `https://nas.local:6080/vnc.html` 접속
   (자체서명이라 경고가 뜬다. §6 을 먼저 하면 안 뜬다)
2. `.env` 의 `VNC_PASSWORD` 입력
3. 컨테이너 크롬이 보인다. `https://nid.naver.com` 으로 가서 로그인
4. **로그인 상태 유지를 켠다.** IP보안은 끄는 편이 좋다 —
   켜져 있으면 접속 IP 가 바뀔 때 세션이 끊긴다
5. 로그인이 끝나면 그대로 창을 닫는다. 세션은 `./data/chrome-profile` 에 남는다

확인:

```bash
curl --cacert certs/ca.crt -H "Authorization: Bearer <토큰>" \
  -H "Accept: application/json, text/event-stream" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' \
  https://nas.local:8443/mcp
```

---

## 6. 클라이언트 연결

### ⓐ CA 를 신뢰시킨다 (기기마다 한 번)

`certs/ca.crt` 를 각 기기로 복사한 뒤:

| OS | 방법 |
|---|---|
| macOS | 키체인 접근 → 시스템 → `ca.crt` 끌어놓기 → 더블클릭 → 신뢰 → "이 인증서 사용 시" **항상 신뢰** |
| Windows | `certutil -addstore -f ROOT ca.crt` (관리자 권한) |
| Linux | `sudo cp ca.crt /usr/local/share/ca-certificates/naver-blog-mcp.crt && sudo update-ca-certificates` |

### ⓑ Node 기반 클라이언트는 한 단계 더 필요하다

**Claude Code·Claude Desktop 은 Node 로 돌고, Node 는 OS 신뢰 저장소를 보지
않는다.** ⓐ 만 해서는 여전히 인증서 오류가 난다. 환경변수로 CA 를 따로
알려 줘야 한다.

```bash
export NODE_EXTRA_CA_CERTS=/절대경로/ca.crt
```

로그인 셸 설정(`~/.zshrc` 등)에 넣어 두어야 매번 적용된다. 데스크톱 앱은
GUI 로 실행되면 셸 설정을 읽지 않으니, macOS 라면 `launchctl setenv
NODE_EXTRA_CA_CERTS /절대경로/ca.crt` 를 쓰고 앱을 재시작한다.

> 이게 자체서명 방식의 실질적인 비용이다. 기기가 늘어날수록 이 작업이
> 반복된다. 번거로워지면 Tailscale(`*.ts.net` 정식 인증서 제공)이나 보유
> 도메인 + Let's Encrypt DNS-01 로 갈아타면 이 절차가 통째로 사라진다.

### ⓒ 서버 등록

```bash
claude mcp add --transport http naver-blog https://nas.local:8443/mcp \
  --header "Authorization: Bearer <토큰>"
```

`nas.local` 자리에는 §3 의 SAN 에 넣은 이름을 그대로 쓴다.

---

## 7. 운영

### 세션이 끊겼을 때

네이버 세션은 영구적이지 않다. `check_auth` 가 실패하면 §5 를 다시 한다.
noVNC 통로를 상시 열어 두는 이유가 이것이다.

### 백업

`./data/chrome-profile` 이 이 시스템의 유일한 상태다. 날아가면 다시 로그인
하면 되므로 치명적이진 않지만, 백업해 두면 재로그인을 아낀다.

`.env` 와 `certs/ca.key` 는 **비밀이다.** 저장소에 커밋되지 않도록
`.gitignore` 에 넣어 두었다.

### 로그

```bash
docker compose logs -f naver-blog-mcp   # 전체
ls ./data/logs/                          # 프로세스별 (mcp, xvfb, x11vnc, novnc)
ls ./data/diagnostics/                   # 실패 시점 스크린샷·HTML
```

에디터 DOM 이 바뀌어 실패하면 `diagnostics/` 의 스크린샷을 보고 셀렉터를
고친다. 추측으로 고치지 않기 위해 남기는 자료다.

---

## 8. 알아 둘 위험

- **네이버 탐지** — Xvfb 위 실제 크롬이라 `--headless` 보다 낫지만 완전하지
  않다. 폰트·WebGL 같은 신호로 컨테이너를 구분당할 여지는 남는다. 집 NAS 는
  가정용 IP 라 IP 평판 면에서는 오히려 유리하다.
- **자동 발행 빈도** — 하루 1~2 건을 넘기지 않는다. 네이버는 자동 생성 글의
  대량 발행에 민감하다(저품질 판정).
- **기본 공개범위는 비공개다.** 공개 발행은 도구 호출에서 명시적으로 요청할
  때만 일어난다. 공개범위를 확실히 지정하지 못하면 서버가 발행을 중단한다.
- **6080 포트를 외부에 열지 않는다.** noVNC 는 컨테이너 크롬을 그대로
  조작할 수 있는 통로다. 내부망에서만 접근되게 둔다.
