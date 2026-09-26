"""
시청자 수·팬 구성 + 중계 멘트 해석 (골·결정적 장면·하프타임·종료)

  평균 시청자 = (홈 팀 전 세계 팬 + 원정 팀 전 세계 팬) × 1.5 ÷ 10,000   (경기마다 Haiku 1회, audience.json 에 저장)
  팬 구성     = 홈 팬 : 원정 팬 : 중립 33%. 한국 선수가 한 팀에만 있으면 중립은 전부 그 팀 (양 팀 다면 반반)
  상황 변동   = 경기 전 70% → 킥오프 100% · 골 +10~15% · 결정적 장면·한국 선수 +2~4% · 하프타임 85% · 종료 55%

  받는 이벤트: commentary(멘트) live(경기 중) lineup(라인업 바뀜)
  보내는 이벤트: goal_cue(골 멘트) match_end_cue(경기 종료 멘트) viewers(시청자 수)
"""
import asyncio
import random
import re
import time


from blocks import ai, bus, store, tpl
from lineup_scan import obj

NEUTRAL = 1 / 3
PHASE = {"pre": 0.70, "live": 1.0, "half": 0.85, "end": 0.55}
BOOST_HALF_LIFE = 120    # 골 때 몰려온 시청자가 절반 빠지는 시간(초)
SCENES = {  # 중계 멘트 해석 블록 ('골키퍼' 같은 말에 안 걸리게 '골' 단독은 안 씀). 위에서부터 판정
    "half": re.compile(r"하프 ?타임|전반(전)?(이)? ?(종료|끝)"),
    "end": re.compile(r"경기 ?종료|경기가 (끝|종료)|후반(전)?(이)? ?(종료|끝)|종료 휘슬|최종 스코어"),
    "start": re.compile(r"킥오프|경기 시작|전반(전)?(이)? ?시작|후반(전)?(이)? ?(시작|출발)"),
    "goal": re.compile(r"골입니다|골!|고+올|득점|골망|골을 넣|동점골|역전골|결승골|추가골|만회골|해트트릭"),
    "big": re.compile(r"페널티|PK|퇴장|레드 ?카드|VAR|결정적|일대일|1 ?대 ?1|골대|크로스바|선방|슈팅|슛"),
}
_list = {"type": "array", "items": {"type": "string"}}
SCHEMA = obj(home_fans={"type": "integer"}, away_fans={"type": "integer"}, home_korean=_list, away_korean=_list)


def scenes(t: str) -> set:
    return {k for k, rx in SCENES.items() if rx.search(t)}


def _players(team):
    return ", ".join(" / ".join(str(x) for x in p[1:3]) for p in team.get("players") or [] if len(p) > 1)


