"""
채팅 — Claude Haiku로 유튜브 라이브 채팅 만들기 + 스스로 평가해서 프롬프트 개선

  흐름 (sundeberg/fake-twitch-chat, MIT 의 방식을 한국어 축구 중계에 맞게):
    - 중계 멘트가 들리면 → 그 장면에 반응하는 채팅 묶음 (장면 크기만큼), 대기 중인 흐름 채팅은 버림
    - 멘트가 없어도 → 대기열이 줄면 흐름 채팅 묶음을 미리 채움 (채팅창이 멈추지 않게)
    - 골 멘트 → Haiku 응답을 기다리지 않고 바로 "ㅅㅅㅅㅅㅅ", "고오오올" 떼창을 먼저 올림 (진짜 사람처럼 반응이 빠르게)
    - 닉네임은 viewers.py 가 붙임 (AI는 보통 메시지만). 속도는 시청자 수 √ 에 비례 + 장면에 따라
  자가 개선: COACH_EVERY_SEC 마다 최근 채팅을 Haiku가 평가 → prompt_template.md [LEARNED] 규칙 추가·삭제

  받는 이벤트: commentary event lineup score clock viewers goal_cue live
"""
import asyncio
import json
import math
import random
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from audience import scenes
from blocks import HERE, ai, bus, store, tpl, prune_rules
from goals import season_goals_text
from lineup_scan import obj

COUNT = {"goal": 20, "big": 10, "event": 12, "normal": 5}      # 장면별 한 번에 만들 채팅 수
SPEED = {"goal": 3.0, "big": 1.6, "event": 2.0, "half": 0.6}   # 장면별 채팅 속도 배율 (시간이 지나면 1로)
SPAM = ["ㅅㅅㅅㅅㅅㅅㅅㅅㅅㅅㅅㅅ", "ㅅㅅㅅㅅㅅㅅ", "고오오오오오올", "골!!!!!!!!!", "GOAL", "ㄱㅇㅇㅇㅇㅇㅇㄹ", "와아아아아아",
        "ㅋㅋㅋㅋㅋㅋㅋㅋㅋㅋㅋ", "미쳤다", "미쳤다 미쳤다", "이게 들어가네", "ㄷㄷㄷㄷㄷㄷ", "ㅁㅊ", "ㅁㅊㅁㅊㅁㅊ", "아아아아아아",
        "골골골골골", "ㅠㅠㅠㅠㅠㅠ", "아 ㅋㅋㅋ", "?????", "와", "헐", "ㄹㅇ 미쳤네", "들어갔다", "나이스!!!!", "캬"]
COACH_SCHEMA = obj(score={"type": "integer"},
                   problems={"type": "array", "items": obj(example={"type": "string"}, issue={"type": "string"})},
                   add_rules={"type": "array", "items": {"type": "string"}},
                   remove_rules={"type": "array", "items": {"type": "integer"}}, summary={"type": "string"})
ROLE = {"GK": "골키퍼", "DF": "수비", "MF": "미드필더", "FW": "공격"}


