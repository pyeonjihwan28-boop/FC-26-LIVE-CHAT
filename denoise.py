"""
실시간 잡음 제거 전처리 — VAD/STT 앞에 넣어 관중 함성·배경 소음 속 해설 명료도를 높임
  (참고: Rikorose/DeepFilterNet, xiph/rnnoise, timsainb/noisereduce)

백엔드 (config.AUDIO_DENOISE 로 선택, 없으면 자동으로 아래로 fallback → 최종 통과):
  deepfilternet  pip install deepfilternet  — 딥러닝, 성능 가장 좋음 (48kHz 내부, soxr 로 리샘플)
  rnnoise        pip install rnnoise        — 초저지연 DSP+RNN (48kHz 10ms 프레임)
  noisereduce    pip install noisereduce    — 정지성 잡음, 청크 단위 처리
  spectral       numpy만으로 동작하는 스트리밍 스펙트럴 서브트랙션 (기본, 의존성 없음)
  off            통과 (안 씀)

사용:
  from denoise import build_denoiser
  d = build_denoiser("auto", 16000, strength=1.0)
  clean_chunk = d.process(chunk)   # chunk: 16kHz 모노 float32 numpy 배열, 길이 가변
"""
import numpy as np

SR_DEFAULT = 16000


class _Passthrough:
    kind = "off"

    def process(self, chunk):
        return np.asarray(chunk, np.float32).ravel()


