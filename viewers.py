"""
시청자 — 닉네임 뽑기 + 자주 오는 시청자 기록 (viewers.json, 내부 기록이라 채팅창엔 '단골' 표시 없음)

  닉네임: 채팅 AI는 보통 메시지만 쓰고, 닉네임은 여기서 붙임 (sundeberg/fake-twitch-chat 방식).
          이번 라이브에 들어온 시청자 풀에서 몇 명이 자주 나오게 (실제 채팅처럼 소수가 많이 침)
  기록:   방송 중 채팅 친 닉네임·말투를 모아 두고, 다음 라이브 시작 때 Haiku가 자주 온 시청자·멤버십·기억을 갱신 (1회 약 0.3센트)
"""
import random
import string
from datetime import datetime


from blocks import ai, store, tpl
from lineup_scan import obj

BOOK = "viewers.json"
MAX_SAMPLES, MIN_CHATS_FOR_PROMOTE, MAX_HISTORY = 6, 3, 5
_S = {"type": "string"}
SCHEMA = obj(new_regulars={"type": "array", "items": obj(name=_S, trait=_S)},
             memories={"type": "array", "items": obj(name=_S, memory=_S)},
             members={"type": "array", "items": _S}, summary=_S)

# 닉네임 재료 (한국 유튜브 스포츠 채팅에서 흔한 모양)
_A = ["새벽", "치킨", "야식", "출근전", "퇴근후", "직관", "해축", "축구", "볼보는", "잠못드는", "주말", "월급날", "졸린", "배고픈",
      "전술", "골", "역습", "빌드업", "침대", "소파", "라면", "맥주", "커피", "편의점", "군대", "고3", "대학원생", "아재"]
_B = ["고양이", "강아지", "러", "맨", "왕", "장인", "요정", "러버", "중독", "덕후", "마스터", "봇", "형", "누나", "짱", "킹",
      "팬", "보이", "걸", "러닝", "쟁이", "대장", "초보", "고수"]
_PLAYER_TAIL = ["팬", "사랑", "원툴", "믿는다", "가즈아", "짱", "최고", "월클"]


