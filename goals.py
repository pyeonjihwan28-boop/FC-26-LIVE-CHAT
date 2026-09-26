"""
스코어·득점자·누적 득점 — 경기 중 화면으로 확인해서 채팅 AI에게 알려줌

  - 'hud' 이벤트(화면 아래 공 가진 선수 "15 RODRI")를 계속 기록 → lineup.json 으로 어느 팀인지 판단, 등번호도 바로잡음
  - 'goal_cue' 이벤트(골 멘트)가 오면 몇 초 뒤부터 스코어보드를 Haiku로 읽어서 (1회 약 0.1센트)
    점수가 오른 팀의, 골 멘트 직전에 공을 가졌던 선수를 득점자로 → 'event' 로 채팅이 반응
  - 경기가 끝나면(경기 종료 멘트 또는 85분 이후) 득점자를 career_stats.json 에 쌓음 (득점 기록만)
"""
import asyncio
import time
from collections import deque
from datetime import datetime


from blocks import ai, bus, store, text, tpl
from lineup_scan import obj

CHECK_AFTER = (5, 12, 25, 45)      # 골 멘트 뒤 스코어보드 확인 시점(초) — 점수가 바뀌면 그만
SCORER_WINDOW = 30                 # 골 멘트 전 이만큼(초) 안에 공을 가진 선수 중에서 득점자
ROW = obj(short={"type": "string"}, score={"type": "integer"})
SCORE_SCHEMA = obj(top=ROW, bottom=ROW, clock={"type": "string"}, ok={"type": "boolean"})
STATS = "career_stats.json"


