"""
화면 읽기 — FC 26 화면 한 장을 Claude Haiku로 읽어서 lineup.json 에 반영

  화면마다 '읽기 블록' 하나: READERS[화면] = 프롬프트 구역 + 응답 스키마 + 잘라낼 영역 + 반영 함수
  공통 흐름:  read(화면, 이미지, OCR) → apply(화면, 결과, OCR)      (프롬프트 글은 전부 prompt_template.md)

    me     팀 관리 → 스쿼드            우리 선발 11명 (포메이션은 이름표 높이로 계산)
    opp    상대 분석 → 예상 라인업     상대 선발 11명 (화면에 적힌 포메이션대로)
    match  커리어 홈(센트럴)           다음 경기 두 팀·대회·날짜 + 내 팀·홈/원정
    pause  경기 중 일시정지 팀 관리    포메이션을 못 읽은 팀 다시 읽기 + 실제 등번호

  python lineup_scan.py me|opp|match|pause [--image 스샷.png] [--delay 5]    (보통은 main.py 가 알아서 실행)
"""
import argparse
import os
import re
import sys
import time

from dotenv import load_dotenv
from PIL import Image, ImageGrab

import config

from blocks import HERE, ai, store, text, tpl

LINEUP = HERE / "lineup.json"       # (다른 모듈 호환용 경로)
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
LANE = {"L": 0, "LC": 1, "C": 2, "RC": 3, "R": 4}
ROW_GAPS = (0.035, 0.03, 0.04, 0.025, 0.045, 0.05, 0.02)   # 줄 나누는 높이 차 (화면 높이 비율), 앞에서부터 시도
FORMATIONS = {  # FC 26 에 있는 포메이션
    "3-1-4-2", "3-4-1-2", "3-4-2-1", "3-4-3", "3-5-2", "4-1-2-1-2", "4-1-3-2", "4-1-4-1", "4-2-1-3", "4-2-2-2",
    "4-2-3-1", "4-2-4", "4-3-1-2", "4-3-2-1", "4-3-3", "4-4-1-1", "4-4-2", "4-5-1", "5-2-1-2", "5-2-3", "5-3-2", "5-4-1",
    "4-1-2-3", "3-2-4-1", "4-2-1-2-1", "3-3-1-3", "3-1-2-1-3",
}


# ── 스키마 블록 ──────────────────────────────────────────────────
def obj(**props):
    """JSON 스키마 object (전부 필수, 추가 필드 없음)"""
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


S, I, N, B = {"type": "string"}, {"type": "integer"}, {"type": "number"}, {"type": "boolean"}
LANE_S = {"type": "string", "enum": list(LANE)}
KIT = dict(kit_base=S, kit_stripe=S, kit_number=S)
LINEUP_SCHEMA = obj(players={"type": "array", "items": obj(visible_name=S, name_ko=S, name_en=S, number=I, x=N, y=N,
                                                          lane=LANE_S, sure=B)},
                    formation_text=S, team_name_ko=S, team_short=S, **KIT)
MATCH_SCHEMA = obj(competition_ko=S, competition_en=S, kickoff_ko=S, kickoff_en=S, my_team_label=S,
                   home=obj(name_ko=S, short=S, **KIT), away=obj(name_ko=S, short=S, **KIT),
                   my_side={"type": "string", "enum": ["home", "away"]})
PAUSE_TEAM = obj(formation=S, players={"type": "array", "items": obj(number=I, visible_name=S, name_ko=S, name_en=S,
                                                                      row=I, lane=LANE_S)})
PAUSE_SCHEMA = obj(home=PAUSE_TEAM, away=PAUSE_TEAM)
TEAM_SCHEMA = obj(team_short=S, **KIT)


# ── 작은 블록 ────────────────────────────────────────────────────
def side_of(who: str) -> str:
    my = config.MY_SIDE if config.MY_SIDE in ("home", "away") else "home"
    return my if who == "me" else ("away" if my == "home" else "home")


