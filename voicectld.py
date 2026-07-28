#!/usr/bin/env python
"""voicectl — push-to-talk dictation into whatever window has focus.

Holds a rolling buffer of microphone audio so that speech from *before* the
key went down still gets transcribed, streams it through Moonshine while you
talk, and pastes the result via the primary selection when you let go.

Because the paste happens at the compositor level, it works identically in a
local terminal, a browser, or a Claude Code session running over SSH — the
remote end just sees characters arrive on stdin.

Control it with the `voicectl` client; see README.md.
"""

from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

import numpy as np

import moonshine_voice as mv
from moonshine_voice.transcriber import Transcriber

CONFIG_PATH = Path(__file__).resolve().parent / "config.toml"
SOCKET_PATH = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "voicectl.sock"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Rolling audio buffer
# --------------------------------------------------------------------------


class RingBuffer:
    """Fixed-size circular buffer of float32 samples.

    Positions are absolute sample counts since startup rather than indices
    into the array, so a reader can express "everything since I armed" without
    caring where the write head has wrapped to.
    """

    def __init__(self, capacity: int):
        self._buf = np.zeros(capacity, dtype=np.float32)
        self._capacity = capacity
        self._written = 0
        self._lock = threading.Lock()

    @property
    def written(self) -> int:
        with self._lock:
            return self._written

    def write(self, samples: np.ndarray) -> None:
        with self._lock:
            if len(samples) >= self._capacity:
                samples = samples[-self._capacity :]
            n = len(samples)
            start = self._written % self._capacity
            end = start + n
            if end <= self._capacity:
                self._buf[start:end] = samples
            else:
                split = self._capacity - start
                self._buf[start:] = samples[:split]
                self._buf[: n - split] = samples[split:]
            self._written += n

    def read(self, start: int, count: int) -> tuple[np.ndarray, int]:
        """Read up to `count` samples from absolute position `start`.

        Returns the samples and the new position. A reader that falls more
        than `capacity` behind is silently fast-forwarded to the oldest
        surviving sample — dropping audio beats replaying garbage.
        """
        with self._lock:
            oldest = max(0, self._written - self._capacity)
            start = max(start, oldest)
            count = min(count, self._written - start)
            if count <= 0:
                return np.empty(0, dtype=np.float32), start
            i = start % self._capacity
            if i + count <= self._capacity:
                out = self._buf[i : i + count].copy()
            else:
                split = self._capacity - i
                out = np.concatenate([self._buf[i:], self._buf[: count - split]])
            return out, start + count


class Capture:
    """Keeps a `pw-record` process alive, feeding the ring buffer.

    The stream stays open for the daemon's whole lifetime. That is what
    removes the startup lag that makes press-to-record swallow your first
    word: the device is already awake when the key goes down.
    """

    def __init__(self, cfg: dict, ring: RingBuffer):
        self.ring = ring
        self.source = cfg["source"]
        self.sample_rate = cfg["sample_rate"]
        self.latency_ms = cfg.get("latency_ms", 20)
        # Deliberately smaller than chunk_ms: this governs how much audio is
        # still in flight when the key comes up, which is separate from how
        # much we hand the model at a time.
        block_ms = cfg.get("read_block_ms", 20)
        self.block = int(self.sample_rate * block_ms / 1000) * 2  # s16 = 2 bytes
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> None:
        cmd = ["pw-record"]
        if self.source:
            cmd += ["--target", self.source]
        cmd += [
            "--rate", str(self.sample_rate),
            "--channels", "1",
            "--format", "s16",
            "--container", "raw",
            "--latency", f"{self.latency_ms}ms",
            "-",
        ]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        log(f"capturing from {self.source or 'default source'} @ {self.sample_rate} Hz")

    def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        while self._running:
            data = self._proc.stdout.read(self.block)
            if not data:
                if self._running:
                    log("capture stream ended unexpectedly — is the mic still present?")
                break
            samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            self.ring.write(samples)

    def stop(self) -> None:
        self._running = False
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()


# --------------------------------------------------------------------------
# Transcription
# --------------------------------------------------------------------------