class SpectralDenoiser:
    """스트리밍 스펙트럴 서브트랙션 (overlap-add, hann 75% overlap). 의존성 없음.
    묵음 구간에서 잡음 스펙트럼을 추정해 말하는 구간에서 빼낸다. 관중 함성처럼 비정상성 잡음은
    완벽히 못 없애도, 해설이 없는 구간을 조용히 만들어 VAD 오검출과 STT 환각을 줄인다."""
    kind = "spectral"

    def __init__(self, sr=16000, strength=1.0, floor=0.1):
        self.N, self.H = 512, 128                     # 32ms 프레임, 8ms hop
        self.win = np.hanning(self.N).astype(np.float32)
        self.cola = 1.5                                # hann^2 를 75% overlap 으로 더하면 1.5
        self.beta = float(strength)                    # over-subtraction (클수록 강하게 제거)
        self.floor = float(floor)                      # gain floor (musical noise 억제)
        self.noise = np.full(self.N // 2 + 1, 1e-6, np.float32)
        self.in_buf = np.zeros(0, np.float32)
        self.acc = np.zeros(self.N, np.float32)
        self.out = np.zeros(0, np.float32)

    def process(self, chunk):
        self.in_buf = np.concatenate([self.in_buf, np.asarray(chunk, np.float32).ravel()])
        while len(self.in_buf) >= self.N:
            frame = self.in_buf[:self.N]
            self.in_buf = self.in_buf[self.H:]
            X = np.fft.rfft(frame * self.win)
            mag = np.abs(X)
            P = (mag * mag).astype(np.float32)
            rms = float(np.sqrt(P.mean()))
            noise_level = float(np.sqrt(self.noise.mean()))
            if rms < 2.0 * noise_level + 2e-5:         # 묵음으로 판단 → 잡음 추정 갱신
                self.noise = 0.95 * self.noise + 0.05 * P
            gain = np.sqrt(np.maximum(P - self.beta * self.noise, 0.0) / (P + 1e-12))
            gain = np.maximum(gain, self.floor)
            y = np.fft.irfft(X * gain, n=self.N).real.astype(np.float32) * self.win
            self.acc += y
            self.out = np.concatenate([self.out, (self.acc[:self.H] / self.cola).astype(np.float32)])
            self.acc = np.concatenate([self.acc[self.H:], np.zeros(self.H, np.float32)])
        n = min(len(np.asarray(chunk).ravel()), len(self.out))
        out = self.out[:n]
        self.out = self.out[n:]
        return out


class NoisereduceDenoiser:
    kind = "noisereduce"

    def __init__(self, sr=16000, strength=1.0):
        import noisereduce as nr  # noqa: F401
        self.nr, self.sr, self.prop = nr, sr, min(1.0, max(0.3, float(strength)))

    def process(self, chunk):
        x = np.asarray(chunk, np.float32).ravel()
        if len(x) < 512:
            return x
        return self.nr.reduce_noise(y=x, sr=self.sr, stationary=True,
                                    prop_decrease=self.prop).astype(np.float32)


class RNNoiseDenoiser:
    kind = "rnnoise"

    def __init__(self, sr=16000):
        import soxr
        self.sr = sr
        self.rs_up = soxr.ResampleStream(sr, 48000, 1, dtype="float32", quality="HQ")
        self.rs_down = soxr.ResampleStream(48000, sr, 1, dtype="float32", quality="HQ")
        try:
            from rnnoise import RNNoise
            self.rn = RNNoise()
        except Exception:
            import pyrnnoise  # type: ignore
            self.rn = pyrnnoise.RNNoise()

    def process(self, chunk):
        x = np.asarray(chunk, np.float32).ravel()
        up = self.rs_up.resample_chunk(x)
        if len(up) == 0:
            return np.zeros(0, np.float32)
        # rnnoise 는 48kHz 10ms(480샘플) 프레임 단위. 남은 건 다음에.
        n = (len(up) // 480) * 480
        if n == 0:
            self._tail = getattr(self, "_tail", np.zeros(0, np.float32))
            self._tail = np.concatenate([self._tail, up])
            return np.zeros(0, np.float32)
        buf = np.concatenate([getattr(self, "_tail", np.zeros(0, np.float32)), up])
        n = (len(buf) // 480) * 480
        self._tail = buf[n:]
        out = np.concatenate([self.rn.process_frame(buf[i:i + 480].astype(np.float32)) for i in range(0, n, 480)])
        return self.rs_down.resample_chunk(out.astype(np.float32))


class DeepFilterDenoiser:
    kind = "deepfilternet"

    def __init__(self, sr=16000):
        from df import init_df, enhance  # pip install deepfilternet
        import soxr
        self.enhance = enhance
        self.model, self.state, self.df_sr = init_df()
        self.rs_up = soxr.ResampleStream(sr, self.df_sr, 1, dtype="float32", quality="HQ")
        self.rs_down = soxr.ResampleStream(self.df_sr, sr, 1, dtype="float32", quality="HQ")

    def process(self, chunk):
        x = np.asarray(chunk, np.float32).ravel()
        up = self.rs_up.resample_chunk(x)
        if len(up) == 0:
            return np.zeros(0, np.float32)
        out, self.state = self.enhance(self.model, up[None, :], self.state)
        return self.rs_down.resample_chunk(np.asarray(out[0], np.float32))


_ORDER = {
    "auto": ["deepfilternet", "rnnoise", "noisereduce", "spectral"],
    "deepfilternet": ["deepfilternet", "spectral"],
    "rnnoise": ["rnnoise", "spectral"],
    "noisereduce": ["noisereduce", "spectral"],
    "spectral": ["spectral"],
    "off": ["off"],
}
_CTORS = {
    "deepfilternet": DeepFilterDenoiser,
    "rnnoise": RNNoiseDenoiser,
    "noisereduce": NoisereduceDenoiser,
    "spectral": SpectralDenoiser,
    "off": _Passthrough,
}


def build_denoiser(backend="auto", sr=SR_DEFAULT, strength=1.0):
    """요청한 백엔드를 순서대로 시도, 실패하면 다음으로 fallback. 무조건 동작하는 객체를 반환."""
    if str(backend).lower() == "off":               # 일부러 끈 것 → 조용히 통과 (fallback 경고 없이)
        return _Passthrough()
    for name in _ORDER.get(str(backend).lower(), _ORDER["auto"]):
        try:
            d = _CTORS[name](sr=sr, strength=strength) if name in ("spectral", "noisereduce") else _CTORS[name](sr=sr)
            print(f"[전처리] 잡음 제거: {name} (강도 {strength})")
            return d
        except Exception as e:
            if name == str(backend).lower():
                print(f"[전처리] {name} 사용 불가({e}) → 다음 백엔드 시도")
            continue
    print("[전처리] 잡음 제거 백엔드를 하나도 쓸 수 없어 통과시킵니다")
    return _Passthrough()
