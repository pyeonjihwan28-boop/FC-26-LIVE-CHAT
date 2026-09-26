"""
채팅·라인업 파이썬 창 (tkinter) — python main.py 를 실행하면 자동으로 뜸

  python overlay_app.py            서버 없이 가짜 채팅 + lineup.json 으로 모양 미리보기

- 디자인은 overlay.html / lineup.html 을 그대로 옮김 (Pillow로 그려서 창에 표시)
- 채팅: main.py 가 post() 로 넘겨주는 메시지를 표시 (창 크기 조절 가능)
- 라인업: lineup.json 이 바뀌면 1초 안에 다시 그림 (자동 매핑·직접 수정 모두)
- OBS: 소스 추가 → '윈도우 캡처' 로 각 창을 잡으면 됨. 창 하나를 닫으면 프로그램 종료
"""
import ctypes
import queue
import random
import re
import threading
import time
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageTk

from blocks import bus, store

HERE = Path(__file__).resolve().parent
FONTS = Path("C:/Windows/Fonts")
MAX_MSGS = 60

try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # 고해상도에서 글자 안 흐리게
except Exception:
    pass

_font_cache = {}


def font(kind, px):
    """kind: ko(맑은 고딕) / kob(맑은 고딕 굵게) / num(Bahnschrift 굵은 좁은 글꼴) / emoji"""
    key = (kind, round(px * 2) / 2)
    if key not in _font_cache:
        name = {"ko": "malgun.ttf", "kob": "malgunbd.ttf", "num": "bahnschrift.ttf", "emoji": "seguiemj.ttf"}[kind]
        try:
            f = ImageFont.truetype(str(FONTS / name), max(1, round(px)))
            if kind == "num":
                f.set_variation_by_name("Bold SemiCondensed")
        except Exception:
            f = ImageFont.load_default(max(1, round(px)))
        _font_cache[key] = f
    return _font_cache[key]


def hexrgb(h, a=255):
    h = (h or "#ffffff").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a)
    except ValueError:
        return (255, 255, 255, a)


def fnv(s):
    h = 2166136261
    for ch in s:
        h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    return h


def paste(im, src, xy):
    """RGBA 이미지를 투명도대로 얹기"""
    im.paste(src, (round(xy[0]), round(xy[1])), src)


def hsl(h, s, l):
    import colorsys
    r, g, b = colorsys.hls_to_rgb(h / 360, l, s)
    return (round(r * 255), round(g * 255), round(b * 255), 255)


# ── 채팅 ─────────────────────────────────────────────────────
TIERS = [  # 후원 금액 구간별 색 (본문, 머리, 글자) — overlay.html 과 같음
    (100000, "#e5484d", "#c9363c", "#ffffff"),
    (50000, "#d6409f", "#b8318a", "#ffffff"),
    (20000, "#f76b15", "#dc5a0c", "#ffffff"),
    (10000, "#ffc53d", "#f0ae1c", "#241a00"),
    (5000, "#3dd68c", "#26b875", "#03261a"),
    (2000, "#4ccce6", "#2fb2cd", "#062a33"),
    (0, "#5b8def", "#4574d6", "#ffffff"),
]
NAME_COLORS = {"": (170, 170, 170, 255), "member": (43, 166, 64, 255), "mod": (94, 132, 241, 255)}
TOKEN = re.compile(r"\S+\s*|\s+")


def wrap_runs(runs, width, first_x=0):
    """runs: [(text, font, fill, emoji)] → 줄 목록 [[(x, text, font, fill, emoji), ...], ...]
    단어 단위로 줄바꿈, 한 단어가 너무 길면 글자 단위로 자름 (overflow-wrap: anywhere)"""
    lines, cur, x = [], [], first_x
    for text, f, fill, emoji in runs:
        for tok in TOKEN.findall(text):
            w = f.getlength(tok)
            if x + f.getlength(tok.rstrip()) <= width or not cur and x == 0 and w <= width:
                cur.append((x, tok, f, fill, emoji))
                x += w
                continue
            if f.getlength(tok.rstrip()) <= width:        # 다음 줄로
                lines.append(cur)
                cur, x = [(0, tok, f, fill, emoji)], w
                continue
            piece = ""                                    # 긴 단어는 글자 단위로
            for ch in tok:
                if x + f.getlength(piece + ch) > width and (piece or cur):
                    if piece:
                        cur.append((x, piece, f, fill, emoji))
                    lines.append(cur)
                    cur, x, piece = [], 0, ""
                piece += ch
            if piece:
                cur.append((x, piece, f, fill, emoji))
                x += f.getlength(piece)
    if cur:
        lines.append(cur)
    return lines or [[]]


