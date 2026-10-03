"""Speech provider abstraction. Sarvam REST (verified live 2026-09-28): TTS bulbul:v3 returns base64 WAV
(8 kHz mono PCM16 when requested); STT saaras:v3 accepts WAV multipart, mode `codemix`."""

from __future__ import annotations

import base64
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx

from nirantar.core.errors import NirantarError
from nirantar.voice import LANG_CODES, SAMPLE_RATE
from nirantar.voice.audio import pcm_to_wav, wav_to_pcm


class VoiceUnavailable(NirantarError):
    pass


@dataclass(frozen=True)
class Transcript:
    text: str
    language_code: str | None
    latency_ms: int


class SpeechProvider(Protocol):
    def stt(self, pcm: bytes, rate: int, language: str) -> Transcript: ...
    def tts(self, text: str, language: str, rate: int) -> tuple[bytes, int]: ...  # (pcm, latency_ms)


class SarvamSpeech:
    BASE = "https://api.sarvam.ai"

    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None,
                 stt_model: str = "saaras:v3", tts_model: str = "bulbul:v3", timeout_s: float = 20.0) -> None:
        key = api_key or os.environ.get("SARVAM_API_KEY", "")
        if not key:
            raise ValueError("SARVAM_API_KEY missing")
        self._h = {"api-subscription-key": key}
        self._c = client or httpx.Client(timeout=timeout_s)
        self.stt_model, self.tts_model = stt_model, tts_model

    def stt(self, pcm: bytes, rate: int, language: str) -> Transcript:
        t0 = time.perf_counter()
        r = self._c.post(f"{self.BASE}/speech-to-text", headers=self._h,
                         files={"file": ("utterance.wav", pcm_to_wav(pcm, rate), "audio/wav")},
                         data={"model": self.stt_model, "mode": "codemix",
                               "language_code": LANG_CODES.get(language, "unknown")})
        if r.status_code != 200:
            raise VoiceUnavailable(f"sarvam stt {r.status_code}: {r.text[:200]}")
        d = r.json()
        return Transcript(d.get("transcript", ""), d.get("language_code"), int((time.perf_counter() - t0) * 1000))

    def tts(self, text: str, language: str, rate: int = SAMPLE_RATE) -> tuple[bytes, int]:
        t0 = time.perf_counter()
        r = self._c.post(f"{self.BASE}/text-to-speech", headers=self._h,
                         json={"text": text[:2500], "target_language_code": LANG_CODES.get(language, "en-IN"),
                               "model": self.tts_model, "speech_sample_rate": rate})
        if r.status_code != 200:
            raise VoiceUnavailable(f"sarvam tts {r.status_code}: {r.text[:200]}")
        pcm, got_rate = wav_to_pcm(base64.b64decode(r.json()["audios"][0]))
        if got_rate != rate:
            raise VoiceUnavailable(f"sarvam returned {got_rate} Hz, expected {rate}")
        return pcm, int((time.perf_counter() - t0) * 1000)


    # ---- whole files (WhatsApp voice notes): Sarvam accepts OGG/Opus, MP3, AAC, WAV… directly (docs 2026-10-02)
    def transcribe_file(self, audio: bytes, mime: str, language: str | None = None) -> Transcript:
        t0 = time.perf_counter()
        kind = mime.split(";")[0].strip() or "audio/ogg"
        ext = {"audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/aac": "aac", "audio/amr": "amr",
               "audio/wav": "wav"}.get(kind, "ogg")
        r = self._c.post(f"{self.BASE}/speech-to-text", headers=self._h,
                         files={"file": (f"voice.{ext}", audio, kind)},
                         data={"model": self.stt_model, "mode": "codemix",
                               "language_code": LANG_CODES.get(language or "", "unknown")})
        if r.status_code != 200:
            raise VoiceUnavailable(f"sarvam stt {r.status_code}: {r.text[:200]}")
        d = r.json()
        return Transcript(d.get("transcript", ""), d.get("language_code"), int((time.perf_counter() - t0) * 1000))

    def tts_file(self, text: str, language: str, codec: str = "mp3") -> bytes:
        r = self._c.post(f"{self.BASE}/text-to-speech", headers=self._h,
                         json={"text": text[:2500], "target_language_code": LANG_CODES.get(language, "en-IN"),
                               "model": self.tts_model, "output_audio_codec": codec})
        if r.status_code != 200:
            raise VoiceUnavailable(f"sarvam tts {r.status_code}: {r.text[:200]}")
        return base64.b64decode(r.json()["audios"][0])


class StubSpeech:
    """Deterministic provider for tests: STT returns queued transcripts; TTS returns tone-free silence."""

    def __init__(self, transcripts: list[str] | Callable[[bytes], str]) -> None:
        self._t = transcripts
        self.spoken: list[str] = []

    def stt(self, pcm: bytes, rate: int, language: str) -> Transcript:
        text = self._t(pcm) if callable(self._t) else (self._t.pop(0) if self._t else "")
        return Transcript(text, None, 1)

    def tts(self, text: str, language: str, rate: int = SAMPLE_RATE) -> tuple[bytes, int]:
        self.spoken.append(text)
        return b"\x00\x00" * (rate // 2), 1   # 0.5 s of silence

    def transcribe_file(self, audio: bytes, mime: str, language: str | None = None) -> Transcript:
        return self.stt(audio, SAMPLE_RATE, language or "en")

    def tts_file(self, text: str, language: str, codec: str = "mp3") -> bytes:
        self.spoken.append(text)
        return b"ID3stub-audio"
