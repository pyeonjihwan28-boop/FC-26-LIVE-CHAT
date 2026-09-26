"""
화면 감시 — FC 26 창을 1초마다 캡처 → Windows 내장 OCR(무료) → 무슨 화면인지 판별 → 필요할 때만 Haiku로 읽기

  보내는 이벤트 (bus):  game(상태)  live(경기 중?)  pause(일시정지?)  clock(경기 시간 분)  hud(등번호, 이름)
                        screen_read(화면, 경기가 바뀌었나, 매치데이인가)
  읽는 화면(lineup_scan.READERS): match 커리어 홈 / me 스쿼드 / opp 예상 라인업 / pause 일시정지 팀 관리
  경기 화면: 왼쪽 위 스코어보드 시계가 보이면 경기 중, 아래쪽 '공 가진 선수'(15 RODRI) 기록, 스코어보드 이미지 보관

  python screen_watch.py --image 스샷.png   저장된 스크린샷으로 화면 판별만 테스트 (Haiku 호출 없음)
"""
import argparse
import asyncio
import ctypes
import ctypes.wintypes as wt
import os
import re
import sys
import threading
import time
from pathlib import Path

from PIL import Image, ImageGrab

import config
from blocks import bus, store, text

try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # 창 좌표를 실제 픽셀로
except Exception:
    pass

u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
EXCLUDE_FROM_CAPTURE = []   # 캡처하는 순간에만 안 찍히게 할 우리 창(채팅·라인업)의 hwnd — overlay_app 이 채움
OCR_MAX_WIDTH = 1920
WORD = re.compile(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ.'\-]{2,}")          # 영어 선수 이름 조각
ANY_WORD = re.compile(r"[0-9A-Za-zÀ-ÿ가-힣]{2,}")
HUD = re.compile(r"^\s*(\d{1,2})\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .'\-]{1,25}?)\s*$")   # 공 가진 선수 "15 RODRI"
CLOCK = re.compile(r"^\s*(\d{1,3})\s*[:：]\s*\d{2}(\s*\+\s*\d{1,2})?\s*$")       # 스코어보드 시계 "09:37"
HUD_BOX, SCOREBOARD_BOX = (0.22, 0.78, 0.78, 0.98), (0, 0, 0.24, 0.2)
SAME_FRAME, SAME_LINEUP, LEAVE_TICKS = 0.7, 0.9, 2    # 화면이 멈춤 / 지난번과 같은 라인업 / 이만큼 안 보이면 나간 것

# 화면 블록: 판별 키워드는 config.WATCH_KEYWORDS, 읽기는 lineup_scan.READERS
SCREENS = {  # 이름: (최소 단어 수, 커리어 메뉴인가 = 보이면 경기 중 아님, 지문에 쓸 영역 x<)
    "pause": (16, False, 1.0),
    "opp": (8, True, 0.6),        # 오른쪽 팀 정보 패널은 지문에서 제외
    "me": (8, True, 0.6),         # 오른쪽 '선수 정보'는 커서 따라 바뀌어서 제외
    "match": (4, True, 0.55),     # 오른쪽 뉴스 카드는 계속 넘어가서 제외
}
LABEL = {"me": "우리 팀 스쿼드", "opp": "상대 예상 라인업", "match": "커리어 홈(다음 경기)", "pause": "경기 중 일시정지(선발 라인업)"}


# ── 게임 창 · 캡처 ──────────────────────────────────────────────
def window_info(hwnd):
    """(제목, exe 이름, pid)"""
    buf = ctypes.create_unicode_buffer(512)
    u32.GetWindowTextW(hwnd, buf, 512)
    pid = wt.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = ""
    h = k32.OpenProcess(0x1000, False, pid.value)
    if h:
        name, size = ctypes.create_unicode_buffer(512), wt.DWORD(512)
        if k32.QueryFullProcessImageNameW(h, 0, name, ctypes.byref(size)):
            exe = Path(name.value).name
        k32.CloseHandle(h)
    return buf.value, exe, pid.value


def top_windows():
    """보이는 창을 맨 위부터 차례로 (hwnd, rect)"""
    out, cloaked = [], ctypes.c_int(0)

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def each(hwnd, _):
        if u32.IsWindowVisible(hwnd) and not u32.IsIconic(hwnd):
            ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)   # 숨은 앱 창 제외
            if not cloaked.value:
                r = wt.RECT()
                u32.GetWindowRect(hwnd, ctypes.byref(r))
                out.append((hwnd, (r.left, r.top, r.right, r.bottom)))
        return True

    u32.EnumWindows(each, 0)
    return out


