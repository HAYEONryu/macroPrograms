#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
에스쁘아 퍼스널 컨설팅 - 예약 가능 감시 + PC 알림 (단일 파일)

동작:
  - 실행 시점부터 당일 오후 5시까지 지정 간격으로 반복 확인 후 자동 종료
  - 오늘 기준 미래 15일(주말 제외) 중 예약 가능한 날짜 탐색
  - 발견 시 PC 알림창(모달) + 소리 + 로그

사용법:
  python espoir_monitor.py                 # 60분 간격, 17시 종료
  python espoir_monitor.py --interval 30   # 30분 간격
  python espoir_monitor.py --end 18        # 18시 종료
  python espoir_monitor.py --once          # 1회만 확인
  python espoir_monitor.py --dump          # 달력 셀 구조 덤프
  python espoir_monitor.py --headful       # 창 띄우고 실행
  python espoir_monitor.py --test-alert     # 알림창만 테스트
"""

import argparse
import asyncio
import ctypes
import json
import os
import platform
import re
import importlib.util
import shutil
import smtplib
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from email.message import EmailMessage

from playwright.async_api import async_playwright

VERSION = "2026-07-24o"   # --version 으로 확인 가능


# ---------- 설정 파일(config.py) 로딩 ----------
def _load_config_file():
    """스크립트와 같은 폴더의 config.py 에서 대문자 변수를 읽어온다."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
    if not os.path.exists(path):
        return {}, path
    try:
        spec = importlib.util.spec_from_file_location("espoir_config", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return {k: getattr(mod, k) for k in dir(mod) if k.isupper()}, path
    except Exception as e:
        print(f"[경고] config.py 를 읽지 못했습니다: {e}")
        return {}, path


_CFG, _CFG_PATH = _load_config_file()


def conf(key, default=""):
    """우선순위: 환경변수 > config.py > 기본값"""
    v = os.environ.get(key)
    if v not in (None, ""):
        return v
    v = _CFG.get(key)
    if v not in (None, ""):
        return v
    return default

# ========================= 설정 =========================
BASE      = "https://www.espoir.com"
SERVICE_URL = f"{BASE}/ko/service/service_private.do"
PROGRAM     = "pcs"   # 퍼스널 컨설팅

# 로그인 링크 후보 (위에서부터 시도).
# id -> 상대경로 -> 절대 xpath 순. 절대 xpath는 화면이 깨지면 어긋날 수 있어 뒤로 뒀다.
LOGIN_LINK_SELECTORS = [
    "xpath=/html/body/div[1]/section[1]/div[2]/ul[1]/li[1]/a",   # 지정 경로(우선)
    "xpath=/html/body/div/section[1]/div[2]/ul[1]/li[1]/a",
    "#btn_header_login",
    'a[id*="header_login" i]',
    "xpath=//ul//li/a[normalize-space()='로그인']",
    "xpath=//a[normalize-space()='로그인']",
    "xpath=/html/body/div[1]/section[1]/div[2]/ul[1]/li[1]/a",
    "xpath=/html/body/div/section[1]/div[2]/ul[1]/li[1]/a",
]
LOGIN_SUBMIT_XPATH = "/html/body/div[1]/section/div[2]/form/div[5]/button"
MYPAGE_XPATH       = "/html/body/div[1]/section[1]/div[2]/ul[1]/li[2]/a"
REFRESH_AFTER_LOGIN = True   # 로그인 직후 새로고침
RSV_URL   = (f"{BASE}/ko/service/service_private_rsv.do"
             "?i_sProgramcd=pcs&i_sAgree=Y&i_sUserType=EAK_USER&i_sAgree_p=Y")

# 로그인 진입점.
# one-ap.amorepacific.com 의 로그인 URL은 sessionDataKey/sid/cid 가 일회성이라
# 직접 하드코딩하면 만료됩니다. 아래 주소로 들어가면 사이트가 매번 새 인증 URL을
# 발급해 주고, redirectUri 덕분에 로그인 후 곧바로 예약 페이지로 돌아옵니다.
LOGIN_ENTRY = (f"{BASE}/oauth2AuthorizeUser?reset=true&cid=&sid="
               f"&redirectUri={urllib.parse.quote(RSV_URL, safe='')}")

AUTH_HOST = "one-ap.amorepacific.com"   # 아모레퍼시픽 통합 로그인 도메인

ESPOIR_ID = conf("ESPOIR_ID", "id11")
ESPOIR_PW = conf("ESPOIR_PW", "pw11")

DAYS_AHEAD    = 15        # 확인할 일수 (오늘 포함 15일 = 오늘 ~ 오늘+14일)

END_HOUR   = 17           # 자동 종료 시각 (오후 5시)
END_MINUTE = 0

TRY_WITHOUT_LOGIN  = False  # False = 처음부터 로그인 수행 (권장)
AUTO_FALLBACK_LOGIN = True  # 비로그인 실패 시 자동으로 로그인 후 재시도

# 알림 강도 설정
ALERT_MODE         = "window"  # "window"=전체화면 경보 / "dialog"=기본 대화상자 / "both"
ALERT_SOUND_TIMES  = 12        # 알람음 반복 횟수 (0이면 무음)
ALERT_SPEAK        = True      # 음성 안내(TTS)
ALERT_OPEN_BROWSER = True      # 예약 페이지 자동 열기
ALERT_OPEN_DELAY   = 4         # 경보 표시 후 브라우저를 열기까지 대기(초)
ALERT_CLOSE_ON_OPEN = True     # 브라우저를 연 뒤 경보창 자동 닫기
ALERT_AUTO_CLOSE   = 900       # 경보창 자동 닫힘(초). 0이면 수동으로만 닫힘

LOG_FILE = "monitor_log.txt"
NOTIFY_ONLY_ON_CHANGE = True

NOTICE_TEXT = "위 내용을 확인하고 예약을 진행합니다."

# ---- 이메일 알림 ----
MAIL_TO   = conf("MAIL_TO", "hannau416@gmail.com")   # 받는 사람
MAIL_USER = conf("MAIL_USER", "")                    # 보내는 Gmail 주소
MAIL_PASS = conf("MAIL_PASS", "")                    # Gmail 앱 비밀번호(16자리)
MAIL_HOST = conf("MAIL_HOST", "smtp.gmail.com")
MAIL_PORT = int(conf("MAIL_PORT", "465"))

TG_TOKEN    = conf("TG_TOKEN", "")
TG_CHAT     = conf("TG_CHAT", "")
WEBHOOK_URL = conf("WEBHOOK_URL", "")
# ========================================================

WD = ["월", "화", "수", "목", "금", "토", "일"]

# href="#" 링크가 URL에 #을 붙여 화면이 깨지는 것을 막는다.
# preventDefault 는 기본 이동만 막고 사이트의 onclick 핸들러는 그대로 실행된다.
HASH_GUARD = """
(() => {
  document.addEventListener('click', (e) => {
    const a = e.target && e.target.closest && e.target.closest('a');
    if (!a) return;
    const h = a.getAttribute('href');
    if (h === '#' || h === '') e.preventDefault();
  }, true);
  const strip = () => {
    if (location.hash) history.replaceState(null, '', location.pathname + location.search);
  };
  window.addEventListener('hashchange', strip);
  document.addEventListener('DOMContentLoaded', strip);
  strip();
})();
"""


# ---------- 대상 날짜 ----------
def target_dates():
    """오늘 포함 DAYS_AHEAD 일 (주말 포함).
    DAYS_AHEAD=15 -> 오늘 ~ 오늘+14일 = 총 15일"""
    today = date.today()
    return [today + timedelta(days=i) for i in range(DAYS_AHEAD)]


def fmt(d):
    return f"{d:%Y-%m-%d}({WD[d.weekday()]})"


# ---------- 로그 ----------
VERBOSE = False   # --verbose 로 켜면 파일 전용 로그도 콘솔에 표시


def log(msg, console=True):
    """console=False 면 monitor_log.txt 에만 기록한다."""
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    if console or VERBOSE:
        print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
    except Exception:
        pass


def flog(msg):
    """파일 전용 로그 (상세 진단용)"""
    log(msg, console=False)


def check_credentials():
    """플레이스홀더 값이 그대로면 경고."""
    placeholders = {"id11", "pw11", "본인아이디", "본인비번", ""}
    if ESPOIR_ID in placeholders or ESPOIR_PW in placeholders:
        log("!" * 60)
        log("경고: 로그인 정보가 예시값 그대로입니다.")
        log(f"  현재 ID='{ESPOIR_ID}' / PW 길이={len(ESPOIR_PW)}")
        log("  config.py 를 만들어 아래처럼 설정하세요.")
        log('    ESPOIR_ID = "본인아이디"')
        log('    ESPOIR_PW = "본인비번"')
        log("  (config.example.py 를 config.py 로 복사해서 수정)")
        log("!" * 60)
        return False
    return True


# ---------- PC 알림창 ----------
def beep():
    try:
        if platform.system() == "Windows":
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        sys.stdout.write("\a")
        sys.stdout.flush()
    except Exception:
        pass


def speak(message):
    """음성 안내(TTS). 실패해도 무시."""
    if not ALERT_SPEAK:
        return
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(["say", message])
        elif system == "Windows":
            ps = ("Add-Type -AssemblyName System.Speech; "
                  "(New-Object System.Speech.Synthesis.SpeechSynthesizer)"
                  f".Speak('{message}')")
            subprocess.Popen(["powershell", "-NoProfile", "-Command", ps])
        else:
            if shutil.which("espeak"):
                subprocess.Popen(["espeak", "-v", "ko", message])
    except Exception:
        pass


def sound_loop(times):
    """알람음을 여러 번 반복 재생 (별도 스레드)."""
    if times <= 0:
        return

    def _run():
        system = platform.system()
        for _ in range(times):
            try:
                if system == "Windows":
                    import winsound
                    winsound.Beep(880, 350)
                    winsound.Beep(1320, 350)
                elif system == "Darwin":
                    subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"],
                                   check=False, timeout=5)
                else:
                    if shutil.which("paplay"):
                        subprocess.run(
                            ["paplay", "/usr/share/sounds/freedesktop/stereo/complete.oga"],
                            check=False, timeout=5)
                    else:
                        sys.stdout.write("\a")
                        sys.stdout.flush()
            except Exception:
                pass
            time.sleep(0.45)

    threading.Thread(target=_run, daemon=True).start()


