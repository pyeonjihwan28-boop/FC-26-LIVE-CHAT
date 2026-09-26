"""
FC 26 가상 라이브 채팅 — 설정 (튜닝은 대부분 이 파일만)
"""
from blocks import store as _store

# ── 오버레이 서버 (main.py) ──────────────────────────────────────────
HOST = "127.0.0.1"
PORT = 8765

# ── 방송 배경: 시청자들은 '스포츠 채널 유튜브의 실제 축구 생중계 라이브'로 보고 있음 ─────────
CHANNEL_NAME = "스포츠TV"      # 중계 채널 이름
PROGRAM_NAME = ""              # 중계 프로그램 이름 (비우면 "축구 생중계")
FANS = "auto"                  # "auto" 두 팀 전 세계 팬 수로 시청자 수·팬 구성 계산 / "mixed" 반반 / "neutral" 중립 위주
EXTRA_CONTEXT = ""             # 이 경기의 배경 (예: "우승 경쟁이 걸린 엘 클라시코")
MY_TEAM_NAME = ""              # 내 팀 (커리어 홈 화면에서 자동으로 채워짐. 우클릭 → 설정에서 직접 입력도 가능)
MY_TEAM_SHORT = ""
MY_SIDE = "home"               # 내 팀 자리 "home"/"away" (자동으로 채워짐)

# ── 화면 인식 (screen_watch.py) ──────────────────────────────────────
LINEUP_WATCH = True            # 화면 감시 켜기
WAIT_FOR_LINEUP = True         # 경기 정보·양 팀 선발을 읽기 전엔 채팅 안 함 (킥오프가 보이면 이미 있는 라인업으로 시작)
LIVE_ONLY = True               # 채팅은 경기 중(왼쪽 위 스코어보드 시계가 보일 때)에만
LIVE_GRACE_SEC = 90            # 스코어보드가 이만큼 안 보여도 경기 중으로 봄 (리플레이·하프타임)
SCORE_CHECK_SEC = 90           # 경기 중 스코어보드 확인 간격 (멘트를 놓친 골 대비, 1회 약 0.1센트). 0 = 골 멘트 때만
WATCH_INTERVAL_SEC = 1.0       # 화면 확인 간격 (게임 프레임이 떨어지면 늘리기)
WATCH_WINDOW = r"EA SPORTS FC™? ?26|\bFC ?26\.exe"   # 게임 창 (제목·exe 정규식). "" = 주 모니터 전체 (캡처보드 등)
WATCH_KEYWORDS = {             # 화면 판별 글자 (전부 있어야 함, 띄어쓰기 무시, "A|B" = A 또는 B)
    "pause": ["선발 라인업", "벤치", "체력|평점|게임 플랜"],                   # 경기 중 일시정지 팀 관리
    "opp": ["예상 라인업"],                                                   # 상대 분석 → 예상 라인업
    "me": ["스쿼드", "선수 정보|교체"],                                       # 팀 관리 → 스쿼드
    "match": ["다음 경기|경기까지|매치데이", "아카데미|오피스|커스터마이징|센트럴"],  # 커리어 홈(센트럴)
}

# ── Claude ─────────────────────────────────────────────────────────
CLAUDE_MODEL = "claude-haiku-4-5-20251001"   # 채팅·화면 읽기
COACH_MODEL = "claude-opus-5-5"              # 자가 개선(채팅 평가)만 이 모델
COACH_EFFORT = "medium"                      # Opus 5.5 생각 깊이 (low / medium / high)
PRICES = {CLAUDE_MODEL: (1.0, 5.0), "claude-opus-5-5": (4.0, 20.0)}   # 비용 표시용 $/100만 토큰 (입력, 출력)
PRICE_IN_PER_MTOK, PRICE_OUT_PER_MTOK = PRICES[CLAUDE_MODEL]
MAX_TOKENS = 1500              # 채팅 묶음 한 번의 최대 길이