def kit_of(d: dict, old=None):
    """Haiku 응답의 kit_base/kit_stripe/kit_number → 유니폼 dict (색이 이상하면 이전 값)"""
    if not (HEX.match(d.get("kit_base", "")) and HEX.match(d.get("kit_number", ""))):
        return old
    kit = {"base": d["kit_base"], "number": d["kit_number"]}
    if HEX.match(d.get("kit_stripe", "")):
        kit["stripe"] = d["kit_stripe"]
    return kit


def lineup_ok(team: dict) -> bool:
    """선발 11명 + FC 에 있는 포메이션으로 제대로 읽혔는지"""
    f = team.get("formation", "")
    return len(team.get("players") or []) == 11 and f in FORMATIONS and sum(map(int, f.split("-"))) == 10


def consistent_name(visible, en):
    """Haiku가 붙인 영어 이름이 화면 이름과 같은 사람인지 (보이는 이름 조각이 들어 있는지)"""
    toks = [text.norm(t) for t in re.findall(r"[a-zA-ZÀ-ÿ]{3,}", visible)]
    return not toks or any(len(t) >= 3 and text.contains(t, text.norm(en)) for t in toks)


def unique_numbers(nums):
    """0·중복 등번호를 비어 있는 번호로 (골키퍼 1, 나머지 2부터)"""
    seen, out = set(), []
    for i, n in enumerate(nums):
        if not n or n in seen or not 1 <= n <= 99:
            n = next(k for k in ([1] if i == 0 else []) + list(range(2, 100)) if k not in seen and k not in nums)
        seen.add(n)
        out.append(n)
    return out


def match_pairs(left, right, key_l, key_r, threshold, extra=lambda i, j: 0.0):
    """두 목록을 이름이 가장 비슷한 것끼리 1:1 로 짝지음 → {왼쪽 번호: 오른쪽 번호}"""
    pairs = []
    for i, a in enumerate(left):
        for j, b in enumerate(right):
            ka, kb = key_l(a), key_r(b)
            r = max(text.similar(ka, kb), 0.75 if len(kb) >= 3 and kb in ka else 0)
            if r >= threshold:
                pairs.append((r - extra(i, j), i, j))
    out, used = {}, set()
    for _, i, j in sorted(pairs, reverse=True):
        if i not in out and j not in used:
            out[i] = j
            used.add(j)
    return out


# ── 선발 → 줄·포메이션 ──────────────────────────────────────────
def screen_positions(players, ocr_lines):
    """선수별 화면 위치 (0~1). OCR이 이름을 읽은 선수는 OCR 위치(정확), 나머지는 Haiku 좌표를 OCR 기준으로 맞춤"""
    hz = [(p["x"] / 100, p["y"] / 100) for p in players]
    cands = [(t, x, y) for t, x, y in ocr_lines if len(text.norm(t)) >= 3]
    dist = lambda i, j: 0.5 * ((cands[j][1] - hz[i][0]) ** 2 + (cands[j][2] - hz[i][1]) ** 2) ** 0.5   # 같은 이름이면 가까운 쪽
    got = match_pairs(players, cands, lambda p: text.norm(p["visible_name"]), lambda c: text.norm(c[0]), 0.7, dist)
    if len(got) < 3:
        return hz

    def fit(axis):                                           # OCR ≈ a · Haiku + b (최소제곱)
        xs, ys = [hz[i][axis] for i in got], [cands[j][axis + 1] for j in got.values()]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        var = sum((x - mx) ** 2 for x in xs)
        a = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var if var > 1e-6 else 1.0
        return lambda v: my + a * (v - mx)

    fx, fy = fit(0), fit(1)
    return [cands[got[i]][1:] if i in got else (fx(hz[i][0]), fy(hz[i][1])) for i in range(len(players))]


