"""네이버 블로그 자동 발행 (브라우저 자동화).

⚠️ 주의: 네이버 블로그 자동 발행은 네이버 운영정책 위반 소지가 있으며,
검색 노출 차단(저품질)·계정 정지 리스크가 있습니다. 반드시 본인 소유 블로그에서,
가능하면 부계정으로 먼저 테스트하세요. 기본 발행 범위는 '비공개'입니다.

설계상 견고성:
- Selenium 은 지연 임포트한다. 미설치 시 이 모듈만 실패하고 나머지는 동작한다.
- 로그인 판정은 DOM 이 아니라 쿠키(NID_SES)로 한다(더 안정적).
- 기본은 수동 로그인 + 프로필 디렉터리 재사용 → 자격증명 자동 입력/캡차 회피를 피한다.
- 스마트에디터 셀렉터는 NaverSelectors 로 한곳에 모아 DOM 변경 시 코드 수정 없이 교체 가능.
- 각 단계 실패는 어느 단계인지 명확한 NaverError 로 보고한다.
"""

from __future__ import annotations

import logging
import random
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .blocks import Post, PublishResult
from .config import NaverConfig
from .errors import BlogWriterError, CredentialError
from .images import resolve_image

log = logging.getLogger(__name__)

WRITE_URL = "https://blog.naver.com/{blog_id}?Redirect=Write&"
NAVER_HOME = "https://www.naver.com/"
LOGIN_URL = "https://nid.naver.com/nidlogin.login"

#: 2026-08-20 사용자가 제공한 실제 브라우저 HAR 캡처로 확인한 사실:
#: 스마트에디터 ONE 에 이미지를 넣으면(드래그드롭/붙여넣기) 에디터 자체 JS가
#: platform.editor.naver.com 에서 세션키를 받아 blog.upphoto.naver.com 으로
#: 업로드하는 흐름을 스스로 처리한다 — 즉, 처음부터 화면에 존재하는
#: <input type=file> 은 없다(그래서 send_keys 로 찾아 넣는 방식이 늘 실패했다).
#: 그래서 이 흐름을 직접 흉내내는 대신, 에디터가 실제로 반응하는 입력
#: 방식(파일 드롭)을 브라우저 안에서 재현해 에디터 자신의 업로드 로직을 그대로
#: 타게 한다 — 네이버의 세션키 발급/업로드 API 를 직접 호출하는 것보다
#: 훨씬 덜 깨지기 쉽다(내부 인증 토큰 형식에 의존하지 않는다).
_DROP_IMAGE_JS = """
const [b64, mime, filename] = arguments;
const byteChars = atob(b64);
const bytes = new Uint8Array(byteChars.length);
for (let i = 0; i < byteChars.length; i++) bytes[i] = byteChars.charCodeAt(i);
const file = new File([bytes], filename, {type: mime});
const dt = new DataTransfer();
dt.items.add(file);
const active = document.activeElement;
let target = (active && active.closest && active.closest('.se-component-content')) ? active : null;
if (!target) {
  // activeElement 가 못 미더울 때는 '첫' 문단이 아니라 '마지막' 문단을 써야
  // 한다 — 첫 문단으로 떨어지면 이미지가 글 맨 앞에 끼어들어 순서가
  // 뒤죽박죽 섞인다(실사고: 사용자 리포트).
  const paras = document.querySelectorAll('.se-component-content .se-text-paragraph');
  target = paras.length ? paras[paras.length - 1]
    : (document.querySelector('.se-main-container') || document.body);
}
const rect = target.getBoundingClientRect();
const opts = {
  bubbles: true, cancelable: true, dataTransfer: dt,
  clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2,
};
target.dispatchEvent(new DragEvent('dragenter', opts));
target.dispatchEvent(new DragEvent('dragover', opts));
target.dispatchEvent(new DragEvent('drop', opts));
"""


def copy_to_clipboard(text: str) -> bool:
    """OS 기본 도구로 클립보드에 복사한다(추가 의존성 없음).

    네이버는 send_keys 로 한 글자씩 입력하는 것을 자동화로 탐지하므로,
    붙여넣기가 가장 안정적인 입력 방법이다.
    """
    try:
        if sys.platform == "darwin":
            proc = subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
        elif sys.platform == "win32":
            # PowerShell Set-Clipboard 가 유니코드를 안전하게 처리한다.
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "$input | Set-Clipboard"],
                input=text.encode("utf-8"), check=True,
            )
        else:
            for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
                try:
                    subprocess.run(cmd, input=text.encode("utf-8"), check=True)
                    return True
                except (OSError, subprocess.CalledProcessError):
                    continue
            return False
        return proc.returncode == 0
    except (OSError, subprocess.CalledProcessError) as e:
        log.debug("클립보드 복사 실패: %s", e)
        return False


def clear_clipboard() -> None:
    """비밀번호가 클립보드에 남지 않도록 지운다."""
    copy_to_clipboard(" ")


class NaverError(BlogWriterError):
    pass


@dataclass
class NaverSelectors:
    """스마트에디터 ONE 상호작용 셀렉터. 네이버 DOM 변경 시 여기만 고치면 된다."""

    main_frame: str = "mainFrame"
    # 팝업(이전 글 이어쓰기 / 도움말) 닫기 버튼 후보들
    popup_close: tuple = (
        "button.se-popup-button-cancel",
        ".se-help-panel-close-button",
        "button.se-popup-button-close",
    )
    #: 제목 영역(문서 제목 컴포넌트 안의 문단)
    title_area: str = ".se-documentTitle .se-text-paragraph, .se-title-text .se-text-paragraph"
    #: 제목 컴포넌트를 식별하는 셀렉터 — 본문을 고를 때 이 안쪽은 제외한다
    title_container: str = ".se-documentTitle, .se-title-text"
    #: 본문 문단. ⚠️ 제목도 .se-component-content 안에 있으므로 반드시 제목을 제외해야 한다
    #: (제외하지 않으면 본문이 제목 칸에 입력된다 — 실제로 발생했던 버그)
    body_area: str = ".se-text-paragraph"
    #: 서식 툴바 — 글꼴/크기 드롭다운(문구 기반 선택의 진입점)
    font_family_opener: tuple = (
        ".se-toolbar-option-font-family-code button",
        "button[data-name='font-family']",
        ".se-font-family-toolbar-button",
    )
    font_size_opener: tuple = (
        ".se-toolbar-option-font-size-code button",
        "button[data-name='font-size']",
        ".se-font-size-toolbar-button",
    )
    #: 문단 서식(본문/소제목/인용구) 전환 버튼 — 클릭하면 드롭다운이 열린다.
    #: 2026-08-20 사용자가 실제 DOM(우클릭 검사)을 캡처해 확인해 준 정확한 값:
    #:   <button data-name="text-format" class="se-text-format-toolbar-button ...">
    #:     <span class="se-toolbar-label">본문</span> (또는 "소제목")
    #: 이전에 추측했던 data-name='paragraph-style'/'style' 은 전부 틀렸었다 —
    #: 그래서 소제목 전환 시도 자체가 항상 조용히 실패하고 있었다(버튼을 못
    #: 찾아 드롭다운이 열리지도 않음).
    text_format_opener: tuple = (
        "button[data-name='text-format']",
        ".se-text-format-toolbar-button",
    )
    #: 구분선(수평선) 삽입 버튼 — 소제목 사이 섹션을 시각적으로 나누는 데 쓴다.
    #: 2026-08-20 사용자가 실제 DOM 캡처로 확인: data-name="horizontal-line"
    #: (카멜케이스 'horizontalLine' 이 아니다 — 이전 추측이 틀렸던 이유).
    #: 같은 data-name 을 가진 버튼이 두 개 있는데(추가 버튼 / 스타일 선택
    #: 드롭다운 버튼), '추가' 버튼의 클래스가 더 구체적이라 그걸 먼저 시도한다.
    divider_button: tuple = (
        ".se-insert-horizontal-line-default-toolbar-button",
        "button[data-name='horizontal-line']",
    )
    #: 이미지를 클릭하면 뜨는 플로팅 툴바의 "작게하기"(축소) 버튼 후보.
    #: 2026-08-20 사용자 HAR 로 동작만 확인됨(DOM 구조는 캡처되지 않음):
    #: 클릭 1번마다 documentModel 상 이미지 width 가 정확히 이전 값의 75%로
    #: 줄어든다(예: 693px → 519px, 실사고 아님 — 실측). 아래 후보는 다른
    #: 툴바 버튼과 같은 명명 규칙을 따른 추정이며, 실제로는 문구("작게")
    #: 기반 탐색을 우선한다(_shrink_image_if_too_big 참고). 둘 다 실패하면
    #: 조용히 건너뛴다 — 이미지가 큰 채로 남을 뿐 발행 자체는 막지 않는다.
    image_shrink_button: tuple = (
        ".se-toolbar-option-image-size-down-code button",
        "button[data-name='imageScaleDown']",
        "button[data-name='shrink']",
    )
    #: 이미지 업로드용 숨김 파일 입력(스마트에디터는 URL 삽입 불가, 파일 업로드만 지원).
    #: ⚠️ "사진" 툴바 버튼을 추측 클릭해서 이 input 을 드러내려던 예전 방식은
    #: 절대 쓰지 않는다 — 그 버튼이 내부적으로 진짜 input 을 프로그램적으로
    #: click() 하면, OS 네이티브 파일 선택창이 열려버리고 Selenium 은 그 창을
    #: 절대 닫거나 다룰 수 없어(웹드라이버 자동화 범위 밖) 브라우저가 통째로
    #: 멈춘다(실제로 발생: 사용자가 macOS 파일 열기 창이 갑자기 뜨는 걸 목격).
    #: 그래서 이 input 은 클릭 없이 순수 DOM 탐색으로만 찾는다(_find_file_input).
    image_file_input: str = "input[type='file']"
    #: 임시저장 버튼. 네이버가 클래스 해시를 자주 바꾸므로 부분 일치 후보를
    #: 함께 둔다. 전부 실패하면 표시 문구("저장")로 다시 찾는다(_save_draft).
    save_draft_button: tuple = (
        "button[class*='save_btn']",
        ".save_btn__bzc5B",
        "button[class*='Save']",
    )
    publish_open_button: str = ".publish_btn__m9KHH, button.publish_btn__m9KHH"
    publish_confirm_button: str = ".confirm_btn__WEaBq, button.confirm_btn__WEaBq"
    #: 로그인 폼 — 네이버가 DOM 을 바꿔도 후보를 늘려 대응한다
    login_id_input: tuple = (
        "#id",
        "#loginId",
        "input[name='id']",
        "input[placeholder*='아이디']",
    )
    login_pw_input: tuple = (
        "#pw",
        "#loginPw",
        "input[name='pw']",
        "input[type='password']",
    )
    #: 로그인 버튼. 2026-08 실제 DOM 확인:
    #:   <button type="button" class="btn_done" id="loginBtn_row">
    #: type 이 submit 이 아니고 클래스도 btn_login 이 아니므로 예전 후보는 모두 빗나간다.
    login_submit: tuple = (
        "#loginBtn_row",          # 가로 배치(현행 PC)
        "#loginBtn_column",       # 세로 배치 변형
        "#loginBtn",
        ".btn_login_dual .dual_item.login button",
        "button.btn_done",
        "#log\\.login",            # 구버전
        "button[type='submit']",
    )
    #: 로그인 상태 유지(체크하면 세션이 오래 간다)
    keep_login_toggle: tuple = ("#keep", "input[name='keepLogin']", ".keep_check input")
    #: IP보안(켜져 있으면 IP 가 바뀔 때 세션이 끊긴다 → 해제 권장)
    ip_security_toggle: tuple = ("#switch", "input[name='switch']", ".ip_check input")
    open_public_radio: str = "input#public"
    open_private_radio: str = "input#private"