class Dictator:
    """Owns the Moonshine transcriber and the arm/disarm lifecycle."""

    def __init__(
        self,
        cfg: dict,
        ring: RingBuffer,
        sample_rate: int,
        chunk_ms: int,
        preroll_ms: int,
        postroll_ms: int,
    ):
        self.ring = ring
        self.sample_rate = sample_rate
        self.chunk_samples = int(sample_rate * chunk_ms / 1000)
        self.preroll_samples = int(sample_rate * preroll_ms / 1000)
        self.postroll = postroll_ms / 1000

        model_path = Path(cfg["path"]).expanduser()
        arch = getattr(mv.ModelArch, cfg["arch"])
        options = dict(cfg.get("options", {}))

        t0 = time.perf_counter()
        try:
            self.transcriber = Transcriber(model_path, model_arch=arch, options=options)
        except Exception as exc:
            # An option the native library rejects shouldn't cost us the daemon.
            log(f"transcriber options rejected ({exc}); retrying with defaults")
            self.transcriber = Transcriber(model_path, model_arch=arch)
        log(f"model loaded in {time.perf_counter() - t0:.2f}s ({cfg['arch']})")

        self._lines: dict[int, str] = {}
        self._lines_lock = threading.Lock()
        self.transcriber.add_listener(self._on_event)

        self._armed = False
        self._feeder: threading.Thread | None = None
        self._pos = 0
        # Absolute ring position where the previous session stopped consuming.
        # The pre-roll is never allowed to reach back past this, so no audio
        # can be transcribed twice no matter how fast you re-trigger.
        self._last_end = 0
        self._t_armed = 0.0
        self._api_lock = threading.Lock()

    @property
    def armed(self) -> bool:
        return self._armed

    def _on_event(self, event) -> None:
        with self._lines_lock:
            self._lines[event.line.line_id] = event.line.text

    def arm(self) -> None:
        if self._armed:
            return
        with self._lines_lock:
            self._lines.clear()
        # Rewind past the moment the key went down, so the words you started
        # saying before your hand caught up are already in the stream — but
        # never back into audio a previous session already consumed.
        self._pos = max(0, self.ring.written - self.preroll_samples, self._last_end)
        self._t_armed = time.perf_counter()
        with self._api_lock:
            self.transcriber.start()
        self._armed = True
        self._feeder = threading.Thread(target=self._feed_loop, daemon=True)
        self._feeder.start()

    def _feed_loop(self) -> None:
        while self._armed:
            if self.ring.written - self._pos >= self.chunk_samples:
                samples, self._pos = self.ring.read(self._pos, self.chunk_samples)
                with self._api_lock:
                    self.transcriber.add_audio(samples.tolist(), self.sample_rate)
            else:
                time.sleep(0.005)

    def disarm(self) -> list[str]:
        """Stop listening and return the transcript lines."""
        if not self._armed:
            return []
        self._armed = False
        if self._feeder:
            self._feeder.join(timeout=2)

        # Capture runs behind real time, so at the moment the key came up the
        # tail of the sentence is still moving through pw-record. Wait for it
        # to land rather than truncating mid-word.
        if self.postroll > 0:
            time.sleep(self.postroll)

        # Whatever arrived between the last full chunk and the end of postroll.
        remaining = self.ring.written - self._pos
        if remaining > 0:
            samples, self._pos = self.ring.read(self._pos, remaining)
            with self._api_lock:
                self.transcriber.add_audio(samples.tolist(), self.sample_rate)

        with self._api_lock:
            self.transcriber.stop()
        self._last_end = self._pos

        held = time.perf_counter() - self._t_armed
        with self._lines_lock:
            lines = [t for t in self._lines.values() if t and t.strip()]
        log(f"held {held:.1f}s, {len(lines)} line(s)")
        return lines

    def cancel(self) -> None:
        if not self._armed:
            return
        self._armed = False
        if self._feeder:
            self._feeder.join(timeout=2)
        with self._api_lock:
            self.transcriber.stop()
        with self._lines_lock:
            self._lines.clear()
        # Cancelling means "forget I said that" — don't let the next pre-roll
        # reach back and resurrect it.
        self._last_end = self.ring.written
        log("cancelled")

    def retro(self, seconds: float) -> list[str]:
        """Transcribe the last `seconds` of audio already in the ring.

        The buffer is sitting there anyway; this is what makes it worth
        keeping more of than the pre-roll strictly needs.
        """
        count = int(seconds * self.sample_rate)
        start = max(0, self.ring.written - count)
        samples, _ = self.ring.read(start, count)
        if not len(samples):
            return []
        with self._api_lock:
            transcript = self.transcriber.transcribe_without_streaming(
                samples.tolist(), self.sample_rate
            )
        # Reaching back is deliberate here, but the next push-to-talk shouldn't
        # repeat what we just grabbed.
        self._last_end = self.ring.written
        lines = getattr(transcript, "lines", None) or []
        return [l.text for l in lines if getattr(l, "text", "").strip()]


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def build_substituter(subs: dict[str, str]):
    """Case-insensitive whole-word replacement, longest key first.

    Moonshine has no vocabulary biasing, so corrections happen here instead.
    """
    if not subs:
        return lambda text: text
    keys = sorted(subs, key=len, reverse=True)
    pattern = re.compile(r"\b(" + "|".join(re.escape(k) for k in keys) + r")\b", re.IGNORECASE)
    lookup = {k.lower(): v for k, v in subs.items()}
    return lambda text: pattern.sub(lambda m: lookup[m.group(0).lower()], text)