def spawn_alert_window(message):
    """전체화면 경보창을 별도 프로세스로 띄운다 (감시 루프를 막지 않음)."""
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__),
                          "--alert-window", message])
        return True
    except Exception as e:
        log(f"  (경보창 실행 실패: {e})")
        return False


def alert_window_main(message):
    """전체화면 점멸 경보창. 별도 프로세스에서 실행됨."""
    try:
        import tkinter as tk
    except Exception:
        pc_alert("에스쁘아 예약 가능!", message)   # tkinter 없으면 기본 대화상자
        time.sleep(10)
        return

    sound_loop(ALERT_SOUND_TIMES)
    speak("예약 가능한 날짜가 열렸습니다")

    root = tk.Tk()
    root.title("에스쁘아 예약 가능!")
    try:
        root.attributes("-fullscreen", True)
    except Exception:
        root.geometry("1000x700")
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    root.configure(bg="#C1121F")
    root.lift()
    root.focus_force()

    wrap = tk.Frame(root, bg="#C1121F")
    wrap.place(relx=0.5, rely=0.5, anchor="center")

    title = tk.Label(wrap, text="예약 가능!", fg="white", bg="#C1121F",
                     font=("Helvetica", 96, "bold"))
    title.pack(pady=(0, 30))

    body = tk.Label(wrap, text=message, fg="white", bg="#C1121F",
                    font=("Helvetica", 34), justify="center")
    body.pack(pady=(0, 40))

    btns = tk.Frame(wrap, bg="#C1121F")
    btns.pack()

    opened = {"done": False}

    def open_page():
        """예약 페이지를 브라우저로 열고, 설정에 따라 경보창을 닫는다."""
        if not opened["done"]:
            opened["done"] = True
            try:
                webbrowser.open(RSV_URL)
            except Exception:
                pass
        if ALERT_CLOSE_ON_OPEN:
            try:
                root.after(600, root.destroy)   # 브라우저가 뜬 뒤 닫기
            except Exception:
                pass

    tk.Button(btns, text="예약 페이지 열기", command=open_page,
              font=("Helvetica", 22, "bold"), bg="white", fg="#C1121F",
              padx=28, pady=14, relief="flat").pack(side="left", padx=14)
    tk.Button(btns, text="닫기  (Esc)", command=root.destroy,
              font=("Helvetica", 22), bg="#7A0A14", fg="white",
              padx=28, pady=14, relief="flat").pack(side="left", padx=14)

    # 배경 점멸
    colors = ["#C1121F", "#FF8800"]
    state = {"i": 0}

    def flash():
        state["i"] = (state["i"] + 1) % len(colors)
        c = colors[state["i"]]
        for w in (root, wrap, title, body, btns):
            try:
                w.configure(bg=c)
            except Exception:
                pass
        root.after(550, flash)

    flash()
    root.bind("<Escape>", lambda e: root.destroy())
    if ALERT_AUTO_CLOSE > 0:
        root.after(ALERT_AUTO_CLOSE * 1000, root.destroy)
    if ALERT_OPEN_BROWSER:
        # 경보를 잠시 보여준 뒤 브라우저를 열고, 설정에 따라 창을 닫는다
        root.after(max(1, ALERT_OPEN_DELAY) * 1000, open_page)

    root.mainloop()


def pc_alert(title, message):
    """OS 기본 알림창(모달)을 띄운다. 감시 루프를 막지 않도록 별도 스레드."""
    def _run():
        system = platform.system()
        try:
            if system == "Windows":
                MB_OK = 0x0
                MB_ICONINFORMATION = 0x40
                MB_SYSTEMMODAL = 0x1000
                MB_SETFOREGROUND = 0x10000
                ctypes.windll.user32.MessageBoxW(
                    None, message, title,
                    MB_OK | MB_ICONINFORMATION | MB_SYSTEMMODAL | MB_SETFOREGROUND)

            elif system == "Darwin":
                script = (f'display dialog {json.dumps(message)} '
                          f'with title {json.dumps(title)} '
                          f'buttons {{"확인"}} default button "확인" '
                          f'with icon caution')
                subprocess.run(["osascript", "-e", script], check=False)

            else:  # Linux
                if shutil.which("zenity"):
                    subprocess.run(["zenity", "--info", f"--title={title}",
                                    f"--text={message}"], check=False)
                elif shutil.which("kdialog"):
                    subprocess.run(["kdialog", "--title", title,
                                    "--msgbox", message], check=False)
                elif shutil.which("notify-send"):
                    subprocess.run(["notify-send", "-u", "critical",
                                    title, message], check=False)
        except Exception as e:
            log(f"  (알림창 표시 실패: {e})")

    threading.Thread(target=_run, daemon=True).start()
    beep()


def telegram_notify(text):
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data), timeout=10)
    except Exception as e:
        log(f"  (텔레그램 전송 실패: {e})")


def webhook_notify(text):
    if not WEBHOOK_URL:
        return
    try:
        payload = json.dumps({"content": text, "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(
            WEBHOOK_URL, data=payload,
            headers={"Content-Type": "application/json"}), timeout=10)
    except Exception as e:
        log(f"  (웹훅 전송 실패: {e})")


