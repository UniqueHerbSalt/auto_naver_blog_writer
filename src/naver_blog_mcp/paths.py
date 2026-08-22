"""OS별 설정/데이터 디렉터리 결정. OS 의존 코드는 이 모듈에만 둔다."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "NaverBlogMCP"


def config_dir() -> Path:
    override = os.environ.get("NAVER_BLOG_CONFIG_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME.lower()


def data_dir() -> Path:
    override = os.environ.get("NAVER_BLOG_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME.lower()


def log_dir() -> Path:
    return data_dir() / "logs"


def default_config_path() -> Path:
    return config_dir() / "config.toml"


def ensure_dirs() -> None:
    for d in (config_dir(), data_dir(), log_dir()):
        d.mkdir(parents=True, exist_ok=True)
