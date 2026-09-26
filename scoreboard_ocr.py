"""
스코어보드 로컬 OCR — 골 확인 때마다 Haiku 호출(1회 약 0.1센트)하는 대신 Windows OCR로 먼저 읽음
  (참고: PaddlePaddle/PaddleSports 스코어보드 인식, hl123-123/soccer-scoreboard-recognition)

  read_scoreboard(img, away_short) -> {"home": int, "away": int} 또는 None
  - screen_watch.SCOREBOARD_BOX=(0,0,0.24,0.2) 로 잘린 왼쪽 위 스코어보드 이미지를 받음
  - 확실히 읽히면 점수 반환, 애매하면 None → 기존처럼 Haiku fallback (goals.py._read)
  - OCR 인스턴스는 스레드별로 캐시(WinOCR은 만든 스레드에서만 써야 함)
  - blocks(anthropic) 의존성 없이 동작하도록 norm/contains 를 인라인 구현
"""
import difflib
import re
import threading

CLOCK = re.compile(r"\d{1,3}\s*[:：]\s*\d{2}")
SCORE_DIGIT = re.compile(r"(?<!\d)(\d{1,2})(?!\d)")   # 단독으로 붙은 1~2자리 숫자 = 점수
_tls = threading.local()


def _norm(s):
    """소문자 영어(와 한글)만 남김"""
    return re.sub(r"[^a-z가-힣]", "", str(s).lower())


def _contains(needle, hay, ratio=0.8):
    """hay 안에 needle 이 (오타 조금 허용해서) 들어 있는지"""
    if not needle:
        return False
    if needle in hay:
        return True
    n = len(needle)
    return any(difflib.SequenceMatcher(None, needle, hay[i:i + n]).ratio() >= ratio
               for i in range(max(1, len(hay) - n + 1)))


def _ocr():
    if not hasattr(_tls, "ocr"):
        from screen_watch import WinOCR
        _tls.ocr = WinOCR()
    return _tls.ocr


def parse_scoreboard_lines(lines, away_short=""):
    """WinOCR 결과 [(text, x_center, y_center)] → {"home":점수,"away":점수} 또는 None.
    스코어보드는 보통 두 줄(위=홈팀 약칭+점수, 아래=원정팀 약칭+점수) + 시계(MM:SS)."""
    scores = []                                   # (y, 점수, 그 줄 텍스트)
    for t, x, y in lines:
        if CLOCK.search(t):                       # 시계 줄은 제외
            continue
        digits = SCORE_DIGIT.findall(t)
        if len(digits) == 1:                      # 점수 숫자가 하나만 있는 줄 = 팀 점수 줄
            scores.append((y, int(digits[0]), t))
    if len(scores) < 2:
        return None
    scores.sort(key=lambda s: s[0])               # y 순 → 위/아래
    top, bottom = scores[0], scores[-1]            # 셋 이상 잡히면 가장 위·아래만 사용
    away = _norm((away_short or "").strip())
    if away and _contains(away[:4], _norm(top[2])[:6]):   # 위 줄이 원정 약칭이면 바꿈
        return {"home": bottom[1], "away": top[1]}
    return {"home": top[1], "away": bottom[1]}


def read_scoreboard(img, away_short=""):
    """img: PIL Image (스코어보드 영역으로 이미 잘림). 동기 호출(블로킹) → async 에서는 executor 에서."""
    try:
        lines = _ocr().read(img, scale=2.0)        # 스코어보드는 작아서 2배 확대
    except Exception:
        return None
    return parse_scoreboard_lines(lines, away_short)
