"""설정. TOML 파일 + 환경변수 오버라이드.

MCP 서버는 보통 클라이언트 설정(`.mcp.json` 등)의 `env` 로 값을 받으므로
환경변수를 1급으로 지원한다. TOML 은 값이 많아질 때를 위한 선택지다.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from .errors import ConfigError
from .paths import config_dir


@dataclass
class NaverConfig:
    blog_id: str = ""           # 블로그 주소 아이디 (비우면 naver_id 사용)
    login_mode: str = "manual"  # manual(권장) | auto
    naver_id: str = ""          # auto 모드에서만 필요
    naver_pw: str = ""
    headless: bool = False      # 네이버 headless 탐지가 강해 기본 False
    profile_dir: str = ""       # 크롬 사용자 데이터 디렉터리(로그인 유지). 비우면 데이터 디렉터리
    chromedriver_path: str = "" # 비우면 Selenium Manager 자동 해석
    #: 크롬에 덧붙일 인자(공백 구분). 컨테이너에서는 보통
    #: "--no-sandbox --disable-dev-shm-usage" 가 필요하다.
    extra_chrome_args: str = ""
    open_type: str = "private"  # 발행 공개범위: private(비공개, 안전 기본) | public
    category: str = ""          # 기본 카테고리 이름(비우면 블로그 기본값)

    #: 소제목을 굵게 표시한다(본문과 시각적으로 구분).
    heading_bold: bool = True
    #: 본문 글꼴(비우면 에디터 기본값). 예: "나눔고딕"
    font_family: str = ""
    #: 본문 글자 크기.
    font_size: str = "15"
    #: 소제목 글자 크기. 본문(15)과 확실히 구분되는 값.
    #: 네이버 크기 드롭다운에 없는 값이면 조용히 건너뛰고 이전 크기를 유지한다.
    heading_font_size: str = "24"

    wait_timeout: int = 30
    login_wait_s: int = 600            # 로그인 대기 상한(초). 2단계 인증 여유
    image_upload_wait_s: float = 4.0   # 이미지 업로드 완료 대기(초)
    link_convert_wait_s: float = 1.5   # URL 자동 링크 변환 대기(초)
    #: 삽입한 이미지가 칼럼 폭을 꽉 채울 만큼 크면 "작게하기"를 한 번 눌러 축소한다.
    shrink_large_images: bool = True

    def validate(self) -> None:
        if self.login_mode not in ("manual", "auto"):
            raise ConfigError(f"login_mode 는 manual 또는 auto 여야 합니다: {self.login_mode!r}")
        if self.open_type not in ("private", "public"):
            raise ConfigError(f"open_type 은 private 또는 public 이어야 합니다: {self.open_type!r}")
        if self.login_mode == "auto" and not (self.naver_id and self.naver_pw):
            raise ConfigError("login_mode=auto 인데 naver_id/naver_pw 가 비어 있습니다")
        if not (self.blog_id or self.naver_id):
            raise ConfigError("blog_id 또는 naver_id 중 하나는 있어야 합니다")


ENV_PREFIX = "NAVER_BLOG_"


def _coerce(raw: str, target_type):
    if target_type is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if target_type is int:
        return int(raw)
    if target_type is float:
        return float(raw)
    return raw


def load_config(path: Path | None = None) -> NaverConfig:
    """TOML 을 읽고 환경변수로 덮어쓴다. 둘 다 없으면 기본값.

    환경변수 이름은 `NAVER_BLOG_` + 대문자 필드명이다 (예: `NAVER_BLOG_BLOG_ID`).
    """
    data: dict = {}
    path = path or config_dir() / "config.toml"
    if path.is_file():
        with open(path, "rb") as f:
            loaded = tomllib.load(f)
        # [naver] 섹션이 있으면 그걸 쓰고, 없으면 최상위를 쓴다.
        data = loaded.get("naver", loaded)

    defaults = NaverConfig()
    known = {f.name: type(getattr(defaults, f.name)) for f in fields(NaverConfig)}
    kwargs = {k: v for k, v in data.items() if k in known}

    for name, want in known.items():
        raw = os.environ.get(ENV_PREFIX + name.upper())
        if raw is not None:
            kwargs[name] = _coerce(raw, want)

    # TOML 에서 문자열로 적힌 값도 필드 타입에 맞춰 준다.
    for name, value in list(kwargs.items()):
        if isinstance(value, str) and known[name] is not str:
            kwargs[name] = _coerce(value, known[name])

    cfg = NaverConfig(**kwargs)
    cfg.validate()
    return cfg