class Injector:
    def __init__(self, cfg: dict):
        self.join = cfg["line_join"]
        self.delay = cfg["paste_delay_ms"] / 1000
        self.auto_paste = cfg["auto_paste"]
        self.suffix = cfg.get("suffix", " ")
        self.last_text = ""

    def send(self, lines: list[str]) -> str:
        text = self.join.join(lines).strip()
        if not text:
            log("nothing transcribed")
            return ""
        return self._emit(text + self.suffix)

    def resend(self) -> str:
        """Re-paste the last transcript, for when something with focus ate it."""
        if not self.last_text:
            return ""
        return self._emit(self.last_text)

    def _emit(self, payload: str) -> str:
        subprocess.run(["wl-copy", "--primary"], input=payload.encode(), check=True)
        if self.auto_paste:
            # wl-copy forks to serve the selection; give it a beat to claim it.
            time.sleep(self.delay)
            subprocess.run(["wtype", "-M", "shift", "-k", "Insert", "-m", "shift"], check=True)
        self.last_text = payload
        shown = payload.strip()
        log(f"sent {len(payload)} chars: {shown[:70]}{'...' if len(shown) > 70 else ''}")
        return payload


# --------------------------------------------------------------------------
# Daemon
# --------------------------------------------------------------------------


class Daemon:
    def __init__(self, config: dict):
        audio = config["audio"]
        self.ring = RingBuffer(audio["ring_seconds"] * audio["sample_rate"])
        self.capture = Capture(audio, self.ring)
        self.dictator = Dictator(
            config["model"],
            self.ring,
            audio["sample_rate"],
            audio["chunk_ms"],
            audio["preroll_ms"],
            audio["postroll_ms"],
        )
        self.injector = Injector(config["output"])
        self.substitute = build_substituter(config.get("substitutions", {}))
        self._stop = threading.Event()

    def handle(self, command: str) -> str:
        cmd, _, arg = command.strip().partition(" ")

        if cmd == "start":
            self.dictator.arm()
            return "armed"

        if cmd == "stop":
            lines = self.dictator.disarm()
            if not lines:
                return "empty"
            text = self.injector.send([self.substitute(l) for l in lines])
            return f"sent: {text}"

        if cmd == "cancel":
            self.dictator.cancel()
            return "cancelled"

        if cmd == "retro":
            seconds = float(arg) if arg else 10.0
            lines = self.dictator.retro(seconds)
            if not lines:
                return "empty"
            text = self.injector.send([self.substitute(l) for l in lines])
            return f"sent: {text}"

        if cmd == "last":
            text = self.injector.resend()
            return f"resent: {text.strip()}" if text else "nothing to resend"

        if cmd == "status":
            secs = self.ring.written / self.dictator.sample_rate
            return f"{'listening' if self.dictator.armed else 'idle'} (captured {secs:.0f}s)"

        if cmd == "quit":
            self._stop.set()
            return "stopping"

        return f"unknown command: {cmd}"

    def serve(self) -> None:
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(SOCKET_PATH))
        server.listen(4)
        server.settimeout(0.5)
        log(f"listening on {SOCKET_PATH}")

        while not self._stop.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            with conn:
                try:
                    data = conn.recv(4096).decode().strip()
                    if data:
                        conn.sendall((self.handle(data) + "\n").encode())
                except Exception as exc:
                    log(f"error handling {data!r}: {exc}")
                    try:
                        conn.sendall(f"error: {exc}\n".encode())
                    except OSError:
                        pass

        server.close()
        SOCKET_PATH.unlink(missing_ok=True)

    def run(self) -> None:
        self.capture.start()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: self._stop.set())
        try:
            self.serve()
        finally:
            self.capture.stop()
            log("stopped")


def main() -> None:
    with open(CONFIG_PATH, "rb") as fh:
        config = tomllib.load(fh)
    Daemon(config).run()


if __name__ == "__main__":
    main()
