"""
FC 26 한국어 해설 → AI 가상 유튜브 라이브 채팅 (Claude Haiku)

  python main.py                  기본 스피커 소리(루프백)를 듣고 채팅 생성
  python main.py --list-devices   캡처 가능한 장치 목록
  python main.py --device 5       특정 장치 (캡처보드·가상 케이블 등, 번호나 이름 일부)
  python main.py --file 녹화.mp4   녹화 파일로 테스트
  python main.py --text           해설을 직접 타이핑해서 테스트 (음성 인식 없이 채팅만)
  python main.py --no-watch       화면 감시(라인업 자동 매핑) 끄기
  python main.py --no-window      채팅·라인업 창 없이 (OBS 브라우저 소스만)

오버레이: http://127.0.0.1:8765/   (OBS 브라우저 소스는 뒤에 ?transparent=1)
라인업:   http://127.0.0.1:8765/lineup   (lineup.json 수정)
"""
import argparse
import asyncio
import json
import os
import queue
import random
import re
import sys
import threading
import time
from pathlib import Path

import numpy as np
from aiohttp import web
from dotenv import load_dotenv

import config
from blocks import bus

HERE = Path(__file__).resolve().parent
SR = 16000
MAX_MERGE_SEC = 25  # 인식이 밀렸을 때 한 번에 묶어서 인식할 최대 길이


# ── 오버레이 서버 (WebSocket 브로드캐스트) ─────────────────────
class Hub:
    def __init__(self, ui=None):
        self.clients = set()
        self.q = asyncio.Queue()
        self.ui = ui                            # 파이썬 창 (overlay_app.py), 없으면 None

    def publish(self, payload: dict):
        self.q.put_nowait(json.dumps(payload, ensure_ascii=False))
        if self.ui:
            self.ui.post(payload)

    async def run(self):
        while True:
            data = await self.q.get()
            for ws in list(self.clients):
                try:
                    await ws.send_str(data)
                except Exception:
                    self.clients.discard(ws)


def build_app(hub: Hub) -> web.Application:
    async def index(request):
        return web.FileResponse(HERE / "overlay.html", headers={"Cache-Control": "no-store"})

    async def lineup(request):
        return web.FileResponse(HERE / "lineup.html", headers={"Cache-Control": "no-store"})

    async def lineup_json(request):
        return web.FileResponse(HERE / "lineup.json", headers={"Cache-Control": "no-store"})

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=25)
        await ws.prepare(request)
        hub.clients.add(ws)
        print(f"[오버레이] 연결됨 (현재 {len(hub.clients)}개)")
        try:
            async for _ in ws:
                pass
        finally:
            hub.clients.discard(ws)
        return ws

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/lineup", lineup)
    app.router.add_get("/lineup.json", lineup_json)
    return app


