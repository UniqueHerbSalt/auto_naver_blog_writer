#!/usr/bin/env bash
# 자체서명 인증서 발급 — 로컬 CA 를 먼저 만들고 그 CA 로 서버 인증서에 서명한다.
#
# 서버 인증서를 직접 자체서명하지 않는 이유: 인증서를 갱신할 때마다 모든
# 기기에 다시 설치해야 한다. CA 를 신뢰시켜 두면 CA 하나만 한 번 설치하면
# 되고, 이후 서버 인증서는 얼마든지 다시 발급할 수 있다.
#
# 사용법:
#   ./scripts/gen-certs.sh nas.local 192.168.0.10
#   (접속에 쓸 이름과 IP 를 전부 인자로 넘긴다 — SAN 에 없으면 거부당한다)
set -euo pipefail

OUT="${CERT_DIR:-./certs}"
CA_DAYS=3650      # CA 는 길게 — 자주 바꾸면 기기마다 재설치해야 한다
CERT_DAYS=825     # 서버 인증서는 짧게 (일부 클라이언트가 825일 초과를 거부)

if [ $# -eq 0 ]; then
    echo "사용법: $0 <호스트명|IP> [추가 호스트명|IP ...]" >&2
    echo "예:    $0 nas.local 192.168.0.10" >&2
    exit 1
fi

mkdir -p "$OUT"
cd "$OUT"

# --- SAN 목록 구성. 접속에 쓸 이름이 여기 없으면 클라이언트가 거부한다. ---
# ⚠️ 인라인 subjectAltName 은 "DNS:이름,IP:주소" 형식이다. "DNS.1:" 같은
# 번호 형식은 별도 [alt_names] 섹션에서만 유효하며, 인라인으로 쓰면 openssl
# 이 거부해 SAN 이 통째로 빠진 인증서가 나온다(모든 클라이언트가 거부한다).
san=""
for host in "$@"; do
    if echo "$host" | grep -qE '^[0-9]+(\.[0-9]+){3}$'; then
        san="${san}IP:${host},"
    else
        san="${san}DNS:${host},"
    fi
done
san="${san}DNS:localhost,IP:127.0.0.1"

# --- CA (없을 때만 생성 — 다시 만들면 모든 기기에 재설치해야 한다) ---
if [ ! -f ca.key ]; then
    echo "로컬 CA 를 만듭니다..."
    openssl genrsa -out ca.key 4096 2>/dev/null
    openssl req -x509 -new -nodes -key ca.key -sha256 -days "$CA_DAYS" -out ca.crt \
        -subj "/CN=Naver Blog MCP Local CA/O=naver-blog-mcp"
else
    echo "기존 CA 를 재사용합니다 (ca.crt)."
fi

# --- 서버 인증서 ---
openssl genrsa -out server.key 2048 2>/dev/null
openssl req -new -key server.key -out server.csr -subj "/CN=$1" 2>/dev/null
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
    -out server.crt -days "$CERT_DAYS" -sha256 \
    -extfile <(printf "subjectAltName=%s\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n" "$san")
rm -f server.csr

# SAN 이 실제로 들어갔는지 확인한다. 빠진 인증서는 조용히 쓸모없다.
if ! openssl x509 -in server.crt -noout -ext subjectAltName | grep -q .; then
    echo "오류: 인증서에 SAN 이 없습니다. 이대로면 클라이언트가 거부합니다." >&2
    exit 1
fi

chmod 600 server.key ca.key
echo
echo "완료: $OUT"
echo "  ca.crt      ← 접속할 기기마다 이걸 신뢰시킨다 (한 번만)"
echo "  server.crt  ← 컨테이너로 마운트"
echo "  server.key  ← 컨테이너로 마운트 (외부 유출 금지)"
echo
echo "SAN: $san"
echo "위 목록에 없는 이름/IP 로 접속하면 인증서가 거부됩니다."
