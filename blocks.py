"""
기본 블록 — 프로그램 전체가 이 다섯 가지만 조립해서 씀

  store  JSON 파일 읽기·저장 (원자적 저장, 파일별 잠금)          store.load("lineup.json"), store.save(...)
  ai     Claude Haiku 호출 (JSON 스키마 응답 · 이미지 · 비용 집계)  ai.ask(...), await ai.ask_async(...), ai.stream_lines(...)
  bus    이벤트 전달 (어느 스레드에서 보내도 이벤트 루프에서 처리)  bus.on("live", fn), bus.emit("live", True)
  text   이름 비교 (OCR 오타·잘린 이름 허용)                       text.norm(), text.similar(), text.contains()
  tpl    프롬프트 양식 (prompt_template.md 구역)                   tpl.fill("REACTION", ...)
"""
import asyncio
import base64
import copy
import difflib
import io
import json
import os
import re
import threading
from collections import defaultdict
from pathlib import Path

import anthropic

HERE = Path(__file__).resolve().parent


# ── store: JSON 파일 ────────────────────────────────────────────
class _Store:
    _PLAYER = re.compile(r'\[\s+(-?\d+),\s+("(?:[^"\\]|\\.)*")(?:,\s+("(?:[^"\\]|\\.)*"))?\s+\]')

    def __init__(self):
        self._locks = defaultdict(threading.RLock)

    def path(self, name) -> Path:
        return HERE / name

    def lock(self, name):
        return self._locks[name]

    def mtime(self, name):
        try:
            return self.path(name).stat().st_mtime
        except OSError:
            return None

    def load(self, name, default=None):
        with self._locks[name]:
            try:
                return json.loads(self.path(name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return copy.deepcopy(default)

    def save(self, name, data):
        """반쯤 쓴 파일을 다른 쪽이 읽지 않게 임시 파일에 쓰고 바꿔치기. lineup.json 은 선수 한 명을 한 줄로"""
        with self._locks[name]:
            text = json.dumps(data, ensure_ascii=False, indent=2 if name == "lineup.json" else 1)
            if name == "lineup.json":
                text = self._PLAYER.sub(lambda m: "[" + ", ".join(g for g in m.groups() if g) + "]", text)
            tmp = self.path(name).with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.path(name))

    def update(self, name, fn, default=None):
        """읽기 → fn(data) 로 고치기 → 저장 을 한 번에 (다른 스레드와 안 겹치게). fn 의 반환값을 돌려줌"""
        with self._locks[name]:
            data = self.load(name, default)
            out = fn(data)
            self.save(name, data)
            return out


store = _Store()


# ── ai: Claude Haiku ────────────────────────────────────────────
class _AI:
    def __init__(self):
        self.total = 0.0
        self._sync = self._async = None
        self._lock = threading.Lock()

    @property
    def cfg(self):
        import config
        return config

    def _cost(self, usage, label, model=None):
        c = self.cfg
        p_in, p_out = c.PRICES.get(model or c.CLAUDE_MODEL, (c.PRICE_IN_PER_MTOK, c.PRICE_OUT_PER_MTOK))
        cost = (usage.input_tokens * p_in + usage.output_tokens * p_out) / 1_000_000
        with self._lock:
            self.total += cost
        if label:
            print(f"[Haiku] {label} · ${cost:.4f} (누적 ${self.total:.3f})")
        return cost

    @staticmethod
    def image_part(img, crop=(0, 0, 1, 1), max_edge=1568, fmt="JPEG"):
        """PIL 이미지 → 메시지용 이미지 블록 (잘라서·줄여서)"""
        w, h = img.size
        img = img.crop((round(crop[0] * w), round(crop[1] * h), round(crop[2] * w), round(crop[3] * h)))
        img.thumbnail((max_edge, max_edge))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, fmt, **({"quality": 88} if fmt == "JPEG" else {}))
        return {"type": "image", "source": {"type": "base64", "media_type": f"image/{fmt.lower()}",
                                            "data": base64.standard_b64encode(buf.getvalue()).decode()}}

    def _request(self, prompt, schema, image, crop, max_tokens, fmt, model=None, effort=None):
        content = ([self.image_part(image, crop, fmt=fmt)] if image is not None else []) + [{"type": "text", "text": prompt}]
        out = {"format": {"type": "json_schema", "schema": schema}}
        if effort:                                           # 생각 깊이 (Opus 계열만)
            out["effort"] = effort
        return dict(model=model or self.cfg.CLAUDE_MODEL, max_tokens=max_tokens,
                    messages=[{"role": "user", "content": content}], output_config=out)

    @staticmethod
    def _parse(resp):
        if resp.stop_reason == "refusal":
            raise RuntimeError("모델이 요청을 거절했습니다 (refusal)")
        return json.loads(next(b.text for b in resp.content if b.type == "text"))   # 생각(thinking) 블록은 건너뜀

    def ask(self, prompt, schema, image=None, crop=(0, 0, 1, 1), max_tokens=1500, label="", fmt="JPEG"):
        """동기 호출 → 스키마대로 된 dict (화면 감시 스레드 등에서)"""
        if self._sync is None:
            self._sync = anthropic.Anthropic(max_retries=2, timeout=60.0)
        resp = self._sync.messages.create(**self._request(prompt, schema, image, crop, max_tokens, fmt))
        self._cost(resp.usage, label)
        return self._parse(resp)

    async def ask_async(self, prompt, schema, image=None, crop=(0, 0, 1, 1), max_tokens=1500, label="", fmt="JPEG",
                        model=None, effort=None):
        if self._async is None:
            self._async = anthropic.AsyncAnthropic(max_retries=1, timeout=120.0)
        resp = await self._async.messages.create(**self._request(prompt, schema, image, crop, max_tokens, fmt, model, effort))
        self._cost(resp.usage, label, model)
        return self._parse(resp)

    async def stream_lines(self, system, prompt, max_tokens):
        """스트리밍으로 한 줄씩 (채팅 생성). 끝나면 마지막에 None, 비용은 ai.total 에 합산"""
        if self._async is None:
            self._async = anthropic.AsyncAnthropic(max_retries=1, timeout=60.0)
        buf = ""
        async with self._async.messages.stream(model=self.cfg.CLAUDE_MODEL, max_tokens=max_tokens, system=system,
                                               messages=[{"role": "user", "content": prompt}]) as stream:
            async for delta in stream.text_stream:
                buf += delta
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    yield line
            if buf:
                yield buf
            self.last_stream_cost = self._cost((await stream.get_final_message()).usage, "")


