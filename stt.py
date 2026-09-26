"""
한국어 해설 음성 인식 (faster-whisper, 로컬 실행)
- GPU(NVIDIA)가 있으면 large-v3-turbo, 없으면 small 모델을 자동 선택
- Whisper가 한국어 무음/소음에서 자주 만들어내는 '환각 문장'을 걸러냄
"""
import difflib
import os
import re

import numpy as np

SR = 16000
_DLL_HANDLES = []

# 한국어 Whisper가 소음·무음 구간에서 자주 지어내는 문장들
HALLUCINATIONS = [re.compile(p) for p in (
    r"시청\s*해?\s*주셔서\s*감사",
    r"구독.{0,8}좋아요|좋아요.{0,8}구독",
    r"알림\s*설정",
    r"다음\s*(영상|시간|편)에서?\s*(만나|뵙)",
    r"(MBC|KBS|SBS|YTN|JTBC|EBS)\s*뉴스",
    r"뉴스\s*[가-힣]{2,4}\s*입니다",
    r"자막\s*(제공|제작|by)|한글\s*자막",
    r"^\W*(감사합니다|고맙습니다|네|예|아|음|어|오|응)\W*$",
)]


def _register_cuda_dlls():
    """Windows: pip로 설치한 NVIDIA 라이브러리(cuBLAS·cuDNN)를 CTranslate2가 찾을 수 있게 경로 등록"""
    if os.name != "nt":
        return
    try:
        import nvidia  # nvidia-cublas-cu12 / nvidia-cudnn-cu12 가 설치돼 있으면 존재
    except ImportError:
        return
    for base in list(getattr(nvidia, "__path__", [])):
        for sub in ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime"):
            d = os.path.join(base, sub, "bin")
            if os.path.isdir(d):
                try:
                    _DLL_HANDLES.append(os.add_dll_directory(d))
                except OSError:
                    pass
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


class KoreanSTT:
    def __init__(self, cfg):
        _register_cuda_dlls()
        import ctranslate2

        self.cfg = cfg
        self.language_guard = cfg.LANGUAGE_GUARD
        prompt = cfg.STT_PROMPT.strip()
        if cfg.PLAYER_NAMES:
            prompt += " " + ", ".join(cfg.PLAYER_NAMES) + "."
        self.prompt = prompt
        self.hotwords = None                        # 라인업 선수 이름 (set_names)

        device = cfg.STT_DEVICE
        if device == "auto":
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        try:
            self._load(device)
            self._decode(np.zeros(SR, np.float32), "ko")  # 워밍업: 라이브러리 문제를 시작할 때 바로 드러냄
        except Exception as e:
            if device != "cuda":
                raise
            print(f"[STT] GPU로 실행 실패 → CPU로 전환합니다.\n      원인: {e}\n"
                  "      (GPU를 쓰려면 README의 'GPU 설정'을 확인하세요)")
            self._load("cpu")

    def set_names(self, names: list[str]):
        """경기마다 바뀌는 선수 이름(한국어·영어)을 인식 힌트로. 다른 스레드에서 불러도 됨"""
        self.hotwords = ", ".join(dict.fromkeys(n.strip() for n in names if n.strip())) or None

    def _load(self, device):
        from faster_whisper import WhisperModel

        c = self.cfg
        on_gpu = device == "cuda"
        model = c.STT_MODEL if c.STT_MODEL != "auto" else ("large-v3-turbo" if on_gpu else "small")
        compute = c.STT_COMPUTE_TYPE if c.STT_COMPUTE_TYPE != "auto" else ("float16" if on_gpu else "int8")
        self.beam = c.STT_BEAM_SIZE or (5 if on_gpu else 1)
        print(f"[STT] faster-whisper '{model}' ({device}, {compute}) 로딩… (첫 실행은 모델 다운로드)")
        self.model = WhisperModel(model, device=device, compute_type=compute, cpu_threads=c.STT_CPU_THREADS)
        self.device = device

    def _decode(self, audio, language):
        segments, info = self.model.transcribe(
            audio,
            language=language,
            task="transcribe",
            beam_size=self.beam,
            temperature=0.0,
            condition_on_previous_text=False,   # 반복 환각 방지
            initial_prompt=self.prompt,
            hotwords=self.hotwords,             # 초기 프롬프트와 별도 칸 (최대 223토큰, 넘치면 뒤쪽이 잘림)
            without_timestamps=True,
            vad_filter=False,                   # 이미 VAD로 잘라서 넣음
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
        )
        segs = [s for s in segments if s.text.strip()]
        text = " ".join(s.text.strip() for s in segs)
        avg_logprob = sum(s.avg_logprob for s in segs) / len(segs) if segs else -99.0
        no_speech_prob = max((s.no_speech_prob for s in segs), default=1.0)
        return text, info, avg_logprob, no_speech_prob

    def transcribe(self, audio: np.ndarray) -> str | None:
        min_lp = getattr(self.cfg, "STT_MIN_LOGPROB", -99.0)
        max_ns = getattr(self.cfg, "STT_MAX_NOSPEECH_PROB", 1.0)
        if self.language_guard:
            text, info, avg_lp, ns_p = self._decode(audio, None)
            if info.language != "ko":
                if info.language_probability >= 0.5:
                    return None                  # 확실히 한국어가 아님 (메뉴 음악 가사 등)
                text, _, avg_lp, ns_p = self._decode(audio, "ko")  # 애매하면 한국어로 다시
        else:
            text, _, avg_lp, ns_p = self._decode(audio, "ko")
        if avg_lp < min_lp or ns_p > max_ns:
            return None                          # 저신뢰 → 환각으로 간주 (신뢰도 기반 필터)
        return self.clean(text, len(audio) / SR)

    def clean(self, text: str, seconds: float) -> str | None:
        t = re.sub(r"\s+", " ", text).strip()
        if len(t.replace(" ", "")) > max(14, seconds * 14):
            return None                              # 길이에 비해 글자가 너무 많음 = 환각
        t = re.sub(r"(.)\1{5,}", r"\1\1\1", t)      # "아아아아아아아" → "아아아"
        if len(t.replace(" ", "")) < 2:
            return None
        if getattr(self.cfg, "STT_REPETITION_FILTER", False):
            words = re.findall(r"[가-힣A-Za-z]{2,}", t)
            if len(words) >= 6 and len(set(words)) * 3 < len(words):
                return None                          # 같은 단어 반복 환각
            if re.search(r"(.{4,}?)\1{2,}", t + " "):  # 같은 4음절 이상 구절 3회 이상 반복 (마지막 반복 뒤 구분자가
                return None                             # 없어서 매칭이 안 되는 걸 막으려고 끝에 공백 하나 붙여서 검사)
        if any(p.search(t) for p in HALLUCINATIONS):
            return None
        for hint in (self.prompt, self.hotwords or ""):
            if hint and (t in hint or difflib.SequenceMatcher(None, t, hint).ratio() > 0.6):
                return None                          # 힌트 문장·선수 목록을 그대로 읊은 환각
        return t
