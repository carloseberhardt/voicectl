#!/usr/bin/env python
"""Benchmark Moonshine streaming transcription against a WAV file.

Measures the three numbers that decide whether the push-to-talk design feels
instant: model load time (paid once at arm time), realtime factor while
streaming, and tail latency from last audio chunk to final text.
"""

import sys
import time
from pathlib import Path

import moonshine_voice as mv
from moonshine_voice.transcriber import Transcriber

MODEL_ARCH = mv.ModelArch.MEDIUM_STREAMING
MODEL_DIR = Path.home() / (
    ".cache/moonshine_voice/download.moonshine.ai/model/medium-streaming-en/quantized"
)
CHUNK_MS = 100  # how often the daemon would hand over audio


def resolve_model_path():
    try:
        p = Path(mv.get_model_path("medium-streaming-en"))
        if p.exists():
            return p
    except Exception:
        pass
    return MODEL_DIR


def main(wav_path):
    audio, sample_rate = mv.load_wav_file(wav_path)
    duration = len(audio) / sample_rate
    print(f"audio            : {duration:.2f}s @ {sample_rate} Hz")

    model_path = resolve_model_path()
    t0 = time.perf_counter()
    transcriber = Transcriber(model_path, model_arch=MODEL_ARCH)
    print(f"model load       : {time.perf_counter() - t0:.2f}s  (once, at arm time)")

    events = []
    transcriber.add_listener(lambda ev: events.append((time.perf_counter(), ev)))

    transcriber.start()
    chunk = int(sample_rate * CHUNK_MS / 1000)

    t_start = time.perf_counter()
    for i in range(0, len(audio), chunk):
        transcriber.add_audio(audio[i : i + chunk], sample_rate)
    t_fed = time.perf_counter()

    transcriber.stop()
    t_done = time.perf_counter()

    compute = t_done - t_start
    print(f"total compute    : {compute:.2f}s")
    print(f"realtime factor  : {duration / compute:.1f}x  (>1 keeps up with speech)")
    print(f"tail latency     : {(t_done - t_fed) * 1000:.0f}ms  (release key -> text)")

    latencies = [
        ev.line.last_transcription_latency_ms
        for _, ev in events
        if getattr(ev.line, "last_transcription_latency_ms", None)
    ]
    if latencies:
        print(
            f"per-update       : max {max(latencies):.0f}ms, "
            f"mean {sum(latencies) / len(latencies):.0f}ms, n={len(latencies)}"
        )

    print(f"\nevents received  : {len(events)}")
    print("-" * 60)
    print(render(events))


def render(events):
    """Rebuild the transcript. Streaming re-emits a line as it revises it, so
    key by line_id and keep the latest revision of each."""
    lines = {}
    for _, ev in events:
        lines[ev.line.line_id] = ev.line.text
    return "\n".join(t for t in lines.values() if t and t.strip()) or "(no text)"


if __name__ == "__main__":
    wav = sys.argv[1] if len(sys.argv) > 1 else "test.wav"
    if not Path(wav).exists():
        sys.exit(f"no such file: {wav}")
    main(wav)