ai = _AI()


# ── bus: 이벤트 ─────────────────────────────────────────────────
class _Bus:
    """bus.on("이벤트", 함수) 로 받고, bus.emit("이벤트", ...) 로 보냄.
    이벤트 루프를 연결(bind)하면 다른 스레드에서 emit 해도 루프 스레드에서 차례로 실행됨"""

    def __init__(self):
        self._subs = defaultdict(list)
        self.loop = None

    def bind(self, loop):
        self.loop = loop

    def on(self, event, fn):
        self._subs[event].append(fn)
        return fn

    def emit(self, event, *args, **kw):
        loop = self.loop
        if loop and loop.is_running():
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is not loop:
                loop.call_soon_threadsafe(self._run, event, args, kw)
                return
        self._run(event, args, kw)

    def _run(self, event, args, kw):
        for fn in list(self._subs[event]):
            try:
                out = fn(*args, **kw)
                if asyncio.iscoroutine(out):
                    asyncio.ensure_future(out)
            except Exception as e:
                print(f"[이벤트 {event}] {type(e).__name__}: {e}")


bus = _Bus()


# ── text: 이름 비교 ─────────────────────────────────────────────
class _Text:
    @staticmethod
    def norm(t, hangul=False):
        """소문자 영어(와 한글)만 남김"""
        return re.sub(r"[^a-z가-힣]" if hangul else r"[^a-z]", "", str(t).lower())

    @staticmethod
    def similar(a, b):
        return difflib.SequenceMatcher(None, a, b).ratio()

    def contains(self, needle, hay, ratio=0.8):
        """hay 안에 needle 이 (오타 조금 허용해서) 들어 있는지"""
        if not needle:
            return False
        if needle in hay:
            return True
        n = len(needle)
        return any(self.similar(needle, hay[i:i + n]) >= ratio for i in range(max(1, len(hay) - n + 1)))