# ── 음성 파이프라인: 오디오 → VAD → STT (백그라운드 스레드) ─────
class VoicePipeline:
    def __init__(self, args, on_text):
        from audio_input import open_source
        from stt import KoreanSTT
        from vad import Segmenter, SileroVAD

        self.on_text = on_text
        self.audio_q = queue.Queue(maxsize=600)
        self.seg_q = queue.Queue()
        self.stop_ev = threading.Event()
        self.last_lag_warn = 0.0

        self.source = open_source(args)      # 장치가 틀렸으면 모델 로딩 전에 바로 알려줌
        self.segmenter = Segmenter(
            SileroVAD(cache_dir=str(HERE / "models")), self._on_segment,
            threshold=config.VAD_THRESHOLD,
            min_silence_ms=config.VAD_MIN_SILENCE_MS,
            max_segment_sec=config.VAD_MAX_SEGMENT_SEC,
            min_speech_ms=config.VAD_MIN_SPEECH_MS,
            pad_ms=config.VAD_PAD_MS,
        )
        self.stt = KoreanSTT(config)

    def _on_audio(self, chunk):            # 오디오 스레드
        try:
            self.audio_q.put_nowait(chunk)
        except queue.Full:
            pass

    def _on_segment(self, audio):          # VAD 스레드
        self.seg_q.put((audio, time.monotonic()))

    def _vad_loop(self):
        started, heard, warned = time.monotonic(), False, False
        while not self.stop_ev.is_set():
            try:
                chunk = self.audio_q.get(timeout=0.4)
            except queue.Empty:
                self.segmenter.flush()
                if not heard and not warned and time.monotonic() - started > 12:
                    warned = True
                    print("[오디오] 12초째 소리가 안 들어옵니다. 게임 소리가 나오는 장치가 맞는지 "
                          "python main.py --list-devices 로 확인하세요.")
                continue
            if not heard and np.abs(chunk).max() > 1e-4:
                heard = True
                print("[오디오] 소리 들어오는 중 ✓")
            self.segmenter.feed(chunk)

    def _stt_loop(self):
        gap = np.zeros(int(0.15 * SR), np.float32)
        while not self.stop_ev.is_set():
            try:
                segs = [self.seg_q.get(timeout=0.5)]
            except queue.Empty:
                continue
            while True:                     # 밀린 구간이 있으면 한 번에 묶어서 인식
                try:
                    segs.append(self.seg_q.get_nowait())
                except queue.Empty:
                    break
            keep, total = [], 0
            for audio, t in reversed(segs):  # 너무 밀렸으면 최신 것 위주로
                if keep and total + len(audio) > MAX_MERGE_SEC * SR:
                    break
                keep.append((audio, t))
                total += len(audio)
            keep.reverse()
            merged = np.concatenate([x for a, _ in keep for x in (a, gap)][:-1])
            try:
                text = self.stt.transcribe(merged)
            except Exception as e:
                print(f"[STT] 인식 오류: {e}")
                continue
            lag = time.monotonic() - keep[-1][1]
            if lag > 5 and time.monotonic() - self.last_lag_warn > 30:
                self.last_lag_warn = time.monotonic()
                print(f"[STT] 인식이 {lag:.0f}초 밀리고 있습니다. config.py 에서 STT_MODEL을 더 작게 "
                      "(예: \"small\") 하거나 STT_BEAM_SIZE = 1 로 바꿔보세요.")
            if text:
                self.on_text(text, {"lag": lag, "sec": len(merged) / SR})

    def start(self):
        threading.Thread(target=self._vad_loop, name="vad", daemon=True).start()
        threading.Thread(target=self._stt_loop, name="stt", daemon=True).start()
        self.source.start(self._on_audio)

    def stop(self):
        self.stop_ev.set()
        try:
            self.source.stop()
        except Exception:
            pass


def lineup_names(data) -> list:
    """lineup.json → 음성 인식 힌트용 이름 목록 (경기 정보 텍스트는 chat_brain.py 가 bus "lineup" 로 직접 구성)"""
    ko, en = [], []
    for side in ("home", "away"):
        players = (data.get(side) or {}).get("players") or []
        ko += [p[1] for p in players if len(p) > 1]
        en += [p[2] for p in players if len(p) > 2]
    # 힌트는 잘리므로 해설에 실제로 나오는 한국어 이름을 앞에
    return list(config.PLAYER_NAMES) + ko + en


async def watch_lineup(holder):
    """lineup.json 이 바뀌면 (자동 매핑·직접 수정 모두) 음성 인식 힌트에 반영하고 bus "lineup" 로 알림
    (chat_brain·audience 는 각자 이 이벤트를 구독해서 채팅·시청자 수에 반영함)"""
    path, seen, applied_stt = HERE / "lineup.json", None, False
    while True:
        await asyncio.sleep(0 if seen is None else 2)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        stt = holder.get("stt")
        if mtime == seen and (applied_stt or not stt):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:                  # 직접 고치다 JSON 문법이 틀린 경우 등
            print(f"[라인업] lineup.json 읽기 실패: {e}")
            seen = mtime
            continue
        names = lineup_names(data)
        if seen is not None and mtime != seen:
            print(f"[라인업] 이름 {len(names)}개를 음성 인식·채팅에 반영")
        seen = mtime
        bus.emit("lineup", data)
        if stt:
            stt.set_names(names)
            applied_stt = True


def _stdin_loop(on_text):
    print("\n해설 문장을 입력하고 Enter → 채팅이 생성됩니다. (종료: Ctrl+C)\n")
    for line in sys.stdin:
        text = line.strip()
        if text:
            on_text(text, {"lag": 0.0, "sec": 0.0})


# ── 실행 ────────────────────────────────────────────────────
async def autosave(book):
    while True:
        await asyncio.sleep(20)
        book.save()                             # 방송 중 채팅 기록 (강제 종료돼도 다음 방송 때 반영)