PROMPT_COACH = True            # 경기 중 채팅을 평가해서 prompt_template.md [LEARNED] 규칙을 스스로 고침
COACH_EVERY_SEC = 30           # 평가 간격 (Opus 5.5, 1회 약 3~6센트 → 시간당 약 4~7달러)
COACH_MIN_CHATS = 8            # 지난 평가 뒤 채팅이 이만큼은 쌓여야 평가
COACH_MAX_RULES = 15           # 규칙 최대 개수 (넘으면 오래된 것부터)

# ── 채팅 속도 ──────────────────────────────────────────────────────
# 분당 메시지 = 12 × √(시청자 ÷ 1000) × 장면 배율 (골 3배·결정적 장면 1.6배·하프타임 0.6배), 최대 CHAT_RATE_MAX
CHAT_RATE_MAX = 90             # 분당 최대 메시지 수 (올리면 비용도 비례해서 늘어남)
AMBIENT_BATCH = 15             # 멘트 없을 때 한 번에 미리 만들어 둘 흐름 채팅 수
PREFETCH_SEC = 8               # 대기열이 이만큼(초) 치만 남으면 다음 묶음을 미리 만듦
MIN_CALL_GAP_SEC = 1.2         # 채팅 생성 호출 최소 간격
IDLE_STOP_AFTER_MIN = 5        # 멘트가 이만큼 계속 없으면 흐름 채팅도 멈춤 (자리 비움 = 비용 0)
MAX_BACKLOG = 60               # 대기 메시지 최대 (넘으면 오래된 것부터 버림)
STALE_AFTER_SEC = 14           # 반응 채팅은 이만큼, 흐름 채팅은 3배 지나면 버림 (뒷북 방지)
DONATION_COOLDOWN_SEC = 45     # 슈퍼챗 최소 간격
MEMBER_JOIN_COOLDOWN_SEC = 240 # 멤버십 가입 최소 간격

# ── 음성 인식 (faster-whisper, 로컬) ───────────────────────────────
STT_MODEL = "small"            # auto → GPU: large-v3-turbo / CPU: small
STT_DEVICE = "auto"            # auto / cuda / cpu
STT_COMPUTE_TYPE = "auto"      # auto → GPU: float16 / CPU: int8
STT_BEAM_SIZE = 1              # 0 = 자동 (GPU 5, CPU 1). 낮을수록 빠름
STT_CPU_THREADS = 4
LANGUAGE_GUARD = True          # 한국어가 아닌 소리(메뉴 음악 영어 가사 등)는 무시
PLAYER_NAMES = []              # 인식 힌트 (lineup.json 선수 이름은 자동으로 들어감 → 그 밖의 이름만)
STT_PROMPT = "축구 경기 중계 해설입니다. 슈팅! 골입니다! 크로스, 코너킥, 프리킥, 페널티킥, 오프사이드, 선방."
VAD_THRESHOLD = 0.5            # 올리면 관중 소음에 덜 반응
VAD_MIN_SILENCE_MS = 450
VAD_MAX_SEGMENT_SEC = 7.0
VAD_MIN_SPEECH_MS = 300
VAD_PAD_MS = 200
# ── 음성 전처리 (잡음 제거) — 관중 함성 속 해설 명료도 ↑, STT 환각 ↓ (denoise.py) ──
AUDIO_DENOISE = "spectral"   # off / spectral(기본, 의존성 없음) / noisereduce / rnnoise / deepfilternet(pip설치필요) / auto
AUDIO_DENOISE_STRENGTH = 1.0 # 0.5~2.0. 관중 함성이 심하면 1.5, 해설이 뭉개지면 0.7
VAD_ENERGY_GATE = 0.0        # RMS가 이보다 작은 오디오 조각은 Silero VAD에 넣기 전에 버림 (CPU 절약·오검출 감소). 0=끔, 보통 0.001~0.005
# ── 스코어보드 로컬 OCR — 골 확인 때 Haiku 대신 Windows OCR로 먼저 읽어 비용 절감 (scoreboard_ocr.py) ─
SCOREBOARD_LOCAL_OCR = True  # True: 로컬 OCR 먼저 시도, 실패하면 Haiku fallback. False: 항상 Haiku
# ── STT 환각 억제 (faster-whisper 신뢰도 기반 추가 필터, stt.py) ─────────────────
STT_MIN_LOGPROB = -1.2       # 세그먼트 평균 로그확률이 이보다 낮으면 환각으로 간주 (-1.5~-0.8, 높을수록 엄격)
STT_MAX_NOSPEECH_PROB = 0.6  # no_speech_prob 이보다 높으면 무음/소음으로 간주
STT_REPETITION_FILTER = True # 같은 단어·구절 반복 환각 걸러냄
# ── 스트리밍 중간 인식 — 말하는 중에도 주기적으로 인식해 자막 표시·골 조기 감지 (지연 감소) ──
STT_STREAMING = True         # True: 중간 인식 사용. 최종 인식은 기존 VAD 세그먼트 그대로
STT_PARTIAL_INTERVAL = 2.5   # 중간 인식 간격(초). 짧을수록 빠르지만 STT 부하↑ (2.0~4.0 권장)