def grab_game():
    """→ (이미지 또는 None, 상태). "ok" / "missing"(FC 26 창이 없거나 최소화) / "covered"(다른 프로그램 창이 가림)"""
    if not config.WATCH_WINDOW:                              # "" = 주 모니터 전체 (캡처보드 등)
        return _grab(), "ok"
    wins = top_windows()
    idx = next((i for i, (h, r) in enumerate(wins) if r[2] - r[0] >= 400 and r[3] - r[1] >= 300
                and re.search(config.WATCH_WINDOW, "{1} {0}".format(*window_info(h)[:2]), re.I)), None)
    if idx is None:
        return None, "missing"
    gl, gt, gr, gb = rect = wins[idx][1]
    area = max(1, (gr - gl) * (gb - gt))
    for h, (l, t, r, b) in wins[:idx]:                       # 게임보다 위에 있는 창 (우리 창은 괜찮음)
        if window_info(h)[2] != os.getpid() and \
                max(0, min(r, gr) - max(l, gl)) * max(0, min(b, gb) - max(t, gt)) / area > 0.15:
            return None, "covered"
    return _grab(rect), "ok"


def _grab(bbox=None):
    """우리 창은 이 순간만 캡처에서 빼고 찍음 (화면에는 그대로 보임 = 깜빡임 없음)"""
    hwnds = list(EXCLUDE_FROM_CAPTURE)
    for h in hwnds:
        u32.SetWindowDisplayAffinity(h, 0x11)                # WDA_EXCLUDEFROMCAPTURE
    try:
        if hwnds:
            time.sleep(0.05)
        return (ImageGrab.grab(bbox=bbox, all_screens=True) if bbox else ImageGrab.grab()).convert("RGB")
    finally:
        for h in hwnds:
            u32.SetWindowDisplayAffinity(h, 0)


def crop(img, box):
    w, h = img.size
    return img.crop((round(box[0] * w), round(box[1] * h), round(box[2] * w), round(box[3] * h)))


# ── OCR (Windows.Media.Ocr) ────────────────────────────────────
class WinOCR:
    def __init__(self, lang="ko"):
        try:    # winrt 를 먼저 불러오면 나중에 onnxruntime(음성 구간 감지)을 불러올 때 프로그램이 통째로 죽음 → 항상 먼저
            import onnxruntime  # noqa: F401
        except ImportError:
            pass
        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.media.ocr import OcrEngine
        self.engine = OcrEngine.try_create_from_language(Language(lang))
        if self.engine is None:
            raise RuntimeError("Windows 한국어 OCR이 없습니다. 설정 → 시간 및 언어 → 언어에서 한국어를 추가하세요.")
        self._bmp, self._fmt = SoftwareBitmap, BitmapPixelFormat.BGRA8
        self.loop = asyncio.new_event_loop()                 # 이 객체를 만든 스레드에서만 사용

    def read(self, img: Image.Image, scale=1.0):
        """[(글자, 가로 중심 0~1, 세로 중심 0~1)] — 같은 줄이라도 간격이 넓으면 따로 (이름표 두 개가 한 줄로 붙는 것 방지)"""
        if scale != 1.0:
            img = img.resize((round(img.width * scale), round(img.height * scale)))
        if img.width > OCR_MAX_WIDTH:
            img = img.resize((OCR_MAX_WIDTH, round(img.height * OCR_MAX_WIDTH / img.width)))
        r, g, b = img.split()
        bmp = self._bmp.create_copy_from_buffer(Image.merge("RGBA", (b, g, r, Image.new("L", img.size, 255))).tobytes(),
                                                self._fmt, img.width, img.height)
        res = self.loop.run_until_complete(self.engine.recognize_async(bmp))
        out = []
        for line in res.lines:
            segs = [[]]
            for w in line.words:
                rc = w.bounding_rect
                if segs[-1] and rc.x - (segs[-1][-1].bounding_rect.x + segs[-1][-1].bounding_rect.width) > 2.5 * rc.height:
                    segs.append([])
                segs[-1].append(w)
            for ws in segs:
                rc = [w.bounding_rect for w in ws]
                x1, x2 = min(q.x for q in rc), max(q.x + q.width for q in rc)
                y1, y2 = min(q.y for q in rc), max(q.y + q.height for q in rc)
                out.append((" ".join(w.text for w in ws) if len(segs) > 1 else line.text,
                            (x1 + x2) / 2 / img.width, (y1 + y2) / 2 / img.height))
        return out


