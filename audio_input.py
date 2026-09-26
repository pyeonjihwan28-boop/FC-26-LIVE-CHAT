"""
게임 소리 입력
- DeviceSource : 스피커로 나가는 소리(WASAPI 루프백) 또는 캡처보드·가상 케이블 같은 입력 장치
- FileSource   : 녹화 파일을 실시간 속도로 흘려보내는 테스트용 입력
모든 소스는 16kHz 모노 float32 조각을 on_audio(chunk) 콜백으로 넘깁니다.
"""
import threading
import time

import numpy as np

SR = 16000


class _ToMono16k:
    """임의 샘플레이트·채널 → 16kHz 모노 (스트리밍 리샘플러)"""

    def __init__(self, in_rate: int, channels: int):
        self.channels = channels
        self.rs = None
        if in_rate != SR:
            import soxr
            self.rs = soxr.ResampleStream(in_rate, SR, 1, dtype="float32", quality="HQ")

    def __call__(self, raw: bytes) -> np.ndarray:
        x = np.frombuffer(raw, dtype=np.float32)
        if self.channels > 1:
            x = x.reshape(-1, self.channels).mean(axis=1)  # 해설은 보통 가운데 정위 → 모노 합산에 유리
        x = np.ascontiguousarray(x, dtype=np.float32)
        return self.rs.resample_chunk(x) if self.rs is not None else x


def _pyaudio():
    try:
        import pyaudiowpatch
        return pyaudiowpatch
    except ImportError:
        raise SystemExit("게임 소리 실시간 캡처는 Windows 전용입니다 (pip install PyAudioWPatch). "
                         "다른 OS에서는 --file 이나 --text 모드로 테스트하세요.")


def _wasapi_capture_devices(p, pyaudio):
    """WASAPI 장치 중 녹음 가능한 것(루프백 + 입력)"""
    for d in p.get_device_info_generator_by_host_api(host_api_type=pyaudio.paWASAPI):
        if d["maxInputChannels"] > 0:
            yield d


def list_devices():
    pyaudio = _pyaudio()
    with pyaudio.PyAudio() as p:
        try:
            default_idx = p.get_default_wasapi_loopback()["index"]
        except Exception:
            default_idx = None
        print("\n캡처 가능한 장치 (WASAPI)")
        print("-" * 72)
        for d in _wasapi_capture_devices(p, pyaudio):
            kind = "스피커 소리(루프백)" if d["isLoopbackDevice"] else "입력 장치"
            mark = "  ◀ 기본값" if d["index"] == default_idx else ""
            print(f"[{d['index']:>3}] {kind:<11} {d['name']}  "
                  f"({int(d['defaultSampleRate'])}Hz, {d['maxInputChannels']}ch){mark}")
        print("-" * 72)
        print("사용법: python main.py --device <번호 또는 이름 일부>\n")


class DeviceSource:
    def __init__(self, query: str | None = None):
        pyaudio = _pyaudio()
        self.pyaudio = pyaudio
        self.p = pyaudio.PyAudio()
        self.info = self._pick(query)
        self.rate = int(self.info["defaultSampleRate"])
        self.channels = int(self.info["maxInputChannels"])
        self.stream = None
        kind = "루프백" if self.info["isLoopbackDevice"] else "입력"
        print(f"[오디오] {kind}: {self.info['name']} ({self.rate}Hz, {self.channels}ch)")

    def _pick(self, query):
        if query is None:
            try:
                return self.p.get_default_wasapi_loopback()
            except Exception as e:
                raise SystemExit(f"기본 스피커의 루프백 장치를 찾지 못했습니다 ({e}). "
                                 "python main.py --list-devices 로 확인하세요.")
        if str(query).isdigit():
            return self.p.get_device_info_by_index(int(query))
        found = [d for d in _wasapi_capture_devices(self.p, self.pyaudio)
                 if str(query).lower() in d["name"].lower()]
        if not found:
            raise SystemExit(f"'{query}' 장치가 없습니다. python main.py --list-devices 로 확인하세요.")
        found.sort(key=lambda d: not d["isLoopbackDevice"])  # 이름이 겹치면 루프백 우선
        return found[0]

    def start(self, on_audio):
        convert = _ToMono16k(self.rate, self.channels)
        pa = self.pyaudio

        def callback(in_data, frame_count, time_info, status):
            try:
                chunk = convert(in_data)
                if len(chunk):
                    on_audio(chunk)
            except Exception as e:  # 콜백에서 예외가 나면 스트림이 멈추므로 삼킴
                print(f"[오디오] 변환 오류: {e}")
            return (None, pa.paContinue)

        self.stream = self.p.open(
            format=pa.paFloat32,
            channels=self.channels,
            rate=self.rate,
            input=True,
            input_device_index=self.info["index"],
            frames_per_buffer=max(256, self.rate // 30),  # 약 33ms
            stream_callback=callback,
        )
        self.stream.start_stream()

    def stop(self):
        try:
            if self.stream is not None:
                self.stream.stop_stream()
                self.stream.close()
        finally:
            self.p.terminate()


class FileSource:
    """녹화 파일(mp4/mkv/mp3/wav …)을 실시간 속도로 재생하듯 흘려보냄"""

    def __init__(self, path: str, speed: float = 1.0):
        from faster_whisper import decode_audio

        self.audio = decode_audio(path, sampling_rate=SR)
        self.speed = speed
        self._stop = threading.Event()
        print(f"[오디오] 파일: {path} ({len(self.audio) / SR:.0f}초)")

    def start(self, on_audio):
        def run():
            step = SR // 10  # 100ms씩
            t0 = time.perf_counter()
            for i in range(0, len(self.audio), step):
                if self._stop.is_set():
                    return
                on_audio(self.audio[i:i + step])
                delay = t0 + (i + step) / SR / self.speed - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
            print("[오디오] 파일 재생 끝 (Ctrl+C로 종료)")

        threading.Thread(target=run, name="file-audio", daemon=True).start()

    def stop(self):
        self._stop.set()


def open_source(args):
    if getattr(args, "file", None):
        return FileSource(args.file)
    return DeviceSource(getattr(args, "device", None))
