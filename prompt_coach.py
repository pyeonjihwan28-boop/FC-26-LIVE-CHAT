"""
프롬프트 자가 개선 — 경기 중 COACH_EVERY_SEC 마다 Claude Haiku가 최근 채팅 기록을 보고
어색한 채팅·상황과 안 맞는 채팅을 찾아서 prompt_template.md 의 [LEARNED] 규칙을 고침 (1회 약 0.3~0.6센트)

  - 평가 재료: 오늘 경기 정보(선발·팬 구성·시청자 수) + 중계 멘트와 그때 나온 채팅들 + 지금 규칙
  - 고치는 곳: [LEARNED] 구역만 (출력 형식 등 기본 규칙은 안 건드림). 한 번에 최대 3개 추가·필요 없는 규칙 삭제
  - 기록: 평가 결과는 prompt_review_log.jsonl, 고치기 전 파일은 prompts_history/
  - 채팅 AI는 파일이 바뀌면 다음 호출부터 새 규칙으로 채팅
"""
import asyncio
import json
from datetime import datetime
from pathlib import Path

import anthropic

import prompts

HERE = Path(__file__).resolve().parent
LOG = HERE / "prompt_review_log.jsonl"

REVIEW_PROMPT = """너는 한국 유튜브 스포츠 생중계 채팅창을 흉내 내는 AI의 '연출 감독'이다.
아래는 방금 경기 중에 AI가 만든 채팅 기록이다. 실제 한국 유튜브 해외축구 생중계 채팅창과 비교해서 평가하고,
AI가 다음부터 더 자연스럽게 치도록 '개선 규칙'을 고쳐라.

[오늘 경기]
{match}

{audience}

[채팅 AI 의 기본 규칙 요약]
- 실제 경기처럼 (게임·스트리머 언급 금지), 유튜브 라이브 채팅 말투, 짧게, 장면 크기에 맞는 분량
- 스코어·득점자는 중계 멘트에 나온 것만, 선수는 선발 명단 기준, 팬 구성 비율대로 응원

[지금 개선 규칙] (번호: 규칙)
{rules}

[최근 채팅 기록] (시각 · 경기 시간 · 그때 들린 중계 멘트 → AI가 만든 채팅)
{log}

평가할 것:
1. 어색한 채팅: 실제 사람이 안 칠 말투, 번역투, 너무 길거나 설명조, 같은 표현·같은 닉네임 반복, 억지 드립, 이모지 과다
2. 상황과 안 맞는 채팅: 중계 멘트 내용과 안 맞는 반응, 없는 선수·지어낸 스코어, 장면 크기와 안 맞는 분량(골인데 조용함 등),
   팬 구성과 안 맞는 응원 비율, 시간대·경기 시간과 안 맞는 말, 상대 팀 팬 반응 부족 등
3. 잘하고 있는 점은 규칙으로 만들 필요 없음

출력:
- score: 1~10 (실제 채팅창 같은 정도)
- problems: 구체적 문제 (최대 6개). example 에 문제 채팅 그대로, issue 에 왜 문제인지
- add_rules: 새 개선 규칙 (최대 3개). 짧고(60자 이내) 구체적이고 다음 경기에도 통하는 일반 규칙으로. 특정 선수·이번 경기 전용 내용 금지.
  이미 있는 규칙과 겹치면 추가하지 마라. 출력 형식(JSON 줄)을 바꾸는 규칙 금지.
  팬 구성 비율·시청자 수는 프로그램이 [채팅창 팬 구성]으로 정해 주므로 비율 숫자를 규칙으로 정하지 마라 ("팬 구성 비율을 지켜라" 정도는 OK).
- remove_rules: 지우거나 바꿀 규칙 번호 (틀렸거나, 채팅을 오히려 어색하게 만들었거나, 다른 규칙과 겹치거나 충돌하는 것)
- summary: 한 줄 총평"""

SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer"},
        "problems": {"type": "array", "items": {
            "type": "object", "properties": {"example": {"type": "string"}, "issue": {"type": "string"}},
            "required": ["example", "issue"], "additionalProperties": False}},
        "add_rules": {"type": "array", "items": {"type": "string"}},
        "remove_rules": {"type": "array", "items": {"type": "integer"}},
        "summary": {"type": "string"},
    },
    "required": ["score", "problems", "add_rules", "remove_rules", "summary"],
    "additionalProperties": False,
}


class PromptCoach:
    def __init__(self, cfg, brain):
        self.cfg, self.brain = cfg, brain
        self.client = anthropic.AsyncAnthropic(max_retries=1, timeout=60.0)
        self.seen = 0                                     # 이미 평가한 기록 수 (brain.log 누적 기준)
        self.total = 0

    async def run(self):
        c = self.cfg
        if not c.PROMPT_COACH:
            return
        while True:
            await asyncio.sleep(c.COACH_EVERY_SEC)
            log = list(self.brain.log)
            fresh = [e for e in log[-60:] if e["chats"]]
            new_chats = sum(len(e["chats"]) for e in log) - self.seen
            if not self.brain.ready or new_chats < c.COACH_MIN_CHATS:
                continue                                  # 경기 중이 아니거나 평가할 채팅이 적음 → 비용 0
            self.seen = sum(len(e["chats"]) for e in log)
            try:
                await self.review(fresh[-30:])
            except Exception as e:
                print(f"   [코치] 평가 실패 (다음에 다시): {e}")

    async def review(self, entries):
        rules = prompts.learned_rules()
        log_text = "\n".join(
            f"- {e['t']} · {str(e['minute']) + '분' if e.get('minute') is not None else '-'} · \"{e['cue']}\"\n    → "
            + "\n    → ".join(e["chats"]) for e in entries)
        aud = self.brain.audience.prompt_text() if self.brain.audience else ""
        prompt = (REVIEW_PROMPT.replace("{match}", self.brain.lineup or "(모름)").replace("{audience}", aud)
                  .replace("{rules}", "\n".join(f"{i + 1}: {r}" for i, r in enumerate(rules)) or "(없음)")
                  .replace("{log}", log_text))
        resp = await self.client.messages.create(
            model=self.cfg.CLAUDE_MODEL, max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        )
        res = json.loads(next(b.text for b in resp.content if b.type == "text"))
        cost = (resp.usage.input_tokens * self.cfg.PRICE_IN_PER_MTOK
                + resp.usage.output_tokens * self.cfg.PRICE_OUT_PER_MTOK) / 1_000_000
        self.total += cost

        drop = {i - 1 for i in res["remove_rules"] if 1 <= i <= len(rules)}
        kept = [r for i, r in enumerate(rules) if i not in drop]
        added = [r.strip()[:80] for r in res["add_rules"][:3] if r.strip()]
        new = prompts.prune_rules(kept + added, self.cfg.COACH_MAX_RULES)  # 의미 중복 규칙 합치기
        if new != rules:
            prompts.save_learned(new)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"time": datetime.now().isoformat(timespec="seconds"), **res,
                                "rules_before": rules, "rules_after": new, "cost": round(cost, 5)}, ensure_ascii=False) + "\n")
        print(f"\n   [코치] 채팅 평가 {res['score']}/10 · {res['summary']}  (${cost:.4f}, 누적 ${self.total:.3f})")
        for p in res["problems"][:3]:
            print(f"   [코치]   ✗ \"{p['example'][:40]}\" — {p['issue'][:70]}")
        for i in sorted(drop):
            print(f"   [코치]   − 규칙 삭제: {rules[i]}")
        for r in added:
            print(f"   [코치]   + 규칙 추가: {r}")
        return res