class ChatView:
    def __init__(self, cfg, root, scale):
        self.cfg, self.root, self.s = cfg, root, scale
        self.msgs = []
        self.caption, self.caption_until = "", 0.0
        self.live = False
        self.viewers = 0                                  # 동시 시청자 (audience.py)
        self.waiting = None                               # 대기 중이면 {"match"/"me"/"opp"/"live": bool}
        self.request = ""                                 # 사용자에게 부탁 (예: '상대 분석'을 눌러 주세요)
        self.cover = False                                # 일시정지 중: 채팅을 가림
        self.dirty = True
        self._avatars = {}
        w = round(getattr(cfg, "WINDOW_CHAT_SIZE", (400, 700))[0] * scale)
        h = round(getattr(cfg, "WINDOW_CHAT_SIZE", (400, 700))[1] * scale)
        root.title("실시간 채팅")
        root.configure(bg="#0f0f0f")
        root.geometry(f"{w}x{h}")
        root.minsize(round(240 * scale), round(200 * scale))
        self.label = tk.Label(root, bg="#0f0f0f", bd=0, highlightthickness=0)
        self.label.pack(fill="both", expand=True)
        self.size = (w, h)
        self.label.bind("<Configure>", self._on_resize)

    def _on_resize(self, e):
        if (e.width, e.height) != self.size and e.width > 10 and e.height > 10:
            self.size = (e.width, e.height)
            self.dirty = True

    def set_waiting(self, w):
        self.waiting = w
        self.dirty = True

    def set_cover(self, on):
        self.cover = bool(on)
        self.dirty = True

    def set_request(self, text):
        self.request = text or ""
        self.dirty = True

    def set_viewers(self, n):
        self.viewers = int(n)
        self.live = True
        self.dirty = True

    def add(self, m):
        if not m.get("name") or not (m.get("text") or m.get("type") == "member"):
            return
        self.msgs.append(m)
        del self.msgs[:-MAX_MSGS]
        self.live = True
        self.dirty = True

    def set_caption(self, text):
        self.caption, self.caption_until = text, time.monotonic() + 8
        self.dirty = True

    def tick(self):
        if self.caption and time.monotonic() > self.caption_until:
            self.caption = ""
            self.dirty = True
        if self.dirty:
            self.dirty = False
            self.photo = ImageTk.PhotoImage(self.render())
            self.label.configure(image=self.photo)

    # 그리기 ──────────────────────────────────────────────
    def avatar(self, name, px):
        key = (name, px)
        if key not in self._avatars:
            k = 4
            im = Image.new("RGBA", (px * k, px * k), (0, 0, 0, 0))
            d = ImageDraw.Draw(im)
            d.ellipse((0, 0, px * k - 1, px * k - 1), fill=hsl(fnv(name) % 360, 0.42, 0.40))
            s = re.sub(r"^@(user-)?", "", name).strip()
            d.text((px * k / 2, px * k / 2 + k), (s[:1] or "?").upper(), font=font("kob", px * k * 0.46),
                   fill="white", anchor="mm")
            self._avatars[key] = im.resize((px, px), Image.LANCZOS)
        return self._avatars[key]

    def render(self):
        W, H = self.size
        fs = getattr(self.cfg, "WINDOW_FONT_SIZE", 14) * self.s
        im = Image.new("RGB", (W, H), (15, 15, 15))
        d = ImageDraw.Draw(im, "RGBA")                   # 반투명 색은 섞어서 그림
        y = H - 10 * self.s                              # 새 메시지는 아래, 오래된 건 위로 밀려남
        for m in reversed(self.msgs):
            block = self._layout(m, W, fs)
            y -= block["h"]
            if y + block["h"] < 0:
                break
            block["draw"](im, d, y)
        if self.cover:                                    # 일시정지: 채팅을 가리고 안내
            d.rectangle((0, 0, W, H), fill=(15, 15, 15, 255))
            d.text((W / 2, H * 0.42), "⏸", font=font("emoji", fs * 3), anchor="mm", embedded_color=True)
            d.text((W / 2, H * 0.42 + fs * 3.2), "경기 일시정지 중", font=font("kob", fs * 1.3), fill=(241, 241, 241, 255), anchor="mm")
            d.text((W / 2, H * 0.42 + fs * 5.2), "‘경기 재개’를 누르면 채팅이 다시 올라옵니다", font=font("ko", fs * 0.95),
                   fill=(160, 160, 160, 255), anchor="mm")
        elif self.waiting is not None:
            self._draw_waiting(d, W, H, fs)
        self._draw_head(d, W, fs)                        # 위로 넘친 메시지는 헤더가 덮음
        return im

    def _draw_head(self, d, W, fs):
        s, top = self.s, 0
        if getattr(self.cfg, "WINDOW_HEADER", True):
            top = round(46 * s)
            d.rectangle((0, 0, W, top), fill=(15, 15, 15, 255))
            r = 4 * s
            cx, cy = 16 * s + r, top / 2
            if self.live:
                d.ellipse((cx - r - 3 * s, cy - r - 3 * s, cx + r + 3 * s, cy + r + 3 * s), fill=(90, 32, 30, 255))
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 78, 69, 255) if self.live else (90, 90, 90, 255))
            title = "실시간 채팅" + (" · 송출 대기" if self.waiting is not None else "" if self.live else " · 준비 중")
            d.text((cx + r + 10 * s, cy), title, font=font("ko", 15 * s), fill=(241, 241, 241, 255), anchor="lm")
            if self.viewers:                              # 유튜브 '○○명 시청 중'
                d.text((W - 16 * s, cy), f"{self.viewers:,}명 시청 중", font=font("ko", 12.5 * s),
                       fill=(170, 170, 170, 255), anchor="rm")
            d.line((0, top - 1, W, top - 1), fill=(40, 40, 40, 255), width=max(1, round(s)))
        if self.request:
            f = font("kob", fs * 0.95)
            lines = wrap_runs([(self.request, f, (255, 255, 255, 255), False)], W - 48 * s)
            lh = f.size * 1.45
            bh = len(lines) * lh + 16 * s
            d.rectangle((0, top, W, top + 10 * s + bh), fill=(15, 15, 15, 255))
            d.rounded_rectangle((12 * s, top + 10 * s, W - 12 * s, top + 10 * s + bh), 8 * s, fill=(247, 107, 21, 255))
            self._draw_lines(d, lines, 24 * s, top + 18 * s, lh)
            top += 10 * s + bh
        if self.caption:
            f = font("ko", fs * 0.86)
            lines = wrap_runs([("🎙 ", font("emoji", fs * 0.8), None, True),
                               (self.caption, f, (207, 207, 207, 255), False)], W - 44 * s)
            lh = f.size * 1.5
            ch = len(lines) * lh + 12 * s
            d.rectangle((0, top, W, top + 8 * s + ch), fill=(15, 15, 15, 255))
            d.rounded_rectangle((12 * s, top + 8 * s, W - 12 * s, top + 8 * s + ch), 6 * s, fill=(0, 0, 0, 255))
            self._draw_lines(d, lines, 22 * s, top + 14 * s, lh)

    def _draw_waiting(self, d, W, H, fs):
        """라인업을 다 읽기 전: 화면 가운데 안내 (채팅은 아직 시작 안 함)"""
        s = self.s
        names = {"match": ("커리어 홈(센트럴)", "경기·내 팀"), "me": ("팀 관리 → 스쿼드", "우리 선발"),
                 "opp": ("상대 분석 → 예상 라인업", "상대 선발"), "live": ("경기 화면", "킥오프")}
        rows = [(names[k][0], names[k][1], v) for k, v in self.waiting.items() if k in names]
        y = H * 0.36
        d.rounded_rectangle((16 * s, y - fs * 2, W - 16 * s, y + fs * (4.6 + 1.9 * len(rows))), 10 * s,
                            fill=(15, 15, 15, 235), outline=(40, 40, 40, 255))
        only_live = [k for k in self.waiting] == ["live"]
        d.text((W / 2, y), "경기가 시작되면 채팅이 송출됩니다" if only_live else "경기·라인업을 읽고 경기가 시작되면 채팅이 송출됩니다",
               font=font("kob", fs * (1.05 if only_live else 0.95)), fill=(241, 241, 241, 255), anchor="mm")
        y += fs * 2.6
        for screen, who, done in rows:
            mark, color = ("●", (61, 214, 140, 255)) if done else ("○", (150, 150, 150, 255))
            d.text((W / 2, y), f"{mark}  {who}: {screen}", font=font("ko", fs * 0.95), fill=color, anchor="mm")
            y += fs * 1.9
        d.text((W / 2, y + fs * 0.6), "킥오프하면 자동으로 시작" if only_live else "게임에서 화면을 차례로 잠깐씩 띄워 두세요", font=font("ko", fs * 0.85),
               fill=(130, 130, 130, 255), anchor="mm")

    @staticmethod
    def _draw_lines(d, lines, x0, y0, lh):
        for i, line in enumerate(lines):
            base = y0 + i * lh + lh * 0.72
            for x, text, f, fill, emoji in line:
                if emoji:
                    d.text((x0 + x, base), text, font=f, anchor="ls", embedded_color=True)
                else:
                    d.text((x0 + x, base), text, font=f, fill=fill, anchor="ls")

    def _layout(self, m, W, fs):
        s = self.s
        if m.get("type") == "member":
            return self._layout_member(m, W, fs)
        if m.get("donation", 0) > 0:
            return self._layout_tip(m, W, fs)
        av = round(24 * s)
        x0 = 16 * s + av + 14 * s
        role = m.get("role") or ""
        runs = [(m["name"] + " ", font("ko", fs * 0.93), NAME_COLORS.get(role, NAME_COLORS[""]), False)]
        if role in ("mod", "member"):
            runs.append(("🔧 " if role == "mod" else "⭐ ", font("emoji", fs * 0.78), None, True))
        runs.append((m["text"], font("ko", fs), (241, 241, 241, 255), False))
        lh = fs * 1.55
        lines = wrap_runs(runs, W - x0 - 16 * s)
        h = len(lines) * lh + 8 * s

        def draw(im, d, y):
            paste(im, self.avatar(m["name"], av), (round(16 * s), round(y + 4 * s + 1 * s)))
            self._draw_lines(d, lines, x0, y + 4 * s, lh)
        return {"h": h, "draw": draw}

    def _layout_member(self, m, W, fs):
        """유튜브 새 멤버 카드: 초록 머리(아바타·이름·'새 멤버') + 한마디가 있으면 연한 초록 본문"""
        s = self.s
        av = round(24 * s)
        pad = 14 * s
        inner = W - 24 * s
        lh = fs * 1.55
        white = (255, 255, 255, 255)
        channel = getattr(self.cfg, "CHANNEL_NAME", "")
        lines = wrap_runs([(m["text"], font("ko", fs), white, False)], inner - 2 * pad) if m.get("text") else []
        top_h = 8 * s * 2 + fs * 1.3 + fs * 0.86 * 1.3
        body_h = (len(lines) * lh + 18 * s) if lines else 0
        h = 12 * s + top_h + body_h

        def draw(im, d, y):
            x1, y1 = 12 * s, y + 6 * s
            x2 = x1 + inner
            d.rounded_rectangle((x1, y1, x2, y1 + top_h + body_h), 8 * s, fill=(11, 128, 67, 255))
            d.rounded_rectangle((x1, y1, x2, y1 + top_h), 8 * s, fill=(15, 157, 88, 255))
            if lines:
                d.rectangle((x1, y1 + top_h - 8 * s, x2, y1 + top_h), fill=(15, 157, 88, 255))
            paste(im, self.avatar(m["name"], av), (x1 + pad, y1 + top_h / 2 - av / 2))
            tx = x1 + pad + av + 12 * s
            d.text((tx, y1 + 8 * s), m["name"], font=font("kob", fs), fill=white, anchor="lt")
            sub = "새 멤버" + (f" · {channel}에 오신 것을 환영합니다!" if channel else "")
            d.text((tx, y1 + 8 * s + fs * 1.3), sub, font=font("ko", fs * 0.86), fill=(220, 245, 230, 255), anchor="lt")
            if lines:
                self._draw_lines(d, lines, x1 + pad, y1 + top_h + 8 * s, lh)
        return {"h": h, "draw": draw}

    def _layout_tip(self, m, W, fs):
        s = self.s
        amt = int(m["donation"])
        _, body_c, head_c, fg = next(t for t in TIERS if amt >= t[0])
        fg = hexrgb(fg)
        av = round(24 * s)
        pad = 14 * s
        inner = W - 24 * s
        f = font("ko", fs)
        lh = fs * 1.55
        lines = wrap_runs([(m["text"], f, fg, False)], inner - 2 * pad) if m.get("text") else []
        top_h = 8 * s * 2 + fs * 0.88 * 1.3 + fs * 1.3
        body_h = (len(lines) * lh + 18 * s) if lines else 0
        h = 12 * s + top_h + body_h

        def draw(im, d, y):
            x1, y1 = 12 * s, y + 6 * s
            x2, y2 = x1 + inner, y1 + top_h + body_h
            d.rounded_rectangle((x1, y1, x2, y2), 8 * s, fill=hexrgb(body_c))
            if lines:
                d.rounded_rectangle((x1, y1, x2, y1 + top_h + 8 * s), 8 * s, fill=hexrgb(head_c))
                d.rectangle((x1, y1 + top_h, x2, y1 + top_h + 8 * s + 2), fill=hexrgb(body_c))
            else:
                d.rounded_rectangle((x1, y1, x2, y2), 8 * s, fill=hexrgb(head_c))
            paste(im, self.avatar(m["name"], av), (round(x1 + pad), round(y1 + top_h / 2 - av / 2)))
            tx = x1 + pad + av + 12 * s
            d.text((tx, y1 + 8 * s), m["name"], font=font("ko", fs * 0.88), fill=fg[:3] + (217,), anchor="lt")
            d.text((tx, y1 + 8 * s + fs * 0.88 * 1.3), f"₩{amt:,}", font=font("kob", fs), fill=fg, anchor="lt")
            if lines:
                self._draw_lines(d, lines, x1 + pad, y1 + top_h + 8 * s, lh)
        return {"h": h, "draw": draw}