def send_mail(subject, body):
    """예약 가능 알림 메일 발송. 표준 라이브러리만 사용."""
    if not (MAIL_USER and MAIL_PASS):
        log("  메일 건너뜀 (MAIL_USER / MAIL_PASS 환경변수 미설정)")
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = MAIL_USER
        msg["To"] = MAIL_TO
        msg.set_content(body)

        if MAIL_PORT == 465:
            with smtplib.SMTP_SSL(MAIL_HOST, MAIL_PORT, timeout=20) as smtp:
                smtp.login(MAIL_USER, MAIL_PASS)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(MAIL_HOST, MAIL_PORT, timeout=20) as smtp:
                smtp.starttls()
                smtp.login(MAIL_USER, MAIL_PASS)
                smtp.send_message(msg)
        log(f"  메일 발송 완료 -> {MAIL_TO}")
        return True
    except Exception as e:
        log(f"  메일 발송 실패: {e}")
        return False


def notify_available(dates):
    title = "에스쁘아 예약 가능!"
    listed = "\n".join(fmt(d) for d in dates)
    body = f"예약 가능한 날짜가 열렸습니다.\n\n{listed}"
    log("*** 예약 가능: " + ", ".join(fmt(d) for d in dates) + " ***")

    used_window = False
    if ALERT_MODE in ("window", "both"):
        used_window = spawn_alert_window(body)
    if ALERT_MODE in ("dialog", "both") or not used_window:
        pc_alert(title, body)
        sound_loop(ALERT_SOUND_TIMES)
        speak("예약 가능한 날짜가 열렸습니다")

    send_mail(f"[에스쁘아] 예약 가능 {len(dates)}일",
              f"{body}\n\n예약 페이지:\n{SERVICE_URL}\n")
    telegram_notify(f"{title}\n{body}\n{SERVICE_URL}")
    webhook_notify(f"{title}\n{body}\n{SERVICE_URL}")


def notify_problem(reason):
    title = "에스쁘아 감시 - 확인 불가"
    body = f"달력을 읽지 못했습니다.\n\n{reason}\n\n로그를 확인하세요."
    log(f"!!! 확인 불가: {reason}")
    pc_alert(title, body)
    telegram_notify(f"{title}\n{body}")


# ---------- 클릭 헬퍼 ----------
async def click_text(page, text, exclude=None):
    for loc in [page.get_by_role("button", name=text, exact=True),
                page.get_by_role("link", name=text, exact=True),
                page.locator(f'button:has-text("{text}")'),
                page.locator(f'a:has-text("{text}")'),
                page.get_by_text(text, exact=False)]:
        try:
            n = await loc.count()
        except Exception:
            continue
        for i in range(n):
            item = loc.nth(i)
            try:
                if exclude and exclude in ((await item.inner_text()) or ""):
                    continue
                await item.click(timeout=1500)
                return True
            except Exception:
                continue
    return False


# ---------- 로그인 ----------
LOGIN_STATE_JS = r"""
() => {
  // 이 사이트의 헤더 로그인 버튼은 <a id="btn_header_login" href="#"> 형태다.
  // href 에 oauth2AuthorizeUser 가 없으므로 링크 유무로 판단하면 안 된다.
  const loginBtn  = document.querySelector(
      '#btn_header_login, a[id*="header_login" i]');
  const logoutBtn = document.querySelector(
      '#btn_header_logout, a[id*="header_logout" i], a[href*="logout" i]');
  const t = document.body ? document.body.innerText : '';

  let state = 'unknown';
  if (logoutBtn) state = 'in';            // 로그아웃 버튼 존재 = 로그인됨
  else if (loginBtn) state = 'out';       // 로그인 버튼 존재 = 로그아웃됨
  else if (/로그아웃/.test(t)) state = 'in';

  return {
    state: state,
    hasLoginBtn: !!loginBtn,
    hasLogoutBtn: !!logoutBtn,
    loginBtnId: loginBtn ? (loginBtn.id || '') : ''
  };
}
"""


async def login_state(page):
    """'in' | 'out' | 'unknown'"""
    try:
        r = await page.evaluate(LOGIN_STATE_JS)
        return r.get("state", "unknown")
    except Exception:
        return "unknown"


def state_label(st):
    return {"in": "로그인됨", "out": "로그아웃 상태", "unknown": "판별 불가"}.get(st, st)


async def is_logged_in(ctx):
    """로그아웃 버튼이 실제로 확인될 때만 True (추측하지 않음)."""
    for pg in ctx.pages:
        try:
            if pg.is_closed() or "espoir.com" not in (pg.url or ""):
                continue
            if await login_state(pg) == "in":
                return True
        except Exception:
            continue
    return False


async def verify_calendar_access(page):
    """
    진짜 성공 기준: 예약 페이지에서 달력 날짜 셀이 실제로 읽히는가.
    반환 (성공여부, 진단정보)
    """
    info = await load_calendar(page)
    ok = bool(info.get("hasCalendar")) and info.get("dayCells", 0) > 0
    return ok, info


async def dump_debug(page, tag):
    """실패 원인 파악용 스크린샷 + HTML 저장."""
    ts = datetime.now().strftime("%H%M%S")
    try:
        await page.screenshot(path=f"debug_{tag}_{ts}.png", full_page=True)
        html = await page.content()
        with open(f"debug_{tag}_{ts}.html", "w", encoding="utf-8") as f:
            f.write(html)
        log(f"  [디버그] debug_{tag}_{ts}.png / .html 저장")
    except Exception as e:
        log(f"  [디버그] 저장 실패: {e}")


async def _find_auth_page(ctx, page):
    """로그인이 새 탭/팝업으로 열렸으면 그 페이지를 반환."""
    for pg in ctx.pages:
        try:
            if not pg.is_closed() and AUTH_HOST in (pg.url or ""):
                return pg
        except Exception:
            continue
    return page