def kst_now():
    t = datetime.now(timezone(timedelta(hours=9))) + timedelta(minutes=5)   # 10분 단위 반올림
    t = t.replace(minute=t.minute // 10 * 10)
    part = "새벽" if t.hour < 6 else "아침" if t.hour < 11 else "낮" if t.hour < 17 else "저녁" if t.hour < 21 else "밤"
    return f"{'월화수목금토일'[t.weekday()]}요일 {part} {t.hour}시 {t.minute:02d}분"


def lineup_text(data) -> str:
    """lineup.json → 채팅 AI 에게 줄 경기 정보 (대회·날짜·양 팀 선발 11명 등번호·포지션·포메이션)"""
    home, away = data.get("home") or {}, data.get("away") or {}
    comp = f"{data.get('competition') or data.get('league', '')} {data.get('kickoff') or data.get('round', '')}".strip()
    out = [f"{comp} · 홈 {home.get('name', '?')} vs 원정 {away.get('name', '?')}".strip(" ·")]
    for side, team in (("홈", home), ("원정", away)):
        players = team.get("players") or []
        if not players:
            out.append(f"- {team.get('name', '')} ({side}): 선발 아직 모름")
            continue
        rows = [1] + [int(x) for x in re.findall(r"\d", team.get("formation") or "")]
        roles = [("GK" if r == 0 else "DF" if r == 1 else "FW" if r == len(rows) - 1 else "MF")
                 for r, n in enumerate(rows) for _ in range(n)] + ["?"] * 11
        groups = {}
        for role, p in zip(roles, players):
            groups.setdefault(role, []).append(f"{p[0]}번 " + " / ".join(str(x) for x in p[1:3] if x))
        out.append(f"- {team.get('name', '')} ({side}, 선발 11명, 포메이션 {team.get('formation') or '?'}, 줄은 왼쪽→오른쪽): "
                   + " | ".join(f"{ROLE.get(r, r)}: " + ", ".join(v) for r, v in groups.items()))
    return "\n".join(out)


class ChatBrain:
    def __init__(self, cfg, publish, viewers, audience=None):
        self.cfg, self.publish, self.viewers, self.audience = cfg, publish, viewers, audience
        self.roles = viewers.roles()
        self.system, self.system_ver = None, None
        self.history = deque(maxlen=8)            # (시각, 중계 멘트)
        self.recent = deque(maxlen=20)            # (시각, 닉네임, 메시지) — 최근 2분만 AI 에게
        self.fresh = []                           # 아직 반응 안 한 멘트
        self.outbox = deque()                     # (생성 시각, 종류 r/a/s, 메시지)
        self.wake = asyncio.Event()
        self.log = deque(maxlen=300)              # 코치 평가용 {"t","cue","minute","chats"}
        self.ready = True                         # False = 송출 대기 (main.py 가 관리)
        self.lineup, self.lineup_data = "", {}
        self.minute, self.score, self.goals, self.count = None, None, [], 0
        self.speed, self.speed_at = 1.0, 0.0
        self.last_heard = self.last_call = 0.0
        self.last_donation = self.last_join = 0.0
        self.coach_seen = 0
        bus.on("commentary", self.add_commentary)
        bus.on("event", lambda t: self._cue(f"[화면 확인] {t}", "event"))
        bus.on("lineup", self.set_lineup)
        bus.on("score", lambda s, g: (setattr(self, "score", s), setattr(self, "goals", g)))
        bus.on("clock", lambda m: setattr(self, "minute", m))
        bus.on("viewers", lambda n: setattr(self, "count", n))
        bus.on("goal_cue", self.goal_burst)
        bus.on("live", lambda live: live or setattr(self, "minute", None))

    # ── 입력 ────────────────────────────────────────────────────
    def set_lineup(self, data):
        self.lineup_data, self.lineup = data, lineup_text(data)
        self.viewers.build_pool(data)
        self.viewers.set_match(self.lineup.split("\n", 1)[0])

    def add_commentary(self, t):
        now = time.monotonic()
        self.history.append((now, t))
        self.last_heard = now
        sc = scenes(t)
        self._cue(t, "goal" if "goal" in sc else "big" if "big" in sc else "half" if "half" in sc else "normal")

    def _cue(self, t, kind):
        self.fresh.append((t, kind))
        if kind in SPEED:
            self.speed, self.speed_at = max(self.speed_now(), SPEED[kind]), time.monotonic()
        self.wake.set()

    def goal_burst(self):
        """골 멘트 즉시: AI 응답을 기다리지 않고 떼창부터 (비용 0)"""
        if not self.ready:
            return
        now = time.monotonic()
        for _ in range(random.randint(10, 18)):
            self._queue(now, "s", self.viewers.pick_name(), random.choice(SPAM), first=True)
        self.speed, self.speed_at = SPEED["goal"], now

    # ── 속도: 시청자 수 √ 비례 × 장면 ─────────────────────────────
    def speed_now(self):
        return 1 + (self.speed - 1) * 0.5 ** ((time.monotonic() - self.speed_at) / 20)   # 20초마다 절반씩 평소로

    def rate(self):
        """분당 메시지 수"""
        base = 12 * math.sqrt(max(self.count, 1000) / 1000)
        return min(self.cfg.CHAT_RATE_MAX, base * self.speed_now())

    # ── 생성 루프 ───────────────────────────────────────────────
    async def run(self):
        while True:
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
            if not self.ready:
                self.fresh.clear()                            # 송출 대기 중: 멘트는 듣기만
                continue
            wait = self.cfg.MIN_CALL_GAP_SEC - (time.monotonic() - self.last_call)
            if self.fresh and wait > 0:
                await asyncio.sleep(wait)                     # 그 사이 들어온 멘트는 한 번에 묶임
            if self.fresh:
                cues, self.fresh = self.fresh, []
                kinds = [k for _, k in cues]
                kind = next((k for k in ("goal", "event", "big") if k in kinds), "normal")
                self.outbox = deque(m for m in self.outbox if m[1] != "a")   # 지난 장면용 흐름 채팅은 버림
                now_line = " ".join(t for t, _ in cues)
                context, chat = self._context(exclude_last=sum(1 for _, k in cues if k != "event"))
                await self._generate(tpl.fill("REACTION", context=context, now=now_line, chat=chat)
                                     + f"\n(채팅 {COUNT[kind]}개 정도)", now_line)
            elif self._need_flow():
                context, chat = self._context()
                since = time.monotonic() - self.last_heard
                n = self.cfg.AMBIENT_BATCH
                await self._generate(tpl.fill("AMBIENT", context=context, chat=chat, count=n,
                                              since=f"마지막 중계 멘트 {int(since)}초 전" if self.last_heard else "중계 멘트 아직 없음"),
                                     "(흐름 채팅)", kind="a")

    def _need_flow(self):
        """대기열이 몇 초 치만 남으면 흐름 채팅을 미리 채움 (자리 비움이 길면 멈춤 = 비용 0)"""
        left = sum(1 for m in self.outbox if m[1] != "s") / max(self.rate() / 60, 0.1)
        idle = self.last_heard and time.monotonic() - self.last_heard > self.cfg.IDLE_STOP_AFTER_MIN * 60
        return left < self.cfg.PREFETCH_SEC and not idle and time.monotonic() - self.last_call > self.cfg.MIN_CALL_GAP_SEC

    def _context(self, exclude_last=0):
        now = time.monotonic()
        items = list(self.history)[:-exclude_last] if exclude_last else list(self.history)
        earlier = "\n".join(f"- {int(now - t)}초 전: {x}" for t, x in items if now - t < 120) or "- (없음)"
        chat = "\n".join(f"{n}: {m}" for t, n, m in self.recent if now - t < 120)
        match = tpl.fill("MATCH", lineup=self.lineup) + "\n\n" if self.lineup else ""
        record = season_goals_text(self.lineup_data)
        match += tpl.fill("RECORD", record=record) + "\n\n" if record else ""
        clock = ""
        if self.minute is not None:
            half = "전반" if self.minute < 45 else "후반" if self.minute < 90 else "추가시간·연장"
            clock = tpl.fill("CLOCK", minute=self.minute, half=half) + "\n\n"
        if self.score:
            goals = ", ".join(f"{g['minute'] if g['minute'] is not None else '?'}분 {g['name'] or '(득점자 모름)'}({g['team']}"
                              + (f", 누적 {g['season']}호골" if g.get("season", 0) > 1 else "") + ")"
                              + (" 추정" if g["guess"] else "") for g in self.goals) or "없음"
            clock += tpl.fill("SCORE", home=self.score["home"], away=self.score["away"], goals=goals) + "\n\n"
        aud = self.audience.prompt_text() + "\n\n" if self.audience and self.audience.prompt_text() else ""
        context = tpl.fill("CONTEXT", time=kst_now(), match=match, clock=clock, audience=aud, earlier=earlier)
        return context, "[최근 채팅]\n" + (chat or "(아직 없음)")

    def _build_system(self):
        c = self.cfg
        fans = tpl.fill({"mixed": "FANS_MIXED", "neutral": "FANS_NEUTRAL"}.get(c.FANS, "FANS_AUTO"))
        return tpl.fill("SYSTEM", channel=c.CHANNEL_NAME, program=c.PROGRAM_NAME or "축구 생중계", fans=fans,
                        extra=tpl.fill("EXTRA", extra=c.EXTRA_CONTEXT) if c.EXTRA_CONTEXT else "",
                        viewers=self.viewers.prompt_text(), learned=tpl.learned_text())

    async def _generate(self, prompt, cue, kind="r"):
        if tpl.version() != self.system_ver:                  # prompt_template.md 가 바뀜 (직접 수정·자가 개선)
            self.system, self.system_ver = self._build_system(), tpl.version()
        self.last_call = time.monotonic()
        batch = []
        self.log.append({"t": f"{datetime.now():%H:%M:%S}", "cue": cue, "minute": self.minute, "chats": batch})
        t0, first = time.monotonic(), None
        try:
            async for line in ai.stream_lines(self.system, prompt, self.cfg.MAX_TOKENS):
                msg = self._take(line, kind)
                if msg:
                    batch.append(msg)
                    first = first or time.monotonic() - t0
            print(f"   [Haiku] 채팅 {len(batch)}개 · 첫 메시지 {first or 0:.1f}s · 속도 {self.rate():.0f}/분 · 누적 ${ai.total:.3f}")
        except Exception as e:
            print(f"   [Haiku] 채팅 생성 오류 ({type(e).__name__}): {e}")

    def _take(self, line, kind):
        """응답 한 줄(JSON) → 대기열. 올린 메시지 문자열 반환"""
        m = re.search(r"\{.*\}", line)
        try:
            o = json.loads(m.group(0)) if m else None
        except json.JSONDecodeError:
            o = None
        if not isinstance(o, dict):
            return None
        if o.get("event") == "goal":                          # 채팅 AI 가 골 장면이라고 알려줌 → 스코어보드 확인
            bus.emit("goal_cue")
            return None
        msg = str(o.get("text", "")).strip()[:150]
        name = str(o.get("name", "")).strip()[:24] or self.viewers.pick_name()
        now = time.monotonic()
        if o.get("join") is True and not self.roles.get(name) and now - self.last_join > self.cfg.MEMBER_JOIN_COOLDOWN_SEC:
            self.last_join, self.roles[name] = now, "member"
            self.viewers.join_member(name)
            self.outbox.append((now, kind, {"type": "member", "name": name, "text": msg}))
            return f"(새 멤버) {name}: {msg}"
        if not msg:
            return None
        try:
            don = int(o.get("donation") or 0)
        except (TypeError, ValueError):
            don = 0
        if don > 0 and now - self.last_donation > self.cfg.DONATION_COOLDOWN_SEC:
            don, self.last_donation = max(1000, min(500_000, round(don, -3))), now
        else:
            don = 0
        self._queue(now, kind, name, msg, don)
        return f"{name}: {msg}" + (f" (슈퍼챗 ₩{don:,})" if don else "")

    def _queue(self, now, kind, name, msg, don=0, first=False):
        item = (now, kind, {"type": "chat", "name": name, "text": msg, "role": self.roles.get(name, ""), "donation": don})
        self.outbox.appendleft(item) if first else self.outbox.append(item)
        while len(self.outbox) > self.cfg.MAX_BACKLOG:
            self.outbox.popleft() if not first else self.outbox.pop()

    # ── 내보내기: 속도대로, 20% 확률로 2~3개 몰아서 ─────────────────
    async def emitter(self):
        while True:
            if not self.outbox or not self.ready:
                await asyncio.sleep(0.2)
                continue
            gap = 60 / self.rate()
            for _ in range(random.randint(2, 3) if random.random() < 0.2 else 1):
                while self.outbox:
                    born, kind, msg = self.outbox.popleft()
                    if time.monotonic() - born < (self.cfg.STALE_AFTER_SEC * (3 if kind == "a" else 1)):
                        break                                 # 너무 늦은 메시지는 버림 (장면과 안 맞는 뒷북)
                else:
                    break
                self.publish(msg)
                self.recent.append((time.monotonic(), msg["name"], msg["text"]))
                self.viewers.record(msg["name"], msg["text"])
                print(f"      {'🟢 새 멤버' if msg['type'] == 'member' else '💬'} {msg['name']}: {msg['text']}"
                      + (f" 💰₩{msg['donation']:,}" if msg.get("donation") else ""))
            backlog = len(self.outbox)
            await asyncio.sleep(gap * random.uniform(0.3, 1.7) / (1 + 0.05 * max(0, backlog - 8)))

    # ── 자가 개선: 채팅 평가 → [LEARNED] 규칙 ──────────────────────
    async def coach(self):
        c = self.cfg
        while c.PROMPT_COACH:
            await asyncio.sleep(c.COACH_EVERY_SEC)
            new = [e for e in list(self.log)[self.coach_seen:] if e["chats"]]   # 지난 평가 뒤에 나온 채팅만
            if not self.ready or sum(len(e["chats"]) for e in new) < c.COACH_MIN_CHATS:
                continue                                      # 경기 중이 아니거나 평가할 채팅이 적음 → 비용 0
            self.coach_seen = len(self.log)
            try:
                await self.review(new[-30:])
            except Exception as e:
                print(f"   [코치] 평가 실패 (다음에 다시): {e}")

    async def review(self, entries):
        rules = tpl.learned_rules()
        log_text = "\n".join(f"- {e['t']} · {str(e['minute']) + '분' if e.get('minute') is not None else '-'} · \"{e['cue']}\"\n    → "
                             + "\n    → ".join(e["chats"]) for e in entries)
        res = await ai.ask_async(tpl.fill("COACH", match=self.lineup or "(모름)",
                                          audience=self.audience.prompt_text() if self.audience else "",
                                          rules="\n".join(f"{i + 1}: {r}" for i, r in enumerate(rules)) or "(없음)", log=log_text),
                                 COACH_SCHEMA, max_tokens=16000, label=f"채팅 평가 ({self.cfg.COACH_MODEL})",
                                 model=self.cfg.COACH_MODEL, effort=self.cfg.COACH_EFFORT)
        drop = {i - 1 for i in res["remove_rules"] if 1 <= i <= len(rules)}
        kept = [r for i, r in enumerate(rules) if i not in drop]
        added = [r.strip()[:80] for r in res["add_rules"][:2] if r.strip()]   # 자주 도니까 조금씩
        new = prune_rules(kept + added, self.cfg.COACH_MAX_RULES)  # 의미 중복 규칙 합치기
        if new != rules:
            tpl.save_learned(new)
        with (HERE / "prompt_review_log.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"time": datetime.now().isoformat(timespec="seconds"), **res,
                                "rules_before": rules, "rules_after": new}, ensure_ascii=False) + "\n")
        print(f"\n   [코치] 채팅 평가 {res['score']}/10 · {res['summary']}")
        for p in res["problems"][:3]:
            print(f"   [코치]   ✗ \"{p['example'][:40]}\" — {p['issue'][:70]}")
        for i in sorted(drop):
            print(f"   [코치]   − 규칙 삭제: {rules[i]}")
        for r in added:
            print(f"   [코치]   + 규칙 추가: {r}")
        return res