class Viewers:
    def __init__(self, cfg):
        self.cfg = cfg
        self.data = store.load(BOOK) or {"shows": 0, "history": [], "session": None,
                                        "regulars": [dict(v, since=0, visits=0, memory="") for v in cfg.VIEWERS]}
        self.new_members = []
        self.pool, self.weights = [], []                 # 이번 라이브 닉네임 풀
        self.dirty = False

    # ── 닉네임 ─────────────────────────────────────────────────
    def build_pool(self, lineup: dict):
        """라이브 시작·경기 바뀔 때: 닉네임 80개 (선수·팀 이름 넣은 것 포함). 앞쪽일수록 자주 나옴"""
        players = [p[1] for s in ("home", "away") for p in (lineup.get(s) or {}).get("players") or [] if len(p) > 1]
        teams = [(lineup.get(s) or {}).get("short", "") for s in ("home", "away")]
        names = set()
        while len(names) < 80:
            r = random.random()
            if r < 0.45:
                n = random.choice(_A) + random.choice(_B) + (str(random.randint(1, 999)) if random.random() < 0.4 else "")
            elif r < 0.65 and players:
                n = random.choice(players).replace(" ", "") + random.choice(_PLAYER_TAIL)
            elif r < 0.75 and any(teams):
                n = random.choice([t for t in teams if t]).lower() + random.choice(["_fan", "love", str(random.randint(1, 99))])
            elif r < 0.9:
                n = "@user-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
            else:
                n = random.choice(["ㅇㅇ", "지나가던사람", "익명", "축린이", "해버지", "무명"]) + str(random.randint(1, 99))
            names.add(n)
        self.pool = list(names)
        random.shuffle(self.pool)
        self.weights = [1 / (i + 1) ** 0.8 for i in range(len(self.pool))]   # 소수가 많이 치게 (지프 분포)

    def pick_name(self) -> str:
        if not self.pool:
            self.build_pool(store.load("lineup.json", {}))
        regs = self.data["regulars"]
        if regs and random.random() < 0.12:                  # 자주 오는 시청자도 가끔
            return random.choice(regs)["name"]
        return random.choices(self.pool, self.weights)[0]

    def roles(self) -> dict:
        return {v["name"]: v.get("role", "") for v in self.data["regulars"]}

    # ── 라이브 시작: 지난 기록 → 자주 오는 시청자 갱신 ───────────────
    async def start_show(self):
        s = self.data.get("session")
        if s and s.get("chatters"):
            try:
                await self._promote(s)
            except Exception as e:
                print(f"[채널] 시청자 기록 갱신 실패 (다음 라이브 때 다시): {e}")
                return
        self.data["shows"] = self.data.get("shows", 0) + 1
        self.data["session"] = {"show": self.data["shows"], "date": f"{datetime.now():%Y-%m-%d %H:%M}", "match": "", "chatters": {}}
        self.dirty = True
        self.save()
        print(f"[채널] 라이브 {self.data['shows']}회차")

    async def _promote(self, s):
        regs = {v["name"]: v for v in self.data["regulars"]}
        chat = s["chatters"]
        active = [(n, c) for n, c in chat.items() if n in regs]
        fresh = sorted(((n, c) for n, c in chat.items() if n not in regs and not n.startswith("@user-")
                        and c["count"] >= MIN_CHATS_FOR_PROMOTE), key=lambda x: -x[1]["count"])[:25]
        fmt = lambda c: " / ".join(c["samples"])
        res = await ai.ask_async(tpl.fill(
            "VIEWER_BOOK", show=s.get("show", "?"), match=s.get("match") or "(기록 없음)", n=self.cfg.NEW_REGULARS_PER_SHOW,
            regulars="\n".join(f"- {v['name']} — {v['trait']}" + (f" ({v['memory']})" if v.get("memory") else "") for v in regs.values()),
            regular_samples="\n".join(f"- {n}: {fmt(c)}" for n, c in active) or "(없음)",
            newcomers="\n".join(f"- {n} ({c['count']}): {fmt(c)}" for n, c in fresh) or "(없음)"),
            SCHEMA, max_tokens=1500, label="시청자 기록 갱신")
        show = s.get("show", 0)
        for n, _ in active:
            regs[n]["visits"] = regs[n].get("visits", 0) + 1
        for m in res["memories"]:
            if m["name"] in regs and m["memory"].strip():
                regs[m["name"]]["memory"] = m["memory"].strip()[:60]
        for n in res["members"]:
            if n in regs and not regs[n].get("role"):
                regs[n]["role"] = "member"
                self.new_members.append(n)
                print(f"[채널] 🟢 {n} 멤버십 가입 (라이브 중에 환영 메시지)")
        valid = {n for n, _ in fresh}
        for v in res["new_regulars"][: self.cfg.NEW_REGULARS_PER_SHOW]:
            if v["name"] in valid and v["name"] not in regs:
                self.data["regulars"].append({"name": v["name"], "trait": v["trait"].strip()[:80], "role": "",
                                              "since": show, "visits": 1, "memory": ""})
                print(f"[채널] 자주 오는 시청자 추가: {v['name']} — {v['trait']}")
        if res["summary"].strip():
            self.data["history"] = (self.data.get("history", []) + [
                {"show": show, "date": s.get("date", ""), "summary": res["summary"].strip()[:80]}])[-MAX_HISTORY:]
        self.data["session"] = None

    # ── 방송 중 기록 ────────────────────────────────────────────
    def record(self, name, msg):
        s = self.data.get("session")
        if not s:
            return
        c = s["chatters"].setdefault(name, {"count": 0, "samples": []})
        c["count"] += 1
        if len(c["samples"]) < MAX_SAMPLES:
            c["samples"].append(msg[:60])
        elif c["count"] % 5 == 0:
            c["samples"][c["count"] // 5 % MAX_SAMPLES] = msg[:60]
        self.dirty = True

    def join_member(self, name):
        regs = {v["name"]: v for v in self.data["regulars"]}
        if name in regs:
            regs[name]["role"] = regs[name].get("role") or "member"
        elif not name.startswith("@user-"):
            self.data["regulars"].append({"name": name, "trait": "라이브 중에 멤버십 가입한 시청자", "role": "member",
                                          "since": self.data.get("shows", 0), "visits": 1, "memory": ""})
        self.dirty = True

    def set_match(self, match):
        s = self.data.get("session")
        if s and s.get("match") != match:
            s["match"], self.dirty = match, True

    def save(self):
        if self.dirty:
            store.save(BOOK, self.data)
            self.dirty = False

    # ── 채팅 AI 에게 ────────────────────────────────────────────
    def prompt_text(self) -> str:
        regs = sorted(self.data["regulars"], key=lambda v: (v.get("role") == "mod", v.get("visits", 0)), reverse=True)
        lines = [f"- {v['name']} — {v['trait']}" + (f" (지난 라이브: {v['memory']})" if v.get("memory") else "")
                 for v in regs[: self.cfg.MAX_REGULARS_IN_PROMPT]]
        past = "\n".join(f"- #{x['show']} ({x['date'][:10]}): {x['summary']}" for x in self.data.get("history", [])[-3:])
        return "\n".join(lines) + ("\n\n" + tpl.fill("PAST_SHOWS", past=past) if past else "")