def _require_selenium():
    try:
        from selenium import webdriver  # noqa: F401
        from selenium.webdriver.chrome.options import Options  # noqa: F401
        from selenium.webdriver.common.by import By  # noqa: F401
        from selenium.webdriver.support.ui import WebDriverWait  # noqa: F401
        from selenium.webdriver.support import expected_conditions as EC  # noqa: F401
    except ImportError as e:
        raise NaverError(
            "네이버 발행에는 selenium 이 필요합니다. `pip install \"autocoopang[naver]\"` "
            "또는 `pip install selenium` 후 다시 시도하세요."
        ) from e


class NaverPublisher:
    name = "네이버 블로그"

    def __init__(
        self,
        cfg: NaverConfig,
        *,
        profile_dir: str,
        driver_factory=None,
        sleeper=time.sleep,
        clock=time.time,
        rng=None,
    ):
        self._cfg = cfg
        self._profile_dir = cfg.profile_dir or profile_dir
        self._driver_factory = driver_factory  # 테스트에서 주입
        self._sleep = sleeper
        self._clock = clock
        self._rng = rng or random.Random()
        self._driver = None
        self._sel = NaverSelectors()
        self._image_diag_dumped = False  # 이미지 업로드 진단 덤프를 발행당 1번만 남긴다

    # --- 드라이버 관리 ---
    def _build_driver(self):
        _require_selenium()
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        options = Options()
        options.add_argument(f"--user-data-dir={self._profile_dir}")
        options.add_argument("--start-maximized")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        for arg in self._cfg.extra_chrome_args.split():
            # 컨테이너에서는 --no-sandbox 등이 필요하다. Xvfb 위에서 돌리면
            # --headless 없이 실제 크롬으로 뜰 수 있어 네이버 탐지에 덜 걸린다.
            options.add_argument(arg)
        if self._cfg.headless:
            # 네이버는 headless 탐지가 강해 기본 비권장. 사용자가 켠 경우에만.
            options.add_argument("--headless=new")
        service = Service(self._cfg.chromedriver_path) if self._cfg.chromedriver_path else Service()
        return webdriver.Chrome(options=options, service=service)

    def _ensure_driver(self):
        if self._driver is None:
            self._driver = self._driver_factory() if self._driver_factory else self._build_driver()
        return self._driver

    def _has_session(self) -> bool:
        """페이지를 이동하지 않고 로그인 쿠키만 확인한다.

        ⚠️ 절대 여기서 driver.get() 을 호출하면 안 된다. 로그인 대기 중에
        주기적으로 호출되므로, 이동시키면 사용자가 입력 중인 아이디·비밀번호와
        2단계 인증 화면이 통째로 날아간다(실제로 발생했던 버그).
        NID_SES 는 .naver.com 도메인 쿠키라 로그인 페이지에서도 조회된다.
        """
        driver = self._ensure_driver()
        try:
            cookie = driver.get_cookie("NID_SES")
        except Exception:  # noqa: BLE001 - 페이지 전환 중 일시적 실패
            return False
        return bool(cookie and cookie.get("value"))

    def _is_logged_in(self) -> bool:
        """초기 1회 확인용 — 네이버 홈으로 이동한 뒤 세션을 확인한다."""
        driver = self._ensure_driver()
        driver.get(NAVER_HOME)
        self._sleep(1.0)
        return self._has_session()

    # --- Publisher 프로토콜 ---
    def verify(self) -> None:
        """로그인 세션을 확인/확보한다.

        auto 모드: 아이디/비번을 클립보드 붙여넣기로 자동 입력하고 로그인한다.
          캡차·2단계 인증 등으로 자동 완료가 안 되면 실패시키지 않고 브라우저를
          열어둔 채 사용자가 마저 끝내도록 대기한다(하이브리드).
        manual 모드(기본): 처음부터 사용자가 직접 로그인. 프로필 재사용으로
          다음 실행부터는 자동 통과.
        """
        if self._is_logged_in():
            log.info("네이버 로그인 세션 확인됨")
            return

        if self._cfg.login_mode == "auto":
            self._auto_login()
            if self._has_session():
                log.info("네이버 자동 로그인 성공")
                return
            log.warning(
                "자동 로그인이 완료되지 않았습니다(캡차/2단계 인증 가능성). "
                "열린 브라우저에서 로그인을 마저 완료해 주세요."
            )
            self._wait_for_manual_login()
            return

        driver = self._ensure_driver()
        driver.get(LOGIN_URL)
        log.warning(
            "네이버에 로그인해 주세요(열린 브라우저에서 직접). 최대 %d초 대기합니다.",
            self._cfg.login_wait_s,
        )
        self._wait_for_manual_login()

    def _wait_for_manual_login(self) -> None:
        """사용자가 로그인을 끝낼 때까지 조용히 기다린다.

        페이지를 건드리지 않고 쿠키만 확인하므로, 2단계 인증처럼 시간이 걸리는
        절차도 방해받지 않는다. 남은 시간을 주기적으로 알려준다.
        """
        deadline = self._clock() + self._cfg.login_wait_s
        next_notice = self._clock() + 30.0
        while self._clock() < deadline:
            self._sleep(2.0)
            if self._has_session():  # 이동 없이 쿠키만 확인
                log.info("네이버 로그인 확인됨 — 계속 진행합니다")
                self._sleep(1.5)  # 세션 안정화
                return
            if self._clock() >= next_notice:
                remaining = int(deadline - self._clock())
                log.info("로그인 대기 중… (남은 시간 %d초)", max(0, remaining))
                next_notice = self._clock() + 30.0
        raise CredentialError(
            f"네이버 로그인 대기 시간({self._cfg.login_wait_s}초) 초과. "
            f"2단계 인증을 쓰신다면 설정에서 login_wait_s 를 늘리세요."
        )

    def _auto_login(self) -> None:
        """아이디/비번 자동 입력 후 로그인 시도.

        입력 전략(순서대로 시도):
        1) 클립보드 붙여넣기 — 네이버가 한 글자씩 타이핑(send_keys)을 자동화로
           탐지하므로 가장 안정적이다.
        2) JS 값 주입 + input/change 이벤트 디스패치 — 클립보드를 못 쓸 때.
        어느 쪽도 실패하면 예외 대신 조용히 반환해 상위에서 수동 인계로 넘어간다.
        """
        if not self._cfg.naver_id or not self._cfg.naver_pw:
            raise CredentialError("auto 로그인 모드인데 naver_id/naver_pw 가 비어 있습니다")

        from selenium.webdriver.common.by import By

        driver = self._ensure_driver()
        driver.get(LOGIN_URL)
        self._sleep(1.5)
        id_el = self._find_first(driver, self._sel.login_id_input)
        pw_el = self._find_first(driver, self._sel.login_pw_input)
        if id_el is None or pw_el is None:
            log.warning("네이버 로그인 폼을 찾지 못했습니다 — 수동 로그인으로 전환")
            return

        try:
            id_ok = self._fill(driver, id_el, self._cfg.naver_id)
            pw_ok = self._fill(driver, pw_el, self._cfg.naver_pw)
            if not (id_ok and pw_ok):
                # 입력 확인에 실패했어도 실제로는 들어갔을 수 있으므로 제출까지 시도한다.
                # (여기서 포기하면 아이디만 남고 로그인 버튼도 안 눌린 채 끝난다)
                log.warning("입력 확인에 실패했지만 로그인 제출을 시도합니다 "
                            "(아이디=%s, 비밀번호=%s)",
                            "확인" if id_ok else "미확인",
                            "확인" if pw_ok else "미확인")

            self._apply_login_options(driver)
            self._sleep(0.6)
            submit = self._find_first(driver, self._sel.login_submit)
            if submit is None:
                log.warning("로그인 버튼을 찾지 못했습니다 — Enter 키로 제출을 시도합니다")
                from selenium.webdriver.common.keys import Keys

                pw_el.send_keys(Keys.ENTER)
            else:
                submit.click()
            self._sleep(3.0)
        except Exception as e:  # noqa: BLE001
            log.warning("자동 로그인 중 오류(%s) — 수동 로그인으로 전환", e)
        finally:
            clear_clipboard()  # 비밀번호가 클립보드에 남지 않도록

    def _apply_login_options(self, driver) -> None:
        """로그인 상태 유지=켬, IP보안=끔 으로 맞춰 세션이 오래 유지되게 한다.

        IP보안이 켜져 있으면 IP 가 바뀔 때마다 세션이 끊겨 매번 다시 로그인해야 한다.
        옵션을 찾지 못해도 로그인 자체는 계속 진행한다.
        """
        self._set_toggle(driver, self._sel.keep_login_toggle, True, "로그인 상태 유지")
        self._set_toggle(driver, self._sel.ip_security_toggle, False, "IP보안")

    def _set_toggle(self, driver, selectors, desired: bool, label: str) -> None:
        element = self._find_first(driver, selectors, require_visible=False)
        if element is None:
            # id/class 가 바뀐 경우 화면 문구로 찾아본다(현행 로그인 화면 대응)
            if self._toggle_by_label(driver, label, desired):
                return
            log.debug("%s 옵션을 찾지 못했습니다(건너뜀)", label)
            return
        try:
            current = bool(element.is_selected())
            if current == desired:
                log.debug("%s 이미 %s 상태", label, "켬" if desired else "끔")
                return
            # 숨겨진 체크박스는 라벨 클릭이 필요할 수 있어 JS 로 직접 토글한다
            driver.execute_script(
                "arguments[0].click();"
                "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));",
                element,
            )
            self._sleep(0.3)
            log.info("%s → %s", label, "켬" if desired else "끔")
        except Exception as e:  # noqa: BLE001 - 옵션 실패가 로그인을 막지 않는다
            log.debug("%s 설정 실패: %s", label, e)

    def _toggle_by_label(self, driver, label: str, desired: bool) -> bool:
        """화면 문구로 토글/체크박스를 찾아 원하는 상태로 맞춘다."""
        try:
            result = driver.execute_script(
                """
                const want = arguments[0], desired = arguments[1];
                const nodes = Array.from(document.querySelectorAll('label, span, div, button'));
                for (const node of nodes) {
                  const text = (node.innerText || node.textContent || '').trim();
                  if (text !== want) continue;
                  const scope = node.closest('label, li, div') || node;
                  const input = scope.querySelector(
                    "input[type='checkbox'], input[type='radio'], [role='switch']");
                  if (input) {
                    const on = input.checked !== undefined
                      ? input.checked
                      : input.getAttribute('aria-checked') === 'true';
                    if (on === desired) return 'already';
                    input.click();
                    return 'clicked';
                  }
                  node.click();
                  return 'clicked-text';
                }
                return '';
                """,
                label, desired,
            )
        except Exception as e:  # noqa: BLE001
            log.debug("%s 문구 기반 토글 실패: %s", label, e)
            return False
        if result:
            log.info("%s → %s (%s)", label, "켬" if desired else "끔", result)
            return True
        return False

    @staticmethod
    def _find_first(driver, selectors, require_visible: bool = True):
        """후보 셀렉터를 차례로 시도해 처음 찾은 요소를 돌려준다(없으면 None)."""
        from selenium.webdriver.common.by import By

        for selector in selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:  # noqa: BLE001
                continue
            for element in elements:
                try:
                    if not require_visible or element.is_displayed():
                        return element
                except Exception:  # noqa: BLE001
                    continue
        return None

    def _fill(self, driver, element, value: str) -> bool:
        """입력 전략을 순서대로 시도하고, 실제로 값이 들어갔는지 매번 검증한다.

        네이버 로그인 폼은 (1) 한 글자씩 send_keys 하는 자동화를 탐지하고
        (2) 붙여넣기(onpaste)를 아예 막아둔다. 그래서 JS/클립보드 레벨이 아니라
        **브라우저 레벨 입력(Chrome DevTools Protocol)** 을 우선 사용한다.

        1) CDP Input.insertText — IME 확정 입력과 같은 경로. paste 이벤트가 아니라서
           onpaste 차단과 무관하고, 키 입력 합성도 아니라 탐지 대상이 아니다.
        2) CDP Input.dispatchKeyEvent — 브라우저가 만드는 진짜 키 이벤트.
           JS 입장에선 실제 타이핑과 구분되지 않는다(사람처럼 불규칙 지연을 준다).
        3) 클립보드 붙여넣기 — onpaste 차단 핸들러를 걷어낸 뒤 시도.
        4) JS 값 주입 — 최후 수단(가장 탐지되기 쉬움).
        """
        strategies = (self._cdp_insert, self._cdp_type, self._paste, self._js_fill)
        last = len(strategies) - 1
        for i, attempt in enumerate(strategies):
            try:
                if attempt(driver, element, value):
                    actual = self._value_of(element)
                    # value 를 읽을 수 없는 경우(None)는 실패로 단정하지 않는다.
                    # 네이버는 입력값을 감추기도 하는데, 여기서 실패로 보고 지워버리면
                    # 정상 입력된 아이디까지 날아간다(실제로 발생했던 버그).
                    if actual is None or actual == value:
                        log.debug("입력 성공: %s", attempt.__name__)
                        return True
                    log.debug("%s: 값 불일치(%d자 입력됨)", attempt.__name__, len(actual))
            except Exception as e:  # noqa: BLE001 - 다음 전략으로 계속
                log.debug("%s 실패: %s", attempt.__name__, e)
            # 마지막 시도 뒤에는 지우지 않는다 — 입력된 내용을 남겨두고
            # 사용자가 이어서 로그인할 수 있게 한다.
            if i < last:
                self._clear(driver, element)
        return False

    def _clear(self, driver, element) -> None:
        try:
            driver.execute_script("arguments[0].value = '';", element)
        except Exception:  # noqa: BLE001
            pass

    def _focus(self, driver, element) -> None:
        try:
            element.click()
        except Exception:  # noqa: BLE001
            driver.execute_script("arguments[0].focus();", element)
        self._sleep(0.15)

    def _cdp_insert(self, driver, element, value: str) -> bool:
        """CDP Input.insertText — 붙여넣기 차단·키입력 탐지를 모두 우회한다."""
        self._focus(driver, element)
        driver.execute_cdp_cmd("Input.insertText", {"text": value})
        self._sleep(0.2)
        return True

    def _cdp_type(self, driver, element, value: str) -> bool:
        """CDP 로 브라우저 레벨 키 이벤트를 보낸다(사람 같은 불규칙 지연 포함)."""
        self._focus(driver, element)
        for ch in value:
            driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "keyDown", "text": ch})
            driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "keyUp", "text": ch})
            self._sleep(self._rng.uniform(0.05, 0.18))
        return True

    def _unlock_paste(self, driver, element) -> None:
        """onpaste 등 붙여넣기 차단 핸들러를 제거한다."""
        try:
            driver.execute_script(
                "for (const ev of ['paste','copy','cut','contextmenu','drop']) {"
                "  arguments[0]['on'+ev] = null;"
                "  arguments[0].addEventListener(ev, e => e.stopImmediatePropagation(), true);"
                "  document['on'+ev] = null;"
                "}",
                element,
            )
        except Exception as e:  # noqa: BLE001
            log.debug("붙여넣기 차단 해제 실패: %s", e)

    def _paste(self, driver, element, value: str) -> bool:
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.keys import Keys

        if not copy_to_clipboard(value):
            return False
        self._unlock_paste(driver, element)
        modifier = Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL
        self._focus(driver, element)
        ActionChains(driver).key_down(modifier).send_keys("v").key_up(modifier).perform()
        self._sleep(0.3)
        return True

    def _js_fill(self, driver, element, value: str) -> bool:
        driver.execute_script(
            "arguments[0].value = arguments[1];"
            "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));"
            "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));",
            element, value,
        )
        self._sleep(0.2)
        return True

    @staticmethod
    def _value_of(element):
        """입력란의 현재 값. 읽을 수 없으면 None (빈 문자열과 구분한다).

        None 과 "" 을 구분하지 않으면 '값을 못 읽는 상황'을 '입력 실패'로 오판해
        정상 입력된 값을 지워버리게 된다.
        """
        try:
            return element.get_attribute("value")
        except Exception:  # noqa: BLE001
            return None

    # --- 발행 흐름 ---
    #
    # 발행은 되돌리기 어려우므로 세 단계로 나눈다:
    #   write_post()     글쓰기 페이지를 열고 제목·본문을 작성한다(에디터는 열어 둔다)
    #   save_draft()     네이버 임시저장함에 넣는다
    #   finish_publish() 사람이 확인한 뒤 실제로 발행한다
    #
    # 세 단계는 같은 드라이버 세션을 공유한다. write_post 뒤에 브라우저를 닫으면
    # 작성 중이던 내용이 사라지므로 close() 는 마지막에만 부른다.

    def write_post(self, post: Post) -> None:
        """글쓰기 페이지를 열고 제목과 본문을 작성한다. 발행하지 않는다."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import TimeoutException, NoSuchElementException

        driver = self._ensure_driver()
        wait = WebDriverWait(driver, self._cfg.wait_timeout)
        step = "글쓰기 페이지 열기"
        try:
            driver.get(WRITE_URL.format(blog_id=self._cfg.blog_id or self._cfg.naver_id))
            self._sleep(2.0)

            step = "에디터 프레임 진입"
            wait.until(EC.frame_to_be_available_and_switch_to_it((By.ID, self._sel.main_frame)))
            self._sleep(1.5)

            step = "팝업 닫기"
            self._dismiss_popups(driver)

            step = "제목 입력"
            title_el = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, self._sel.title_area))
            )
            self._type_into(driver, title_el, post.title)

            step = "본문 작성"
            body_el = self._find_body_element(driver)
            self._write_blocks(driver, body_el, post)

            # 글 순서가 실제로 의도대로 배치됐는지는 코드만으로 확신할 수 없다
            # (에디터 내부 동작이라). 작성 직후 상태를 스크린샷으로 남겨 눈으로
            # 확인할 수 있게 한다.
            # ⚠️ _dump_diagnostics 는 캡처를 위해 default_content 로 나가므로,
            # 저장/발행 버튼은 mainFrame 안에 있어 반드시 되돌아와야 한다.
            self._dump_diagnostics(driver, "본문_작성_완료")
            self._return_to_main_frame(driver)
            log.info("본문 작성 완료 — 아직 발행하지 않았습니다")
        except (TimeoutException, NoSuchElementException) as e:
            dump = self._dump_diagnostics(driver, step)
            raise NaverError(
                f"글 작성 실패 [{step}] — 에디터 DOM 이 바뀌었을 수 있습니다. "
                f"({type(e).__name__})"
                + (f"\n진단 파일: {dump}" if dump else "")
            ) from e

    def save_draft(self) -> bool:
        """네이버 임시저장함에 저장한다.

        저장 버튼을 못 찾아도 예외로 올리지 않는다 — 본문은 이미 에디터에 들어가
        있으므로, 사람이 브라우저에서 직접 저장하거나 그대로 발행할 수 있다.
        찾지 못한 경우 False 를 돌려주고 호출자가 그 사실을 알린다.
        """
        driver = self._ensure_driver()
        self._return_to_main_frame(driver)

        button = self._find_first(driver, self._sel.save_draft_button)
        if button is None:
            # 클래스 해시가 바뀌었을 때를 위한 문구 기반 폴백.
            if self._click_button_by_text(driver, ("저장",)):
                self._sleep(1.5)
                log.info("임시저장 완료(문구 기반 탐색)")
                return True
            log.warning(
                "임시저장 버튼을 찾지 못했습니다. 본문은 에디터에 들어가 있으니 "
                "브라우저에서 직접 저장하거나 발행하세요."
            )
            self._dump_diagnostics(driver, "임시저장_버튼_없음")
            return False
        try:
            button.click()
            self._sleep(1.5)
            log.info("임시저장 완료")
            return True
        except Exception as e:  # noqa: BLE001 - 저장 실패가 본문을 날리지 않는다
            log.warning("임시저장 클릭 실패: %s", e)
            return False

    def finish_publish(self, *, open_type: str = "", category: str = "") -> PublishResult:
        """작성해 둔 글을 실제로 발행한다."""
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.common.exceptions import TimeoutException, NoSuchElementException

        driver = self._ensure_driver()
        wait = WebDriverWait(driver, self._cfg.wait_timeout)
        open_type = open_type or self._cfg.open_type
        category = category or self._cfg.category
        try:
            self._return_to_main_frame(driver)
            url = self._do_publish(driver, wait, open_type=open_type, category=category)
            log.info("네이버 발행 완료 (공개범위=%s): %s", open_type, url)
            return PublishResult(url=url, extra={"open_type": open_type, "category": category})
        except (TimeoutException, NoSuchElementException) as e:
            dump = self._dump_diagnostics(driver, "발행")
            raise NaverError(
                f"발행 실패 — 에디터 DOM 이 바뀌었을 수 있습니다. ({type(e).__name__})"
                + (f"\n진단 파일: {dump}" if dump else "")
            ) from e
        finally:
            try:
                driver.switch_to.default_content()
            except Exception:  # noqa: BLE001
                pass

    def publish(self, post: Post) -> PublishResult:
        """작성과 발행을 한 번에. 검수 없이 바로 나가므로 신중히 쓴다."""
        self.write_post(post)
        return self.finish_publish(category=post.category)

    def _dismiss_popups(self, driver) -> None:
        from selenium.webdriver.common.by import By

        for sel in self._sel.popup_close:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    if el.is_displayed():
                        el.click()
                        self._sleep(0.4)
                except Exception:  # noqa: BLE001
                    continue

    def _type_into(self, driver, element, text: str) -> None:
        from selenium.webdriver.common.action_chains import ActionChains

        element.click()
        self._sleep(0.3)
        ActionChains(driver).send_keys(text).perform()
        self._sleep(0.3)

    def _find_body_element(self, driver):
        """제목을 제외한 첫 본문 문단을 찾는다.

        제목과 본문 모두 .se-text-paragraph 를 쓰기 때문에 단순 셀렉터로는
        제목이 먼저 잡힌다. 제목 컨테이너 안쪽을 명시적으로 제외한다.
        """
        element = driver.execute_script(
            """
            const titleSel = arguments[0], bodySel = arguments[1];
            const nodes = document.querySelectorAll(bodySel);
            for (const node of nodes) {
              if (!node.closest(titleSel)) return node;
            }
            return null;
            """,
            self._sel.title_container,
            self._sel.body_area,
        )
        if element is None:
            raise NaverError(
                "본문 입력 영역을 찾지 못했습니다(제목과 구분 불가). "
                "naver.NaverSelectors 의 title_container/body_area 를 확인하세요."
            )
        return element

    def _focus_body_end(self, driver, body_el=None) -> None:
        """본문 커서를 실제 '마지막' 문단 끝으로 옮긴다.

        ⚠️ body_el 을 처음 한 번만 찾아서 계속 재사용하면(과거 방식) 그
        사이 이미지 삽입·줄바꿈으로 문단이 늘어난 뒤엔 더 이상 '마지막'
        문단이 아니게 된다 — 이미지를 첨부한 뒤 커서가 엉뚱한 위치(문서
        맨 앞 등)로 튀면서 글이 뒤죽박죽 섞이는 실사고가 있었다(사용자
        리포트). 그래서 호출할 때마다 DOM 에서 마지막 문단을 다시 찾는다.
        body_el 인자는 못 찾았을 때의 폴백으로만 쓴다(하위 호환).
        """
        el = None
        try:
            el = driver.execute_script(
                """
                const bodySel = arguments[0], titleSel = arguments[1];
                const nodes = document.querySelectorAll(bodySel);
                let last = null;
                for (const node of nodes) {
                  if (!node.closest(titleSel)) last = node;
                }
                return last;
                """,
                self._sel.body_area, self._sel.title_container,
            )
        except Exception as e:  # noqa: BLE001
            log.debug("마지막 문단 탐색 실패: %s", e)
        el = el or body_el
        if el is None:
            return
        try:
            el.click()
        except Exception:  # noqa: BLE001
            pass
        self._sleep(0.3)
        try:
            driver.execute_script(
                """
                const el = arguments[0];
                el.focus();
                const range = document.createRange();
                range.selectNodeContents(el);
                range.collapse(false);
                const sel = window.getSelection();
                sel.removeAllRanges();
                sel.addRange(range);
                """,
                el,
            )
        except Exception as e:  # noqa: BLE001
            log.debug("커서 이동 실패(클릭 위치 사용): %s", e)
        self._sleep(0.2)

    def _write_blocks(self, driver, body_el, post) -> None:
        """본문을 블록 단위로 실제 요소로 작성한다.

        본문 전체를 한 덩어리 텍스트로 타이핑하면 이미지는 글자로, 링크는 맨
        URL 로 들어간다. 그래서 블록마다 다르게 처리한다:

        - image   → 실제 파일을 업로드해 삽입하고, 캡션이 있으면 아래 줄에 출처를 쓴다
        - link    → 자동 링크가 걸리도록 URL 을 독립 줄로 입력
        - quote   → 인용문 + 출처 URL
        - list    → 항목마다 한 줄
        - heading → 앞뒤 여백을 준 소제목 줄

        이미지 업로드가 실패해도 글 전체를 버리지 않고 해당 이미지만 건너뛴다.
        """
        self._focus_body_end(driver, body_el)
        self._apply_base_format(driver)

        tmp_dir = Path(tempfile.mkdtemp(prefix="naver-blog-img-"))
        inserted_images = 0
        attempted_images = 0
        seen_heading = False  # 두 번째 소제목부터 직전에 구분선을 넣는다
        try:
            for block in post.blocks:
                # 매 블록 작성 전에 커서를 실제 마지막 위치로 되돌린다 — 이미지
                # 삽입(드롭 이벤트) 이후 에디터가 포커스를 엉뚱한 곳으로 옮겨
                # 놓는 경우가 있어(실사고: 글이 뒤죽박죽 섞임), 다음 블록을
                # 쓰기 직전마다 항상 재확인한다.
                self._focus_body_end(driver)

                if block.kind == "heading":
                    if seen_heading:
                        # 구분선은 소제목과 다음 소제목을 구별하는 용도로만 쓴다
                        # (문단마다 넣으면 너무 잦다).
                        self._insert_divider(driver)
                    seen_heading = True
                    self._newline(driver)
                    self._write_heading(driver, block.text, block.level)
                    self._newline(driver)
                    # ⚠️ 커서가 새 문단으로 넘어간 '뒤'에 복구해야 한다 — 소제목
                    # 문단 안에서 복구하면 방금 쓴 소제목까지 같이 되돌아간다.
                    self._restore_body_format(driver)

                elif block.kind == "paragraph":
                    self._type_line(driver, block.text)
                    self._newline(driver)

                elif block.kind == "image":
                    attempted_images += 1
                    if self._insert_image(driver, block, tmp_dir):
                        inserted_images += 1
                    self._focus_body_end(driver)  # 삽입 직후 커서 위치 재확인

                elif block.kind == "quote":
                    self._insert_quote(driver, block)

                elif block.kind == "list":
                    self._insert_list(driver, block)

                elif block.kind == "link":
                    self._insert_link(driver, block)

                elif block.kind == "divider":
                    self._insert_divider(driver)

            if attempted_images and inserted_images < attempted_images:
                # 이미지가 부분/전부 빠지면 글이 '깡통'처럼 보이니 조용히 넘어가지
                # 않고 눈에 띄게 경고한다.
                log.warning(
                    "본문 작성 완료 — 이미지 %d개 중 %d개만 삽입됨(%d개 실패). "
                    "이미지를 미리 내려받아 로컬 경로로 넘겼는지 확인하세요.",
                    attempted_images, inserted_images, attempted_images - inserted_images,
                )
            else:
                log.info("본문 작성 완료 (이미지 %d개 삽입)", inserted_images)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _insert_divider(self, driver) -> None:
        """소제목 사이에 구분선(수평선)을 넣어 섹션을 시각적으로 나눈다.

        2026-08-20 사용자 요청: 소제목 다음 섹션으로 넘어가기 전에 구분선을
        넣으면 구별이 더 쉽다. 버튼을 못 찾아도(추측 셀렉터이므로) 에러로
        취급하지 않고 조용히 건너뛴다 — 구분선 없이도 글은 정상이다.
        """
        btn = self._find_first(driver, self._sel.divider_button)
        if btn is None:
            log.debug("구분선 버튼을 찾지 못했습니다(건너뜀)")
            return
        try:
            btn.click()
            self._sleep(0.4)
            self._newline(driver)
        except Exception as e:  # noqa: BLE001 - 구분선 실패가 글을 막지 않는다
            log.debug("구분선 삽입 실패: %s", e)

    def _pick_from_toolbar(self, driver, openers, value: str, label: str) -> bool:
        """툴바 드롭다운을 열고 표시 문구로 항목을 고른다(글꼴/크기).

        실패해도 에디터 기본 서식으로 계속 진행한다.
        """
        opener = self._find_first(driver, openers)
        if opener is None:
            log.debug("%s 드롭다운을 찾지 못했습니다(기본값 사용)", label)
            return False
        try:
            opener.click()
            self._sleep(0.4)
            picked = driver.execute_script(
                """
                const want = String(arguments[0]).trim();
                const nodes = document.querySelectorAll('li, button, a, span');
                for (const node of nodes) {
                  const text = (node.innerText || node.textContent || '').trim();
                  if (text === want || text === want + 'pt' || text === want + 'px') {
                    node.click();
                    return true;
                  }
                }
                return false;
                """,
                value,
            )
            self._sleep(0.3)
            if picked:
                log.info("%s: %s", label, value)
                return True
            log.warning("%s '%s' 항목을 찾지 못했습니다(기본값 사용)", label, value)
            # 열어둔 드롭다운을 닫는다
            try:
                opener.click()
            except Exception:  # noqa: BLE001
                pass
        except Exception as e:  # noqa: BLE001
            log.debug("%s 선택 실패: %s", label, e)
        return False

    def _apply_base_format(self, driver) -> None:
        """본문 작성 전에 기본 글꼴/크기를 적용한다(설정된 경우에만)."""
        if self._cfg.font_family:
            self._pick_from_toolbar(
                driver, self._sel.font_family_opener, self._cfg.font_family, "글꼴"
            )
        if self._cfg.font_size:
            self._pick_from_toolbar(
                driver, self._sel.font_size_opener, self._cfg.font_size, "글자 크기"
            )

    def _write_heading(self, driver, text: str, level: int = 3) -> None:
        """소제목을 본문과 구분되게 쓴다(대괄호로 감싸기 + 네이버 '소제목' 문단 서식).

        2026-08-20: 이전엔 문단 서식 드롭다운 여는 버튼의 셀렉터를
        data-name='paragraph-style'/'style' 로 추측했는데 전부 틀렸다 —
        그래서 드롭다운 자체가 안 열려 매번 조용히 실패하고 있었다. 사용자가
        실제 DOM(우클릭 검사)을 캡처해 정확한 값(data-name="text-format")을
        확인해 줘서 그걸로 고쳤다. 그 전에 시도했던 클립보드 HTML 붙여넣기도
        (검증 안 된 OS 클립보드 트릭이라) 화면 확인 결과 실패했었다 — 이제는
        확실한 DOM 증거가 있는 이 방식으로 되돌린다.
        ⚠️ 소제목을 쓴 뒤 '본문'으로 되돌리는 건 반드시 커서가 새 문단으로
        넘어간 '다음'에 해야 한다 — 아직 소제목 문단 안에 커서가 있는 채로
        되돌리면 문단 서식 드롭다운이 그 커서가 있는 문단(=방금 쓴 소제목
        자신)에 적용돼, 막 적용한 굵게/큰 글자가 그 자리에서 바로 취소돼
        버린다(실사고: 사용자가 화면으로 확인 — 소제목이 잠깐 크고 굵게
        보이다가 바로 아래 본문을 이어 쓰면서 다시 작아짐). 그래서 복구는
        이 메서드가 아니라 호출자가 줄바꿈 이후에 _restore_body_format 로
        따로 한다.
        """
        wrapped = f"【{text}】" if level == 2 else f"[{text}]"
        if self._cfg.heading_bold:
            self._pick_from_toolbar(
                driver, self._sel.text_format_opener, "소제목", "문단 서식(소제목)"
            )
        if self._cfg.heading_font_size:
            self._pick_from_toolbar(
                driver, self._sel.font_size_opener,
                self._cfg.heading_font_size, "소제목 크기",
            )
        self._type_line(driver, wrapped)

    def _restore_body_format(self, driver) -> None:
        """소제목 다음 문단으로 커서가 넘어간 '뒤'에 본문 서식을 복구한다.

        _write_heading 안에서 바로 복구하면 아직 소제목 문단 안이라 방금 쓴
        소제목까지 같이 되돌아간다(자세한 배경은 _write_heading 참고) — 그래서
        반드시 줄바꿈 이후, 새 문단에 커서가 있을 때 호출해야 한다.
        """
        if self._cfg.heading_bold:
            self._pick_from_toolbar(
                driver, self._sel.text_format_opener, "본문", "문단 서식(본문)"
            )
        if self._cfg.heading_font_size and self._cfg.font_size:
            self._pick_from_toolbar(
                driver, self._sel.font_size_opener, self._cfg.font_size, "본문 크기 복구"
            )

    def _type_line(self, driver, text: str) -> None:
        """한 줄을 입력한다. 붙여넣기가 막힌 곳도 있으므로 CDP insertText 를 쓴다."""
        if not text:
            return
        try:
            driver.execute_cdp_cmd("Input.insertText", {"text": text})
        except Exception:  # noqa: BLE001 - CDP 불가 시 일반 입력으로
            from selenium.webdriver.common.action_chains import ActionChains

            ActionChains(driver).send_keys(text).perform()
        self._sleep(self._rng.uniform(0.15, 0.35))

    def _newline(self, driver) -> None:
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.keys import Keys

        ActionChains(driver).send_keys(Keys.ENTER).perform()
        self._sleep(0.2)

    def _find_file_input(self, driver):
        """현재 프레임(mainFrame)과 최상위 문서 양쪽에서 파일 input 을 찾는다.

        스마트에디터가 업로드 모달을 iframe 밖(최상위 document)에 붙이는 경우가
        있어, mainFrame 안에서 못 찾으면 default_content 로도 확인해 본다.
        찾았다면 (input, 그 input 이 있던 프레임이 mainFrame인지 여부) 를 돌려준다.
        """
        from selenium.webdriver.common.by import By

        inputs = driver.find_elements(By.CSS_SELECTOR, self._sel.image_file_input)
        if inputs:
            return inputs[0], True
        try:
            driver.switch_to.default_content()
            inputs = driver.find_elements(By.CSS_SELECTOR, self._sel.image_file_input)
            if inputs:
                return inputs[0], False
        except Exception:  # noqa: BLE001
            pass
        finally:
            if not inputs:
                # mainFrame 으로 되돌아가야 이후 본문 작성이 계속 정상 동작한다
                try:
                    from selenium.webdriver.support.ui import WebDriverWait
                    from selenium.webdriver.support import expected_conditions as EC
                    WebDriverWait(driver, 5).until(
                        EC.frame_to_be_available_and_switch_to_it((By.ID, self._sel.main_frame))
                    )
                except Exception:  # noqa: BLE001
                    pass
        return None, True

    def _upload_image_file(self, driver, path: Path, what: str) -> bool:
        """로컬 이미지 파일을 에디터에 업로드한다(공통 경로).

        1순위: 파일 드롭 이벤트 재현 — 에디터 자신의 업로드 로직을 그대로 탄다
               (실제 HAR 캡처로 확인된 방식, 가장 신뢰도 높음).
        2순위: "사진" 버튼 클릭 + input[type=file] 탐색 — 드롭이 안 먹히는
               구버전/예외적인 DOM 대비 폴백.
        """
        if self._drop_image_file(driver, path, what):
            return True
        return self._upload_image_file_via_input(driver, path, what)

    def _read_image_b64(self, path: Path) -> tuple[str, str]:
        import base64
        import mimetypes

        mime, _ = mimetypes.guess_type(path.name)
        mime = mime or "image/jpeg"
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        return b64, mime

    def _count_pstatic_images(self, driver) -> int:
        """업로드 완료 여부를 간접 확인한다 — 값을 못 읽으면(테스트용 더미
        드라이버 포함) 0으로 취급해, None 비교로 죽지 않고 그냥 '아직 없음'
        으로 처리되게 한다."""
        try:
            result = driver.execute_script(
                "return document.querySelectorAll(\"img[src*='pstatic.net']\").length;"
            )
            return int(result) if isinstance(result, (int, float)) else 0
        except Exception:  # noqa: BLE001
            return 0

    def _drop_image_file(self, driver, path: Path, what: str) -> bool:
        """이미지 파일을 실제로 드롭한 것처럼 에디터에 흘려 넣는다.

        에디터가 알아서 세션키 발급 → 업로드까지 처리하므로(비동기), 결과는
        업로드된 이미지(pstatic.net)가 화면에 늘어났는지 잠깐 기다리며 확인한다.
        """
        try:
            b64, mime = self._read_image_b64(path)
        except OSError as e:
            log.warning("이미지 파일을 읽지 못했습니다(%s) — %s", type(e).__name__, what)
            return False
        before = self._count_pstatic_images(driver)
        try:
            driver.execute_script(_DROP_IMAGE_JS, b64, mime, path.name)
        except Exception as e:  # noqa: BLE001
            log.debug("이미지 드롭 이벤트 삽입 실패(%s): %s", what, e)
            return False
        deadline = self._clock() + max(self._cfg.image_upload_wait_s, 8.0)
        while self._clock() < deadline:
            self._sleep(0.5)
            if self._count_pstatic_images(driver) > before:
                log.info("이미지 삽입: %s", what)
                return True
        return False

    def _upload_image_file_via_input(self, driver, path: Path, what: str) -> bool:
        """폴백: input[type=file] 을 DOM 에서 그냥 찾아본다(클릭 없이).

        ⚠️ 여기서 절대 "사진" 버튼 등을 추측 클릭하면 안 된다 — 그 버튼이
        내부적으로 진짜 파일 input 을 click() 하면 OS 네이티브 파일 선택창이
        열려버리고, Selenium 은 그 창을 닫거나 다룰 수 없어 브라우저가 통째로
        멈춘다(실제로 발생한 사고). 이미 DOM 에 존재하는 input 을 조용히
        찾는 것만 시도하고, 없으면 순순히 건너뛴다.
        """
        try:
            file_input, was_in_main_frame = self._find_file_input(driver)
            if file_input is None:
                log.warning("이미지 업로드 입력란을 찾지 못했습니다(%s 건너뜀)", what)
                if not self._image_diag_dumped:
                    self._dump_diagnostics(driver, "이미지_입력란_없음")
                    self._image_diag_dumped = True
                return False
            # 숨겨진 input 은 send_keys 가 안 되므로 잠시 보이게 만든다
            # (driver 는 이 시점에 file_input 을 찾은 프레임 컨텍스트에 그대로 있다)
            driver.execute_script(
                "arguments[0].style.display='block';"
                "arguments[0].style.visibility='visible';"
                "arguments[0].style.opacity=1;"
                "arguments[0].style.width='1px';arguments[0].style.height='1px';",
                file_input,
            )
            file_input.send_keys(str(path.resolve()))
            self._sleep(self._cfg.image_upload_wait_s)
            if not was_in_main_frame:
                # default_content 에서 찾았으니 이후 본문 작성을 위해 되돌아간다
                self._return_to_main_frame(driver)
            log.info("이미지 삽입(폴백 경로): %s", what)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("이미지 삽입 실패(%s) — %s 건너뜁니다", type(e).__name__, what)
            return False

    def _return_to_main_frame(self, driver) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        try:
            driver.switch_to.default_content()
            WebDriverWait(driver, 5).until(
                EC.frame_to_be_available_and_switch_to_it((By.ID, self._sel.main_frame))
            )
        except Exception:  # noqa: BLE001
            pass

    def _insert_image(self, driver, block, tmp_dir: Path) -> bool:
        """이미지를 에디터에 업로드해 삽입하고, 캡션이 있으면 아래에 출처를 쓴다.

        스마트에디터는 URL 삽입을 지원하지 않아 파일 업로드만 가능하다. 블록의
        `src` 는 로컬 경로가 권장이며(글을 쓰는 쪽이 미리 내려받는다), http(s)
        URL 은 폴백으로만 처리한다 — images.resolve_image 참고.
        """
        path = resolve_image(block.src, tmp_dir)
        if path is None:
            log.warning("이미지를 건너뜁니다: %s", block.src[:120])
            return False

        ok = self._upload_image_file(driver, path, block.alt or path.name)
        if not ok:
            return False
        if self._cfg.shrink_large_images:
            self._shrink_image_if_too_big(driver)

        if block.caption:
            # 정보성 글에서 캡션은 출처다. 이미지 바로 아래 줄에 남긴다.
            self._focus_body_end(driver)
            self._type_line(driver, block.caption)
            self._newline(driver)
        return True

    def _shrink_image_if_too_big(self, driver) -> None:
        """방금 삽입한 이미지가 칼럼 폭을 꽉 채우면 '작게하기'를 한 번 눌러 축소한다.

        2026-08-20 사용자 요청: "이미지 크기를 꽉 채우게 하지말고 70%정도 크기로만
        들어가게" — 모든 이미지를 줄일 필요는 없고 너무 큰 경우에만 줄이면 된다.
        네이버 HAR 로 확인한 사실: 새로 삽입된 사진은 기본적으로 본문 칼럼 폭에
        꽉 차게(contentMode="fit") 들어오고, "작게하기" 버튼을 한 번 누르면
        폭이 정확히 이전 값의 75%로 줄어든다(693px → 519px 확인). 그래서 이미지
        폭이 본문 문단 폭과 거의 같을 때만 "너무 크다"고 보고 한 번만 줄인다 —
        원본이 이미 작은 이미지(칼럼 폭보다 좁게 들어온 경우)는 건드리지 않는다.

        ⚠️ 버튼 자체의 DOM 은 캡처되지 않았다(HAR 은 네트워크 요청만 담아
        width 변화는 확인됐지만 어떤 클래스의 버튼을 눌렀는지는 알 수 없다).
        그래서 후보 셀렉터 탐색이 실패하면 문구("작게") 기반 탐색으로
        보완하고, 그마저 실패하면 조용히 건너뛴다 — 이미지가 큰 채로 남을
        뿐 발행 자체를 막지 않는다(다른 추정 셀렉터 기능과 동일한 원칙).
        """
        try:
            img = driver.execute_script(
                "const imgs = document.querySelectorAll(\"img[src*='pstatic.net']\");"
                "return imgs.length ? imgs[imgs.length - 1] : null;"
            )
            if img is None:
                return
            para = driver.execute_script(
                """
                const bodySel = arguments[0], titleSel = arguments[1];
                const nodes = document.querySelectorAll(bodySel);
                let last = null;
                for (const node of nodes) {
                  if (!node.closest(titleSel)) last = node;
                }
                return last;
                """,
                self._sel.body_area, self._sel.title_container,
            )
            col_width = driver.execute_script(
                "return arguments[0] ? arguments[0].getBoundingClientRect().width : 0;", para
            )
            img_width = driver.execute_script(
                "return arguments[0].getBoundingClientRect().width;", img
            )
            if not col_width or not img_width or img_width < col_width * 0.95:
                return  # 이미 충분히 작다 — 건드리지 않는다
            try:
                img.click()
            except Exception:  # noqa: BLE001
                driver.execute_script("arguments[0].click();", img)
            self._sleep(0.4)
            btn = self._find_first(driver, self._sel.image_shrink_button)
            if btn is not None:
                btn.click()
            else:
                found = driver.execute_script(
                    """
                    const nodes = document.querySelectorAll('button, a, span[role="button"]');
                    for (const node of nodes) {
                      const label = (node.getAttribute('aria-label') || node.getAttribute('title')
                        || node.innerText || node.textContent || '').trim();
                      if (label.includes('작게')) { node.click(); return true; }
                    }
                    return false;
                    """
                )
                if not found:
                    log.debug("이미지 축소 버튼을 찾지 못했습니다(건너뜀)")
                    return
            self._sleep(0.4)
            log.info("이미지가 칼럼 폭을 꽉 채워 한 단계 축소했습니다")
        except Exception as e:  # noqa: BLE001 - 축소 실패가 발행을 막지 않는다
            log.debug("이미지 축소 실패: %s", e)

    def _insert_link(self, driver, block) -> None:
        """링크를 독립된 줄로 입력한다.

        스마트에디터는 URL 을 독립된 줄에 넣고 줄바꿈하면 자동으로 하이퍼링크
        (및 링크 카드)로 바꾼다. 표시 문구가 있으면 URL 앞 줄에 먼저 쓴다.
        """
        if not block.url:
            return
        if block.text and block.text != block.url:
            self._type_line(driver, block.text)
            self._newline(driver)
        self._type_line(driver, block.url)
        self._newline(driver)  # 자동 링크 변환 트리거
        self._sleep(self._cfg.link_convert_wait_s)

    def _insert_quote(self, driver, block) -> None:
        """인용문을 넣는다. 출처 URL 이 있으면 다음 줄에 붙인다.

        에디터의 인용구 서식 전환은 문단 서식 드롭다운을 거쳐야 하는데, 실패가
        잦고 실패하면 뒤따르는 본문 서식까지 인용구로 끌려간다. 그래서 인용
        부호를 붙인 평문으로 쓴다 — 덜 예쁘지만 절대 깨지지 않는다.
        """
        if not block.text:
            return
        self._type_line(driver, f"\u201c{block.text}\u201d")
        self._newline(driver)
        if block.url:
            self._type_line(driver, block.url)
            self._newline(driver)
            self._sleep(self._cfg.link_convert_wait_s)

    def _insert_list(self, driver, block) -> None:
        """목록을 항목마다 한 줄씩 쓴다.

        에디터의 목록 버튼을 쓰면 이후 문단이 계속 목록으로 이어져 빠져나오기가
        까다롭다. 글머리 기호를 직접 붙이는 편이 안전하다.
        """
        for i, item in enumerate(block.items, start=1):
            marker = f"{i}. " if block.ordered else "\u00b7 "
            self._type_line(driver, marker + item)
            self._newline(driver)

    def _do_publish(self, driver, wait, *, open_type: str, category: str) -> str:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC

        wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, self._sel.publish_open_button))
        ).click()
        self._sleep(1.2)

        if category:
            self._select_category(driver, category)

        label = "전체공개" if open_type == "public" else "비공개"
        if not self._select_open_type(driver, label):
            # 공개 범위를 확실히 못 정했는데 전체공개로 나가면 사고다 — 중단한다.
            if open_type != "public":
                raise NaverError(
                    f"공개 설정에서 '{label}' 을 선택하지 못했습니다. "
                    "의도치 않은 전체공개를 막기 위해 발행을 중단합니다."
                )
            log.warning("공개 설정 '%s' 선택 실패 — 네이버 기본값으로 진행", label)
        self._sleep(0.5)

        wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, self._sel.publish_confirm_button))
        ).click()
        self._sleep(3.0)
        return driver.current_url

    def _click_by_label(self, driver, texts, scope: str = "") -> bool:
        """화면에 보이는 글자로 라디오/체크박스를 찾아 클릭한다.

        네이버는 id/class 를 자주 바꾸지만 '비공개' 같은 표시 문구는 안정적이다.
        라벨을 클릭해야 실제로 선택되는 경우가 많아 라벨 → input 순으로 시도한다.
        """
        script = """
        const wanted = arguments[0];
        const labels = Array.from(document.querySelectorAll('label'));
        for (const want of wanted) {
          for (const label of labels) {
            const text = (label.innerText || label.textContent || '').trim();
            if (text !== want) continue;
            const input = label.querySelector('input')
              || (label.htmlFor ? document.getElementById(label.htmlFor) : null);
            if (input && input.checked) return 'already';
            label.click();
            if (input && !input.checked) { input.click(); }
            return 'clicked';
          }
        }
        return '';
        """
        try:
            result = driver.execute_script(script, list(texts))
        except Exception as e:  # noqa: BLE001
            log.debug("라벨 클릭 실패(%s): %s", texts, e)
            return False
        if result:
            log.debug("라벨 선택(%s): %s", texts, result)
            return True
        return False

    def _click_button_by_text(self, driver, texts) -> bool:
        """표시 문구로 버튼/링크를 찾아 클릭한다.

        _click_by_label 은 라디오·체크박스용(label → input)이고, 이쪽은 저장·
        발행처럼 그냥 눌리는 버튼용이다. 네이버는 클래스 해시를 자주 바꾸지만
        버튼에 적힌 글자는 잘 바뀌지 않는다.
        """
        script = """
        const wanted = arguments[0];
        const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
        for (const want of wanted) {
          for (const node of nodes) {
            const text = (node.innerText || node.textContent || '').trim();
            if (text !== want) continue;
            const rect = node.getBoundingClientRect();
            if (!rect.width || !rect.height) continue;   // 숨은 버튼은 건너뛴다
            node.click();
            return true;
          }
        }
        return false;
        """
        try:
            return bool(driver.execute_script(script, list(texts)))
        except Exception as e:  # noqa: BLE001
            log.debug("버튼 클릭 실패(%s): %s", texts, e)
            return False

    def list_categories(self) -> list[str]:
        """블로그 카테고리 이름 목록을 읽는다.

        발행 패널을 열어야 카테고리 목록이 렌더링되므로 글쓰기 페이지를 열고
        발행 버튼을 누른다. **발행 확인 버튼은 누르지 않으므로 글이 나가지
        않는다.** 읽고 나면 패널을 esc 로 닫는다.
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        driver = self._ensure_driver()
        wait = WebDriverWait(driver, self._cfg.wait_timeout)
        driver.get(WRITE_URL.format(blog_id=self._cfg.blog_id or self._cfg.naver_id))
        self._sleep(2.0)
        wait.until(EC.frame_to_be_available_and_switch_to_it((By.ID, self._sel.main_frame)))
        self._sleep(1.0)
        self._dismiss_popups(driver)

        wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, self._sel.publish_open_button))
        ).click()
        self._sleep(1.2)

        names = driver.execute_script(
            """
            const out = [];
            for (const sel of document.querySelectorAll('select')) {
              for (const opt of sel.options) {
                const t = (opt.text || '').trim();
                if (t) out.push(t);
              }
            }
            if (out.length) return out;
            // 커스텀 드롭다운 — 카테고리 영역의 항목을 훑는다
            const scopes = document.querySelectorAll(
              '[class*="category"], [class*="Category"]');
            for (const scope of scopes) {
              for (const item of scope.querySelectorAll('li, label, a')) {
                const t = (item.innerText || item.textContent || '').trim();
                if (t && t.length < 40 && !out.includes(t)) out.push(t);
              }
            }
            return out;
            """
        ) or []

        try:
            driver.switch_to.active_element.send_keys(Keys.ESCAPE)
        except Exception:  # noqa: BLE001
            pass
        try:
            driver.switch_to.default_content()
        except Exception:  # noqa: BLE001
            pass
        return [str(n) for n in names]

    def _select_open_type(self, driver, label: str) -> bool:
        """공개 설정 라디오를 선택하고 실제로 선택됐는지 확인한다."""
        if not self._click_by_label(driver, [label]):
            return False
        self._sleep(0.3)
        # 확인: 해당 라벨의 input 이 checked 인지
        try:
            checked = driver.execute_script(
                """
                const want = arguments[0];
                for (const label of document.querySelectorAll('label')) {
                  const text = (label.innerText || label.textContent || '').trim();
                  if (text !== want) continue;
                  const input = label.querySelector('input')
                    || (label.htmlFor ? document.getElementById(label.htmlFor) : null);
                  return !!(input && input.checked);
                }
                return false;
                """,
                label,
            )
        except Exception:  # noqa: BLE001
            checked = False
        if checked:
            log.info("공개 설정: %s", label)
        else:
            log.warning("공개 설정 '%s' 확인 실패", label)
        return bool(checked)

    def _select_category(self, driver, name: str) -> None:
        """카테고리 드롭다운에서 이름으로 선택한다(실패해도 발행은 계속)."""
        try:
            result = driver.execute_script(
                """
                const want = arguments[0];
                // 1) 표준 <select>
                for (const sel of document.querySelectorAll('select')) {
                  for (const opt of sel.options) {
                    if ((opt.text || '').trim() === want) {
                      sel.value = opt.value;
                      sel.dispatchEvent(new Event('change', {bubbles: true}));
                      return 'select';
                    }
                  }
                }
                // 2) 커스텀 드롭다운 — 목록을 펼친 뒤 항목 클릭
                const openers = document.querySelectorAll(
                  '.selectbox_button, [class*="category"] button, [class*="Category"] button');
                for (const opener of openers) { try { opener.click(); } catch (e) {} }
                const items = document.querySelectorAll('li, label, a, span, button');
                for (const item of items) {
                  const text = (item.innerText || item.textContent || '').trim();
                  if (text === want) { item.click(); return 'custom'; }
                }
                return '';
                """,
                name,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("카테고리 '%s' 선택 실패(기본 카테고리로 진행): %s", name, e)
            return
        if result:
            log.info("카테고리: %s", name)
        else:
            log.warning("카테고리 '%s' 를 찾지 못했습니다 — 기본 카테고리로 발행합니다", name)
        self._sleep(0.5)

    def _dump_diagnostics(self, driver, step: str):
        """실패 시점의 페이지 HTML 과 스크린샷을 저장한다.

        셀렉터를 추측으로 고치지 않고 실제 DOM 을 보고 맞추기 위한 것이다.
        저장 경로를 로그와 오류 메시지에 남긴다.
        """
        try:
            base = Path(self._profile_dir).parent / "diagnostics"
            base.mkdir(parents=True, exist_ok=True)
            stamp = str(int(self._clock()))
            safe_step = "".join(ch if ch.isalnum() else "_" for ch in step)[:40]
            html_path = base / f"{stamp}_{safe_step}.html"
            try:
                driver.switch_to.default_content()
            except Exception:  # noqa: BLE001
                pass
            html_path.write_text(driver.page_source, encoding="utf-8")
            try:
                driver.save_screenshot(str(base / f"{stamp}_{safe_step}.png"))
            except Exception:  # noqa: BLE001
                pass
            log.error("진단 자료를 저장했습니다: %s", html_path)
            return html_path
        except Exception as e:  # noqa: BLE001 - 진단 저장 실패가 원인을 덮지 않도록
            log.debug("진단 저장 실패: %s", e)
            return None

    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:  # noqa: BLE001
                pass
            self._driver = None