class Audience:
    def __init__(self, cfg):
        self.cfg = cfg
        self.sig, self.info = None, None
        self.avg = self.count = 0
        self.phase, self.boost = "pre", 0.0
        self.boost_at = self.last_heard = time.monotonic()
        self.lock = asyncio.Lock()
        bus.on("commentary", self.on_commentary)
        bus.on("live", lambda live: live and self.phase == "pre" and setattr(self, "phase", "live"))
        bus.on("lineup", lambda *_: asyncio.ensure_future(self.refresh()))

    # ── 경기가 바뀌면: 팬 수 (같은 선발이면 저장된 값) ───────────────
    async def refresh(self):
        async with self.lock:
            data = store.load("lineup.json", {})
            home, away = data.get("home") or {}, data.get("away") or {}
            if not home.get("players") or not away.get("players"):
                return
            sig = f"{home.get('name', '')} | {_players(home)} || {away.get('name', '')} | {_players(away)}"
            if sig == self.sig:
                return
            info = store.load("audience.json", {}).get(sig)
            if not info:
                try:
                    res = await ai.ask_async(tpl.fill("FAN_COUNT", home=home.get("name", "?"), away=away.get("name", "?"),
                                                          home_players=_players(home), away_players=_players(away)),
                                             SCHEMA, max_tokens=400, label="팬 수 확인")
                except Exception as e:
                    print(f"[시청자] 팬 수 확인 실패: {e}")
                    return
                names = {p[1] for t in (home, away) for p in t.get("players") or [] if len(p) > 1}
                info = {"home": home.get("name", "홈"), "away": away.get("name", "원정"),
                        "home_fans": max(0, res["home_fans"]), "away_fans": max(0, res["away_fans"]),
                        "home_korean": [n for n in res["home_korean"] if n in names],   # 명단에 없는 이름은 버림
                        "away_korean": [n for n in res["away_korean"] if n in names]}
                store.update("audience.json", lambda c: c.__setitem__(sig, info), {})
            new_match = self.info is None or (self.info["home"], self.info["away"]) != (info["home"], info["away"])
            self.sig, self.info = sig, info
            self.avg = max(1, round((info["home_fans"] + info["away_fans"]) * 1.5 / 10_000))
            if new_match:
                self.phase, self.boost = "pre", 0.0
                self.count = round(self.avg * PHASE["pre"])
            ko = [f"{n}({info[s]})" for s in ("home", "away") for n in info[f"{s}_korean"]]
            print(f"[시청자] {info['home']} 팬 {info['home_fans']:,} + {info['away']} 팬 {info['away_fans']:,} "
                  f"→ 평균 시청자 {self.avg:,}명" + (f" · 한국 선수: {', '.join(ko)}" if ko else ""))
            print(f"[시청자] 팬 구성: {self.mix_text()}")
            bus.emit("viewers", self.count)

    # ── 팬 구성 ─────────────────────────────────────────────
    def shares(self):
        i = self.info
        total = i["home_fans"] + i["away_fans"]
        h, a = ((1 - NEUTRAL) * i["home_fans"] / total, (1 - NEUTRAL) * i["away_fans"] / total) if total else (1 / 3, 1 / 3)
        n = NEUTRAL
        kh, ka = bool(i["home_korean"]), bool(i["away_korean"])
        if kh and ka:
            h, a, n = h + n / 2, a + n / 2, 0
        elif kh or ka:
            h, a, n = (h + n, a, 0) if kh else (h, a + n, 0)
        return h, a, n

    def mix_text(self):
        i = self.info
        h, a, n = self.shares()
        pct = lambda x: f"{x * 100:.0f}%" if x >= 0.01 or x == 0 else "1% 미만"
        out = f"{i['home']} 팬 {pct(h)}, {i['away']} 팬 {pct(a)}, 중립 축구팬 {pct(n)}"
        for s in ("home", "away"):
            if i[f"{s}_korean"]:
                out += f" (한국 선수 {', '.join(i[f'{s}_korean'])} 때문에 한국 시청자는 전부 {i[s]} 응원. 그 선수 장면마다 채팅 폭발)"
        return out

    def prompt_text(self):
        return f"[채팅창 팬 구성] {self.mix_text()}\n[동시 시청자] 약 {self.count:,}명" if self.info else ""

    # ── 중계 멘트 → 경기 흐름·시청자 변동 ─────────────────────────
    def on_commentary(self, t):
        self.last_heard = time.monotonic()
        sc = scenes(t)
        if "half" in sc:
            self.phase = "half"
        elif "end" in sc:
            self.phase = "end"
            bus.emit("match_end_cue")
        elif "start" in sc or self.phase == "pre" and sc & {"goal", "big"}:
            self.phase = "live"
        if "goal" in sc:
            self._add_boost(random.uniform(0.10, 0.15))
            bus.emit("goal_cue")
        elif "big" in sc:
            self._add_boost(random.uniform(0.02, 0.04))
        if self.info and any(n in t for s in ("home", "away") for n in self.info[f"{s}_korean"]):
            self._add_boost(random.uniform(0.02, 0.03))       # 한국 선수 장면

    def _decayed(self):
        return self.boost * 0.5 ** ((time.monotonic() - self.boost_at) / BOOST_HALF_LIFE)

    def _add_boost(self, x):
        self.boost, self.boost_at = min(0.6, self._decayed() + x), time.monotonic()

    def target(self):
        silent = time.monotonic() - self.last_heard
        quiet = 1.0 if silent < 60 or self.phase == "end" else max(0.85, 1 - (silent - 60) / 600)
        return self.avg * PHASE[self.phase] * (1 + self._decayed()) * quiet

    async def ticker(self):
        while True:
            await asyncio.sleep(random.uniform(3, 6))
            if self.avg:
                gap = self.target() - self.count
                self.count = max(1, round(self.count + gap * (0.35 if gap > 0 else 0.12) + random.gauss(0, self.avg * 0.01)))
                bus.emit("viewers", self.count)