text = _Text()


# ── tpl: prompt_template.md (Claude Haiku 에게 보내는 글 전부) ────────────
class _Template:
    """=====[이름]===== 줄로 구역을 나누고, {자리} 는 fill() 이 채움. 파일이 바뀌면 다음 호출부터 자동으로 다시 읽음"""
    MARK = re.compile(r"^=====\[([A-Z_]+)\]=====\s*$", re.M)
    FILE, HISTORY = HERE / "prompt_template.md", HERE / "prompts_history"

    def __init__(self):
        self._lock, self._mtime, self._sec = threading.Lock(), None, {}

    def sections(self):
        with self._lock:
            mtime = self.FILE.stat().st_mtime
            if mtime != self._mtime:
                parts = self.MARK.split(self.FILE.read_text(encoding="utf-8"))
                self._sec = {parts[i]: parts[i + 1].strip("\n") for i in range(1, len(parts) - 1, 2)}
                self._mtime = mtime
            return self._sec

    def version(self):
        self.sections()
        return self._mtime

    def fill(self, name, **values):
        out = self.sections().get(name, "")
        for k, v in values.items():
            out = out.replace("{" + k + "}", str(v))
        return out

    def learned_rules(self):
        return [l[2:].strip() for l in self.sections().get("LEARNED", "").splitlines() if l.startswith("- ") and l[2:].strip()]

    def learned_text(self):
        return "\n".join(f"- {r}" for r in self.learned_rules()) or "(아직 없음)"

    def save_learned(self, rules):
        """[LEARNED] 구역만 바꿔 씀. 바꾸기 전 파일은 prompts_history/ 에 보관"""
        import shutil
        from datetime import datetime
        with self._lock:
            src = self.FILE.read_text(encoding="utf-8")
            self.HISTORY.mkdir(exist_ok=True)
            shutil.copy2(self.FILE, self.HISTORY / f"prompt_template_{datetime.now():%Y%m%d_%H%M%S}.md")
            m = re.search(r"^=====\[LEARNED\]=====\s*$", src, re.M)
            body = "\n".join(f"- {r}" for r in rules) or "(아직 없음)"
            nxt = self.MARK.search(src, m.end()) if m else None
            src = (src[:m.end()] + "\n" + body + "\n" + (src[nxt.start():] if nxt else "")) if m \
                else src.rstrip("\n") + "\n\n=====[LEARNED]=====\n" + body + "\n"
            tmp = self.FILE.with_suffix(".tmp")
            tmp.write_text(src, encoding="utf-8")
            os.replace(tmp, self.FILE)


tpl = _Template()


# ── 규칙 가지치기 (프롬프트 자가개선 [LEARNED] 중복·충돌 제거) ─────────────
def prune_rules(rules, max_rules=15, sim=0.72):
    """의미가 비슷한 규칙을 합치고(중복 제거), 최대 개수를 넘으면 오래된 것부터 버림. 순서 유지."""
    kept = []
    for r in rules:
        r = str(r).strip()
        if not r:
            continue
        if any(difflib.SequenceMatcher(None, r, k).ratio() >= sim or r in k or k in r for k in kept):
            continue
        kept.append(r)
    return kept[-max_rules:]