async def ainput():
    """엔터 입력 대기. 데몬 스레드를 써서 프로그램 종료를 막지 않는다."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def _read():
        try:
            input()
        except Exception:
            pass
        loop.call_soon_threadsafe(
            lambda: (not fut.done()) and fut.set_result(None))

    threading.Thread(target=_read, daemon=True).start()
    await fut


async def safe_goto(page, url, tries=3):
    """이동 실패(chrome-error 등)에서 복구하며 재시도."""
    for i in range(tries):
        try:
            await page.goto(url, wait_until="domcontentloaded")
            return True
        except Exception as e:
            log(f"      이동 실패 {i + 1}/{tries}: {str(e).splitlines()[0][:90]}")
            try:
                await page.goto("about:blank")
            except Exception:
                pass
            await page.wait_for_timeout(1000)
    return False


async def open_ready(page, url, tries=2):
    """페이지 이동 후 CSS/스크립트가 실제로 적용될 때까지 대기.
    스타일시트가 하나도 없으면 화면이 깨진 상태이므로 1회 새로고침한다."""
    for attempt in range(tries):
        if not await safe_goto(page, url):
            continue
        for _ in range(20):                      # 최대 10초
            await page.wait_for_timeout(500)
            try:
                ok = await page.evaluate(
                    "() => document.readyState === 'complete' && "
                    "document.styleSheets.length > 0")
            except Exception:
                ok = False
            if ok:
                return True
        if attempt < tries - 1:
            log("      화면이 준비되지 않아 새로고침")
    return False


async def click_login_link(page):
    """로그인 링크 클릭. 여러 후보를 순서대로 시도하고,
    화면이 깨져 요소가 숨겨진 경우 강제 클릭 -> JS 클릭까지 폴백."""
    for sel in LOGIN_LINK_SELECTORS:
        try:
            loc = page.locator(sel)
            if not await loc.count():
                continue
            item = loc.first
            for how, fn in (
                ("일반", lambda: item.click(timeout=2500)),
                ("강제", lambda: item.click(timeout=2500, force=True)),
                ("JS", lambda: item.evaluate("el => el.click()")),
            ):
                try:
                    await fn()
                    log(f"      로그인 링크 클릭 성공 ({how}, {sel[:42]})")
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    log("      로그인 링크를 찾지 못했습니다")
    return False


async def log_mypage(page):
    """로그인 확인용: 마이페이지 버튼 존재 여부를 로그로 남긴다."""
    try:
        loc = page.locator(f"xpath={MYPAGE_XPATH}")
        n = await loc.count()
        if n:
            txt = ((await loc.first.inner_text()) or "").strip()[:20]
            vis = await loc.first.is_visible()
            log(f"      마이페이지 버튼: 있음 (텍스트='{txt}', 표시={vis})")
            return True
        log("      마이페이지 버튼: 없음")
        return False
    except Exception as e:
        log(f"      마이페이지 버튼 확인 실패: {str(e)[:60]}")
        return False


async def step1_open_service(page):
    """[1] 서비스 페이지 접근"""
    log("[1/6] 서비스 페이지 접근")
    if not await open_ready(page, SERVICE_URL):
        log("      경고: 스타일이 적용되지 않은 상태 (동작에는 지장 없음)")
    # 첫 로드 화면이 깨지는 문제 대응: 1회 새로고침 후 진행
    try:
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        log("      새로고침 완료")
    except Exception as e:
        log(f"      새로고침 실패(무시): {str(e).splitlines()[0][:70]}")
    await page.wait_for_timeout(500)
    st = await login_state(page)
    log(f"      현재 상태: {state_label(st)}")
    await log_mypage(page)
    return st


async def step2_goto_login(page, ctx):
    """[2] 로그인 페이지로 이동 (확인된 xpath 우선)"""
    log("[2/6] 로그인 페이지로 이동")

    async def reached():
        """인증 페이지 도달 시 페이지 반환. 도중에 로그인 완료되면 'DONE'."""
        for _ in range(24):                      # 최대 12초
            await page.wait_for_timeout(500)
            pg = await _find_auth_page(ctx, page)
            if AUTH_HOST in (pg.url or ""):
                return pg
            try:
                if await pg.locator('input[type="password"]').count():
                    return pg
            except Exception:
                pass
            # 기존 인증 세션으로 그대로 로그인되는 경우
            if await login_state(page) == "in":
                return "DONE"
        return None

    # 1순위: 로그인 링크 클릭 (id -> 상대경로 -> 절대 xpath 순)
    if await click_login_link(page):
        pg = await reached()
        if pg == "DONE":
            return "DONE"
        if pg:
            return pg
        log("      클릭했으나 인증 페이지에 도달하지 못함")

    # 2순위: 인증 진입 URL 직접 호출
    log("      인증 URL 직접 호출")
    await page.goto(LOGIN_ENTRY, wait_until="domcontentloaded")
    pg = await reached()
    if pg == "DONE":
        return "DONE"
    return pg or page


async def step3_do_login(page):
    """[3] 아이디/비밀번호 입력 후 로그인"""
    log("[3/6] 로그인 수행")
    log(f"      인증 페이지: {(page.url or '')[:70]}")

    try:
        pw_field = page.locator('input[type="password"]').first
        await pw_field.wait_for(state="visible", timeout=10000)
    except Exception:
        log("      비밀번호 입력란을 찾지 못했습니다")
        await dump_debug(page, "no_pw_field")
        return False

    id_field = None
    try:
        form = pw_field.locator('xpath=ancestor::form[1]')
        if await form.count():
            cand = form.locator('input[type="text"], input[type="email"], '
                                'input[type="tel"], input:not([type])')
            for i in range(await cand.count()):
                if await cand.nth(i).is_visible():
                    id_field = cand.nth(i)
                    break
    except Exception:
        pass
    if id_field is None:
        cand = page.locator('input[type="text"], input[type="email"], '
                            'input[type="tel"], input:not([type])')
        for i in range(await cand.count()):
            try:
                if await cand.nth(i).is_visible():
                    id_field = cand.nth(i)
                    break
            except Exception:
                continue
    if id_field is None:
        log("      아이디 입력란을 찾지 못했습니다")
        await dump_debug(page, "no_id_field")
        return False

    try:
        nm = (await id_field.get_attribute("name")) or \
             (await id_field.get_attribute("id")) or "?"
        await id_field.click(timeout=2000)
        await id_field.fill(ESPOIR_ID, timeout=3000)
        await pw_field.click(timeout=2000)
        await pw_field.fill(ESPOIR_PW, timeout=3000)
        log(f"      입력 완료 (칸={nm}, ID='{ESPOIR_ID[:3]}***', "
            f"PW길이={len(ESPOIR_PW)})")
    except Exception as e:
        log(f"      입력 실패: {e}")
        await dump_debug(page, "fill_fail")
        return False

    # 제출: 확인된 xpath 우선
    submitted, how = False, ""
    try:
        loc = page.locator(f"xpath={LOGIN_SUBMIT_XPATH}")
        if await loc.count():
            await loc.first.click(timeout=3000)
            submitted, how = True, "xpath 버튼"
    except Exception as e:
        log(f"      xpath 제출 실패: {e}")

    if not submitted:
        for loc in [page.get_by_role("button", name=re.compile("로그인|login", re.I)),
                    page.locator('button[type="submit"]'),
                    page.locator('input[type="submit"]')]:
            try:
                item = loc.first
                if await item.count() and await item.is_visible():
                    await item.click(timeout=2500)
                    submitted, how = True, "일반 버튼"
                    break
            except Exception:
                continue
    if not submitted:
        await pw_field.press("Enter")
        how = "엔터키"
    log(f"      제출: {how}")

    left = False
    for _ in range(60):                       # 최대 30초
        await page.wait_for_timeout(500)
        if AUTH_HOST not in (page.url or ""):
            left = True
            break

    if not left:
        log("      인증 페이지에 머물러 있음 (아이디/비번 오류 또는 추가 인증)")
        await dump_debug(page, "auth_stuck")
        return False

    log("      인증 페이지 벗어남")

    if REFRESH_AFTER_LOGIN:
        await page.wait_for_timeout(1500)
        try:
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            log("      새로고침 완료")
        except Exception as e:
            log(f"      새로고침 실패(무시): {e}")
    return True


async def complete_espoir_session(page, ctx):
    """
    IdP 인증은 됐지만 espoir 세션이 없는 상태를 해소한다.
    oauth2AuthorizeUser 직접 호출은 이미 인증된 경우 ERR_INVALID_REDIRECT 가
    발생하므로, 사람이 하는 것과 동일하게
    '서비스 페이지 -> 새로고침 -> 로그인 링크 클릭' 순으로 처리한다.
    """
    log("      espoir 세션 생성 시도 (서비스 페이지 -> 새로고침 -> 로그인 링크)")
    if not await safe_goto(page, SERVICE_URL):
        return False
    await page.wait_for_timeout(1000)

    try:
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
    except Exception:
        pass

    if await login_state(page) == "in":
        return True

    if not await click_login_link(page):
        return False

    for _ in range(24):                          # 최대 12초
        await page.wait_for_timeout(500)
        if await login_state(page) == "in":
            return True
        # 인증 페이지로 갔다면 세션이 없는 것이므로 여기서 중단
        if AUTH_HOST in (page.url or ""):
            log("      인증 페이지로 이동됨 (기존 인증 세션 없음)")
            return False
    return False


async def step4_verify_login(page, ctx):
    """[4] 로그인 완료 확인 (espoir 세션까지)"""
    log("[4/6] 로그인 완료 확인")

    for _ in range(8):
        await page.wait_for_timeout(700)
        st = await login_state(page)
        if st == "in":
            log("      로그인 확인됨 (로그아웃 버튼 존재)")
            return True
        if st == "out":
            break

    await open_ready(page, SERVICE_URL, tries=1)
    await page.wait_for_timeout(800)
    st = await login_state(page)
    log(f"      서비스 페이지 재확인: {state_label(st)}")
    await log_mypage(page)
    if st == "in":
        return True

    # IdP 로그인은 됐으나 espoir 세션이 없는 경우 처리
    for attempt in range(2):
        if await complete_espoir_session(page, ctx):
            log(f"      로그인 확인됨 (세션 생성 {attempt + 1}회차)")
            await log_mypage(page)
            return True

    log("      espoir 세션을 만들지 못했습니다")
    await dump_debug(page, "verify_fail")
    return False


async def ensure_logged_in(page, ctx):
    """[1]~[4] 서비스 페이지 -> (SSO 확인) -> 로그인 -> 확인"""
    st = await step1_open_service(page)
    if st == "in":
        log("      이미 로그인 상태 - 로그인 단계 건너뜀")
        return True

    auth_page = await step2_goto_login(page, ctx)
    if auth_page == "DONE":
        log("      기존 인증 세션으로 로그인 완료 (비밀번호 입력 불필요)")
        return True

    if not await step3_do_login(auth_page):
        return False
    return await step4_verify_login(page, ctx)


async def auto_login(page, ctx, login_url=None):   # 이전 이름 호환
    return await ensure_logged_in(page, ctx)


async def manual_login(ctx):
    print("\n" + "=" * 60)
    print(" 브라우저 창에서 직접 로그인해 주세요.")
    print(" 로그인이 감지되면 자동 진행됩니다. (또는 여기서 엔터)")
    print("=" * 60 + "\n")

    async def detect():
        while True:
            if await is_logged_in(ctx):
                return
            await asyncio.sleep(1.5)

    t1 = asyncio.create_task(detect())
    t2 = asyncio.create_task(ainput())
    _, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    log("로그인 단계 통과")


# ---------- Step1: 유의사항 ----------
NOTICE_JS = r"""
(txt) => {
  const norm = s => (s || '').replace(/\s/g, '');
  const t = norm(txt);
  for (const el of document.querySelectorAll('label, a, button, span, div')) {
    if (!norm(el.textContent).includes(t)) continue;
    if (el.children.length > 3) continue;          // 너무 큰 컨테이너 제외
    let inp = el.htmlFor ? document.getElementById(el.htmlFor) : null;
    if (!inp) inp = el.querySelector('input');
    if (inp) {
      inp.checked = true;
      inp.dispatchEvent(new Event('change', {bubbles:true}));
      inp.dispatchEvent(new Event('click', {bubbles:true}));
    } else {
      el.click();
    }
    return true;
  }
  return false;
}
"""


async def confirm_notice(page):
    """CSS가 깨져도 동작하도록 JS(DOM) 방식을 우선 사용."""
    for _ in range(20):
        try:
            if await page.get_by_text(NOTICE_TEXT, exact=False).count():
                break
        except Exception:
            pass
        await page.wait_for_timeout(300)

    try:
        if await page.evaluate(NOTICE_JS, NOTICE_TEXT):
            await page.wait_for_timeout(400)
            return True
    except Exception:
        pass

    return await click_text(page, NOTICE_TEXT)


# ---------- 진단 ----------
DIAGNOSE_JS = r"""
() => {
  const tables = [...document.querySelectorAll('table')];
  const cal = tables.find(t => /SUN/i.test(t.textContent) && /SAT/i.test(t.textContent));
  const alt = document.querySelector('[class*="calendar"], [class*="cal_"], [id*="calendar"]');
  const root = cal || alt;

  let dayCells = 0;
  if (root) {
    root.querySelectorAll('td, li').forEach(el => {
      const m = (el.textContent || '').trim().match(/\d{1,2}/);
      if (m && parseInt(m[0],10) >= 1 && parseInt(m[0],10) <= 31) dayCells++;
    });
  }
  return {
    url: location.href,
    hasCalendar: !!root,
    dayCells: dayCells,
    hasPwField: !!document.querySelector('input[type=password]'),
    hasNotice: /위 내용을 확인하고/.test(document.body.innerText),
    bodyLen: document.body.innerText.length
  };
}
"""


async def diagnose(page):
    try:
        return await page.evaluate(DIAGNOSE_JS)
    except Exception as e:
        return {"error": str(e)}


# ---------- 달력 ----------
MONTH_JS = r"""
() => {
  const MN = {january:1,february:2,march:3,april:4,may:5,june:6,july:7,
              august:8,september:9,october:10,november:11,december:12,
              jan:1,feb:2,mar:3,apr:4,jun:6,jul:7,aug:8,sep:9,oct:10,nov:11,dec:12};

  const parse = (txt) => {
    if (!txt) return null;
    const t = txt.replace(/\s+/g, ' ').slice(0, 300);
    let m;
    // 2026.07 / 2026-07 / 2026/07 / 2026년 7월
    if ((m = t.match(/(20\d{2})\s*[.\-\/년]\s*(\d{1,2})/)))
      return {y:+m[1], m:+m[2], raw:m[0]};
    // 07.2026 / 7월 2026
    if ((m = t.match(/(\d{1,2})\s*[.\-\/월]\s*(20\d{2})/)))
      return {y:+m[2], m:+m[1], raw:m[0]};
    // JULY 2026 / 2026 JULY
    if ((m = t.match(/([A-Za-z]{3,9})\.?\s*(20\d{2})/)) && MN[m[1].toLowerCase()])
      return {y:+m[2], m:MN[m[1].toLowerCase()], raw:m[0]};
    if ((m = t.match(/(20\d{2})\s*([A-Za-z]{3,9})/)) && MN[m[2].toLowerCase()])
      return {y:+m[1], m:MN[m[2].toLowerCase()], raw:m[0]};
    return null;
  };

  const tables = [...document.querySelectorAll('table')];
  const cal = tables.find(t => /SUN/i.test(t.textContent) && /SAT/i.test(t.textContent))
           || document.querySelector('[class*="calendar"], [class*="cal_"], [id*="calendar"]');

  // 1순위: 월 표시 전용으로 보이는 요소
  const sels = '[class*="month"],[class*="ym"],[class*="cal_tit"],[class*="calTit"],' +
               '[class*="date_tit"],[class*="tit"],h2,h3,h4,strong,em,span';
  for (const el of document.querySelectorAll(sels)) {
    if (el.children.length > 2) continue;
    const r = parse(el.textContent);
    if (r) return {...r, src: 'el:' + (el.className || el.tagName)};
  }

  // 2순위: 달력의 조상 요소를 5단계까지 거슬러 올라가며 탐색
  let node = cal;
  for (let i = 0; node && i < 5; i++) {
    const r = parse(node.innerText);
    if (r) return {...r, src: 'ancestor' + i};
    node = node.parentElement;
  }

  // 3순위: 문서 전체
  const r = parse(document.body.innerText);
  return r ? {...r, src: 'body'} : null;
}
"""

# 달력 내용이 실제로 바뀌었는지 확인하기 위한 지문
SIGNATURE_JS = r"""
() => {
  const tables = [...document.querySelectorAll('table')];
  let cal = tables.find(t => /SUN/i.test(t.textContent) && /SAT/i.test(t.textContent));
  if (!cal) cal = document.querySelector('[class*="calendar"], [class*="cal_"], [id*="calendar"]');
  if (!cal) return '';
  const parts = [];
  cal.querySelectorAll('td, li').forEach(el => {
    const t = (el.textContent || '').trim().replace(/\s+/g, '');
    if (t) parts.push(t + '|' + (el.className || ''));
  });
  return parts.join(',').slice(0, 3000);
}
"""

SCAN_JS = r"""
() => {
  // 이 사이트는 마감을 'disabled' 속성으로 표시한다 (실측 확인).
  // 클래스 키워드는 보조 수단이며, 부분일치 오탐을 피하려고 정밀한 값만 사용한다.
  // (예: 'end' 를 쓰면 'weekend', 'openday' 까지 잘못 걸림)
  const BAD = ['unableday','soldout','impossible','dimmed','blind'];
  const tables = [...document.querySelectorAll('table')];
  let cal = tables.find(t => /SUN/i.test(t.textContent) && /SAT/i.test(t.textContent));
  if (!cal) cal = document.querySelector('[class*="calendar"], [class*="cal_"], [id*="calendar"]');
  if (!cal) return [];

  const cells = [];
  cal.querySelectorAll('td, li').forEach(el => {
    const txt = (el.textContent || '').trim();
    const m = txt.match(/\d{1,2}/);
    if (!m) return;
    const day = parseInt(m[0], 10);
    if (!day || day > 31) return;

    const inner = el.querySelector('a, button');
    const allCls = (el.className + ' ' + (inner ? inner.className : '')).toLowerCase();
    const hasHandler = !!inner || !!el.getAttribute('onclick');
    const disabledAttr = !!(inner && (inner.disabled ||
                            inner.getAttribute('aria-disabled') === 'true'));
    const isBad = BAD.some(k => allCls.includes(k)) || disabledAttr;

    cells.push({
      day: day, tag: el.tagName.toLowerCase(), cls: el.className || '',
      innerTag: inner ? inner.tagName.toLowerCase() : null,
      innerCls: inner ? (inner.className || '') : '',
      innerId: inner ? (inner.id || '') : '',
      disabled: !!(inner && inner.disabled),
      ariaDis: inner ? (inner.getAttribute('aria-disabled') || '') : '',
      hasClick: !!(el.getAttribute('onclick') ||
                   (inner && inner.getAttribute('onclick'))),
      text: txt.replace(/\s+/g, ' ').slice(0, 40),
      available: !!(hasHandler && !isBad)
    });
  });
  return cells;
}
"""


async def read_month(page):
    """달력에 표시된 (년, 월) 읽기. 실패 시 None."""
    try:
        r = await page.evaluate(MONTH_JS)
    except Exception as e:
        log(f"  월 표시 읽기 오류: {e}")
        return None
    if not r:
        return None
    return (r["y"], r["m"], r.get("raw", ""), r.get("src", ""))


async def calendar_signature(page):
    try:
        return await page.evaluate(SIGNATURE_JS)
    except Exception:
        return ""


# 사용자가 확인한 실제 next 버튼 위치 (클래스 없는 순수 <button>)
NEXT_XPATH = ("/html/body/div/form[5]/div/section/div/section/section[2]"
              "/div[2]/div[1]/div/div[1]/button[2]")
PREV_XPATH = NEXT_XPATH.rsplit("button[", 1)[0] + "button[1]"   # 같은 그룹의 첫 버튼

# 달력 표(td 안의 날짜 버튼)를 제외한 '달 이동 버튼'을 찾아 클릭한다.
# 클래스·텍스트가 없어도 동작하도록 위치(구조) 기반으로 탐색.
NAV_JS = r"""
(forward) => {
  const tables = [...document.querySelectorAll('table')];
  let cal = tables.find(t => /SUN/i.test(t.textContent) && /SAT/i.test(t.textContent));
  if (!cal) cal = document.querySelector('[class*="calendar"], [class*="cal_"]');
  if (!cal) return 'nocal';

  // 달력에서 위로 올라가며 '표 바깥의 버튼'이 2개 이상인 컨테이너를 찾는다
  let node = cal;
  for (let i = 0; i < 7 && node; i++) {
    const btns = [...node.querySelectorAll('button')]
                   .filter(b => !b.closest('table'));
    if (btns.length >= 2) {
      const b = forward ? btns[btns.length - 1] : btns[0];
      if (b.disabled || b.getAttribute('aria-disabled') === 'true') return 'disabled';
      b.click();
      return 'clicked';
    }
    node = node.parentElement;
  }
  return 'notfound';
}
"""


async def click_month_nav(page, forward=True):
    """
    다음/이전 달로 이동. 달력 내용이 실제로 바뀌었는지까지 확인.
    반환: 'clicked' | 'disabled' | 'notfound'
    """
    before = await calendar_signature(page)

    async def changed():
        for _ in range(16):
            await page.wait_for_timeout(250)
            after = await calendar_signature(page)
            if after and after != before:
                return True
        return False

    # 1) 확인된 정확한 xpath 우선
    xpath = NEXT_XPATH if forward else PREV_XPATH
    try:
        loc = page.locator(f"xpath={xpath}")
        if await loc.count():
            item = loc.first
            if await item.is_disabled():
                log(f"  {'다음' if forward else '이전'} 달 버튼이 비활성 상태")
                return "disabled"
            await item.click(timeout=2000)
            if await changed():
                return "clicked"
    except Exception:
        pass

    # 2) 구조 기반 탐색 (xpath가 바뀌어도 동작)
    try:
        res = await page.evaluate(NAV_JS, forward)
        if res == "disabled":
            log(f"  {'다음' if forward else '이전'} 달 버튼이 비활성 상태")
            return "disabled"
        if res == "clicked" and await changed():
            return "clicked"
    except Exception as e:
        log(f"  달 이동 중 오류: {e}")

    return "notfound"


async def goto_month(page, year, month, assumed=None):
    """
    목표 (년, 월)로 이동.
    월 표시를 읽을 수 있으면 그 값을 기준으로, 못 읽으면 assumed(추정 현재월)을
    기준으로 필요한 횟수만큼 next를 누른다.
    반환: (성공여부, 도달한 (년,월) 추정치)
    """
    info = await read_month(page)
    if info:
        cur = (info[0], info[1])
        log(f"  달력 월 인식: {info[0]}.{info[1]:02d} (표기 '{info[2]}', {info[3]})")
    else:
        cur = assumed
        log(f"  달력 월 표시를 읽지 못함 -> 추정 {cur[0]}.{cur[1]:02d} 기준으로 이동")

    if cur is None:
        return False, None

    for attempt in range(14):
        if cur == (year, month):
            return True, cur

        diff = (year - cur[0]) * 12 + (month - cur[1])
        forward = diff > 0
        res = await click_month_nav(page, forward)
        if res == "disabled":
            log(f"  {year}.{month:02d} 은 아직 열리지 않았습니다 (이동 버튼 비활성)")
            return "closed", cur
        if res != "clicked":
            log(f"  {'다음' if forward else '이전'} 달 버튼을 찾지 못했거나 "
                f"달력이 바뀌지 않음 -> {year}.{month:02d} 이동 실패")
            return False, cur

        info = await read_month(page)
        if info:
            cur = (info[0], info[1])
        else:
            # 표시를 못 읽으면 클릭 1회 = 1개월로 간주
            m0 = cur[1] + (1 if forward else -1)
            y0 = cur[0] + (1 if m0 > 12 else (-1 if m0 < 1 else 0))
            m0 = 1 if m0 > 12 else (12 if m0 < 1 else m0)
            cur = (y0, m0)

    return cur == (year, month), cur


# ---------- 확인 1회 ----------
async def load_calendar(page):
    """[5] 예약 페이지 이동 -> 유의사항 확인 -> 당월 달력 노출"""
    log("[5/6] 예약 페이지 이동 및 달력 표시")
    await page.goto(RSV_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(800)
    await confirm_notice(page)

    info = {}
    for i in range(10):                 # 최대 약 10초
        await page.wait_for_timeout(1000)
        info = await diagnose(page)
        if info.get("dayCells", 0) > 0:
            log(f"      당월 달력 표시됨 (셀 {info['dayCells']}개"
                + (f", 로딩 {i + 1}초" if i else "") + ")")
            return info
    log("      달력이 표시되지 않음")
    return info


async def check_once(page, ctx, dump=False, logged_in=False):
    """반환: (found_dates, problem_reason)"""
    info = await load_calendar(page)

    # 진단: 달력이 안 보이면 원인 구분
    if info.get("error"):
        return [], f"페이지 평가 오류: {info['error']}", []

    # 로그인 상태 확인 - 로그아웃 상태의 달력은 신뢰할 수 없음
    st = await login_state(page)
    if st == "in":
        log("  로그인 상태: 로그인됨 (세션 유지 - 1~4단계 생략)")
    else:
        log(f"  로그인 상태: {state_label(st)}")
    if st == "out":
        if logged_in:
            return [], "로그인이 풀렸습니다 (헤더에 로그인 버튼 존재)", []
        if AUTO_FALLBACK_LOGIN:
            log("  로그아웃 상태 -> 로그인 후 재확인 "
                "(비로그인 달력은 전부 마감으로 보일 수 있음)")
            if await auto_login(page, ctx):
                return await check_once(page, ctx, dump, logged_in=True)
            return [], "로그인 실패 - 달력 결과를 신뢰할 수 없습니다", []
        return [], "로그아웃 상태 - 달력 결과를 신뢰할 수 없습니다", []

    if info.get("hasPwField") or "oauth" in (info.get("url") or "").lower():
        reason = "로그인 페이지로 리다이렉트됨 (로그인 필요)"
        if AUTO_FALLBACK_LOGIN and not logged_in:
            log("  " + reason + " -> 로그인 후 재시도")
            if await auto_login(page, ctx):
                return await check_once(page, ctx, dump, logged_in=True)
        return [], reason, []

    if not info.get("hasCalendar") or info.get("dayCells", 0) == 0:
        reason = (f"달력이 비어있음 (calendar={info.get('hasCalendar')}, "
                  f"cells={info.get('dayCells')}, notice={info.get('hasNotice')})")
        if AUTO_FALLBACK_LOGIN and not logged_in:
            log("  " + reason + " -> 로그인 후 재시도")
            if await auto_login(page, ctx):
                return await check_once(page, ctx, dump, logged_in=True)
        return [], reason, []

    targets = target_dates()
    by_month = defaultdict(list)
    for d in targets:
        by_month[(d.year, d.month)].append(d)

    log(f"  대상: {fmt(targets[0])} ~ {fmt(targets[-1])} "
        f"(총 {len(targets)}일 / 달력셀 {info['dayCells']}개)")

    found = []
    failed_months = []
    report = []          # 사이클 종료 시 한 번에 출력할 결과
    # 예약 페이지는 현재 월로 열리므로 이를 초기 추정값으로 사용
    assumed = (date.today().year, date.today().month)

    for (y, m), days in sorted(by_month.items()):
        if (y, m) != (date.today().year, date.today().month):
            log(f"[6/6] 익월({y}.{m:02d}) 달력으로 이동")
        ok, assumed = await goto_month(page, y, m, assumed)
        if ok == "closed":
            log(f"  {y}.{m:02d}: 아직 오픈 전 - 예약 가능 없음으로 처리")
            continue
        if not ok:
            log(f"  {y}.{m:02d}: 달력 이동 실패 - 이 달은 확인하지 못했습니다")
            failed_months.append(f"{y}.{m:02d}")
            continue
        try:
            cells = await page.evaluate(SCAN_JS)
        except Exception as e:
            log(f"  스캔 오류: {e}")
            failed_months.append(f"{y}.{m:02d}")
            continue

        avail = {c["day"] for c in cells if c["available"]}

        if dump:
            wanted = {d.day for d in days}
            flog(f"  --- {y}.{m:02d} 셀 덤프 ({len(cells)}개) ---")
            for c in cells:
                mark = "O" if c["available"] else "X"
                star = "*" if c["day"] in wanted else " "
                why = []
                if c.get("disabled"):
                    why.append("disabled")
                if c.get("ariaDis") == "true":
                    why.append("aria-disabled")
                if not c.get("innerTag") and not c.get("hasClick"):
                    why.append("클릭요소없음")
                reason = ("[" + ",".join(why) + "]") if why else ""
                flog(f"   {star}{mark} {c['day']:>2}일 | id={c.get('innerId','')} "
                     f"| inner={c['innerTag']}.{c['innerCls']} {reason}")

        # 상태별 집계 (실측 마크업 기준)
        #   마감      : disabled, class 없음      <button id="b_2" disabled>2</button>
        #   오픈예정  : disabled + class=unableday <button class="unableday" disabled>7</button>
        #   예약가능  : disabled 아님 (클릭 가능)
        total = len(cells)

        def _soon(c):
            return "unableday" in (c.get("innerCls") or "").lower()

        n_soon   = sum(1 for c in cells if _soon(c))
        n_closed = sum(1 for c in cells if c.get("disabled") and not _soon(c))
        n_open   = sum(1 for c in cells if not c.get("disabled"))
        n_id     = sum(1 for c in cells
                       if re.match(r"^b_\d+$", c.get("innerId") or ""))

        report.append(f"{y}.{m:02d} 집계: 전체 {total}일 | 마감 {n_closed} | "
                      f"오픈예정 {n_soon} | 예약가능 {n_open}")

        # 확인 대상(오늘부터 15일)만 따로 집계
        want = {d.day for d in days}
        tc = [c for c in cells if c["day"] in want]
        t_soon   = sum(1 for c in tc if _soon(c))
        t_closed = sum(1 for c in tc if c.get("disabled") and not _soon(c))
        t_open   = [c for c in tc if not c.get("disabled")]
        rng = f"{m}월{days[0].day:02d}~{days[-1].day:02d}일"
        report.append(f"확인 대상 {rng} ({len(days)}일): 마감 {t_closed} / "
                      f"오픈예정 {t_soon} / 예약가능 {len(t_open)}")

        flog(f"  {y}.{m:02d} 상세: id=b_* {n_id}개")
        if t_open:
            flog("    예약가능 상세: " + ", ".join(
                f"{c['day']}일(id={c.get('innerId','') or '없음'}, "
                f"cls={c.get('innerCls','') or '없음'})" for c in t_open))

        hit = [d for d in days if d.day in avail]
        if hit:
            found += hit

    if failed_months and not found:
        return [], f"{', '.join(failed_months)} 달력을 확인하지 못했습니다", report
    if failed_months:
        report.append(f"주의: {', '.join(failed_months)} 은 확인하지 못했습니다")

    return found, None, report


# ---------- 메인 ----------
async def main(interval_min, once, dump, headful, end_hour, login_url=None):
    now = datetime.now()
    unlimited = (end_hour == 0)          # --end 0 -> 사용자가 멈출 때까지 계속
    end_dt = None
    if not unlimited:
        end_dt = now.replace(hour=end_hour, minute=END_MINUTE,
                             second=0, microsecond=0)
        if now >= end_dt and not once:
            log(f"이미 종료 시각({end_hour}:{END_MINUTE:02d})이 지났습니다. "
                f"실행하지 않습니다.")
            log("  계속 실행하려면 --end 0 (무제한) 또는 늦은 시각을 지정하세요.")
            return

    when = "무제한 실행 (Ctrl+C 로 종료)" if unlimited else f"종료 {end_dt:%H:%M}"
    log(f"감시 시작 v{VERSION} (간격 {interval_min}분, 오늘 포함 {DAYS_AHEAD}일, "
        f"{when})")
    log(f"브라우저: {'창 표시(headful)' if headful else 'headless (창 없음, --headful 로 표시)'}")
    log(f"알림: {ALERT_MODE} 모드 / 미리보기는 --test-alert")
    if _CFG:
        log(f"설정: config.py 사용 ({len(_CFG)}개 항목)")
    else:
        log("설정: config.py 없음 (환경변수 또는 기본값 사용)")
    creds_ok = check_credentials()
    if (login_url or not TRY_WITHOUT_LOGIN) and not creds_ok:
        log("로그인 정보 없이 로그인을 시도합니다. 실패할 가능성이 높습니다.")

    async with async_playwright() as p:
        # 헤드리스에서도 일반 브라우저와 동일하게 보이도록 설정
        browser = await p.chromium.launch(
            headless=not headful,
            args=["--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage",
                  "--no-sandbox"])
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 960},
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"))
        # navigator.webdriver 흔적 제거 (헤드리스 감지 회피)
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        await ctx.add_init_script(HASH_GUARD)      # URL 끝 '#' 방지
        page = await ctx.new_page()

        logged_in = False
        if not TRY_WITHOUT_LOGIN:
            logged_in = await ensure_logged_in(page, ctx)
            if not logged_in:
                if headful:
                    await manual_login(ctx)
                    logged_in = True
                else:
                    log("로그인 실패 - 감시를 시작할 수 없습니다.")
                    log("  1) 아이디/비번(ESPOIR_ID/ESPOIR_PW) 확인")
                    log("  2) --headful 로 실행해 직접 로그인")
                    log("  3) debug_*.png 파일에서 실패 화면 확인")
                    notify_problem("로그인에 실패해 감시를 시작하지 못했습니다")
                    await ctx.close()
                    await browser.close()
                    return

        prev = set()
        warned = False
        while True:
            try:
                log("확인 중...")
                found, problem, report = await check_once(
                    page, ctx, dump=dump, logged_in=logged_in)

                # ---- 한 사이클 결과 정리 출력 ----
                log("------ 결과 ------")
                for line in report:
                    log(line)

                if problem:
                    log(f"=> 확인 불가: {problem}")
                    if not warned:            # 문제 알림은 1회만
                        notify_problem(problem)
                        warned = True
                elif found:
                    warned = False
                    log("=> 예약 가능: " + ", ".join(fmt(d) for d in sorted(found)))
                    cur = set(found)
                    if not NOTIFY_ONLY_ON_CHANGE or (cur - prev):
                        notify_available(sorted(cur))
                    else:
                        log("   (이전과 동일 - 알림 생략)")
                    prev = cur
                else:
                    warned = False
                    log("=> 예약 가능한 날짜 없음")
                    prev = set()

            except Exception as e:
                log(f"확인 중 오류: {e}")
                try:
                    await page.screenshot(
                        path=f"monitor_error_{datetime.now():%H%M%S}.png", full_page=True)
                except Exception:
                    pass

            if once:
                break

            now = datetime.now()
            if not unlimited and now >= end_dt:
                break

            if unlimited:
                sleep_sec = interval_min * 60
                tail = "무제한 실행 중"
            else:
                sleep_sec = min(interval_min * 60,
                                (end_dt - now).total_seconds())
                tail = f"종료 {end_dt:%H:%M}"
            nxt = now + timedelta(seconds=sleep_sec)
            log(f"다음 확인: {nxt:%H:%M} ({tail})\n")
            await asyncio.sleep(sleep_sec)

            if not unlimited and datetime.now() >= end_dt:
                break

        await ctx.close()
        await browser.close()

    log(f"감시 종료 ({datetime.now():%H:%M})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=60, help="확인 간격(분), 기본 60")
    ap.add_argument("--end", type=int, default=END_HOUR,
                    help="종료 시각(시), 기본 17. 0 이면 무제한 실행")
    ap.add_argument("--once", action="store_true", help="1회만 확인")
    ap.add_argument("--dump", action="store_true", help="달력 셀 구조 덤프")
    ap.add_argument("--headful", action="store_true", help="브라우저 창 표시")
    ap.add_argument("--test-alert", action="store_true", help="알림 전체 테스트")
    ap.add_argument("--test-mail", action="store_true", help="이메일 발송 테스트")
    ap.add_argument("--verbose", action="store_true",
                    help="파일 전용 상세 로그도 콘솔에 표시")
    ap.add_argument("--version", action="store_true", help="스크립트 버전 표시")
    ap.add_argument("--simulate-hit", action="store_true",
                    help="예약 발견 상황을 그대로 재현 (실제 알림 경로 검증)")
    ap.add_argument("--alert-window", default=None,
                    help=argparse.SUPPRESS)   # 내부용: 경보창 프로세스
    ap.add_argument("--login-first", action="store_true",
                    help="시작하자마자 로그인 (비로그인 시도 건너뜀)")
    ap.add_argument("--login-url", default=None,
                    help="로그인 URL 직접 지정 (일회성 파라미터 포함 URL은 곧 만료됨)")
    args = ap.parse_args()

    # 내부용: 경보창 전용 프로세스로 실행된 경우
    if args.alert_window:
        alert_window_main(args.alert_window)
        sys.exit(0)

    if args.test_alert:
        demo = "예약 가능한 날짜가 열렸습니다.\n\n2026-07-30(목)\n2026-08-04(화)"
        if ALERT_MODE in ("window", "both"):
            alert_window_main(demo)      # 테스트는 현재 프로세스에서 바로 표시
        else:
            pc_alert("에스쁘아 예약 가능!", demo)
            sound_loop(ALERT_SOUND_TIMES)
            speak("예약 가능한 날짜가 열렸습니다")
            time.sleep(10)
        sys.exit(0)

    if args.verbose:
        VERBOSE = True

    if args.test_mail:
        d = date.today() + timedelta(days=1)
        ok = send_mail("[에스쁘아] 메일 발송 테스트",
                       f"테스트 메일입니다.\n\n예약 가능: {fmt(d)}\n\n"
                       f"예약 페이지:\n{SERVICE_URL}\n")
        print("성공" if ok else "실패 - MAIL_USER / MAIL_PASS 를 확인하세요")
        sys.exit(0 if ok else 1)

    if args.version:
        print(f"espoir_monitor.py  버전 {VERSION}")
        print("포함 기능: --simulate-hit, --test-alert, unableday 판정, "
              "월 이동(xpath+구조), 전체화면 경보")
        sys.exit(0)

    if args.simulate_hit:
        # 실제 발견 시와 완전히 동일한 경로(별도 프로세스 경보창 + 소리 +
        # 음성 + 브라우저 열기 + 텔레그램/웹훅)를 그대로 실행한다.
        d0 = date.today()
        demo_dates = [d0 + timedelta(days=1), d0 + timedelta(days=2)]
        log("모의 발견 테스트 - 실제 알림과 동일한 경로로 실행합니다")
        notify_available(demo_dates)
        log("경보창이 떴는지 확인하세요. (뜨지 않으면 tkinter 미설치 가능성)")
        time.sleep(8)
        sys.exit(0)

    if args.interval < 5:
        print("간격이 너무 짧습니다. 최소 5분 이상으로 설정하세요.")
        sys.exit(1)

    try:
        login_url = args.login_url
        if args.login_first and not login_url:
            login_url = LOGIN_ENTRY
        asyncio.run(main(args.interval, args.once, args.dump,
                         args.headful, args.end, login_url))
    except KeyboardInterrupt:
        log("사용자 중단")
