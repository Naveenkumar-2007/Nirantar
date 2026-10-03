"""Indic voice: Sarvam STT/TTS behind an abstraction, Exotel Voicebot stream protocol, turn engine (BB-§29)."""

LANG_CODES = {"te": "te-IN", "hi": "hi-IN", "en": "en-IN", "ta": "ta-IN", "kn": "kn-IN", "mr": "mr-IN",
              "bn": "bn-IN", "gu": "gu-IN", "ml": "ml-IN", "pa": "pa-IN", "or": "od-IN"}
SAMPLE_RATE = 8000       # Exotel Voicebot default (PCM16 mono); Sarvam supports 8 kHz in and out
