"""Audio helpers: WAV <-> PCM16, Exotel frame chunking, energy-based end-of-utterance detection."""

from __future__ import annotations

import array
import io
import math
import wave

FRAME_MS = 20
BYTES_PER_SAMPLE = 2
EXOTEL_MIN_CHUNK = 3200          # Exotel: outgoing media chunks 3,200–100,000 bytes, multiple of 320
EXOTEL_MAX_CHUNK = 100_000
EXOTEL_ALIGN = 320


def wav_to_pcm(wav_bytes: bytes) -> tuple[bytes, int]:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise ValueError("expected 16-bit mono WAV")
        return w.readframes(w.getnframes()), w.getframerate()


def pcm_to_wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def exotel_chunks(pcm: bytes, chunk: int = 3200) -> list[bytes]:
    """Split PCM into Exotel-compliant chunks; the tail is zero-padded to the alignment/minimum."""
    if chunk < EXOTEL_MIN_CHUNK or chunk > EXOTEL_MAX_CHUNK or chunk % EXOTEL_ALIGN:
        raise ValueError("chunk size must be 3200..100000 and a multiple of 320")
    out = [pcm[i:i + chunk] for i in range(0, len(pcm), chunk)]
    if out and len(out[-1]) < EXOTEL_MIN_CHUNK:
        out[-1] = out[-1] + b"\x00" * (EXOTEL_MIN_CHUNK - len(out[-1]))
    return out


def rms(frame: bytes) -> float:
    samples = array.array("h", frame[: len(frame) - len(frame) % 2])
    return math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0.0


class EndOfUtterance:
    """Feed PCM; returns a finished utterance after `silence_ms` of quiet following speech."""

    def __init__(self, rate: int, threshold: float = 500.0, silence_ms: int = 600, min_speech_ms: int = 200) -> None:
        self.frame_bytes = rate * FRAME_MS // 1000 * BYTES_PER_SAMPLE
        self.threshold = threshold
        self.silence_frames = silence_ms // FRAME_MS
        self.min_speech_frames = min_speech_ms // FRAME_MS
        self._buf = bytearray()
        self._utt = bytearray()
        self._speech = 0
        self._quiet = 0

    def feed(self, pcm: bytes) -> bytes | None:
        self._buf.extend(pcm)
        done: bytes | None = None
        while len(self._buf) >= self.frame_bytes:
            frame = bytes(self._buf[: self.frame_bytes])
            del self._buf[: self.frame_bytes]
            loud = rms(frame) >= self.threshold
            if loud:
                self._speech += 1
                self._quiet = 0
                self._utt.extend(frame)
            elif self._speech:
                self._quiet += 1
                self._utt.extend(frame)
                if self._quiet >= self.silence_frames:
                    if self._speech >= self.min_speech_frames:
                        done = bytes(self._utt)
                    self._utt.clear()
                    self._speech = self._quiet = 0
        return done

    def flush(self) -> bytes | None:
        out = bytes(self._utt) if self._speech >= self.min_speech_frames else None
        self._utt.clear()
        self._speech = self._quiet = 0
        return out