# ── 라인업 ───────────────────────────────────────────────────
CORAL, CORAL2 = (255, 75, 68, 255), (255, 122, 89, 255)
INK, MUTED, TEXT = (10, 14, 26, 255), (154, 163, 181, 255), (245, 246, 248, 255)
JERSEY = [(13, 3), (7.5, 5), (1.5, 11.5), (6, 17.5), (10, 15), (10, 37), (30, 37), (30, 15), (34, 17.5),
          (38.5, 11.5), (32.5, 5), (27, 3), (25, 5), (22.5, 6), (20, 6.3), (17.5, 6), (15, 5)]
CARD_W, GAP, PAD = 300, 16, 16


def slots(formation, count):
    rows = [1] + [int(n) for n in re.findall(r"\d+", str(formation or "")) if int(n) > 0]
    out, top, bottom = [], 12, 89
    for r, n in enumerate(rows):
        y = bottom - (r * (bottom - top) / (len(rows) - 1) if len(rows) > 1 else 0)
        w = min(78, (n - 1) * 25)
        for i in range(n):
            out.append((50 - w / 2 + i * w / (n - 1) if n > 1 else 50, y, r == 0))
    return out[:count], len(out)


class LineupView:
    def __init__(self, cfg, win, scale):
        self.cfg, self.win, self.s = cfg, win, scale
        self.k = scale * 2                                 # 2배로 그린 뒤 줄여서 매끄럽게
        self.mtime, self.data = None, None
        self.side = getattr(cfg, "WINDOW_LINEUP_SIDE", "")
        win.title("라인업")
        win.configure(bg="#0f0f0f")
        win.resizable(False, False)
        self.label = tk.Label(win, bg="#0f0f0f", bd=0, highlightthickness=0)
        self.label.pack()
        self.next_check = 0.0

    def tick(self):
        now = time.monotonic()
        if now < self.next_check:
            return
        self.next_check = now + 1.0
        mtime = store.mtime("lineup.json")
        if mtime == self.mtime:
            return
        self.mtime = mtime
        data = store.load("lineup.json")
        if not data:
            return                                         # 문법 오류 등 → 이전 모습 유지
        self.data = data
        try:
            img = self.render(self.data)
        except Exception as e:
            print(f"[라인업 창] 그리기 실패: {e}")
            return
        self.photo = ImageTk.PhotoImage(img)
        self.label.configure(image=self.photo)
        self.win.geometry(f"{img.width}x{img.height}")

    # 그리기 (모든 좌표는 CSS px × k) ────────────────────────
    def render(self, data):
        k = self.k
        sides = [self.side] if self.side in ("home", "away") else ["home", "away"]
        teams = [data[x] for x in sides if isinstance(data.get(x), dict)]
        cards = [self.card(data, t) for t in teams] or [self.card(data, {})]
        W = round((PAD * 2 + CARD_W * len(cards) + GAP * (len(cards) - 1)) * k)
        H = round(PAD * 2 * k + max(c.height for c in cards))
        im = Image.new("RGB", (W, H), (15, 15, 15))
        x = PAD * k
        for c in cards:
            im.paste(c, (round(x), round(H - PAD * k - c.height)))   # align-items: flex-end
            x += (CARD_W + GAP) * k
        return im.resize((round(W / 2), round(H / 2)), Image.LANCZOS)

    def spaced(self, d, xy, text, f, fill, spacing, anchor_right=False):
        """letter-spacing 흉내"""
        widths = [f.getlength(ch) + spacing for ch in text]
        x, y = xy
        if anchor_right:
            x -= sum(widths) - spacing
        for ch, w in zip(text, widths):
            d.text((x, y), ch, font=f, fill=fill, anchor="lm")
            x += w

    def card(self, data, team):
        k = self.k
        kit = team.get("kit") or {}
        players = team.get("players") if isinstance(team.get("players"), list) else []
        slot_list, total = slots(team.get("formation"), len(players))
        warn = bool(players) and len(players) != total
        pitch_w = CARD_W - 24
        pitch_h = pitch_w * 4 / 3
        head_top = 8 + 11 + 8
        pitch_top = head_top + 26 + 10 + 3 + 12
        h_css = pitch_top + pitch_h + 12 + (34 if warn else 0)
        W, H = round(CARD_W * k), round(h_css * k)

        im = Image.new("RGB", (W, H), (15, 15, 15))       # 모서리 밖은 창 배경색
        grad = Image.linear_gradient("L").resize((W, H))  # 위→아래 #111726 → #0b101c
        bg = Image.composite(Image.new("RGB", (W, H), (11, 16, 28)), Image.new("RGB", (W, H), (17, 23, 38)), grad)
        mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, W - 1, H - 1), 14 * k, fill=255)
        im.paste(bg, (0, 0), mask)
        d = ImageDraw.Draw(im, "RGBA")
        d.rounded_rectangle((0, 0, W - 1, H - 1), 14 * k, outline=(255, 255, 255, 18), width=max(1, round(k)))

        # 리그 · 라운드
        fl = font("num", 11 * k)
        self.spaced(d, (14 * k, (8 + 5.5) * k), str(data.get("league", "")), fl, CORAL, 11 * 0.16 * k)
        self.spaced(d, (W - 14 * k, (8 + 5.5) * k), str(data.get("round", "")), fl, MUTED, 11 * 0.1 * k, True)

        # 엠블럼 칩 · 팀 이름 · 포메이션
        cy = (head_top + 13) * k
        short = str(team.get("short", ""))
        fc = font("num", 15 * k)
        cw = max(42 * k, fc.getlength(short) + 14 * k)
        x1, y1, x2, y2 = 14 * k, head_top * k, 14 * k + cw, (head_top + 26) * k
        base, stripe = hexrgb(kit.get("base")), kit.get("stripe")
        chip = Image.new("RGBA", (round(cw), round(26 * k)), base)
        if stripe:
            ImageDraw.Draw(chip).rectangle((cw / 2, 0, cw, 26 * k), fill=hexrgb(stripe))
        cm = Image.new("L", chip.size, 0)
        ImageDraw.Draw(cm).rounded_rectangle((0, 0, chip.width - 1, chip.height - 1), 6 * k, fill=255)
        im.paste(chip, (round(x1), round(y1)), cm)
        d.text(((x1 + x2) / 2, cy), short, font=fc, fill=hexrgb(kit.get("number"), 255), anchor="mm",
               stroke_width=max(1, round(1.2 * k)), stroke_fill=(0, 0, 0, 170))   # 줄무늬 색과 같아도 보이게

        ff = font("num", 14 * k)
        form = str(team.get("formation", ""))
        fw = ff.getlength(form) + 16 * k
        fx2 = W - 14 * k
        d.rounded_rectangle((fx2 - fw, cy - 11 * k, fx2, cy + 11 * k), 5 * k, fill=(255, 255, 255, 255))
        d.text((fx2 - fw / 2, cy + 0.5 * k), form, font=ff, fill=INK, anchor="mm")

        ft = font("kob", 17 * k)
        name = str(team.get("name", ""))
        room = fx2 - fw - 10 * k - (x2 + 10 * k)
        while name and ft.getlength(name) > room:
            name = name[:-2] + "…" if len(name) > 1 else ""
        d.text((x2 + 10 * k, cy), name, font=ft, fill=TEXT, anchor="lm")

        # 코랄 막대
        by = (head_top + 26 + 10) * k
        bar_w = round((CARD_W - 28) * k)
        bar = Image.new("RGBA", (bar_w, max(1, round(3 * k))))
        bd = ImageDraw.Draw(bar)
        for i in range(bar_w):
            t = i / bar_w
            if t < 0.6:
                c = tuple(round(CORAL[j] + (CORAL2[j] - CORAL[j]) * t / 0.6) for j in range(3)) + (255,)
            else:
                c = CORAL2[:3] + (round(255 * (1 - (t - 0.6) / 0.4)),)
            bd.line((i, 0, i, bar.height), fill=c)
        bmask = Image.new("L", bar.size, 0)
        ImageDraw.Draw(bmask).rounded_rectangle((0, 0, bar.width - 1, bar.height - 1), 2 * k, fill=255)
        bar.putalpha(Image.composite(bar.getchannel("A"), Image.new("L", bar.size, 0), bmask))
        paste(im, bar, (14 * k, by))

        # 경기장
        px, py = 12 * k, pitch_top * k
        pw, ph = round(pitch_w * k), round(pitch_h * k)
        field, fmask = self.pitch(pw, ph)
        im.paste(field, (round(px), round(py)), fmask)
        for i, (sx, sy, gk) in enumerate(slot_list):
            p = players[i]
            num = p[0] if len(p) > 0 else ""
            nm = str(p[1]) if len(p) > 1 else ""
            cx, cy = px + pw * sx / 100, py + ph * sy / 100
            self.player(im, d, cx, cy, num, nm, (team.get("gk_kit") or kit) if gk else kit, pitch_w * 0.25 * k)
        if not players:                                    # 새 경기: 선발은 아직 (스쿼드·예상 라인업 화면에서)
            d.text((px + pw / 2, py + ph / 2), "선발 인식 대기 중", font=font("kob", 14 * k),
                   fill=(154, 163, 181, 255), anchor="mm")

        if warn:
            wy = (pitch_top + pitch_h + 12 - 4) * k
            d.rounded_rectangle((12 * k, wy, W - 12 * k, wy + 28 * k), 6 * k, fill=(255, 75, 68, 38))
            msg = f"선수 {len(players)}명 · 포메이션 {team.get('formation') or '?'} 은(는) {total}명 — lineup.json 확인"
            d.text((21 * k, wy + 14 * k), msg, font=font("ko", 12 * k), fill=(255, 179, 174, 255), anchor="lm")
        return im

    def pitch(self, pw, ph):
        """→ (RGB 이미지, 둥근 모서리 마스크)"""
        k = self.k
        # radial-gradient(#18213a 중앙 → #0e1424 가장자리) 근사: 작은 동심원을 부드럽게 확대
        small = Image.new("RGB", (30, 40), (14, 20, 36))
        sd = ImageDraw.Draw(small)
        for i in range(12, 0, -1):
            t = i / 12
            c = (round(24 + (14 - 24) * t), round(33 + (20 - 33) * t), round(58 + (36 - 58) * t))
            sd.ellipse((15 - 18 * t, 20 - 16 * t, 15 + 18 * t, 20 + 16 * t), fill=c)
        im = small.resize((pw, ph), Image.BICUBIC)
        d = ImageDraw.Draw(im, "RGBA")
        for i in range(0, 8, 2):                              # 가로 잔디 줄무늬
            d.rectangle((0, ph * i / 8, pw, ph * (i + 1) / 8), fill=(255, 255, 255, 7))
        sx, sy = pw / 300, ph / 400
        lw = max(1, round(1.5 * k * 276 / 300))
        line = (255, 255, 255, 36)
        R = lambda x1, y1, x2, y2: (x1 * sx, y1 * sy, x2 * sx, y2 * sy)
        d.rectangle(R(8, 8, 292, 392), outline=line, width=lw)
        d.line(R(8, 200, 292, 200), fill=line, width=lw)
        d.ellipse(R(112, 162, 188, 238), outline=line, width=lw)
        d.rectangle(R(80, 8, 220, 64), outline=line, width=lw)
        d.rectangle(R(116, 8, 184, 28), outline=line, width=lw)
        d.rectangle(R(80, 336, 220, 392), outline=line, width=lw)
        d.rectangle(R(116, 372, 184, 392), outline=line, width=lw)
        d.arc(R(120, 34, 180, 94), 0, 180, fill=line, width=lw)
        d.arc(R(120, 306, 180, 366), 180, 360, fill=line, width=lw)
        d.ellipse(R(148, 198, 152, 202), fill=(255, 255, 255, 51))
        mask = Image.new("L", (pw, ph), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, pw - 1, ph - 1), 8 * k, fill=255)
        return im, mask

    def player(self, im, d, cx, cy, num, name, kit, max_w):
        k = self.k
        js = 36 * k
        f = 11.5
        fn = font("kob", f * k)
        while fn.getlength(name) + 10 * k > max_w and f > 8.5:
            f -= 0.5
            fn = font("kob", f * k)
        while name and fn.getlength(name) + 10 * k > max_w:
            name = name[:-2] + "…" if len(name) > 1 else ""
        nh = f * 1.35 * k + 2 * k
        total_h = js + 2 * k + nh
        top = cy - total_h / 2
        shirt = self.jersey(num, kit, round(js))
        im.paste((0, 0, 0), (round(cx - js / 2), round(top + 3 * k)), shirt.getchannel("A").point(lambda a: a // 2))
        paste(im, shirt, (cx - js / 2, top))
        nw = fn.getlength(name) + 10 * k
        ny = top + js + 2 * k
        d.rounded_rectangle((cx - nw / 2, ny, cx + nw / 2, ny + nh), 4 * k, fill=(5, 8, 16, 184))
        d.text((cx, ny + nh / 2), name, font=fn, fill=TEXT, anchor="mm")

    def jersey(self, num, kit, size):
        u = size / 40
        pts = [(x * u, y * u) for x, y in JERSEY]
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).polygon(pts, fill=255)
        body = Image.new("RGB", (size, size), hexrgb(kit.get("base"))[:3])
        bd = ImageDraw.Draw(body, "RGBA")
        if kit.get("stripe"):
            for x in range(6, 40, 8):
                bd.rectangle((x * u, 0, (x + 4) * u, size), fill=hexrgb(kit["stripe"]))
        bd.polygon(pts, outline=(0, 0, 0, 90), width=max(1, round(u)))
        bd.text((20 * u, 29 * u), str(num), font=font("num", 17 * u), fill=hexrgb(kit.get("number") or "#111111"),
                anchor="ms", stroke_width=max(1, round(0.6 * u)), stroke_fill=(0, 0, 0, 64))
        body.putalpha(mask)
        return body


# ── 설정 창 ───────────────────────────────────────────────────
class SettingsDialog:
    """우리 팀 이름·약칭·자리 → settings.json. 저장하면 Haiku가 그 팀 약칭·유니폼 색을 채움"""
    BG, FG, MUTED, FIELD = "#16181d", "#f1f1f1", "#9aa3b5", "#23262e"

    def __init__(self, root, cfg, scale, first=False):
        self.cfg = cfg
        w = self.win = tk.Toplevel(root)
        w.title("설정")
        w.configure(bg=self.BG, padx=round(22 * scale), pady=round(18 * scale))
        w.resizable(False, False)
        w.attributes("-topmost", True)
        f = ("Malgun Gothic", 10)
        fb = ("Malgun Gothic", 10, "bold")
        lab = dict(bg=self.BG, fg=self.FG, font=fb, anchor="w")
        ent = dict(bg=self.FIELD, fg=self.FG, insertbackground=self.FG, relief="flat", font=("Malgun Gothic", 11),
                   highlightthickness=1, highlightbackground="#333844", highlightcolor="#ff4b44")
        hint = dict(bg=self.BG, fg=self.MUTED, font=("Malgun Gothic", 9), anchor="w", justify="left")
        if first:
            tk.Label(w, text="처음 실행이네요. 내가 플레이하는 팀을 알려주세요.", bg=self.BG, fg="#ff7a59",
                     font=fb, anchor="w").pack(fill="x", pady=(0, 10))

        tk.Label(w, text="내 팀 이름", **lab).pack(fill="x")
        self.name = tk.Entry(w, width=30, **ent)
        self.name.insert(0, cfg.MY_TEAM_NAME)
        self.name.pack(fill="x", ipady=4, pady=(2, 2))
        tk.Label(w, text="예: 토트넘, FC 바르셀로나, 내가 만든 팀 이름 (한국어로)", **hint).pack(fill="x", pady=(0, 10))

        tk.Label(w, text="약칭 (선택)", **lab).pack(fill="x")
        self.short = tk.Entry(w, width=8, **ent)
        self.short.insert(0, cfg.MY_TEAM_SHORT)
        self.short.pack(anchor="w", ipady=4, pady=(2, 2))
        tk.Label(w, text="비우면 Claude가 정함 (예: TOT)", **hint).pack(fill="x", pady=(0, 10))

        tk.Label(w, text="내 팀 자리", **lab).pack(fill="x")
        self.side = tk.StringVar(value=cfg.MY_SIDE if cfg.MY_SIDE in ("home", "away") else "home")
        row = tk.Frame(w, bg=self.BG)
        row.pack(fill="x", pady=(2, 12))
        for val, text in (("home", "홈 (라인업 왼쪽)"), ("away", "원정 (오른쪽)")):
            tk.Radiobutton(row, text=text, value=val, variable=self.side, bg=self.BG, fg=self.FG, font=f,
                           selectcolor=self.FIELD, activebackground=self.BG, activeforeground=self.FG).pack(side="left", padx=(0, 14))

        tk.Label(w, text="저장하면 Claude Haiku에게 이 팀을 알려줘서 약칭·유니폼 색을 채우고,\n"
                         "스쿼드 화면 인식과 시청자 수(팬 수) 계산에 이 팀을 씁니다.", **hint).pack(fill="x", pady=(0, 12))
        self.msg = tk.Label(w, text="", bg=self.BG, fg="#ffb3ae", font=("Malgun Gothic", 9), anchor="w")
        self.msg.pack(fill="x")
        btns = tk.Frame(w, bg=self.BG)
        btns.pack(fill="x", pady=(6, 0))
        btn = dict(relief="flat", font=fb, padx=16, pady=4, cursor="hand2", bd=0)
        tk.Button(btns, text="저장", command=self.save, bg="#ff4b44", fg="white", activebackground="#ff7a59",
                  activeforeground="white", **btn).pack(side="right")
        tk.Button(btns, text="취소", command=w.destroy, bg=self.FIELD, fg=self.FG, activebackground="#333844",
                  activeforeground=self.FG, **btn).pack(side="right", padx=(0, 8))
        w.bind("<Return>", lambda e: self.save())
        w.bind("<Escape>", lambda e: w.destroy())
        self.name.focus_set()
        w.update_idletasks()
        w.geometry(f"+{(w.winfo_screenwidth() - w.winfo_width()) // 2}+{(w.winfo_screenheight() - w.winfo_height()) // 3}")

    def save(self):
        name = self.name.get().strip()
        if not name:
            self.msg.configure(text="팀 이름을 입력하세요.")
            return
        short = re.sub(r"[^A-Za-z0-9]", "", self.short.get())[:4].upper()
        changed = (name, short) != (self.cfg.MY_TEAM_NAME, self.cfg.MY_TEAM_SHORT)
        self.cfg.save_settings(MY_TEAM_NAME=name, MY_TEAM_SHORT=short, MY_SIDE=self.side.get())
        print(f"[설정] 저장: 내 팀 {name}" + (f" ({short})" if short else "") + f" · {'홈' if self.side.get() == 'home' else '원정'}")
        if changed:
            def ask():                                     # Haiku에게 그 팀 약칭·유니폼 색 → lineup.json
                try:
                    import lineup_scan
                    lineup_scan.apply_my_team()
                except Exception as e:
                    print(f"[설정] 팀 정보 가져오기 실패 (이름은 저장됨): {e}")
            threading.Thread(target=ask, daemon=True).start()
        self.win.destroy()


# ── 앱 ───────────────────────────────────────────────────────
class OverlayApp:
    """main.py 에서 사용: ui = OverlayApp(config); ui.post(payload) (아무 스레드) ; ui.run() (메인 스레드)"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.q = queue.Queue()
        self.on_close = None                               # 창을 닫으면 main.py 가 불러서 백엔드 정리
        self._quit = False
        self.root = tk.Tk()
        scale = self.root.winfo_fpixels("1i") / 96 * getattr(cfg, "WINDOW_SCALE", 1.0)
        self.chat = ChatView(cfg, self.root, scale)
        self.lineup = None
        if getattr(cfg, "WINDOW_LINEUP", True):
            self.lineup = LineupView(cfg, tk.Toplevel(self.root), scale)
            self.lineup.win.protocol("WM_DELETE_WINDOW", self.root.destroy)
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        topmost = getattr(cfg, "WINDOW_TOPMOST", False)
        for w in [self.root] + ([self.lineup.win] if self.lineup else []):
            w.attributes("-topmost", topmost)
        self._place(scale)
        self.scale = scale
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="채팅 바로 시작 (라인업 대기 건너뛰기)", command=lambda: bus.emit("force_start"))
        menu.add_command(label="라인업 다시 읽기 (다음에 보이는 화면부터)", command=lambda: bus.emit("rescan"))
        menu.add_command(label="설정 (내 팀 직접 입력)…", command=self.open_settings)
        menu.add_separator()
        menu.add_command(label="종료", command=self.root.destroy)
        for w in [self.root] + ([self.lineup.win] if self.lineup else []):
            w.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))
        self._settings = None
        self._hwnds = []

    def hwnds(self):
        """우리 창들의 Windows 핸들 목록 (캡처 제외용). 창이 뜨면 창 스레드에서 채워짐 — 같은 list 를 계속 씀"""
        return self._hwnds

    def open_settings(self, first=False):
        if self._settings and self._settings.win.winfo_exists():
            self._settings.win.lift()
            return
        self._settings = SettingsDialog(self.root, self.cfg, self.scale, first)

    def _place(self, scale):
        """라인업은 왼쪽 위, 채팅은 오른쪽 위"""
        sw = self.root.winfo_screenwidth()
        m = round(40 * scale)
        cw = self.chat.size[0]
        self.root.geometry(f"+{max(0, sw - cw - m)}+{m}")
        if self.lineup:
            self.lineup.win.geometry(f"+{m}+{m}")

    def post(self, payload):                              # 아무 스레드에서나 불러도 됨
        self.q.put(payload)

    def quit(self):                                       # 아무 스레드에서나 불러도 됨
        self._quit = True

    def _pump(self):
        if self._quit:
            self.root.destroy()
            return
        try:
            while True:
                m = self.q.get_nowait()
                if m.get("type") in ("chat", "member"):
                    self.chat.add(m)
                elif m.get("type") == "waiting":
                    self.chat.set_waiting(m.get("sides"))
                elif m.get("type") == "cover":
                    self.chat.set_cover(m.get("on"))
                elif m.get("type") == "request":
                    self.chat.set_request(m.get("text"))
                elif m.get("type") == "viewers":
                    self.chat.set_viewers(m.get("count", 0))
                elif m.get("type") == "commentary" and getattr(self.cfg, "WINDOW_CAPTION", False):
                    self.chat.set_caption(m.get("text", ""))
        except queue.Empty:
            pass
        except Exception as e:
            print(f"[창] {e}")
        self.chat.tick()
        if self.lineup:
            self.lineup.tick()
        self.root.after(50, self._pump)

    def run(self):
        self.root.update()
        u32 = ctypes.windll.user32
        self._hwnds[:] = [u32.GetAncestor(w.winfo_id(), 2) or w.winfo_id()
                          for w in [self.root] + ([self.lineup.win] if self.lineup else [])]
        self.root.after(0, self._pump)
        self.root.mainloop()


def _demo(ui):
    """python overlay_app.py → 가짜 채팅 (overlay.html 의 미리보기와 같은 내용)"""
    import threading
    people = [("축구보는고양이", "member"), ("수비라인뭐함", ""), ("ㅇㅇ", ""), ("킹갓골잡이", ""),
              ("@user-k3x9q2ab", ""), ("치킨시킴", ""), ("전술노트", "member"), ("채팅지기", "mod")]
    lines = ["ㅋㅋㅋㅋㅋㅋ", "와 이걸 막네 ㄷㄷ", "수비 라인 너무 올라온 거 아니냐 저러다 뒷공간 한 방에 털린다 진짜로", "GOOOOOOAL",
             "방금 스루패스 미쳤다", "오늘 폼 좋은데", "ㄹㅇ 해설 텐션 뭐임", "치킨 왔다 ㅎㅎ", "골대 맞은 거 실화냐"]

    def loop():
        ui.post({"type": "commentary", "text": "야말 드리블! 슈팅! 골입니다!"})
        ui.post({"type": "viewers", "count": 60_812})
        ui.post({"type": "member", "name": "새벽축구러", "text": "야말 골 보고 바로 가입함 ㅋㅋ"})
        while not ui._quit:
            name, role = random.choice(people)
            tip = random.choice([1000, 5000, 10000, 20000, 50000]) if random.random() < 0.07 else 0
            ui.post({"type": "chat", "name": name, "role": role, "donation": tip,
                     "text": "오늘 경기 최고다!!" if tip else random.choice(lines)})
            time.sleep(0.3 + random.random() * 1.2)
    threading.Thread(target=loop, daemon=True).start()


if __name__ == "__main__":
    import config
    app = OverlayApp(config)
    _demo(app)
    app.run()