class StartupError(Exception):
    """시작할 수 없는 이유 (사용자에게 그대로 보여줌)"""


def _port_busy_message(port):
    pid = ""
    try:                                        # 포트를 잡고 있는 프로그램 번호 (Windows netstat)
        import subprocess
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, timeout=5).stdout.decode("mbcs", "ignore")
        pid = next((l.split()[-1] for l in out.splitlines() if f":{port} " in l and "LISTEN" in l), "")
    except Exception:
        pass
    who = f" (PID {pid})" if pid else ""
    return (f"포트 {port} 을(를) 다른 프로그램{who}이 쓰고 있습니다. 이 프로그램이 이미 켜져 있을 가능성이 큽니다.\n"
            f"       열려 있는 채팅 창/콘솔 창을 닫거나, 작업 관리자에서 python.exe{who} 를 종료한 뒤 다시 실행하세요.\n"
            f"       (다른 포트로 실행: 실행.bat --port 8766)")


async def welcome_members(book, brain, publish):
    """지난 라이브 기록으로 멤버십에 가입한 시청자 → 라이브 초반에 '새 멤버' 환영 메시지"""
    for name in book.new_members:
        await asyncio.sleep(random.uniform(40, 240))
        while not brain.ready:                  # 라인업 인식 전엔 채팅창이 조용해야 함
            await asyncio.sleep(2)
        publish({"type": "member", "name": name, "text": ""})
        print(f"      🟢 새 멤버 {name}")