def split_rows(pos, lanes, shown=None):
    """화면 위치 → 줄 목록 (아래 = 골키퍼부터, 줄 안은 lane → x 순). 화면에 포메이션이 적혀 있으면 그 인원대로 자르고,
    없으면 FC 포메이션이 나오는 줄 간격을 찾음"""
    order = sorted(range(len(pos)), key=lambda i: -pos[i][1])
    key = lambda i: (LANE.get(lanes[i], 2), pos[i][0])

    def by_gap(gap):
        rows, last = [], None
        for i in order:
            if last is None or last - pos[i][1] > gap:
                rows.append([])
            rows[-1].append(i)
            last = pos[i][1]
        return rows

    if shown and sum(shown) == len(pos) - 1:
        rows, k = [], 0
        for n in [1] + shown:
            rows.append(order[k:k + n])
            k += n
    else:
        rows = next((r for g in ROW_GAPS for r in [by_gap(g)]
                     if len(r[0]) == 1 and "-".join(str(len(x)) for x in r[1:]) in FORMATIONS), by_gap(ROW_GAPS[0]))
    return [sorted(r, key=key) for r in rows]


# ── 읽기 블록: 화면별 프롬프트 만들기 ───────────────────────────
def _ocr_hint(ocr_lines, with_pos=True):
    if not ocr_lines:
        return ""
    body = "\n".join((f"({x * 100:.0f}, {y * 100:.0f}) " if with_pos else "") + t for t, x, y in ocr_lines)
    return "\n\n" + tpl.fill("SCAN_OCR", ocr=body)


def _prompt_lineup(who, ocr_lines):
    if who == "me":
        team = config.MY_TEAM_NAME
        screen = tpl.fill("SCAN_SCREEN_ME") + (" " + tpl.fill("SCAN_HINT_ME", team=team) if team else "")
        p = tpl.fill("SCAN_LINEUP", screen=screen)
    else:
        p = tpl.fill("SCAN_LINEUP", screen=tpl.fill("SCAN_SCREEN_OPP"))
        opp = (store.load("lineup.json", {}).get(side_of("opp")) or {}).get("name", "")
        if opp and config.MY_TEAM_NAME:                        # 커리어 홈에서 읽은 상대 이름을 그대로 쓰게
            p += "\n\n" + tpl.fill("SCAN_HINT_OPP", opp=opp)
    return p + _ocr_hint(ocr_lines)


READERS = {
    #         프롬프트 만들기                                         스키마           잘라낼 영역(비율)       max_tokens
    "me":    (lambda ocr: _prompt_lineup("me", ocr),                   LINEUP_SCHEMA, (0, 0, 0.6, 0.62), 2500),
    "opp":   (lambda ocr: _prompt_lineup("opp", ocr),                  LINEUP_SCHEMA, (0, 0, 1, 1), 2500),
    "match": (lambda ocr: tpl.fill("SCAN_MATCH") + _ocr_hint(ocr, False), MATCH_SCHEMA, (0, 0, 1, 1), 800),
    "pause": (lambda ocr: tpl.fill("SCAN_PAUSE") + _ocr_hint(ocr, False), PAUSE_SCHEMA, (0, 0, 1, 1), 3000),
}
LABEL = {"me": "스쿼드 읽기", "opp": "예상 라인업 읽기", "match": "다음 경기 읽기", "pause": "일시정지 화면 선발 읽기"}


def read(who, img, ocr_lines=()) -> dict:
    """공통 흐름 ①: 화면 한 장 → Haiku 응답 (잘라서 보낸 좌표는 전체 화면 좌표로 되돌림)"""
    make, schema, crop, max_tokens = READERS[who]
    res = ai.ask(make(ocr_lines), schema, image=img, crop=crop, max_tokens=max_tokens, label=LABEL[who])
    for p in res.get("players", []):
        if "x" in p:
            p["x"] = crop[0] * 100 + p["x"] * (crop[2] - crop[0])
            p["y"] = crop[1] * 100 + p["y"] * (crop[3] - crop[1])
    return res