# ── 화면 판별 ──────────────────────────────────────────────────
def flat(lines, x_max=1.0):
    return re.sub(r"\s+", "", " ".join(t for t, x, _ in lines if x < x_max))


def detect(lines):
    """무슨 화면인지 (SCREENS 의 이름) / None. 키워드는 띄어쓰기 무시, '|'는 '또는'"""
    f = flat(lines)
    return next((who for who in SCREENS if all(any(k.replace(" ", "") in f for k in kw.split("|"))
                                              for kw in config.WATCH_KEYWORDS[who])), None)


def fingerprint(lines, who):
    """화면 지문 = 단어 + 대략적 위치 (선수가 바뀌면 달라짐). 계속 바뀌는 오른쪽 패널은 제외"""
    x_max = SCREENS[who][2]
    words = ANY_WORD if who == "match" else WORD
    return {(w.lower(), round(x * 8), round(y * 8)) for t, x, y in lines if x < x_max for w in words.findall(t)}


def similar(a, b):
    return len(a & b) / len(a | b) if a or b else 1.0


def minute_of(lines):
    """경기 화면: 왼쪽 위에 시계만 따로 적힌 줄 → 경기 시간(분) / 없으면 None"""
    return next((int(m.group(1)) for t, x, y in lines if x < 0.4 and y < 0.3 for m in [CLOCK.match(t)] if m), None)