async def amain(args, ui=None):
    from chat_brain import ChatBrain
    from viewers import Viewers

    loop = asyncio.get_running_loop()
    bus.bind(loop)                              # 다른 스레드에서 emit 해도 이 루프에서 처리
    if ui:                                      # 창을 닫으면 여기도 정리하고 끝냄
        me = asyncio.current_task()
        ui.on_close = lambda: loop.call_soon_threadsafe(me.cancel)
    hub = Hub(ui)
    bus.on("viewers", lambda n: hub.publish({"type": "viewers", "count": n}))
    runner = web.AppRunner(build_app(hub))
    await runner.setup()
    try:                                        # 서버부터 (실패하면 라이브 회차가 안 올라가게)
        await web.TCPSite(runner, config.HOST, args.port).start()
    except OSError:
        await runner.cleanup()
        raise StartupError(_port_busy_message(args.port))

    book = Viewers(config)
    await book.start_show()                    # 지난 방송 채팅 기록 → 단골 갱신 (Haiku 1회)
    brain = ChatBrain(config, hub.publish, book)
    url = f"http://{config.HOST}:{args.port}/"
    print(f"\n[오버레이] 브라우저: {url}\n[오버레이] OBS 브라우저 소스: {url}?transparent=1")
    print(f"[라인업]   {url}lineup?transparent=1   (lineup.json 수정하면 자동 반영)\n")

    def handle_text(text, meta):
        print(f"🎙  {text}   (말 끝난 뒤 {meta['lag']:.1f}s)" if meta["lag"] else f"🎙  {text}")
        bus.emit("commentary", text)            # chat_brain·audience 가 각자 구독 (채팅 반응, 골·종료 감지)
        hub.publish({"type": "commentary", "text": text})

    def on_text(text, meta):                # 어느 스레드에서 불려도 안전하게
        loop.call_soon_threadsafe(handle_text, text, meta)

    def build_pipeline():
        p = VoicePipeline(args, on_text)
        p.start()                             # 오디오 장치 초기화·시작을 같은 스레드에서 (WASAPI 안정성)
        return p

    audience = None
    if config.FANS == "auto":
        from audience import Audience
        audience = brain.audience = Audience(config)

    # 채팅 송출 조건: ① 커리어 홈(두 팀·내 팀) ② 스쿼드(우리 선발) ③ 예상 라인업(상대 선발)을 이번 실행에서 읽음
    #                ④ 경기 중 (왼쪽 위 스코어보드 시계가 보임) — 우클릭 '채팅 바로 시작'이면 전부 건너뜀
    watching = config.LINEUP_WATCH and not args.no_watch
    gate = watching and not args.text
    need_lineup = gate and config.WAIT_FOR_LINEUP
    need_live = gate and config.LIVE_ONLY
    got = {"match": False, "me": False, "opp": False}
    st = {"live": False, "forced": False, "game": "ok", "matchday": False, "paused": False}
    steps = {"match": "커리어 홈(센트럴)", "me": "팀 관리 → 스쿼드", "opp": "상대 분석 → 예상 라인업", "live": "경기 시작(킥오프)"}

    def update():
        # 킥오프(경기 화면)가 보이면 무조건 송출 — 다시 켰을 때 이미 매핑된 라인업(lineup.json)은 읽은 걸로 간주
        lineup_ok = st["forced"] or not need_lineup or all(got.values()) or st["live"]
        live_ok = st["forced"] or not need_live or st["live"]
        was = brain.ready
        brain.ready = lineup_ok and live_ok and not st["paused"]
        hub.publish({"type": "cover", "on": st["paused"]})    # 일시정지 중엔 채팅을 가림
        sides = None
        if not brain.ready:
            sides = {k: v for k, v in got.items()} if need_lineup and not st["forced"] else {}
            if need_live:
                sides["live"] = st["live"]
        hub.publish({"type": "waiting", "sides": sides})
        # 요청: 게임 창 > 매치데이 '상대 분석'
        req = ""
        if st["paused"]:
            req = "경기 일시정지 중 — ‘경기 재개’를 눌러 주세요"
        elif watching and st["game"] == "missing":
            req = "FC 26 을 화면에 띄워 주세요 (최소화돼 있거나 꺼져 있음)"
        elif watching and st["game"] == "covered":
            req = "FC 26 을 앞으로 띄워 주세요 (다른 프로그램이 게임을 가리고 있음)"
        elif need_lineup and st["matchday"] and not got["opp"]:
            req = "‘상대 분석’을 눌러 주세요 → 상대 선발을 읽습니다"
        hub.publish({"type": "request", "text": req})
        if brain.ready and not was:
            print("[송출] 채팅 시작!\n")
        elif was and not brain.ready:
            print("[송출] 채팅 멈춤 (일시정지)" if st["paused"] else "[송출] 채팅 멈춤 (경기 중이 아님)" if lineup_ok
                  else "[송출] 채팅 멈춤 (새 경기 라인업 대기)")
        elif not brain.ready:
            left = [steps[k] for k, v in (sides or {}).items() if not v]
            print(f"[대기] 남은 것: {', '.join(left)}" + (f"  ← {req}" if req else ""))

    def on_scanned(who, changed, matchday):     # bus "screen_read" (화면 감시 스레드가 emit → 이 루프에서 실행)
        if who == "match":
            st["matchday"] = matchday
            if changed:
                tracker.reset()                  # 새 경기 → 스코어·득점 기록 새로
            if changed and got["match"]:
                got.update(me=False, opp=False)  # 다음 경기로 넘어감 → 새 선발을 읽을 때까지 채팅 멈춤
                st["forced"] = False
                print("[대기] 새 경기 → 새 라인업을 읽을 때까지 채팅을 멈춥니다")
        got[who] = True
        if all(got.values()) and audience:
            asyncio.create_task(audience.refresh())
        update()

    def on_live(live):                          # bus "live" (goals·audience·chat_brain 도 각자 구독함)
        st["live"] = live
        if live and not all(got.values()):
            print("[송출] 킥오프 감지 → 지금 lineup.json 라인업으로 채팅 시작")
            if audience:
                asyncio.create_task(audience.refresh())
        if not live:
            st["paused"] = False
        update()

    def on_pause(p):                            # bus "pause"
        st["paused"] = p
        update()

    def on_status(status):                      # bus "game"
        st["game"] = status
        update()

    from goals import GoalTracker
    tracker = GoalTracker(config, lambda: watcher.board if watcher else None)

    def force_start():
        if not brain.ready:
            st["forced"] = True
            print("[송출] 직접 시작 (대기 건너뜀)")
            if audience:
                asyncio.create_task(audience.refresh())
            update()

    bus.on("force_start", force_start)          # 채팅 창 우클릭 → '채팅 바로 시작' (rescan 은 screen_watch 가 자체 구독)
    brain.ready = not gate                      # 시작할 때 '채팅 멈춤'이 찍히지 않게
    update()

    holder = {}
    lineup_task = asyncio.create_task(watch_lineup(holder))
    watcher = None
    if watching:
        import screen_watch
        if ui:                                  # 캡처하는 순간에만 우리 창을 캡처에서 뺌 (화면에는 그대로)
            screen_watch.EXCLUDE_FROM_CAPTURE = ui.hwnds()
        bus.on("screen_read", on_scanned)
        bus.on("live", on_live)
        bus.on("pause", on_pause)
        bus.on("game", on_status)
        watcher = screen_watch.ScreenWatcher()
        watcher.start()

    pipeline = None
    if args.text:
        threading.Thread(target=_stdin_loop, args=(on_text,), daemon=True).start()
    else:
        pipeline = await asyncio.to_thread(build_pipeline)  # 모델 로딩 동안에도 서버는 계속 동작
        holder["stt"] = pipeline.stt
        print("[준비 완료] 음성 인식 준비 끝." + (" 경기·라인업을 읽고 경기가 시작되면 채팅이 올라옵니다.\n" if not brain.ready
                                              else " 해설이 들리면 채팅이 올라옵니다.\n"))

    try:
        from prompt_coach import PromptCoach
        tasks = [hub.run(), brain.run(), brain.emitter(), lineup_task, autosave(book),
                 welcome_members(book, brain, hub.publish), PromptCoach(config, brain).run(), tracker.run()]
        if audience:
            tasks.append(audience.ticker())
        await asyncio.gather(*tasks)
    finally:
        book.save()
        if watcher:
            watcher.stop()
        if pipeline:
            pipeline.stop()
        await runner.cleanup()