# ── 창 (overlay_app.py) — OBS에선 '윈도우 캡처'. 창을 닫으면 프로그램 종료 ────────────
SHOW_WINDOWS = True            # 채팅·라인업 창을 띄움 (False 나 --no-window 면 OBS 브라우저 소스만)
WINDOW_SCALE = 1.0
WINDOW_CHAT_SIZE = (400, 700)
WINDOW_FONT_SIZE = 14
WINDOW_HEADER = True
WINDOW_CAPTION = False         # 인식된 해설 문장을 채팅 창 위에 표시 (튜닝용)
WINDOW_LINEUP = True
WINDOW_LINEUP_SIDE = ""        # "" 두 팀 / "home" / "away"
WINDOW_TOPMOST = True          # 항상 위 (게임은 '테두리 없는 창' 모드여야 위에 보임)

# ── 자주 오는 시청자 (처음 실행 때 viewers.json 을 이걸로 만듦, 이후엔 라이브마다 Haiku가 늘려감) ──
# role: "" 일반 / "member" 멤버십(초록 이름) / "mod" 매니저(파란 이름). 채팅창엔 '단골' 표시 없음
VIEWERS = [
    {"name": "해축고인물", "trait": "20년째 해외축구 새벽 시청, 옛날 레전드와 비교", "role": "member"},
    {"name": "전술노트", "trait": "포메이션·압박·교체 타이밍 훈수", "role": "member"},
    {"name": "심판규탄위원회", "trait": "판정마다 불만, VAR 보라고 외침", "role": ""},
    {"name": "킹갓골잡이", "trait": "과몰입, 슈팅만 나와도 느낌표", "role": ""},
    {"name": "ㅇㅇ", "trait": "초성만 침", "role": ""},
    {"name": "출근전에보는중", "trait": "시각에 맞는 생활 얘기 (잠·출근·맥주)", "role": ""},
    {"name": "치킨시킴", "trait": "야식 얘기로 딴소리", "role": ""},
    {"name": "축알못입니다", "trait": "규칙·선수를 잘 몰라서 짧게 질문", "role": ""},
    {"name": "원정석", "trait": "채팅창 다수의 반대편 팀 팬, 티격태격", "role": ""},
    {"name": "국뽕한스푼", "trait": "한국 선수만 봄", "role": ""},
    {"name": "스포츠TV 매니저", "trait": "채널 매니저. 아주 가끔 매너·스포일러 공지", "role": "mod"},
]
NEW_REGULARS_PER_SHOW = 3
MAX_REGULARS_IN_PROMPT = 20

# ── 자동으로 채워지는 값 (settings.json) ─────────────────────────────
SETTINGS_KEYS = ("MY_TEAM_NAME", "MY_TEAM_SHORT", "MY_SIDE")
globals().update({k: v for k, v in (_store.load("settings.json") or {}).items() if k in SETTINGS_KEYS})


def save_settings(**values):
    """커리어 홈 화면 인식·설정 창에서 호출: settings.json 에 저장하고 실행 중에도 바로 반영"""
    globals().update({k: v for k, v in values.items() if k in SETTINGS_KEYS})
    _store.save("settings.json", {k: globals()[k] for k in SETTINGS_KEYS})