class GoalTracker:
    def __init__(self, cfg, get_scoreboard):
        self.cfg, self.get_scoreboard = cfg, get_scoreboard
        self.hud = deque(maxlen=80)                 # (시각, side, 등번호, 한국어 이름)
        self.score, self.goals = None, []
        self.live, self.minute, self.ended, self.checking = False, None, False, None
        bus.on("hud", self.on_hud)
        bus.on("live", self.on_live)
        bus.on("clock", lambda m: setattr(self, "minute", m))
        bus.on("goal_cue", self.cue)
        bus.on("match_end_cue", lambda: setattr(self, "ended", True))
        bus.on("new_match", self.reset)

    # ── 공 가진 선수 ─────────────────────────────────────────────
    def on_hud(self, number, name):
        n = text.norm(name)
        if len(n) < 3:
            return

        def find(data):
            best = None
            for side in ("home", "away"):
                for p in (data.get(side) or {}).get("players") or []:
                    if n in text.norm(p[2] if len(p) > 2 else "") or n in text.norm(p[1], True):
                        if p[0] == number:
                            return side, p
                        best = best or (side, p)
            if best and 1 <= number <= 99 and all(q[0] != number for q in data[best[0]]["players"]):
                print(f"[등번호] {best[1][1]} {best[1][0]} → {number} (경기 화면 기준으로 고침)")
                best[1][0] = number                  # 스쿼드 화면엔 번호가 없어 추정했던 것 → 화면 번호가 진짜
            return best

        hit = store.update("lineup.json", find, {})
        if hit and (not self.hud or self.hud[-1][1:3] != (hit[0], hit[1][0])):
            self.hud.append((time.monotonic(), hit[0], hit[1][0], hit[1][1]))

    # ── 경기 흐름 ───────────────────────────────────────────────
    def on_live(self, live):
        self.live = live
        if live and self.score is None:
            asyncio.ensure_future(self._baseline())
        if not live:
            self.finish()

    def cue(self):
        if self.live and (self.checking is None or self.checking.done()):
            self.checking = asyncio.ensure_future(self._check(time.monotonic()))

    def reset(self, *_):
        self.hud.clear()
        self.score, self.goals, self.minute, self.ended = None, [], None, False
        self._publish()

    async def run(self):
        """멘트를 놓친 골도 잡도록 경기 중 가끔 스코어보드 확인"""
        every = self.cfg.SCORE_CHECK_SEC
        while every:
            await asyncio.sleep(every)
            if self.live and (self.checking is None or self.checking.done()):
                res = await self._read()
                if res:
                    self._apply(res, time.monotonic() - every / 2, guessed=True)

    async def _baseline(self):
        await asyncio.sleep(3)
        res = await self._read()
        if res and self.score is None:
            self.score = res
            self._publish()
            print(f"[스코어] 경기 화면 스코어 {res['home']} : {res['away']}")

    async def _check(self, cue_t):
        for delay in CHECK_AFTER:
            await asyncio.sleep(max(0, cue_t + delay - time.monotonic()))
            res = await self._read()
            if res and self._apply(res, cue_t):
                return
        print("[스코어] 골 멘트였지만 스코어는 그대로 (노골·오프사이드·VAR 등)")

    # ── 스코어 적용 · 경기 끝 기록 ──────────────────────────────
    def _apply(self, res, cue_t, guessed=False):
        """스코어가 올랐으면 득점 기록. 바뀌었으면 True"""
        if self.score is None:
            self.score = res
            self._publish()
            return False
        if res == self.score:
            return False
        old, self.score = self.score, res
        lineup = store.load("lineup.json", {})
        past = store.load(STATS, {"goals": []})["goals"]
        for side in ("home", "away"):
            for _ in range(max(0, res[side] - old[side])):
                pick = [h for h in self.hud if h[1] == side and cue_t - SCORER_WINDOW <= h[0] <= cue_t + 3]
                num, name = (pick[-1][2], pick[-1][3]) if pick else (None, None)
                team = (lineup.get(side) or {}).get("name", side)
                season = (sum(1 for g in past + self.goals if g["team"] == team and g["name"] == name) + 1) if name else 0
                self.goals.append({"minute": self.minute, "side": side, "team": team, "name": name, "number": num,
                                   "score": f"{res['home']}:{res['away']}", "season": season, "guess": guessed or not pick})
                print(f"[득점] {self.minute if self.minute is not None else '?'}분 {team} "
                      f"{f'{name}({num}번)' if name else '득점자 확인 못 함'} → {res['home']}:{res['away']}")
                bus.emit("event", f"{team} {name + ' ' if name else ''}득점 확인!"
                                  f"{f' (누적 {season}호골)' if season > 1 else ''} 스코어 {res['home']}:{res['away']}")
        if any(res[s] < old[s] for s in res):
            print(f"[스코어] 점수가 줄었습니다 (VAR 취소 등) → {res['home']}:{res['away']}")
        self._publish()
        return True

    def finish(self):
        """경기 끝 → 득점자를 누적 기록에 (85분 넘게 봤거나 '경기 종료' 멘트가 나온 경우만, 같은 경기는 한 번)"""
        if self.score is None or not (self.ended or (self.minute or 0) >= 85):
            return
        lineup = store.load("lineup.json", {})
        key = f"{lineup.get('kickoff', '')}|{(lineup.get('home') or {}).get('name')}|{(lineup.get('away') or {}).get('name')}"

        def add(data):
            if not any(g.get("match") == key for g in data["goals"]):
                data["goals"] += [{"date": f"{datetime.now():%Y-%m-%d}", "competition": lineup.get("competition", ""),
                                   "minute": g["minute"], "team": g["team"], "name": g["name"], "number": g["number"],
                                   "match": key} for g in self.goals if g["name"]]
            return len(data["goals"])

        total = store.update(STATS, add, {"goals": []})
        print(f"[기록] 경기 끝 {self.score['home']}:{self.score['away']} · 득점 "
              f"{', '.join(g['name'] or '?' for g in self.goals) or '없음'} → {STATS} (누적 골 {total}개)")
        self.ended = False

    def _publish(self):
        bus.emit("score", self.score, self.goals)

    async def _read(self):
        img = self.get_scoreboard()
        if img is None:
            return None
        if getattr(self.cfg, "SCOREBOARD_LOCAL_OCR", False):
            away = text.norm((store.load("lineup.json", {}).get("away") or {}).get("short", ""))
            try:
                from scoreboard_ocr import read_scoreboard
                loop = asyncio.get_running_loop()
                res = await loop.run_in_executor(None, read_scoreboard, img, away)
            except Exception:
                res = None
            if res is not None:
                return res                          # 로컬 OCR로 읽음 → Haiku 호출 안 함 (비용 0)
        try:
            r = await ai.ask_async(tpl.fill("SCAN_SCOREBOARD"), SCORE_SCHEMA, image=img, max_tokens=150, fmt="PNG")
        except Exception as e:
            print(f"[스코어] 읽기 실패: {e}")
            return None
        if not r["ok"]:
            return None
        away = text.norm((store.load("lineup.json", {}).get("away") or {}).get("short", ""))
        top, bottom = r["top"], r["bottom"]
        if away and text.norm(top["short"])[:3] == away[:3]:   # 보통 위 = 홈, 약칭으로 확인
            top, bottom = bottom, top
        return {"home": max(0, top["score"]), "away": max(0, bottom["score"])}


def season_goals_text(lineup) -> str:
    """채팅 AI 에게: 오늘 두 팀 선수들의 누적 득점"""
    teams = {(lineup.get(s) or {}).get("name", "") for s in ("home", "away")}
    tally = {}
    for g in store.load(STATS, {"goals": []})["goals"]:
        if g["team"] in teams:
            tally[(g["team"], g["name"])] = tally.get((g["team"], g["name"]), 0) + 1
    top = sorted(tally.items(), key=lambda x: -x[1])[:10]
    return ("누적 득점: " + ", ".join(f"{n}({t}) {c}골" for (t, n), c in top)) if top else ""