# ── 감시 루프 ──────────────────────────────────────────────────
class ScreenWatcher:
    def __init__(self):
        self.stop_ev = threading.Event()
        self.scanned = {k: set() for k in SCREENS}           # 마지막으로 Haiku로 읽은 화면 지문
        self.state = {}                                      # 화면 → {img, lines, fp, missing}
        self.prev = None                                     # 직전 프레임 (화면, 지문)
        self.lock = threading.Lock()
        self.busy, self.pending = {}, {}                     # 읽는 중 / 읽는 동안 새로 본 모습
        self.status = self.minute = None
        self.live = self.paused = False
        self.live_seen = 0.0
        self.board = None                                    # 최신 스코어보드 이미지 (goals.py 가 읽음)
        bus.on("rescan", self.forget)

    def start(self):
        threading.Thread(target=self._run, name="screen-watch", daemon=True).start()

    def stop(self):
        self.stop_ev.set()

    def forget(self):
        """다음에 보이는 화면은 전부 새로 읽음 (채팅 창 우클릭 → 라인업 다시 읽기)"""
        for k in self.scanned:
            self.scanned[k] = set()
        print("[화면감시] 다음에 보이는 화면은 전부 다시 읽습니다")

    def _run(self):
        try:
            self.ocr = WinOCR()
        except Exception as e:
            print(f"[화면감시] OCR을 켜지 못해 화면 인식을 끕니다: {e}")
            return
        print("[화면감시] 켜짐 — 커리어 홈 / 스쿼드 / 예상 라인업 / 경기 화면을 자동으로 읽습니다")
        while not self.stop_ev.is_set():
            t0 = time.monotonic()
            try:
                self.tick()
            except Exception as e:
                print(f"[화면감시] 오류: {e}")
            self.stop_ev.wait(max(0.2, config.WATCH_INTERVAL_SEC - (time.monotonic() - t0)))

    def _set(self, attr, value, event, msg=None):
        """상태가 바뀌었을 때만 이벤트"""
        if getattr(self, attr) != value:
            setattr(self, attr, value)
            if msg:
                print(msg)
            bus.emit(event, value)

    def tick(self):
        img, status = grab_game()
        self._set("status", status, "game", {"missing": "[화면감시] FC 26 창이 화면에 없습니다 → 캡처 멈춤",
                                              "covered": "[화면감시] 다른 프로그램이 게임을 가립니다 → 캡처 멈춤"}.get(status))
        lines = self.ocr.read(img) if img is not None else []
        who = detect(lines) if lines else None
        if img is not None:
            self._play(img, who, lines)

        for w in [w for w in self.state if w != who]:       # 떠난 화면: 마지막 모습으로 한 번 더 확인
            st = self.state[w]
            st["missing"] += 1
            if st["missing"] >= LEAVE_TICKS:
                del self.state[w]
                self._maybe_read(w, st)
        fp = fingerprint(lines, who) if who else set()
        stable = who and len(fp) >= SCREENS[who][0] and self.prev and self.prev[0] == who \
            and similar(self.prev[1], fp) >= SAME_FRAME
        self.prev = (who, fp) if who else None
        if stable:
            st = self.state.setdefault(who, {})
            st.update(img=img, lines=lines, fp=fp, missing=0, who=who)
            self._maybe_read(who, st)

    # ── 경기 화면 ───────────────────────────────────────────────
    def _play(self, img, who, lines):
        now = time.monotonic()
        minute = minute_of(lines) if who is None else None
        if minute is not None:
            self.live_seen = now
            self._set("minute", minute, "clock")
        pause = self.live and (who == "pause" or "경기재개" in flat(lines))
        if pause:
            self.live_seen = now                              # 일시정지도 경기 중 (채팅만 멈추고 가림)
        self._set("paused", pause, "pause", "[화면감시] 경기 일시정지" if pause else ("[화면감시] 경기 재개" if self.live else None))
        live = not (who and SCREENS[who][1]) and now - self.live_seen < config.LIVE_GRACE_SEC
        self._set("live", live, "live", "[화면감시] " + ("경기 화면 감지 (스코어보드)" if live else "경기 화면 아님"))
        if not live:
            self.minute = None
        if live and who is None:
            self.board = crop(img, SCOREBOARD_BOX)
            for t, _, _ in self.ocr.read(crop(img, HUD_BOX), scale=1.5):
                m = HUD.match(t)
                if m:
                    bus.emit("hud", int(m.group(1)), m.group(2).strip())
                    break

    # ── 읽기 (Haiku 호출은 백그라운드 스레드 — 그동안에도 화면 감시는 계속) ──
    def _skip(self, who, st):
        """Haiku 없이 끝낼 수 있으면 True (같은 경기·이미 제대로 읽음)"""
        data = store.load("lineup.json", {})
        import lineup_scan
        if who == "match":
            left = flat(st["lines"], SCREENS["match"][2])
            names = [re.sub(r"\s+", "", (data.get(s) or {}).get("name", "")) for s in ("home", "away")]
            return bool(config.MY_TEAM_NAME) and all(text.contains(n, left) for n in names)
        if who == "pause":
            return all(lineup_scan.lineup_ok(data.get(s) or {}) for s in ("home", "away"))
        return similar(st["fp"], self.scanned[who]) >= SAME_LINEUP and \
            lineup_scan.lineup_ok(data.get(lineup_scan.side_of(who)) or {})

    def _maybe_read(self, who, st):
        if "fp" not in st:
            return
        matchday = "상대분석" in flat(st["lines"], 0.55) if who == "match" else False   # 경기 당일에만 생기는 버튼
        with self.lock:
            if self.busy.get(who):
                self.pending[who] = st
                return
            if self._skip(who, st):
                bus.emit("screen_read", who, False, matchday)    # 이미 읽은 그대로 → 비용 0
                return
            self.scanned[who] = st["fp"]
            self.busy[who] = True
        threading.Thread(target=self._read, args=(who, st, matchday), name=f"read-{who}", daemon=True).start()

    def _read(self, who, st, matchday):
        import lineup_scan
        print(f"\n[화면감시] {LABEL[who]} 화면 → Claude Haiku로 읽는 중…")
        t0 = time.monotonic()
        try:
            out = lineup_scan.apply(who, lineup_scan.read(who, st["img"], st["lines"]), st["lines"])
            if out is False and who != "match":
                self.scanned[who] = set()                        # 실패 → 다음에 보이면 다시
            else:
                print(f"[화면감시] {LABEL[who]} 반영 ({time.monotonic() - t0:.1f}초)\n")
                bus.emit("screen_read", who, bool(out) if who == "match" else False, matchday)
        except Exception as e:
            print(f"[화면감시] {LABEL[who]} 읽기 실패: {e}")
            self.scanned[who] = set()
        finally:
            with self.lock:
                self.busy[who] = False
                nxt = self.pending.pop(who, None)
            if nxt is not None:
                self._maybe_read(who, nxt)


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="FC 26 화면 판별 테스트")
    ap.add_argument("--image", required=True, help="스크린샷으로 화면 판별만 테스트")
    args = ap.parse_args()
    lines = WinOCR().read(Image.open(args.image).convert("RGB"))
    for t, x, y in lines:
        print(f"  ({x:.2f}, {y:.2f})  {t}")
    who = detect(lines)
    print(f"\n판별: {who or ('경기 중 (스코어보드)' if minute_of(lines) is not None else '인식할 화면 아님')}"
          + (f" · 단어 {len(fingerprint(lines, who))}개 (읽으려면 {SCREENS[who][0]}개 이상)" if who else ""))


if __name__ == "__main__":
    main()