def apply(who, res, ocr_lines=()):
    """공통 흐름 ②: 응답 → lineup.json. 반환: match 는 '경기가 바뀌었나', 나머지는 성공 여부"""
    return {"me": _apply_lineup, "opp": _apply_lineup, "match": _apply_match, "pause": _apply_pause}[who](who, res, ocr_lines)


# ── 반영 블록 ───────────────────────────────────────────────────
def _apply_lineup(who, res, ocr_lines):
    players = res["players"]
    if not players:
        return False
    for p in players:                              # 보이는 이름과 다른 선수로 바꿔 부른 영어 이름은 버림
        if not consistent_name(p["visible_name"], p["name_en"]):
            p["name_en"], p["sure"] = p["visible_name"], False
    shown = [int(n) for n in re.findall(r"\d", res["formation_text"])]
    rows = split_rows(screen_positions(players, ocr_lines), [p["lane"] for p in players], shown)
    ordered = [players[i] for r in rows for i in r]
    counts = [len(r) for r in rows][1:] if len(rows[0]) == 1 else [len(r) for r in rows]
    side = side_of(who)

    def change(data):
        team = dict(data.get(side) or {})
        team["formation"] = "-".join(map(str, shown if shown == counts else counts))
        old_nums = {p[1]: p[0] for p in team.get("players", [])}   # 직접 고친 등번호 유지
        nums = unique_numbers([old_nums.get(p["name_ko"]) or p["number"] for p in ordered])
        team["players"] = [[n, p["name_ko"], p["name_en"]] for n, p in zip(nums, ordered)]
        name = config.MY_TEAM_NAME if who == "me" else res["team_name_ko"]
        if name:
            team["name"] = name
            team["short"] = ((config.MY_TEAM_SHORT if who == "me" else "") or res["team_short"])[:4].upper() or team.get("short", "")
            team["kit"] = kit_of(res, team.get("kit"))
        data[side] = team
        return team

    team = store.update("lineup.json", change, {})
    guessed = {p["name_ko"] for p in players if not p["sure"]}
    vis = {p["name_ko"]: p["visible_name"] for p in players}
    print(f"\n[{side}] {team.get('name', '')}  {team['formation']}  ({len(team['players'])}명)")
    for num, ko, en in team["players"]:
        print(f"  {vis.get(ko, ''):<18} → {num:>2} {ko} / {en}" + ("   ← 추측, 확인 필요" if ko in guessed else ""))
    if len(players) != 11:
        print(f"⚠ {len(players)}명만 읽혔습니다.")
    return True


def _apply_match(who, res, ocr_lines):
    def change(data):
        changed = False
        data["league"] = res["competition_en"].upper()[:30] or data.get("league", "")
        data["round"] = res["kickoff_en"].upper()[:24]
        data["competition"], data["kickoff"] = res["competition_ko"], res["kickoff_ko"]
        for side in ("home", "away"):
            t, old = res[side], data.get(side) or {}
            if old.get("name") != t["name_ko"]:            # 다른 팀 → 선발은 스쿼드/예상 라인업 화면에서 새로
                changed = True
                old = {"name": t["name_ko"], "formation": "", "players": [], "gk_kit": {"base": "#1FB86A", "number": "#FFFFFF"}}
            old["short"] = t["short"][:4].upper() or old.get("short", "")
            old["kit"] = kit_of(t, old.get("kit"))
            data[side] = old
        return changed

    changed = store.update("lineup.json", change, {})
    me = res[res["my_side"]]["name_ko"]
    config.save_settings(MY_TEAM_NAME=me, MY_TEAM_SHORT="", MY_SIDE=res["my_side"])
    print(f"[경기] {res['competition_ko']} {res['kickoff_ko']} · 홈 {res['home']['name_ko']} vs 원정 {res['away']['name_ko']}"
          f" · 내 팀: {me} ({'홈' if res['my_side'] == 'home' else '원정'}, 화면 표시 '{res['my_team_label']}')")
    return changed