def main():
    try:
        sys.stdout.reconfigure(errors="replace")   # 콘솔 인코딩 문제로 죽지 않게
    except Exception:
        pass
    load_dotenv(HERE / ".env", override=True)

    ap = argparse.ArgumentParser(description="FC 26 해설 → AI 가상 라이브 채팅 (Claude Haiku)")
    ap.add_argument("--list-devices", action="store_true", help="캡처 가능한 오디오 장치 목록")
    ap.add_argument("--device", help="장치 번호 또는 이름 일부 (기본: 기본 스피커 루프백)")
    ap.add_argument("--file", help="녹화 파일로 테스트 (mp4, mkv, mp3, wav …)")
    ap.add_argument("--text", action="store_true", help="해설을 직접 입력해서 테스트 (음성 인식 없음)")
    ap.add_argument("--no-watch", action="store_true", help="화면 감시(라인업 자동 매핑) 끄기")
    ap.add_argument("--no-window", action="store_true", help="채팅·라인업 창을 띄우지 않음 (OBS 브라우저 소스만)")
    ap.add_argument("--port", type=int, default=config.PORT)
    args = ap.parse_args()

    if args.list_devices:
        from audio_input import list_devices
        list_devices()
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY 가 없습니다. .env.example 을 .env 로 복사하고 키를 넣어주세요.")

    if not config.SHOW_WINDOWS or args.no_window:
        try:
            asyncio.run(amain(args))
        except KeyboardInterrupt:
            print("\n종료합니다.")
        except StartupError as e:
            sys.exit(f"[시작 실패] {e}")
        return

    # 창(tkinter)은 메인 스레드, 음성 인식·채팅 생성은 백그라운드 스레드
    from overlay_app import OverlayApp
    ui = OverlayApp(config)
    failed = []

    def backend():
        try:
            asyncio.run(amain(args, ui))
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            pass
        except StartupError as e:
            failed.append(e)
            print(f"[시작 실패] {e}")
        except Exception as e:
            failed.append(e)
            print(f"[오류] {type(e).__name__}: {e}")
        finally:
            ui.quit()                           # 백엔드가 끝나면 창도 닫음

    t = threading.Thread(target=backend, name="backend", daemon=True)
    t.start()
    try:
        ui.run()
    except KeyboardInterrupt:
        pass
    if ui.on_close:
        try:
            ui.on_close()                       # 창을 닫음 → 백엔드 정리
        except RuntimeError:                    # 백엔드가 먼저 끝난 경우
            pass
    t.join(timeout=5)
    if failed:
        sys.exit(1)                             # 실행.bat 이 창을 닫지 않고 메시지를 보여주게
    print("\n종료합니다.")


if __name__ == "__main__":
    main()
