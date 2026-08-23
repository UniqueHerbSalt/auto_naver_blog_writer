# 네이버 블로그 MCP 서버 — NAS(x86_64) 배포용
#
# 이 컨테이너는 단순한 API 서버가 아니라 **로그인된 실제 크롬**을 물고 있다.
#   - 네이버는 headless 크롬을 탐지하므로 Xvfb(가상 디스플레이) 위에 일반
#     크롬을 띄운다. --headless 를 쓰지 않는다.
#   - 최초 로그인(2단계 인증·캡차)은 사람이 해야 하므로 noVNC 로 컨테이너
#     화면에 접속할 통로를 함께 연다.
#   - 로그인 세션은 /data/chrome-profile 볼륨에 남는다. 이 볼륨이 이 시스템의
#     유일한 상태다. 날아가면 다시 로그인해야 한다.
#
# amd64 전용이다 — 구글 크롬은 arm64 빌드를 배포하지 않는다.
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DISPLAY=:99 \
    NAVER_BLOG_DATA_DIR=/data

RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg ca-certificates \
        xvfb x11vnc supervisor \
        novnc websockify \
        # 한글 폰트가 없으면 에디터가 전부 네모로 보인다. 진단 스크린샷도
        # 못 읽고, 폰트 목록은 자동화 탐지 신호로도 쓰인다.
        fonts-nanum fonts-nanum-coding fonts-noto-color-emoji \
    && wget -qO /etc/apt/keyrings/google.asc https://dl-ssl.google.com/linux/linux_signing_key.pub \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google.asc] http://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY docker/supervisord.conf /etc/supervisor/conf.d/naver-blog.conf
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

VOLUME ["/data"]
EXPOSE 8443 6080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