def _apply_pause(who, res, ocr_lines):
    """포메이션을 못 읽은 팀은 이 화면 선발로 바꾸고, 두 팀 다 등번호는 이 화면(실제 등번호)으로 맞춤"""
    fixed = []

    def change(data):
        for side in ("home", "away"):
            team = data.get(side) or {}
            players = [p for p in res[side]["players"] if p["visible_name"]]
            for p in players:
                if not consistent_name(p["visible_name"], p["name_en"]):
                    p["name_en"] = p["visible_name"]
            if not lineup_ok(team) and len(players) == 11:
                ordered = sorted(players, key=lambda p: (p["row"], LANE.get(p["lane"], 2)))
                counts = [sum(1 for p in players if p["row"] == r) for r in sorted({p["row"] for p in players})][1:]
                f = res[side]["formation"]
                team["formation"] = f if f in FORMATIONS else "-".join(map(str, counts))
                team["players"] = [[p["number"], p["name_ko"], p["name_en"]] for p in ordered]
                fixed.append(side)
            else:                                  # 선발은 그대로, 등번호만 실제 번호로
                mine = team.get("players") or []
                got = match_pairs(mine, players, lambda q: text.norm(q[2] if len(q) > 2 else ""),
                                  lambda p: text.norm(p["name_en"]), 0.75)
                real = {players[j]["number"] for j in got.values()}
                for i, q in enumerate(mine):
                    q[0] = players[got[i]]["number"] if i in got else (0 if q[0] in real else q[0])
                for q, n in zip(mine, unique_numbers([q[0] for q in mine])):
                    q[0] = n
            data[side] = team

    store.update("lineup.json", change, {})
    for side in fixed:
        tm = store.load("lineup.json", {})[side]
        print(f"[일시정지] {tm.get('name', side)} 선발을 이 화면으로 다시 읽음 → {tm['formation']}")
    print("[일시정지] 두 팀 등번호를 실제 번호로 맞춤")
    return True


def apply_my_team():
    """설정 창에서 우리 팀을 저장하면: Haiku에게 그 팀 약칭·유니폼 색을 물어서 lineup.json 우리 팀 자리에 반영"""
    name = config.MY_TEAM_NAME
    if not name:
        return
    res = ai.ask(tpl.fill("TEAM_INFO", name=name), TEAM_SCHEMA, max_tokens=300, label="팀 정보")

    def change(data):
        team = data.setdefault(side_of("me"), {})
        team["name"] = name
        team["short"] = (config.MY_TEAM_SHORT or res["team_short"])[:4].upper()
        team["kit"] = kit_of(res, team.get("kit"))
        return team

    team = store.update("lineup.json", change, {})
    print(f"[설정] 우리 팀 {name} ({team['short']}) → lineup.json 반영")


def local_ocr(img) -> list:
    try:
        from screen_watch import WinOCR
        return WinOCR().read(img)
    except Exception as e:
        print(f"[OCR] 로컬 OCR 실패 (Haiku 판단만 사용): {e}")
        return []


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    load_dotenv(HERE / ".env", override=True)
    ap = argparse.ArgumentParser(description="FC 26 화면 한 장 → lineup.json (Claude Haiku)")
    ap.add_argument("who", choices=list(READERS))
    ap.add_argument("--image", help="캡처 대신 쓸 스크린샷 파일")
    ap.add_argument("--delay", type=int, default=5)
    args = ap.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY 가 없습니다. .env 를 확인하세요.")
    if args.image:
        img = Image.open(args.image).convert("RGB")
    else:
        for s in range(args.delay, 0, -1):
            print(f"\r{s}초 뒤 캡처합니다… 게임 화면을 띄워 두세요 ", end="", flush=True)
            time.sleep(1)
        img = ImageGrab.grab().convert("RGB")
    lines = local_ocr(img)
    apply(args.who, read(args.who, img, lines), lines)


if __name__ == "__main__":
    main()
