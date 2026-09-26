"""
prompt_template.md 읽기·쓰기 — 채팅 AI에게 보내는 글의 양식

  =====[이름]===== 줄로 구역을 나누고, {자리} 는 fill() 이 채움.
  파일이 바뀌면 (직접 고치거나 prompt_coach 가 고치면) 다음 호출부터 자동으로 다시 읽음.
"""
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "prompt_template.md"
HISTORY = HERE / "prompts_history"
MARK = re.compile(r"^=====\[([A-Z_]+)\]=====\s*$", re.M)
_lock = threading.Lock()
_cache = {"mtime": None, "sections": {}}


def sections() -> dict:
    """{구역 이름: 내용}. 파일이 바뀌었을 때만 다시 읽음"""
    with _lock:
        try:
            mtime = TEMPLATE.stat().st_mtime
        except OSError:
            raise RuntimeError(f"{TEMPLATE.name} 가 없습니다")
        if mtime != _cache["mtime"]:
            text = TEMPLATE.read_text(encoding="utf-8")
            parts = MARK.split(text)
            _cache["sections"] = {parts[i]: parts[i + 1].strip("\n") for i in range(1, len(parts) - 1, 2)}
            _cache["mtime"] = mtime
        return _cache["sections"]


def version():
    """파일이 바뀌면 달라지는 값 (시스템 프롬프트를 다시 만들지 판단)"""
    sections()
    return _cache["mtime"]


def fill(name: str, **values) -> str:
    text = sections().get(name, "")
    for k, v in values.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def learned_rules() -> list[str]:
    body = sections().get("LEARNED", "")
    return [l[2:].strip() for l in body.splitlines() if l.startswith("- ") and l[2:].strip()]


def learned_text() -> str:
    rules = learned_rules()
    return "\n".join(f"- {r}" for r in rules) if rules else "(아직 없음)"


def save_learned(rules: list[str]):
    """[LEARNED] 구역만 바꿔 씀. 바꾸기 전 파일은 prompts_history/ 에 보관"""
    with _lock:
        text = TEMPLATE.read_text(encoding="utf-8")
        HISTORY.mkdir(exist_ok=True)
        shutil.copy2(TEMPLATE, HISTORY / f"prompt_template_{datetime.now():%Y%m%d_%H%M%S}.md")
        m = re.search(r"^=====\[LEARNED\]=====\s*$", text, re.M)
        body = "\n".join(f"- {r}" for r in rules) if rules else "(아직 없음)"
        if m:
            nxt = MARK.search(text, m.end())
            text = text[:m.end()] + "\n" + body + "\n" + (text[nxt.start():] if nxt else "")
        else:
            text = text.rstrip("\n") + "\n\n=====[LEARNED]=====\n" + body + "\n"
        tmp = TEMPLATE.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, TEMPLATE)
