"""
해설 목소리 구간 잘라내기 (Silero VAD, 32ms 프레임 단위 스트리밍)

관중 함성이 계속 깔려 있어서 '소리 크기' 기준으로는 말소리를 구분할 수 없습니다.
그래서 신경망 VAD(Silero)로 '사람 말소리일 확률'을 보고,
해설이 잠깐 멈추는 순간마다 한 마디씩 잘라 음성 인식으로 넘깁니다.
"""
import urllib.request
from collections import deque
from pathlib import Path

import numpy as np

SR = 16000
FRAME = 512    # Silero 고정 입력 크기 (32ms)
CONTEXT = 64   # 직전 프레임 끝 64샘플을 앞에 붙여서 넣어야 함
OFFICIAL_URL = "https://github.com/snakers4/silero-vad/raw/v5.1.2/src/silero_vad/data/silero_vad.onnx"


class SileroVAD:
    """faster-whisper에 들어있는 Silero 모델을 우선 쓰고, 없으면 공식 모델을 받아서 씀"""

    def __init__(self, cache_dir: str = "models"):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 3

        self.kind = None
        for path in self._candidates(cache_dir):
            sess = ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])
            inputs = {i.name: i.shape for i in sess.get_inputs()}
            if {"input", "h", "c"} <= inputs.keys() and list(inputs["h"]) == [1, 1, 128]:
                self.kind = "hc"      # faster-whisper 1.2+ 내장 모델 (silero v6)
            elif {"input", "state", "sr"} <= inputs.keys():
                self.kind = "state"   # 공식 배포 모델 (silero v5/v6)
            if self.kind:
                self.sess = sess
                break
        if not self.kind:
            raise RuntimeError("사용할 수 있는 Silero VAD 모델을 찾지 못했습니다.")
        self._sr = np.array(SR, dtype=np.int64)
        self.reset()

    @staticmethod
    def _candidates(cache_dir):
        try:
            from faster_whisper.utils import get_assets_path
            yield from sorted(Path(get_assets_path()).glob("silero_vad*.onnx"), reverse=True)
        except Exception:
            pass
        dst = Path(cache_dir) / "silero_vad.onnx"
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            print("[VAD] Silero VAD 모델 내려받는 중…")
            urllib.request.urlretrieve(OFFICIAL_URL, dst)
        yield dst

    def reset(self):
        self.h = np.zeros((1, 1, 128), np.float32)
        self.c = np.zeros((1, 1, 128), np.float32)
        self.state = np.zeros((2, 1, 128), np.float32)
        self.ctx = np.zeros(CONTEXT, np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        """512샘플(32ms) → 말소리 확률 0~1"""
        x = np.concatenate([self.ctx, frame]).astype(np.float32, copy=False)[None, :]
        self.ctx = frame[-CONTEXT:].astype(np.float32, copy=True)
        if self.kind == "hc":
            out, self.h, self.c = self.sess.run(None, {"input": x, "h": self.h, "c": self.c})
        else:
            out, self.state = self.sess.run(None, {"input": x, "state": self.state, "sr": self._sr})
        return float(np.ravel(out)[0])


class Segmenter:
    """VAD 확률로 '해설 한 마디' 단위 오디오를 잘라 on_segment(audio)로 넘김"""

    def __init__(self, vad, on_segment, threshold=0.5, min_silence_ms=450,
                 max_segment_sec=7.0, min_speech_ms=300, pad_ms=200):
        frame_ms = FRAME * 1000 / SR
        self.vad = vad
        self.on_segment = on_segment
        self.th = threshold
        self.neg_th = max(threshold - 0.15, 0.01)   # 히스테리시스: 시작보다 낮은 기준으로 '계속 말하는 중' 판단
        self.min_silence = max(1, round(min_silence_ms / frame_ms))
        self.max_frames = max(1, round(max_segment_sec * 1000 / frame_ms))
        self.min_speech = max(1, round(min_speech_ms / frame_ms))
        self.pad = max(0, round(pad_ms / frame_ms))
        self.pre = deque(maxlen=max(1, self.pad))   # 말 시작 직전 소리 (첫 음절 보호)
        self.leftover = np.zeros(0, np.float32)
        self._clear()

    def _clear(self):
        self.active = False
        self.frames = []
        self.speech = 0
        self.silence = 0

    def feed(self, audio: np.ndarray):
        buf = np.concatenate([self.leftover, audio]) if len(self.leftover) else audio
        n = len(buf) // FRAME
        for i in range(n):
            self._step(buf[i * FRAME:(i + 1) * FRAME])
        self.leftover = buf[n * FRAME:].copy()

    def _step(self, f):
        p = self.vad(f)
        if not self.active:
            if p >= self.th:
                self.active = True
                self.frames = list(self.pre) + [f]
                self.pre.clear()
                self.speech, self.silence = 1, 0
            elif self.pad:
                self.pre.append(f)
            return

        self.frames.append(f)
        if p >= self.th:
            self.speech += 1
        self.silence = self.silence + 1 if p < self.neg_th else 0

        if self.silence >= self.min_silence:
            self._emit(drop_tail=self.silence - self.pad)
        elif len(self.frames) >= self.max_frames:
            self._emit(drop_tail=0, keep_going=True)   # 쉬지 않고 떠드는 중 → 일단 끊어서 보냄

    def _emit(self, drop_tail=0, keep_going=False):
        frames = self.frames[:len(self.frames) - drop_tail] if drop_tail > 0 else self.frames
        if frames and self.speech >= self.min_speech:
            self.on_segment(np.concatenate(frames))
        self._clear()
        self.active = keep_going

    def flush(self):
        """소리가 끊겼을 때 (루프백은 재생 중인 소리가 없으면 데이터가 안 옴) 진행 중인 구간 마무리"""
        if self.active:
            self._emit(drop_tail=max(0, self.silence - self.pad))
