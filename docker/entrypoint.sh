#!/bin/sh
# 기동 전 점검. 잘못된 설정으로 조용히 뜨는 것보다 즉시 죽는 편이 낫다.
set -e

if [ -z "$NAVER_BLOG_BLOG_ID" ] && [ -z "$NAVER_BLOG_NAVER_ID" ]; then
    echo "오류: NAVER_BLOG_BLOG_ID 를 설정하세요." >&2
    exit 1
fi

# 이 서버는 블로그 발행 권한을 그대로 들고 있다. 네트워크에 여는데 토큰이
# 없으면 접근 가능한 누구나 글을 올릴 수 있다.
if [ "$NAVER_BLOG_TRANSPORT" = "http" ] && [ -z "$NAVER_BLOG_TOKEN" ]; then
    echo "오류: 네트워크 모드에는 NAVER_BLOG_TOKEN 이 필요합니다." >&2
    exit 1
fi

if [ -z "$VNC_PASSWORD" ]; then
    echo "오류: VNC_PASSWORD 를 설정하세요 (로그인 화면 접근 통로입니다)." >&2
    exit 1
fi

mkdir -p /data/chrome-profile /data/logs
x11vnc -storepasswd "$VNC_PASSWORD" /data/.vncpasswd >/dev/null 2>&1

exec supervisord -c /etc/supervisor/conf.d/naver-blog.conf
