"""Live voice benchmark against Sarvam: TTS → STT round trip (CER per language) and turn latency.

Round-trip CER measures STT on clean synthetic speech — an optimistic upper bound, NOT telephony WER on real
callers (8 kHz noise, accents, code-mixing). Real WER needs labelled call audio (planned: design partner).
Run: uv run python -m nirantar.voice.benchmark
"""

from __future__ import annotations

import json
import re
import statistics
import time
import unicodedata
from pathlib import Path
from typing import Any

from nirantar.voice.providers import SarvamSpeech
from nirantar.voice.turn import CallSession

CASES = {
    "te": ["మీ చెల్లింపు పెండింగ్‌లో ఉంది", "నేను రేపు జీతం వచ్చాక చెల్లిస్తాను", "లింక్ ఎస్ఎంఎస్‌లో పంపండి"],
    "hi": ["आपका भुगतान बाकी है", "मैं कल सैलरी आने के बाद भुगतान करूंगा", "लिंक एसएमएस पर भेज दीजिए"],
    "en": ["your payment is pending", "i will pay tomorrow after my salary", "please send the link by sms"],
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFC", s.lower())
    return re.sub(r"[\s\W_]+", "", s)


def cer(ref: str, hyp: str) -> float:
    r, h = _norm(ref), _norm(hyp)
    prev = list(range(len(h) + 1))
    for i, rc in enumerate(r, 1):
        cur = [i]
        for j, hc in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc)))
        prev = cur
    return prev[-1] / max(1, len(r))


def run() -> dict[str, Any]:
    speech = SarvamSpeech()
    report: dict[str, Any] = {
        "source": "sarvam live, synthetic TTS audio (clean, 8 kHz)", "languages": {},
        "caveat": ("codemix mode writes English loanwords in Latin script (e.g. pending, salary, SMS); raw CER "
                   "counts these as errors although the words are right. Intent accuracy is the task metric.")}
    for lang, sentences in CASES.items():
        rows: list[dict[str, Any]] = []
        for s in sentences:
            pcm, tts_ms = speech.tts(s, lang, 8000)
            tr = speech.stt(pcm, 8000, lang)
            rows.append({"ref": s, "hyp": tr.text, "cer": round(cer(s, tr.text), 3), "tts_ms": tts_ms,
                         "stt_ms": tr.latency_ms, "audio_s": round(len(pcm) / 16000, 2)})
        from nirantar.voice.turn import intent_of
        for r in rows:
            r["intent_ref"], r["intent_hyp"] = intent_of(r["ref"]), intent_of(r["hyp"])
        agreement = sum(r["intent_ref"] == r["intent_hyp"] for r in rows) / len(rows)
        report["languages"][lang] = {"mean_cer": round(statistics.mean(float(r["cer"]) for r in rows), 3),
                                     "intent_agreement": agreement, "rows": rows}
    # one full turn: caller audio → STT → intent → scripted reply → TTS
    caller, _ = speech.tts("అవును, లింక్ పంపండి", "te", 8000)
    session = CallSession("te", "Chai Club", "₹999", speech)
    t0 = time.perf_counter()
    result = session.turn(caller)
    report["turn"] = {"intent": result.intent, "action": result.action, "stt_ms": result.stt_ms,
                      "tts_ms": result.tts_ms, "total_ms": int((time.perf_counter() - t0) * 1000),
                      "target_ms": 1500, "note": "non-streaming REST; streaming STT/TTS is the next latency step"}
    return report


def main() -> None:
    report = run()
    out = Path(__file__).resolve().parents[3] / "evals" / "results" / "voice_latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    for lang, v in report["languages"].items():
        rows_txt = " | ".join(f"{r['hyp']} ({r['cer']}, stt {r['stt_ms']}ms)" for r in v["rows"])
        print(f"{lang}: mean CER {v['mean_cer']} intent agreement {v['intent_agreement']:.2f}  {rows_txt}")
    print("turn:", report["turn"])


if __name__ == "__main__":
    main()
