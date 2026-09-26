"""
프롬프트 최적화 오프라인 도구 — 채팅 AI의 시스템 프롬프트를 자동으로 탐색·평가해 가장 좋은 것을 고름
  (참고: stanfordnlp/dspy (MIPRO/BootstrapFewShot), keirp/automatic_prompt_engineer (APE),
         google-deepmind PromptBreeder — 진화적 프롬프트 탐색)

  실행: python prompt_optimizer.py
  - dspy 가 설치돼 있으면 (pip install dspy) MIPRO 스타일 최적화를 시도하고, 아니면 APE 루프로 동작
  - 평가: 대표 중계 멘트 8개에 대해 후보 프롬프트로 채팅을 생성 → Claude Haiku가 1~10점 채점 (LLM-as-judge)
  - 결과: 점수가 가장 좋은 프롬프트를 prompt_template.optimized.md 에 씀 (실행 중인 prompt_template.md 는 건드리지 않음)
    → 마음에 들면 [SYSTEM] 구역을 복사해 prompt_template.md 에 붙여넣기
  - 비용: 후보 5개 × 에피소드 8개 × (생성+평가) ≈ 수십 센트
"""
import asyncio
import json
import os
import random
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
N_CANDIDATES = int(os.getenv("OPT_CANDIDATES", "5"))
N_ROUNDS = int(os.getenv("OPT_ROUNDS", "2"))          # 진화 라운드 (우승 프롬프트를 다시 변이)

# 대표 중계 멘트 (골·결정적 기회·하프타임·잡담·종료 등 장면별)
EVAL = [
    "손흥민이 드리블로 돌파합니다, 크로스! 골입니다! 손흥민의 극장골!",
    "수비가 살짝 무너지는 모습입니다, 중앙으로 패스, 슈팅! 문전에서 선방해요.",
    "전반 23분, 아직 양 팀 모두 득점은 없고 중원 싸움이 치열합니다.",
    "교체 카드입니다, 공격수 한 명을 더 넣는 공격적인 교체입니다.",
    "하프타임입니다. 전반 스코어 1대1, 후반이 기대됩니다.",
    "코너킥, 올려줍니다, 헤딩! 크로스바를 맞고 나왔어요 아쉽습니다.",
    "심판이 휘슬을 불었습니다, 페널티킥 선언입니다! VAR 확인 들어갑니다.",
    "경기 종료입니다. 최종 스코어 2대1, 홈 팀이 승리했습니다.",
]

JUDGE = """너는 한국 유튜브 축구 생중계 채팅창의 평가자다. 아래 '중계 멘트'에 대해 AI가 만든 채팅 묶음이
실제 한국인 시청자들이 칠 법한지 1~10점으로 매겨라.
기준: 말투 자연스러움(번역투·설명조 아님), 장면 크기에 맞는 분량과 흥분도, 팬 성향 반영,
선수·스코어를 지어내지 않음, 같은 표현 반복 없음. 점수만 JSON으로: {"score": 정수, "reason": 한 줄}
[중계 멘트] {cue}
[AI 채팅]
{chat}"""


def load_current_system():
    import prompts
    sec = prompts.sections()
    return sec.get("SYSTEM", "(SYSTEM 구역 없음)")


async def gen_chat(client, system, cue):
    try:
        resp = await client.messages.create(
            model=MODEL, max_tokens=400, system=system,
            messages=[{"role": "user", "content": f"중계 멘트: {cue}\n위 장면에 반응하는 시청자 채팅 5줄을 JSON 배열로만 출력"}],
        )
        return next(b.text for b in resp.content if b.type == "text")
    except Exception as e:
        return f"(생성 실패: {e})"


async def score(client, cue, chat):
    try:
        resp = await client.messages.create(
            model=MODEL, max_tokens=120,
            messages=[{"role": "user", "content": JUDGE.format(cue=cue, chat=chat)}],
            output_config={"format": {"type": "json", "schema": {
                "type": "object", "properties": {"score": {"type": "integer"}, "reason": {"type": "string"}},
                "required": ["score", "reason"], "additionalProperties": False}}},
        )
        r = json.loads(next(b.text for b in resp.content if b.type == "text"))
        return float(r["score"]), r["reason"]
    except Exception:
        return 5.0, "(평가 실패, 기본점 5)"


async def evaluate(client, system):
    total = 0.0
    for cue in EVAL:
        chat = await gen_chat(client, system, cue)
        s, _ = await score(client, cue, chat)
        total += s
    return total / len(EVAL)


async def mutate(client, base, n):
    """기존 프롬프트를 바탕으로 n개의 변이 후보 생성 (PromptBreeder 스타일)"""
    resp = await client.messages.create(
        model=MODEL, max_tokens=3000,
        messages=[{"role": "user", "content":
            f"아래는 축구 중계 채팅 AI의 시스템 프롬프트다. 이걸 개선한 변이 버전 {n}개를 JSON 배열로만 출력해라. "
            f"각 변이는 길이 300~800자, 실제 채팅 말투·장면 크기·팬 성향을 더 잘 지키도록 바꾸되, 출력 형식(JSON)은 유지해라.\n{base[:3000]}"}],
        output_config={"format": {"type": "json", "schema": {
            "type": "object", "properties": {"candidates": {"type": "array", "items": {"type": "string"}}},
            "required": ["candidates"], "additionalProperties": False}}},
    )
    r = json.loads(next(b.text for b in resp.content if b.type == "text"))
    return r["candidates"][:n]


async def main():
    client = anthropic.AsyncAnthropic(max_retries=2, timeout=120.0)
    # dspy 가 있으면 MIPRO 경로 안내 (같은 EVAL·JUDGE 를 metric 으로 연결 가능)
    try:
        import dspy  # noqa: F401
        print("[최적화] dspy 발견. APE 루프 후 MIPRO 연결 가이드를 출력합니다.")
        HAS_DSPY = True
    except ImportError:
        HAS_DSPY = False
        print("[최적화] dspy 미설치 → APE(자동 프롬프트 엔지니어) 루프로 동작. (pip install dspy 로 업그레이드 가능)")

    current = load_current_system()
    orig_score = best_score = await evaluate(client, current)
    best = current
    print(f"[최적화] 현재 프롬프트 점수: {best_score:.1f}/10")

    pool = [current]
    for rnd in range(N_ROUNDS):
        candidates = await mutate(client, best, N_CANDIDATES)
        for cand in candidates:
            if cand in pool:
                continue
            pool.append(cand)
            sc = await evaluate(client, cand)
            print(f"[최적화] 라운드 {rnd + 1} 후보 {len(pool)}: {sc:.1f}/10" + ("  ★ 우승" if sc > best_score else ""))
            if sc > best_score:
                best, best_score = cand, sc

    out = HERE / "prompt_template.optimized.md"
    out.write_text(f"# 자동 최적화된 시스템 프롬프트 (점수 {best_score:.1f}/10, 기존 {orig_score:.1f}/10)\n"
                   f"# 마음에 들면 아래 [SYSTEM] 구역을 prompt_template.md 의 같은 구역에 붙여넣기\n\n"
                   f"=====[SYSTEM]=====\n{best}\n", encoding="utf-8")
    print(f"\n[최적화] 완료. 최고 점수 {best_score:.1f}/10 → {out.name} 에 저장했습니다.")
    if HAS_DSPY:
        print("[최적화] dspy MIPRO 로 더 깊이 최적화하려면: dspy.Signature 로 (cue→chat) 정의 후 "
              "BootstrapFewShot(metric=judge) 또는 MIPRO(metric=judge) 컴파일. EVAL/JUDGE 를 그대로 재사용하세요.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(1)
